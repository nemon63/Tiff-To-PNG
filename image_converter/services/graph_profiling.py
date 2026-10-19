from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from functools import wraps
from inspect import signature
from time import perf_counter

from image_converter.domain.node_graph import NodeGraph


@dataclass(frozen=True, slots=True)
class NodeEvaluationTiming:
    node_id: str
    title: str
    socket_id: str
    resolution: tuple[int, int] | None
    calls: int
    own_ms: float
    branch_ms: float
    cache_hits: int
    cache_misses: int


@dataclass(frozen=True, slots=True)
class GraphEvaluationProfile:
    total_ms: float
    max_side: int
    timings: tuple[NodeEvaluationTiming, ...]


@dataclass(slots=True)
class _Scope:
    node_id: str
    socket_id: str
    resolution: tuple[int, int] | None
    child_seconds: float = 0.0
    cache_hits: int = 0
    cache_misses: int = 0


class GraphEvaluationProfiler:
    """Request-local CPU wall-time profiler; never shared between worker threads."""

    def __init__(self, *, clock: Callable[[], float] = perf_counter) -> None:
        self._clock = clock
        self._stack: list[_Scope] = []
        self._timings: dict[tuple, NodeEvaluationTiming] = {}
        self._titles: dict[str, str] = {}
        self._total_seconds = 0.0

    @contextmanager
    def render(self, graph: NodeGraph, node_id: str) -> Iterator[_Scope]:
        if self._stack:
            raise RuntimeError("A profiler cannot run concurrent renders.")
        self._timings.clear()
        self._titles = {node.node_id: node.title for node in graph.nodes}
        self._total_seconds = 0.0
        with self.measure(node_id, "preview", None) as scope:
            yield scope

    @contextmanager
    def measure(
        self, node_id: str, socket_id: str, resolution: tuple[int, int] | None,
    ) -> Iterator[_Scope]:
        scope = _Scope(node_id, socket_id, resolution)
        self._stack.append(scope)
        started = self._clock()
        try:
            yield scope
        finally:
            elapsed = max(0.0, self._clock() - started)
            self._stack.pop()
            if self._stack:
                self._stack[-1].child_seconds += elapsed
            else:
                self._total_seconds = elapsed
            key = (node_id, socket_id, scope.resolution)
            previous = self._timings.get(key)
            self._timings[key] = NodeEvaluationTiming(
                node_id=node_id,
                title=self._titles.get(node_id, node_id),
                socket_id=socket_id,
                resolution=scope.resolution,
                calls=(previous.calls if previous else 0) + 1,
                own_ms=(previous.own_ms if previous else 0.0)
                + max(0.0, elapsed - scope.child_seconds) * 1000,
                branch_ms=(previous.branch_ms if previous else 0.0) + elapsed * 1000,
                cache_hits=(previous.cache_hits if previous else 0) + scope.cache_hits,
                cache_misses=(previous.cache_misses if previous else 0) + scope.cache_misses,
            )

    def cache_lookup(self, hit: bool) -> None:
        if self._stack:
            if hit:
                self._stack[-1].cache_hits += 1
            else:
                self._stack[-1].cache_misses += 1

    def snapshot(self, *, max_side: int) -> GraphEvaluationProfile:
        if self._stack:
            raise RuntimeError("Cannot snapshot an unfinished render.")
        return GraphEvaluationProfile(
            total_ms=self._total_seconds * 1000,
            max_side=max_side,
            timings=tuple(sorted(self._timings.values(), key=lambda item: item.own_ms, reverse=True)),
        )


def profile_render(method):
    node_parameter = tuple(signature(method).parameters)[2]

    @wraps(method)
    def wrapped(executor, *args, **kwargs):
        profiler = executor._profiler
        if profiler is None:
            return method(executor, *args, **kwargs)
        graph = args[0] if args else kwargs["graph"]
        node = args[1] if len(args) > 1 else kwargs[node_parameter]
        with profiler.render(graph, node.node_id) as scope:
            result = method(executor, *args, **kwargs)
            image = result[0] if isinstance(result, tuple) else result
            scope.resolution = image.size
            return result
    return wrapped


def profile_socket(method):
    @wraps(method)
    def wrapped(executor, graph, connection, target_size, *args, **kwargs):
        profiler = executor._profiler
        if profiler is None:
            return method(executor, graph, connection, target_size, *args, **kwargs)
        with profiler.measure(connection.source_node_id, connection.source_socket_id, target_size):
            return method(executor, graph, connection, target_size, *args, **kwargs)
    return wrapped
