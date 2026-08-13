from __future__ import annotations

import os
import tempfile
from collections import OrderedDict, defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from threading import RLock

from PIL import Image, ImageChops, ImageFilter, ImageOps

from image_converter.domain.models import ConversionOptions, ConversionStatus
from image_converter.domain.node_graph import (
    GraphConnection,
    GraphNode,
    GraphValidationIssue,
    GraphValidationSeverity,
    NodeGraph,
    NodeGraphProject,
    NodeType,
    OutputMode,
    SocketDirection,
    SocketType,
    socket_definitions,
)
from image_converter.services.image_loading import copy_first_frame_preserving_alpha
from image_converter.services.pipeline import RESAMPLING_LANCZOS

Logger = Callable[[str], None]


@lru_cache(maxsize=1024)
def _levels_lut(
    black: int,
    white: int,
    gamma: float,
    out_min: int,
    out_max: int,
) -> tuple[int, ...]:
    scale = 1.0 / max(1, white - black)
    lut = []
    for value in range(256):
        normalized = min(max((value - black) * scale, 0.0), 1.0)
        adjusted = normalized ** (1.0 / gamma)
        lut.append(round(out_min + adjusted * (out_max - out_min)))
    return tuple(lut)


@lru_cache(maxsize=512)
def _clamp_lut(minimum: int, maximum: int) -> tuple[int, ...]:
    return tuple(min(max(value, minimum), maximum) for value in range(256))


@lru_cache(maxsize=256)
def _threshold_lut(threshold: int) -> tuple[int, ...]:
    return tuple(255 if value >= threshold else 0 for value in range(256))


@lru_cache(maxsize=1024)
def _remap_lut(
    in_min: int,
    in_max: int,
    out_min: int,
    out_max: int,
) -> tuple[int, ...]:
    scale = 1.0 / max(1, in_max - in_min)
    lut = []
    for value in range(256):
        normalized = min(max((value - in_min) * scale, 0.0), 1.0)
        lut.append(round(out_min + normalized * (out_max - out_min)))
    return tuple(lut)


class GraphExecutionError(RuntimeError):
    pass


@dataclass(slots=True, frozen=True)
class NodeGraphLookup:
    node_by_id: dict[str, GraphNode]
    incoming_by_socket: dict[tuple[str, str], GraphConnection]
    outgoing_by_node: dict[str, tuple[GraphConnection, ...]]

    @classmethod
    def build(cls, graph: NodeGraph) -> NodeGraphLookup:
        outgoing: dict[str, list[GraphConnection]] = defaultdict(list)
        incoming: dict[tuple[str, str], GraphConnection] = {}
        for connection in graph.connections:
            incoming.setdefault(
                (connection.target_node_id, connection.target_socket_id),
                connection,
            )
            outgoing[connection.source_node_id].append(connection)
        return cls(
            node_by_id={node.node_id: node for node in graph.nodes},
            incoming_by_socket=incoming,
            outgoing_by_node={key: tuple(value) for key, value in outgoing.items()},
        )


@dataclass(slots=True, frozen=True)
class GraphExportPlanItem:
    output_node_id: str
    output_name: str
    destination: Path


@dataclass(slots=True, frozen=True)
class GraphExportCollision:
    destination: Path
    outputs: tuple[GraphExportPlanItem, ...]


@dataclass(slots=True, frozen=True)
class GraphExportSourceCollision:
    destination: Path
    output: GraphExportPlanItem
    sources: tuple[Path, ...]


@dataclass(slots=True, frozen=True)
class GraphExportPlan:
    items: tuple[GraphExportPlanItem, ...]
    collisions: tuple[GraphExportCollision, ...] = ()
    source_collisions: tuple[GraphExportSourceCollision, ...] = ()

    @property
    def is_valid(self) -> bool:
        return not self.collisions and not self.source_collisions

    def collision_message(self) -> str:
        lines: list[str] = []
        if self.collisions:
            lines.append("Несколько Output-нод указывают на один файл:")
            for collision in self.collisions:
                names = ", ".join(item.output_name for item in collision.outputs)
                lines.append(f"{collision.destination}: {names}")
        if self.source_collisions:
            if lines:
                lines.append("")
            lines.append("Output-файл совпадает с исходной Texture:")
            for collision in self.source_collisions:
                sources = ", ".join(str(path) for path in collision.sources)
                lines.append(
                    f"{collision.destination}: {collision.output.output_name}; источник: {sources}"
                )
        return "\n".join(lines)


@dataclass(slots=True, frozen=True)
class GraphExportResult:
    output_node_id: str
    output_name: str
    destination: Path
    status: ConversionStatus
    message: str

    @property
    def is_success(self) -> bool:
        return self.status is ConversionStatus.SUCCESS


@dataclass(slots=True)
class GraphExportSummary:
    results: list[GraphExportResult]

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def succeeded(self) -> int:
        return sum(1 for result in self.results if result.status is ConversionStatus.SUCCESS)

    @property
    def skipped(self) -> int:
        return sum(1 for result in self.results if result.status is ConversionStatus.SKIPPED)

    @property
    def failed(self) -> int:
        return sum(1 for result in self.results if result.status is ConversionStatus.FAILED)

    def as_text(self) -> str:
        return (
            f"Graph export: всего={self.total}, успешно={self.succeeded}, "
            f"пропущено={self.skipped}, ошибок={self.failed}"
        )


class NodeGraphPreviewCache:
    DEFAULT_MAX_BYTES = 256 * 1024 * 1024
    DEFAULT_MAX_SIZE_ENTRIES = 2048

    def __init__(
        self,
        max_side: int = 1024,
        fallback_size: tuple[int, int] = (256, 256),
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        max_size_entries: int = DEFAULT_MAX_SIZE_ENTRIES,
    ) -> None:
        self.max_side = max_side
        self.fallback_size = fallback_size
        self.max_bytes = max(0, max_bytes)
        self.max_size_entries = max(1, max_size_entries)
        self._images: OrderedDict[tuple[str, object], tuple[Image.Image, int]] = OrderedDict()
        self.texture_sizes: OrderedDict[str, tuple[int, int] | None] = OrderedDict()
        self.current_bytes = 0
        self.lock = RLock()

    @staticmethod
    def _image_bytes(image: Image.Image) -> int:
        return image.width * image.height * max(1, len(image.getbands()))

    def get_image(self, bucket: str, key: object) -> Image.Image | None:
        cache_key = (bucket, key)
        with self.lock:
            entry = self._images.get(cache_key)
            if entry is None:
                return None
            self._images.move_to_end(cache_key)
            return entry[0]

    def put_image(self, bucket: str, key: object, image: Image.Image) -> None:
        size = self._image_bytes(image)
        cache_key = (bucket, key)
        if size > self.max_bytes:
            with self.lock:
                previous = self._images.pop(cache_key, None)
                if previous is not None:
                    self.current_bytes -= previous[1]
            return
        with self.lock:
            previous = self._images.pop(cache_key, None)
            if previous is not None:
                self.current_bytes -= previous[1]
            while self._images and self.current_bytes + size > self.max_bytes:
                _old_key, (_old_image, old_size) = self._images.popitem(last=False)
                self.current_bytes -= old_size
            self._images[cache_key] = (image, size)
            self.current_bytes += size

    def get_texture_size(self, node_id: str) -> tuple[int, int] | None | object:
        with self.lock:
            if node_id not in self.texture_sizes:
                return _CACHE_MISS
            value = self.texture_sizes[node_id]
            self.texture_sizes.move_to_end(node_id)
            return value

    def put_texture_size(self, node_id: str, value: tuple[int, int] | None) -> None:
        with self.lock:
            self.texture_sizes[node_id] = value
            self.texture_sizes.move_to_end(node_id)
            while len(self.texture_sizes) > self.max_size_entries:
                self.texture_sizes.popitem(last=False)

    def clear(self) -> None:
        with self.lock:
            self._images.clear()
            self.texture_sizes.clear()
            self.current_bytes = 0

    def invalidate_node_and_downstream(self, graph: NodeGraph, node_id: str) -> None:
        dirty_node_ids = self._downstream_node_ids(graph, node_id)
        with self.lock:
            for cache_key, (_image, size) in tuple(self._images.items()):
                item_key = cache_key[1]
                if isinstance(item_key, tuple) and item_key and item_key[0] in dirty_node_ids:
                    self._images.pop(cache_key)
                    self.current_bytes -= size
            for dirty_node_id in dirty_node_ids:
                self.texture_sizes.pop(dirty_node_id, None)

    @staticmethod
    def _downstream_node_ids(graph: NodeGraph, node_id: str) -> set[str]:
        lookup = NodeGraphLookup.build(graph)
        dirty = {node_id}
        pending = [node_id]
        while pending:
            current_id = pending.pop()
            for connection in lookup.outgoing_by_node.get(current_id, ()):
                if connection.target_node_id in dirty:
                    continue
                dirty.add(connection.target_node_id)
                pending.append(connection.target_node_id)
        return dirty


_CACHE_MISS = object()


class NodeGraphExecutor:
    _EXPORT_FORMATS = {
        ".png": "PNG",
        ".jpg": "JPEG",
        ".jpeg": "JPEG",
        ".webp": "WEBP",
        ".tif": "TIFF",
        ".tiff": "TIFF",
        ".bmp": "BMP",
        ".tga": "TGA",
    }

    def __init__(self) -> None:
        self._lookup: NodeGraphLookup | None = None

    def _prepare_lookup(self, graph: NodeGraph) -> NodeGraphLookup:
        self._lookup = NodeGraphLookup.build(graph)
        return self._lookup

    def _find_node(self, graph: NodeGraph, node_id: str) -> GraphNode | None:
        lookup = self._lookup or self._prepare_lookup(graph)
        return lookup.node_by_id.get(node_id)

    def _incoming_connection(
        self,
        graph: NodeGraph,
        *,
        target_node_id: str,
        target_socket_id: str,
    ) -> GraphConnection | None:
        lookup = self._lookup or self._prepare_lookup(graph)
        return lookup.incoming_by_socket.get((target_node_id, target_socket_id))

    def render_display_node(
        self,
        graph: NodeGraph,
        node: GraphNode,
        *,
        preview_cache: NodeGraphPreviewCache | None = None,
        fallback_size: tuple[int, int] | None = None,
        max_side: int | None = None,
    ) -> tuple[Image.Image, str]:
        self._prepare_lookup(graph)
        cache = preview_cache or NodeGraphPreviewCache()
        resolved_fallback = fallback_size or cache.fallback_size
        resolved_max_side = cache.max_side if max_side is None else max_side
        if node.node_type is NodeType.OUTPUT_RGBA:
            target_size = self._determine_output_size(graph, node, cache)
            if target_size is None:
                target_size = resolved_fallback
            target_size = self._fit_preview_size(target_size, resolved_max_side)
            image = self._compose_output_preview_image(graph, node, target_size, cache).convert("RGBA")
            return image, f"Output preview · {image.width}x{image.height} · {image.mode}"
        if node.node_type is NodeType.VIEW:
            image = self.render_view_node(
                graph,
                node,
                preview_cache=cache,
                fallback_size=resolved_fallback,
                max_side=resolved_max_side,
            )
            return image, f"View preview · {image.width}x{image.height} · channel"
        if node.node_type is NodeType.TEXTURE_INPUT:
            target_size = self._fit_preview_size(
                self._load_texture_size(node, cache) or resolved_fallback,
                resolved_max_side,
            )
            image = self._load_texture_preview_image_cached(node, cache, target_size)
            if image.size != target_size:
                image = image.resize(target_size, RESAMPLING_LANCZOS)
            image = image.convert("RGBA")
            return image, f"Display flag · Texture · {image.width}x{image.height} · {image.mode}"
        if node.node_type is NodeType.COLOR:
            target_size = self._fit_preview_size(self._color_size(node), resolved_max_side)
            image = Image.new("RGBA", target_size, self._color_rgba(node))
            return image, f"Display flag · Color · {image.width}x{image.height} · RGBA"

        output_socket = self._first_channel_output_socket(node)
        if output_socket is None:
            raise GraphExecutionError(f"{node.title}: node has no displayable output.")

        preview_connection = GraphConnection(
            connection_id="display_preview",
            source_node_id=node.node_id,
            source_socket_id=output_socket.socket_id,
            target_node_id="",
            target_socket_id="",
        )
        target_size = self._preview_target_size(
            graph,
            preview_connection,
            cache,
            resolved_fallback,
            resolved_max_side,
        )

        channel = self._evaluate_channel_socket_cached(
            graph,
            preview_connection,
            target_size,
            set(),
            cache,
        ).convert("L")
        alpha = Image.new("L", channel.size, 255)
        image = Image.merge("RGBA", (channel, channel, channel, alpha))
        meta = f"Display flag · {output_socket.name} · {image.width}x{image.height} · channel"
        return image, meta

    def render_output_node(self, graph: NodeGraph, output_node: GraphNode) -> Image.Image:
        self._prepare_lookup(graph)
        if output_node.node_type is not NodeType.OUTPUT_RGBA:
            raise GraphExecutionError(f"{output_node.title}: node is not an Output node.")

        missing = self._missing_required_output_inputs(graph, output_node)
        if missing:
            raise GraphExecutionError(f"не подключены каналы {', '.join(missing)}")

        cache = NodeGraphPreviewCache()
        target_size = self._determine_output_size(graph, output_node, cache)
        if target_size is None:
            raise GraphExecutionError("output должен зависеть хотя бы от одной texture-ноды")

        return self._compose_output_image(graph, output_node, target_size, cache)

    def render_view_node(
        self,
        graph: NodeGraph,
        view_node: GraphNode,
        *,
        preview_cache: NodeGraphPreviewCache | None = None,
        fallback_size: tuple[int, int] = (256, 256),
        max_side: int | None = None,
    ) -> Image.Image:
        self._prepare_lookup(graph)
        cache = preview_cache or NodeGraphPreviewCache(fallback_size=fallback_size)
        if view_node.node_type is not NodeType.VIEW:
            raise GraphExecutionError(f"{view_node.title}: node is not a View node.")

        connection = self._incoming_connection(
            graph,
            target_node_id=view_node.node_id,
            target_socket_id="in",
        )
        if connection is None:
            raise GraphExecutionError(f"{view_node.title}: input is not connected.")

        target_size = self._preview_target_size(
            graph,
            connection,
            cache,
            fallback_size,
            cache.max_side if max_side is None else max_side,
        )

        channel = self._evaluate_channel_socket_cached(
            graph,
            connection,
            target_size,
            set(),
            cache,
        ).convert("L")
        alpha = Image.new("L", channel.size, 255)
        return Image.merge("RGBA", (channel, channel, channel, alpha))

    def validate(self, project: NodeGraphProject) -> tuple[str, ...]:
        return tuple(issue.message for issue in self.validate_issues(project))

    def validate_issues(self, project: NodeGraphProject) -> tuple[GraphValidationIssue, ...]:
        issues: list[GraphValidationIssue] = []
        graph = project.graph
        self._prepare_lookup(graph)
        node_ids = {node.node_id for node in graph.nodes}

        for node in graph.nodes:
            if not node.node_id:
                issues.append(
                    GraphValidationIssue(
                        GraphValidationSeverity.ERROR,
                        "Graph содержит node без id.",
                    )
                )
            if node.node_type is NodeType.TEXTURE_INPUT:
                path = Path(str(node.properties.get("path", "")))
                if not str(path):
                    issues.append(
                        GraphValidationIssue(
                            GraphValidationSeverity.ERROR,
                            f"{node.title}: texture path не задан.",
                            node.node_id,
                            "path",
                        )
                    )
                elif not path.exists():
                    issues.append(
                        GraphValidationIssue(
                            GraphValidationSeverity.ERROR,
                            f"{node.title}: texture не найден: {path}",
                            node.node_id,
                            "path",
                        )
                    )
            if node.node_type is NodeType.OUTPUT_RGBA and node.properties.get("enabled", True):
                missing = self._missing_required_output_inputs(graph, node)
                if missing:
                    issues.append(
                        GraphValidationIssue(
                            GraphValidationSeverity.ERROR,
                            f"{node.title}: не подключены каналы {', '.join(missing)}.",
                            node.node_id,
                            ",".join(missing),
                        )
                    )

        for connection in graph.connections:
            if connection.source_node_id not in node_ids:
                issues.append(
                    GraphValidationIssue(
                        GraphValidationSeverity.ERROR,
                        f"Broken connection: source node {connection.source_node_id} не найден.",
                        connection.source_node_id,
                        connection.source_socket_id,
                    )
                )
            if connection.target_node_id not in node_ids:
                issues.append(
                    GraphValidationIssue(
                        GraphValidationSeverity.ERROR,
                        f"Broken connection: target node {connection.target_node_id} не найден.",
                        connection.target_node_id,
                        connection.target_socket_id,
                    )
                )

        if not self._enabled_output_nodes(graph):
            issues.append(
                GraphValidationIssue(
                    GraphValidationSeverity.WARNING,
                    "Нет включенных Output nodes для экспорта.",
                )
            )

        deduped: dict[tuple[str, str, str], GraphValidationIssue] = {}
        for issue in issues:
            deduped[(issue.node_id, issue.socket_id, issue.message)] = issue
        return tuple(deduped.values())

    def export_enabled_outputs(
        self,
        project: NodeGraphProject,
        output_root: Path,
        options: ConversionOptions,
        logger: Logger | None = None,
    ) -> GraphExportSummary:
        return self._export_outputs(
            project,
            self._enabled_output_nodes(project.graph),
            output_root,
            options,
            logger,
        )

    def export_output(
        self,
        project: NodeGraphProject,
        output_node: GraphNode,
        output_root: Path,
        options: ConversionOptions,
        logger: Logger | None = None,
    ) -> GraphExportSummary:
        if output_node.node_type is not NodeType.OUTPUT_RGBA:
            raise GraphExecutionError(f"{output_node.title}: node is not an Output node.")
        return self._export_outputs(project, (output_node,), output_root, options, logger)

    def _export_outputs(
        self,
        project: NodeGraphProject,
        output_nodes: Iterable[GraphNode],
        output_root: Path,
        options: ConversionOptions,
        logger: Logger | None,
    ) -> GraphExportSummary:
        write_log = logger or (lambda _message: None)
        output_nodes = tuple(output_nodes)
        self._prepare_lookup(project.graph)
        plan = self.plan_outputs(project.graph, output_nodes, output_root)
        if not plan.is_valid:
            raise GraphExecutionError(plan.collision_message())
        output_root.mkdir(parents=True, exist_ok=True)
        results: list[GraphExportResult] = []
        cache = NodeGraphPreviewCache()

        for output_node, plan_item in zip(output_nodes, plan.items, strict=True):
            destination = plan_item.destination
            try:
                result = self._export_output_node(project.graph, output_node, destination, options, cache)
            except Exception as exc:
                result = GraphExportResult(
                    output_node_id=output_node.node_id,
                    output_name=output_node.title,
                    destination=destination,
                    status=ConversionStatus.FAILED,
                    message=f"ОШИБКА: {exc}",
                )
            write_log(f"GRAPH {output_node.title} -> {result.message}")
            results.append(result)

        return GraphExportSummary(results=results)

    def plan_enabled_outputs(self, graph: NodeGraph, output_root: Path) -> GraphExportPlan:
        return self.plan_outputs(graph, self._enabled_output_nodes(graph), output_root)

    def plan_outputs(
        self,
        graph: NodeGraph,
        output_nodes: Iterable[GraphNode],
        output_root: Path,
    ) -> GraphExportPlan:
        items = tuple(
            GraphExportPlanItem(
                output_node_id=node.node_id,
                output_name=node.title,
                destination=self._build_output_path(node, output_root).resolve(strict=False),
            )
            for node in output_nodes
        )
        by_destination: dict[str, list[GraphExportPlanItem]] = defaultdict(list)
        for item in items:
            normalized = os.path.normcase(os.path.normpath(str(item.destination)))
            by_destination[normalized].append(item)
        collisions = tuple(
            GraphExportCollision(destination=duplicates[0].destination, outputs=tuple(duplicates))
            for duplicates in by_destination.values()
            if len(duplicates) > 1
        )
        sources_by_path: dict[str, list[Path]] = defaultdict(list)
        for node in graph.nodes:
            if node.node_type is not NodeType.TEXTURE_INPUT:
                continue
            raw_path = str(node.properties.get("path", "")).strip()
            if not raw_path:
                continue
            source = Path(raw_path).resolve(strict=False)
            sources_by_path[self._normalized_path_key(source)].append(source)
        source_collisions = tuple(
            GraphExportSourceCollision(
                destination=item.destination,
                output=item,
                sources=tuple(sources_by_path[self._normalized_path_key(item.destination)]),
            )
            for item in items
            if self._normalized_path_key(item.destination) in sources_by_path
        )
        return GraphExportPlan(
            items=items,
            collisions=collisions,
            source_collisions=source_collisions,
        )

    def resolve_output_paths(self, graph: NodeGraph, output_root: Path) -> list[Path]:
        return [item.destination for item in self.plan_enabled_outputs(graph, output_root).items]

    def resolve_output_path(self, output_node: GraphNode, output_root: Path) -> Path:
        if output_node.node_type is not NodeType.OUTPUT_RGBA:
            raise GraphExecutionError(f"{output_node.title}: node is not an Output node.")
        return self._build_output_path(output_node, output_root)

    def _export_output_node(
        self,
        graph: NodeGraph,
        output_node: GraphNode,
        destination: Path,
        options: ConversionOptions,
        cache: NodeGraphPreviewCache | None = None,
    ) -> GraphExportResult:
        missing = self._missing_required_output_inputs(graph, output_node)
        if missing:
            return GraphExportResult(
                output_node_id=output_node.node_id,
                output_name=output_node.title,
                destination=destination,
                status=ConversionStatus.FAILED,
                message=f"ОШИБКА: не подключены каналы {', '.join(missing)}",
            )

        if destination.exists() and not options.overwrite:
            return GraphExportResult(
                output_node_id=output_node.node_id,
                output_name=output_node.title,
                destination=destination,
                status=ConversionStatus.SKIPPED,
                message=f"пропуск (уже есть): {destination.name}",
            )

        target_size = self._determine_output_size(graph, output_node, cache)
        if target_size is None:
            return GraphExportResult(
                output_node_id=output_node.node_id,
                output_name=output_node.title,
                destination=destination,
                status=ConversionStatus.FAILED,
                message="ОШИБКА: output должен зависеть хотя бы от одной texture-ноды",
            )

        merged = self._compose_output_image(graph, output_node, target_size, cache)
        destination.parent.mkdir(parents=True, exist_ok=True)
        self._save_output_image(merged, destination, options)
        return GraphExportResult(
            output_node_id=output_node.node_id,
            output_name=output_node.title,
            destination=destination,
            status=ConversionStatus.SUCCESS,
            message=f"exported: {destination.name}",
        )

    def _compose_output_image(
        self,
        graph: NodeGraph,
        output_node: GraphNode,
        target_size: tuple[int, int],
        cache: NodeGraphPreviewCache | None = None,
    ) -> Image.Image:
        mode = self._output_mode(output_node)
        channels = [
            self._evaluate_output_input(graph, output_node, socket_id, target_size, cache)
            for socket_id in ("r", "g", "b")
        ]
        if mode is OutputMode.RGBA:
            alpha = self._evaluate_optional_output_input(graph, output_node, "a", target_size, cache)
            channels.append(alpha)

        return Image.merge("RGBA" if mode is OutputMode.RGBA else "RGB", tuple(channels))

    def _compose_output_preview_image(
        self,
        graph: NodeGraph,
        output_node: GraphNode,
        target_size: tuple[int, int],
        cache: NodeGraphPreviewCache,
    ) -> Image.Image:
        missing = self._missing_required_output_inputs(graph, output_node)
        if missing:
            raise GraphExecutionError(f"не подключены каналы {', '.join(missing)}")

        mode = self._output_mode(output_node)
        channels = [
            self._evaluate_output_input_cached(graph, output_node, socket_id, target_size, cache)
            for socket_id in ("r", "g", "b")
        ]
        if mode is OutputMode.RGBA:
            alpha = self._evaluate_optional_output_input_cached(
                graph,
                output_node,
                "a",
                target_size,
                cache,
            )
            channels.append(alpha)

        return Image.merge("RGBA" if mode is OutputMode.RGBA else "RGB", tuple(channels))

    def _evaluate_output_input(
        self,
        graph: NodeGraph,
        output_node: GraphNode,
        socket_id: str,
        target_size: tuple[int, int],
        cache: NodeGraphPreviewCache | None = None,
    ) -> Image.Image:
        connection = self._incoming_connection(
            graph,
            target_node_id=output_node.node_id,
            target_socket_id=socket_id,
        )
        if connection is None:
            raise GraphExecutionError(f"Output channel {socket_id.upper()} is not connected.")
        if cache is not None:
            return self._evaluate_channel_socket_cached(graph, connection, target_size, set(), cache)
        return self._evaluate_channel_socket(graph, connection, target_size, set())

    def _evaluate_optional_output_input(
        self,
        graph: NodeGraph,
        output_node: GraphNode,
        socket_id: str,
        target_size: tuple[int, int],
        cache: NodeGraphPreviewCache | None = None,
    ) -> Image.Image:
        connection = self._incoming_connection(
            graph,
            target_node_id=output_node.node_id,
            target_socket_id=socket_id,
        )
        if connection is None:
            return Image.new("L", target_size, 255)
        if cache is not None:
            return self._evaluate_channel_socket_cached(graph, connection, target_size, set(), cache)
        return self._evaluate_channel_socket(graph, connection, target_size, set())

    def _evaluate_output_input_cached(
        self,
        graph: NodeGraph,
        output_node: GraphNode,
        socket_id: str,
        target_size: tuple[int, int],
        cache: NodeGraphPreviewCache,
    ) -> Image.Image:
        connection = self._incoming_connection(
            graph,
            target_node_id=output_node.node_id,
            target_socket_id=socket_id,
        )
        if connection is None:
            raise GraphExecutionError(f"Output channel {socket_id.upper()} is not connected.")
        return self._evaluate_channel_socket_cached(graph, connection, target_size, set(), cache)

    def _evaluate_optional_output_input_cached(
        self,
        graph: NodeGraph,
        output_node: GraphNode,
        socket_id: str,
        target_size: tuple[int, int],
        cache: NodeGraphPreviewCache,
    ) -> Image.Image:
        connection = self._incoming_connection(
            graph,
            target_node_id=output_node.node_id,
            target_socket_id=socket_id,
        )
        if connection is None:
            return Image.new("L", target_size, 255)
        return self._evaluate_channel_socket_cached(graph, connection, target_size, set(), cache)

    def _evaluate_channel_socket(
        self,
        graph: NodeGraph,
        connection: GraphConnection,
        target_size: tuple[int, int],
        visiting: set[tuple[str, str]],
    ) -> Image.Image:
        key = (connection.source_node_id, connection.source_socket_id)
        if key in visiting:
            raise GraphExecutionError("Graph contains a cycle.")
        visiting.add(key)

        source_node = self._find_node(graph, connection.source_node_id)
        if source_node is None:
            raise GraphExecutionError(f"Source node {connection.source_node_id} not found.")

        if source_node.node_type is NodeType.TEXTURE_INPUT:
            channel = self._load_texture_channel(source_node, connection.source_socket_id)
        elif source_node.node_type is NodeType.COLOR:
            channel = Image.new(
                "L",
                target_size,
                self._color_component(source_node, connection.source_socket_id),
            )
        elif source_node.node_type is NodeType.CONSTANT_CHANNEL:
            channel = Image.new(
                "L",
                target_size,
                self._constant_value(source_node),
            )
        elif source_node.node_type is NodeType.INVERT_CHANNEL:
            input_connection = self._incoming_connection(
                graph,
                target_node_id=source_node.node_id,
                target_socket_id="in",
            )
            if input_connection is None:
                raise GraphExecutionError(f"{source_node.title}: input is not connected.")
            input_channel = self._evaluate_channel_socket(graph, input_connection, target_size, visiting)
            if self._node_enabled(source_node):
                channel = ImageOps.invert(input_channel)
            else:
                channel = input_channel
        elif source_node.node_type is NodeType.LEVELS_CHANNEL:
            input_channel = self._evaluate_required_input(
                graph,
                source_node,
                "in",
                target_size,
                visiting,
            )
            if self._node_enabled(source_node):
                channel = self._apply_levels(input_channel, source_node)
            else:
                channel = input_channel
        elif source_node.node_type is NodeType.REMAP_CHANNEL:
            input_channel = self._evaluate_required_input(
                graph,
                source_node,
                "in",
                target_size,
                visiting,
            )
            if self._node_enabled(source_node):
                channel = self._apply_remap(input_channel, source_node)
            else:
                channel = input_channel
        elif source_node.node_type is NodeType.CLAMP_CHANNEL:
            input_channel = self._evaluate_required_input(
                graph,
                source_node,
                "in",
                target_size,
                visiting,
            )
            if self._node_enabled(source_node):
                channel = self._apply_clamp(input_channel, source_node)
            else:
                channel = input_channel
        elif source_node.node_type is NodeType.THRESHOLD_CHANNEL:
            input_channel = self._evaluate_required_input(
                graph,
                source_node,
                "in",
                target_size,
                visiting,
            )
            if self._node_enabled(source_node):
                channel = self._apply_threshold(input_channel, source_node)
            else:
                channel = input_channel
        elif source_node.node_type is NodeType.BLUR_CHANNEL:
            input_channel = self._evaluate_required_input(
                graph,
                source_node,
                "in",
                target_size,
                visiting,
            )
            if self._node_enabled(source_node):
                channel = self._apply_blur(input_channel, source_node)
            else:
                channel = input_channel
        elif source_node.node_type is NodeType.DILATE_CHANNEL:
            input_channel = self._evaluate_required_input(
                graph,
                source_node,
                "in",
                target_size,
                visiting,
            )
            if self._node_enabled(source_node):
                channel = self._apply_dilate(input_channel, source_node)
            else:
                channel = input_channel
        elif source_node.node_type is NodeType.ERODE_CHANNEL:
            input_channel = self._evaluate_required_input(
                graph,
                source_node,
                "in",
                target_size,
                visiting,
            )
            if self._node_enabled(source_node):
                channel = self._apply_erode(input_channel, source_node)
            else:
                channel = input_channel
        elif source_node.node_type is NodeType.BLEND_CHANNEL:
            a_channel = self._evaluate_required_input(
                graph,
                source_node,
                "a",
                target_size,
                visiting,
            )
            if self._node_enabled(source_node):
                b_channel = self._evaluate_required_input(
                    graph,
                    source_node,
                    "b",
                    target_size,
                    visiting,
                )
                channel = self._apply_blend(a_channel, b_channel, source_node)
            else:
                channel = a_channel
        elif source_node.node_type is NodeType.LUMINANCE:
            red = self._evaluate_required_input(graph, source_node, "r", target_size, visiting)
            if self._node_enabled(source_node):
                green = self._evaluate_required_input(graph, source_node, "g", target_size, visiting)
                blue = self._evaluate_required_input(graph, source_node, "b", target_size, visiting)
                channel = Image.merge("RGB", (red, green, blue)).convert("L")
            else:
                channel = red
        else:
            raise GraphExecutionError(f"Unsupported source node: {source_node.node_type.value}")

        visiting.remove(key)
        if channel.size != target_size:
            channel = channel.resize(target_size, RESAMPLING_LANCZOS)
        return channel.copy()

    def _evaluate_channel_socket_cached(
        self,
        graph: NodeGraph,
        connection: GraphConnection,
        target_size: tuple[int, int],
        visiting: set[tuple[str, str]],
        cache: NodeGraphPreviewCache,
    ) -> Image.Image:
        key = (connection.source_node_id, connection.source_socket_id)
        if key in visiting:
            raise GraphExecutionError("Graph contains a cycle.")

        cache_key = (connection.source_node_id, connection.source_socket_id, target_size)
        cached = cache.get_image("channel", cache_key)
        if cached is not None:
            return cached.copy()

        visiting.add(key)
        try:
            source_node = self._find_node(graph, connection.source_node_id)
            if source_node is None:
                raise GraphExecutionError(f"Source node {connection.source_node_id} not found.")

            if source_node.node_type is NodeType.TEXTURE_INPUT:
                channel = self._load_texture_channel_cached(
                    source_node,
                    connection.source_socket_id,
                    cache,
                    target_size,
                )
            elif source_node.node_type is NodeType.COLOR:
                channel = Image.new(
                    "L",
                    target_size,
                    self._color_component(source_node, connection.source_socket_id),
                )
            elif source_node.node_type is NodeType.CONSTANT_CHANNEL:
                channel = Image.new("L", target_size, self._constant_value(source_node))
            elif source_node.node_type is NodeType.INVERT_CHANNEL:
                input_connection = self._incoming_connection(
                    graph,
                    target_node_id=source_node.node_id,
                    target_socket_id="in",
                )
                if input_connection is None:
                    raise GraphExecutionError(f"{source_node.title}: input is not connected.")
                if not self._node_enabled(source_node):
                    channel = self._evaluate_channel_socket_cached(
                        graph,
                        input_connection,
                        target_size,
                        visiting,
                        cache,
                    )
                else:
                    channel = ImageOps.invert(
                        self._evaluate_channel_socket_cached(
                            graph,
                            input_connection,
                            target_size,
                            visiting,
                            cache,
                        )
                    )
            elif source_node.node_type is NodeType.LEVELS_CHANNEL:
                input_channel = self._evaluate_required_input_cached(
                    graph,
                    source_node,
                    "in",
                    target_size,
                    visiting,
                    cache,
                )
                if self._node_enabled(source_node):
                    channel = self._apply_levels(input_channel, source_node)
                else:
                    channel = input_channel
            elif source_node.node_type is NodeType.REMAP_CHANNEL:
                input_channel = self._evaluate_required_input_cached(
                    graph,
                    source_node,
                    "in",
                    target_size,
                    visiting,
                    cache,
                )
                if self._node_enabled(source_node):
                    channel = self._apply_remap(input_channel, source_node)
                else:
                    channel = input_channel
            elif source_node.node_type is NodeType.CLAMP_CHANNEL:
                input_channel = self._evaluate_required_input_cached(
                    graph,
                    source_node,
                    "in",
                    target_size,
                    visiting,
                    cache,
                )
                if self._node_enabled(source_node):
                    channel = self._apply_clamp(input_channel, source_node)
                else:
                    channel = input_channel
            elif source_node.node_type is NodeType.THRESHOLD_CHANNEL:
                input_channel = self._evaluate_required_input_cached(
                    graph,
                    source_node,
                    "in",
                    target_size,
                    visiting,
                    cache,
                )
                if self._node_enabled(source_node):
                    channel = self._apply_threshold(input_channel, source_node)
                else:
                    channel = input_channel
            elif source_node.node_type is NodeType.BLUR_CHANNEL:
                input_channel = self._evaluate_required_input_cached(
                    graph,
                    source_node,
                    "in",
                    target_size,
                    visiting,
                    cache,
                )
                if self._node_enabled(source_node):
                    channel = self._apply_blur(input_channel, source_node)
                else:
                    channel = input_channel
            elif source_node.node_type is NodeType.DILATE_CHANNEL:
                input_channel = self._evaluate_required_input_cached(
                    graph,
                    source_node,
                    "in",
                    target_size,
                    visiting,
                    cache,
                )
                if self._node_enabled(source_node):
                    channel = self._apply_dilate(input_channel, source_node)
                else:
                    channel = input_channel
            elif source_node.node_type is NodeType.ERODE_CHANNEL:
                input_channel = self._evaluate_required_input_cached(
                    graph,
                    source_node,
                    "in",
                    target_size,
                    visiting,
                    cache,
                )
                if self._node_enabled(source_node):
                    channel = self._apply_erode(input_channel, source_node)
                else:
                    channel = input_channel
            elif source_node.node_type is NodeType.BLEND_CHANNEL:
                a_channel = self._evaluate_required_input_cached(
                    graph,
                    source_node,
                    "a",
                    target_size,
                    visiting,
                    cache,
                )
                if not self._node_enabled(source_node):
                    channel = a_channel
                else:
                    b_channel = self._evaluate_required_input_cached(
                        graph,
                        source_node,
                        "b",
                        target_size,
                        visiting,
                        cache,
                    )
                    channel = self._apply_blend(a_channel, b_channel, source_node)
            elif source_node.node_type is NodeType.LUMINANCE:
                red = self._evaluate_required_input_cached(
                    graph,
                    source_node,
                    "r",
                    target_size,
                    visiting,
                    cache,
                )
                if not self._node_enabled(source_node):
                    channel = red
                else:
                    green = self._evaluate_required_input_cached(
                        graph,
                        source_node,
                        "g",
                        target_size,
                        visiting,
                        cache,
                    )
                    blue = self._evaluate_required_input_cached(
                        graph,
                        source_node,
                        "b",
                        target_size,
                        visiting,
                        cache,
                    )
                    channel = Image.merge("RGB", (red, green, blue)).convert("L")
            else:
                raise GraphExecutionError(f"Unsupported source node: {source_node.node_type.value}")
        finally:
            visiting.remove(key)

        if channel.size != target_size:
            channel = channel.resize(target_size, RESAMPLING_LANCZOS)
        result = channel.copy()
        cache.put_image("channel", cache_key, result)
        return result.copy()

    def _determine_output_size(
        self,
        graph: NodeGraph,
        output_node: GraphNode,
        cache: NodeGraphPreviewCache | None = None,
    ) -> tuple[int, int] | None:
        connections = []
        for socket_id in ("r", "g", "b", "a"):
            connection = self._incoming_connection(
                graph,
                target_node_id=output_node.node_id,
                target_socket_id=socket_id,
            )
            if connection is None:
                continue
            connections.append(connection)
            target_size = self._find_upstream_texture_size(graph, connection, set(), cache)
            if target_size is not None:
                return target_size
        for connection in connections:
            target_size = self._find_upstream_color_size(graph, connection, set())
            if target_size is not None:
                return target_size
        return None

    def _find_upstream_texture_size(
        self,
        graph: NodeGraph,
        connection: GraphConnection,
        visiting: set[tuple[str, str]],
        cache: NodeGraphPreviewCache | None = None,
    ) -> tuple[int, int] | None:
        key = (connection.source_node_id, connection.source_socket_id)
        if key in visiting:
            return None
        visiting.add(key)
        try:
            source_node = self._find_node(graph, connection.source_node_id)
            if source_node is None:
                return None
            if source_node.node_type is NodeType.TEXTURE_INPUT:
                return self._load_texture_size(source_node, cache)
            if source_node.node_type is NodeType.INVERT_CHANNEL:
                return self._find_first_upstream_texture_size(graph, source_node, ("in",), visiting, cache)
            if source_node.node_type in (
                NodeType.LEVELS_CHANNEL,
                NodeType.REMAP_CHANNEL,
                NodeType.CLAMP_CHANNEL,
                NodeType.THRESHOLD_CHANNEL,
                NodeType.BLUR_CHANNEL,
                NodeType.DILATE_CHANNEL,
                NodeType.ERODE_CHANNEL,
            ):
                return self._find_first_upstream_texture_size(graph, source_node, ("in",), visiting, cache)
            if source_node.node_type is NodeType.BLEND_CHANNEL:
                return self._find_first_upstream_texture_size(graph, source_node, ("a", "b"), visiting, cache)
            if source_node.node_type is NodeType.LUMINANCE:
                return self._find_first_upstream_texture_size(graph, source_node, ("r", "g", "b"), visiting, cache)
            return None
        finally:
            visiting.remove(key)

    def _find_first_upstream_texture_size(
        self,
        graph: NodeGraph,
        node: GraphNode,
        socket_ids: tuple[str, ...],
        visiting: set[tuple[str, str]],
        cache: NodeGraphPreviewCache | None = None,
    ) -> tuple[int, int] | None:
        for socket_id in socket_ids:
            connection = self._incoming_connection(
                graph,
                target_node_id=node.node_id,
                target_socket_id=socket_id,
            )
            if connection is None:
                continue
            target_size = self._find_upstream_texture_size(graph, connection, visiting, cache)
            if target_size is not None:
                return target_size
        return None

    def _find_upstream_color_size(
        self,
        graph: NodeGraph,
        connection: GraphConnection,
        visiting: set[tuple[str, str]],
    ) -> tuple[int, int] | None:
        key = (connection.source_node_id, connection.source_socket_id)
        if key in visiting:
            return None
        visiting.add(key)
        try:
            source_node = self._find_node(graph, connection.source_node_id)
            if source_node is None:
                return None
            if source_node.node_type is NodeType.COLOR:
                return self._color_size(source_node)
            if source_node.node_type in (
                NodeType.INVERT_CHANNEL,
                NodeType.LEVELS_CHANNEL,
                NodeType.REMAP_CHANNEL,
                NodeType.CLAMP_CHANNEL,
                NodeType.THRESHOLD_CHANNEL,
                NodeType.BLUR_CHANNEL,
                NodeType.DILATE_CHANNEL,
                NodeType.ERODE_CHANNEL,
            ):
                return self._find_first_upstream_color_size(graph, source_node, ("in",), visiting)
            if source_node.node_type is NodeType.BLEND_CHANNEL:
                return self._find_first_upstream_color_size(graph, source_node, ("a", "b"), visiting)
            if source_node.node_type is NodeType.LUMINANCE:
                return self._find_first_upstream_color_size(graph, source_node, ("r", "g", "b"), visiting)
            return None
        finally:
            visiting.remove(key)

    def _find_first_upstream_color_size(
        self,
        graph: NodeGraph,
        node: GraphNode,
        socket_ids: tuple[str, ...],
        visiting: set[tuple[str, str]],
    ) -> tuple[int, int] | None:
        for socket_id in socket_ids:
            connection = self._incoming_connection(
                graph,
                target_node_id=node.node_id,
                target_socket_id=socket_id,
            )
            if connection is None:
                continue
            target_size = self._find_upstream_color_size(graph, connection, visiting)
            if target_size is not None:
                return target_size
        return None

    def _preview_target_size(
        self,
        graph: NodeGraph,
        connection: GraphConnection,
        cache: NodeGraphPreviewCache,
        fallback_size: tuple[int, int],
        max_side: int | None = None,
    ) -> tuple[int, int]:
        target_size = self._find_upstream_texture_size(graph, connection, set(), cache)
        if target_size is None:
            target_size = fallback_size
        return self._fit_preview_size(
            target_size,
            cache.max_side if max_side is None else max_side,
        )

    @staticmethod
    def _fit_preview_size(size: tuple[int, int], max_side: int) -> tuple[int, int]:
        width, height = size
        if width <= 0 or height <= 0:
            return (1, 1)
        if max_side <= 0:
            return (max(1, width), max(1, height))
        longest_side = max(width, height)
        if longest_side <= max_side:
            return (max(1, width), max(1, height))
        scale = max_side / longest_side
        return (max(1, round(width * scale)), max(1, round(height * scale)))

    def _evaluate_required_input(
        self,
        graph: NodeGraph,
        node: GraphNode,
        socket_id: str,
        target_size: tuple[int, int],
        visiting: set[tuple[str, str]],
    ) -> Image.Image:
        connection = self._incoming_connection(
            graph,
            target_node_id=node.node_id,
            target_socket_id=socket_id,
        )
        if connection is None:
            raise GraphExecutionError(f"{node.title}: input {socket_id.upper()} is not connected.")
        return self._evaluate_channel_socket(graph, connection, target_size, visiting)

    def _evaluate_required_input_cached(
        self,
        graph: NodeGraph,
        node: GraphNode,
        socket_id: str,
        target_size: tuple[int, int],
        visiting: set[tuple[str, str]],
        cache: NodeGraphPreviewCache,
    ) -> Image.Image:
        connection = self._incoming_connection(
            graph,
            target_node_id=node.node_id,
            target_socket_id=socket_id,
        )
        if connection is None:
            raise GraphExecutionError(f"{node.title}: input {socket_id.upper()} is not connected.")
        return self._evaluate_channel_socket_cached(
            graph,
            connection,
            target_size,
            visiting,
            cache,
        )

    def _apply_levels(self, channel: Image.Image, node: GraphNode) -> Image.Image:
        black = self._property_int(node, "black", 0)
        white = self._property_int(node, "white", 255)
        gamma = self._property_float(node, "gamma", 1.0)
        out_min = self._property_int(node, "out_min", 0)
        out_max = self._property_int(node, "out_max", 255)

        if white <= black:
            white = min(255, black + 1)
        gamma = max(0.05, gamma)
        out_min = max(0, min(out_min, 255))
        out_max = max(0, min(out_max, 255))

        return self._ensure_l_mode(channel).point(
            _levels_lut(black, white, gamma, out_min, out_max)
        )

    def _apply_remap(self, channel: Image.Image, node: GraphNode) -> Image.Image:
        in_min = self._property_int(node, "in_min", 0)
        in_max = self._property_int(node, "in_max", 255)
        out_min = self._property_int(node, "out_min", 0)
        out_max = self._property_int(node, "out_max", 255)

        if in_max <= in_min:
            in_max = min(255, in_min + 1)
        out_min = max(0, min(out_min, 255))
        out_max = max(0, min(out_max, 255))

        return self._ensure_l_mode(channel).point(
            _remap_lut(in_min, in_max, out_min, out_max)
        )

    def _apply_clamp(self, channel: Image.Image, node: GraphNode) -> Image.Image:
        minimum = self._property_int(node, "min", 0)
        maximum = self._property_int(node, "max", 255)
        if maximum < minimum:
            minimum, maximum = maximum, minimum
        minimum = max(0, min(minimum, 255))
        maximum = max(0, min(maximum, 255))
        return self._ensure_l_mode(channel).point(_clamp_lut(minimum, maximum))

    def _apply_threshold(self, channel: Image.Image, node: GraphNode) -> Image.Image:
        threshold = self._property_int(node, "threshold", 128)
        threshold = max(0, min(threshold, 255))
        return self._ensure_l_mode(channel).point(_threshold_lut(threshold))

    def _apply_blur(self, channel: Image.Image, node: GraphNode) -> Image.Image:
        radius = max(0, min(self._property_int(node, "radius", 1), 64))
        source = self._ensure_l_mode(channel)
        if radius <= 0:
            return source.copy()
        return source.filter(ImageFilter.BoxBlur(radius))

    def _apply_dilate(self, channel: Image.Image, node: GraphNode) -> Image.Image:
        return self._apply_morphology(channel, node, ImageFilter.MaxFilter)

    def _apply_erode(self, channel: Image.Image, node: GraphNode) -> Image.Image:
        return self._apply_morphology(channel, node, ImageFilter.MinFilter)

    def _apply_morphology(
        self,
        channel: Image.Image,
        node: GraphNode,
        filter_factory,
    ) -> Image.Image:
        radius = max(0, min(self._property_int(node, "radius", 1), 64))
        source = self._ensure_l_mode(channel)
        if radius <= 0:
            return source.copy()
        return source.filter(filter_factory(radius * 2 + 1))

    def _apply_blend(self, a_channel: Image.Image, b_channel: Image.Image, node: GraphNode) -> Image.Image:
        a = self._ensure_l_mode(a_channel)
        b = self._ensure_l_mode(b_channel)
        mode = str(node.properties.get("mode", "multiply")).lower()

        if mode == "add":
            blended = ImageChops.add(a, b)
        elif mode == "subtract":
            blended = ImageChops.subtract(a, b)
        elif mode == "max":
            blended = ImageChops.lighter(a, b)
        elif mode == "min":
            blended = ImageChops.darker(a, b)
        elif mode == "average":
            blended = Image.blend(a, b, 0.5)
        else:
            blended = ImageChops.multiply(a, b)

        opacity = max(0, min(self._property_int(node, "opacity", 100), 100)) / 100.0
        if opacity >= 1.0:
            return blended
        if opacity <= 0.0:
            return a
        return Image.blend(a, blended, opacity)

    @staticmethod
    def _load_texture_channel(node: GraphNode, socket_id: str) -> Image.Image:
        path = Path(str(node.properties.get("path", "")))
        if not path.exists():
            raise GraphExecutionError(f"{node.title}: texture not found: {path}")

        with Image.open(path) as image:
            working_image = _extract_first_frame(image).convert("RGBA")
            if socket_id == "r":
                return working_image.getchannel("R").copy()
            if socket_id == "g":
                return working_image.getchannel("G").copy()
            if socket_id == "b":
                return working_image.getchannel("B").copy()
            if socket_id == "a":
                if "A" in working_image.getbands():
                    return working_image.getchannel("A").copy()
                return Image.new("L", working_image.size, 255)
        raise GraphExecutionError(f"Unsupported texture channel: {socket_id}")

    @staticmethod
    def _load_texture_preview_image(node: GraphNode) -> Image.Image:
        path = Path(str(node.properties.get("path", "")))
        if not path.exists():
            raise GraphExecutionError(f"{node.title}: texture not found: {path}")

        with Image.open(path) as image:
            return _extract_first_frame(image).convert("RGBA")

    def _load_texture_channel_cached(
        self,
        node: GraphNode,
        socket_id: str,
        cache: NodeGraphPreviewCache,
        target_size: tuple[int, int] | None = None,
    ) -> Image.Image:
        cache_key = (node.node_id, socket_id, target_size)
        cached = cache.get_image("texture-channel", cache_key)
        if cached is not None:
            return cached.copy()
        working_image = self._load_texture_preview_image_cached(node, cache, target_size)
        if socket_id == "r":
            channel = working_image.getchannel("R").copy()
        elif socket_id == "g":
            channel = working_image.getchannel("G").copy()
        elif socket_id == "b":
            channel = working_image.getchannel("B").copy()
        elif socket_id == "a":
            if "A" in working_image.getbands():
                channel = working_image.getchannel("A").copy()
            else:
                channel = Image.new("L", working_image.size, 255)
        else:
            raise GraphExecutionError(f"Unsupported texture channel: {socket_id}")
        cache.put_image("texture-channel", cache_key, channel.copy())
        return channel

    def _load_texture_preview_image_cached(
        self,
        node: GraphNode,
        cache: NodeGraphPreviewCache,
        target_size: tuple[int, int] | None = None,
    ) -> Image.Image:
        cache_key = (node.node_id, target_size)
        cached = cache.get_image("texture-preview", cache_key)
        if cached is not None:
            return cached.copy()
        image = self._load_texture_preview_image(node)
        full_size = image.size
        if target_size is not None and image.size != target_size:
            image = image.resize(target_size, RESAMPLING_LANCZOS)
        cache.put_image("texture-preview", cache_key, image.copy())
        cache.put_texture_size(node.node_id, full_size)
        return image

    def _load_texture_size(
        self,
        node: GraphNode,
        cache: NodeGraphPreviewCache | None = None,
    ) -> tuple[int, int] | None:
        if cache is not None:
            cached_size = cache.get_texture_size(node.node_id)
            if cached_size is not _CACHE_MISS:
                return cached_size

        path = Path(str(node.properties.get("path", "")))
        if not path.exists():
            if cache is not None:
                cache.put_texture_size(node.node_id, None)
            return None
        with Image.open(path) as image:
            size = image.size
        if cache is not None:
            cache.put_texture_size(node.node_id, size)
        return size

    @staticmethod
    def _constant_value(node: GraphNode) -> int:
        try:
            value = int(node.properties.get("value", 255))
        except (TypeError, ValueError):
            value = 255
        return max(0, min(value, 255))

    @classmethod
    def _color_rgba(cls, node: GraphNode) -> tuple[int, int, int, int]:
        return tuple(
            cls._color_component(node, socket_id)
            for socket_id in ("r", "g", "b", "a")
        )

    @staticmethod
    def _color_component(node: GraphNode, socket_id: str) -> int:
        property_name = {
            "r": "red",
            "g": "green",
            "b": "blue",
            "a": "alpha",
        }.get(socket_id, "alpha")
        try:
            value = int(node.properties.get(property_name, 255))
        except (TypeError, ValueError):
            value = 255
        return max(0, min(value, 255))

    @staticmethod
    def _color_size(node: GraphNode) -> tuple[int, int]:
        dimensions = []
        for property_name in ("width", "height"):
            try:
                value = int(node.properties.get(property_name, 1024))
            except (TypeError, ValueError):
                value = 1024
            dimensions.append(max(1, min(value, 16384)))
        return (dimensions[0], dimensions[1])

    @staticmethod
    def _node_enabled(node: GraphNode) -> bool:
        return bool(node.properties.get("enabled", True))

    @staticmethod
    def _property_int(node: GraphNode, key: str, default: int) -> int:
        try:
            value = int(node.properties.get(key, default))
        except (TypeError, ValueError):
            value = default
        return value

    @staticmethod
    def _property_float(node: GraphNode, key: str, default: float) -> float:
        try:
            return float(node.properties.get(key, default))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _ensure_l_mode(image: Image.Image) -> Image.Image:
        if image.mode == "L":
            return image
        return image.convert("L")

    @staticmethod
    def _output_mode(node: GraphNode) -> OutputMode:
        try:
            return OutputMode(str(node.properties.get("mode", OutputMode.RGBA.value)))
        except ValueError:
            return OutputMode.RGBA

    @staticmethod
    def _first_channel_output_socket(node: GraphNode):
        return next(
            (
                socket
                for socket in socket_definitions(node.node_type)
                if socket.direction is SocketDirection.OUTPUT
                and socket.socket_type is SocketType.CHANNEL
            ),
            None,
        )

    @staticmethod
    def _build_output_path(node: GraphNode, output_root: Path) -> Path:
        raw_output_path = str(node.properties.get("output_path", "")).strip()
        if raw_output_path:
            destination = Path(raw_output_path)
            if not destination.is_absolute():
                destination = output_root / destination
            if not destination.suffix:
                destination = destination.with_suffix(".png")
            return destination

        raw_filename = str(node.properties.get("filename", "")).strip() or f"{node.title}.png"
        destination = output_root / raw_filename
        if not destination.suffix:
            destination = destination.with_suffix(".png")
        return destination

    def _save_output_image(
        self,
        image: Image.Image,
        destination: Path,
        options: ConversionOptions,
    ) -> None:
        export_format = self._export_format_for_path(destination)
        save_kwargs: dict[str, object] = {"format": export_format}
        output_image = image

        if export_format == "PNG":
            save_kwargs["optimize"] = options.optimize
            save_kwargs["compress_level"] = options.compress_level
        elif export_format == "JPEG":
            output_image = image.convert("RGB")
            save_kwargs["quality"] = 95
            save_kwargs["subsampling"] = 0

        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=destination.parent,
        )
        os.close(file_descriptor)
        temporary_path = Path(temporary_name)
        try:
            output_image.save(temporary_path, **save_kwargs)
            with temporary_path.open("r+b") as temporary_file:
                os.fsync(temporary_file.fileno())
            os.replace(temporary_path, destination)
        finally:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass

    @staticmethod
    def _normalized_path_key(path: Path) -> str:
        return os.path.normcase(os.path.normpath(str(path.resolve(strict=False))))

    def _export_format_for_path(self, destination: Path) -> str:
        export_format = self._EXPORT_FORMATS.get(destination.suffix.lower())
        if export_format:
            return export_format
        supported = ", ".join(sorted(self._EXPORT_FORMATS))
        raise GraphExecutionError(
            f"unsupported export format '{destination.suffix or '<none>'}'. "
            f"Supported formats: {supported}"
        )

    @staticmethod
    def _enabled_output_nodes(graph: NodeGraph) -> list[GraphNode]:
        return [
            node
            for node in graph.nodes
            if node.node_type is NodeType.OUTPUT_RGBA and bool(node.properties.get("enabled", True))
        ]

    def _missing_required_output_inputs(self, graph: NodeGraph, node: GraphNode) -> tuple[str, ...]:
        missing = []
        for socket_id in ("r", "g", "b"):
            if self._incoming_connection(
                graph,
                target_node_id=node.node_id,
                target_socket_id=socket_id,
            ) is None:
                missing.append(socket_id.upper())
        return tuple(missing)


def _extract_first_frame(image: Image.Image) -> Image.Image:
    return copy_first_frame_preserving_alpha(image)
