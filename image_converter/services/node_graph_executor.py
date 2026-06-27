from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from threading import RLock

from PIL import Image, ImageChops, ImageOps

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
    find_node,
    incoming_connection,
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


class GraphExecutionError(RuntimeError):
    pass


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


@dataclass(slots=True)
class NodeGraphPreviewCache:
    max_side: int = 1024
    fallback_size: tuple[int, int] = (256, 256)
    channels: dict[tuple[str, str, tuple[int, int]], Image.Image] = field(default_factory=dict)
    texture_channels: dict[tuple[str, str, tuple[int, int] | None], Image.Image] = field(default_factory=dict)
    texture_preview_images: dict[tuple[str, tuple[int, int] | None], Image.Image] = field(default_factory=dict)
    texture_sizes: dict[str, tuple[int, int] | None] = field(default_factory=dict)
    lock: RLock = field(default_factory=RLock, repr=False)

    def clear(self) -> None:
        with self.lock:
            self.channels.clear()
            self.texture_channels.clear()
            self.texture_preview_images.clear()
            self.texture_sizes.clear()

    def invalidate_node_and_downstream(self, graph: NodeGraph, node_id: str) -> None:
        dirty_node_ids = self._downstream_node_ids(graph, node_id)
        with self.lock:
            self.channels = {
                key: image
                for key, image in self.channels.items()
                if key[0] not in dirty_node_ids
            }
            self.texture_channels = {
                key: image
                for key, image in self.texture_channels.items()
                if key[0] not in dirty_node_ids
            }
            self.texture_preview_images = {
                key: image
                for key, image in self.texture_preview_images.items()
                if key[0] not in dirty_node_ids
            }
            self.texture_sizes = {
                key: image
                for key, image in self.texture_sizes.items()
                if key not in dirty_node_ids
            }

    @staticmethod
    def _downstream_node_ids(graph: NodeGraph, node_id: str) -> set[str]:
        dirty = {node_id}
        pending = [node_id]
        while pending:
            current_id = pending.pop()
            for connection in graph.connections:
                if connection.source_node_id != current_id:
                    continue
                if connection.target_node_id in dirty:
                    continue
                dirty.add(connection.target_node_id)
                pending.append(connection.target_node_id)
        return dirty


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

    def render_display_node(
        self,
        graph: NodeGraph,
        node: GraphNode,
        *,
        preview_cache: NodeGraphPreviewCache | None = None,
        fallback_size: tuple[int, int] | None = None,
    ) -> tuple[Image.Image, str]:
        cache = preview_cache or NodeGraphPreviewCache()
        resolved_fallback = fallback_size or cache.fallback_size
        if node.node_type is NodeType.OUTPUT_RGBA:
            target_size = self._determine_output_size(graph, node, cache)
            if target_size is None:
                raise GraphExecutionError("output должен зависеть хотя бы от одной texture-ноды")
            target_size = self._fit_preview_size(target_size, cache.max_side)
            image = self._compose_output_preview_image(graph, node, target_size, cache).convert("RGBA")
            return image, f"Output preview · {image.width}x{image.height} · {image.mode}"
        if node.node_type is NodeType.VIEW:
            image = self.render_view_node(
                graph,
                node,
                preview_cache=cache,
                fallback_size=resolved_fallback,
            )
            return image, f"View preview · {image.width}x{image.height} · channel"
        if node.node_type is NodeType.TEXTURE_INPUT:
            target_size = self._fit_preview_size(
                self._load_texture_size(node, cache) or resolved_fallback,
                cache.max_side,
            )
            image = self._load_texture_preview_image_cached(node, cache, target_size)
            if image.size != target_size:
                image = image.resize(target_size, RESAMPLING_LANCZOS)
            image = image.convert("RGBA")
            return image, f"Display flag · Texture · {image.width}x{image.height} · {image.mode}"

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
    ) -> Image.Image:
        cache = preview_cache or NodeGraphPreviewCache(fallback_size=fallback_size)
        if view_node.node_type is not NodeType.VIEW:
            raise GraphExecutionError(f"{view_node.title}: node is not a View node.")

        connection = incoming_connection(
            graph,
            target_node_id=view_node.node_id,
            target_socket_id="in",
        )
        if connection is None:
            raise GraphExecutionError(f"{view_node.title}: input is not connected.")

        target_size = self._preview_target_size(graph, connection, cache, fallback_size)

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
        write_log = logger or (lambda _message: None)
        output_root.mkdir(parents=True, exist_ok=True)
        results: list[GraphExportResult] = []
        cache = NodeGraphPreviewCache()

        for output_node in self._enabled_output_nodes(project.graph):
            destination = self._build_output_path(output_node, output_root)
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

    def resolve_output_paths(self, graph: NodeGraph, output_root: Path) -> list[Path]:
        return [
            self._build_output_path(node, output_root)
            for node in self._enabled_output_nodes(graph)
        ]

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
        connection = incoming_connection(
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
        connection = incoming_connection(
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
        connection = incoming_connection(
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
        connection = incoming_connection(
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

        source_node = find_node(graph, connection.source_node_id)
        if source_node is None:
            raise GraphExecutionError(f"Source node {connection.source_node_id} not found.")

        if source_node.node_type is NodeType.TEXTURE_INPUT:
            channel = self._load_texture_channel(source_node, connection.source_socket_id)
        elif source_node.node_type is NodeType.CONSTANT_CHANNEL:
            channel = Image.new(
                "L",
                target_size,
                self._constant_value(source_node),
            )
        elif source_node.node_type is NodeType.INVERT_CHANNEL:
            input_connection = incoming_connection(
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
        with cache.lock:
            cached = cache.channels.get(cache_key)
        if cached is not None:
            return cached.copy()

        visiting.add(key)
        try:
            source_node = find_node(graph, connection.source_node_id)
            if source_node is None:
                raise GraphExecutionError(f"Source node {connection.source_node_id} not found.")

            if source_node.node_type is NodeType.TEXTURE_INPUT:
                channel = self._load_texture_channel_cached(
                    source_node,
                    connection.source_socket_id,
                    cache,
                    target_size,
                )
            elif source_node.node_type is NodeType.CONSTANT_CHANNEL:
                channel = Image.new("L", target_size, self._constant_value(source_node))
            elif source_node.node_type is NodeType.INVERT_CHANNEL:
                input_connection = incoming_connection(
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
        with cache.lock:
            cache.channels[cache_key] = result
        return result.copy()

    def _determine_output_size(
        self,
        graph: NodeGraph,
        output_node: GraphNode,
        cache: NodeGraphPreviewCache | None = None,
    ) -> tuple[int, int] | None:
        for socket_id in ("r", "g", "b", "a"):
            connection = incoming_connection(
                graph,
                target_node_id=output_node.node_id,
                target_socket_id=socket_id,
            )
            if connection is None:
                continue
            target_size = self._find_upstream_texture_size(graph, connection, set(), cache)
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
            source_node = find_node(graph, connection.source_node_id)
            if source_node is None:
                return None
            if source_node.node_type is NodeType.TEXTURE_INPUT:
                return self._load_texture_size(source_node, cache)
            if source_node.node_type is NodeType.INVERT_CHANNEL:
                return self._find_first_upstream_texture_size(graph, source_node, ("in",), visiting, cache)
            if source_node.node_type in (
                NodeType.LEVELS_CHANNEL,
                NodeType.CLAMP_CHANNEL,
                NodeType.THRESHOLD_CHANNEL,
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
            connection = incoming_connection(
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

    def _preview_target_size(
        self,
        graph: NodeGraph,
        connection: GraphConnection,
        cache: NodeGraphPreviewCache,
        fallback_size: tuple[int, int],
    ) -> tuple[int, int]:
        target_size = self._find_upstream_texture_size(graph, connection, set(), cache)
        if target_size is None:
            target_size = fallback_size
        return self._fit_preview_size(target_size, cache.max_side)

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
        connection = incoming_connection(
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
        connection = incoming_connection(
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
        with cache.lock:
            cached = cache.texture_channels.get(cache_key)
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
        with cache.lock:
            cache.texture_channels[cache_key] = channel.copy()
        return channel

    def _load_texture_preview_image_cached(
        self,
        node: GraphNode,
        cache: NodeGraphPreviewCache,
        target_size: tuple[int, int] | None = None,
    ) -> Image.Image:
        cache_key = (node.node_id, target_size)
        with cache.lock:
            cached = cache.texture_preview_images.get(cache_key)
        if cached is not None:
            return cached.copy()
        image = self._load_texture_preview_image(node)
        full_size = image.size
        if target_size is not None and image.size != target_size:
            image = image.resize(target_size, RESAMPLING_LANCZOS)
        with cache.lock:
            cache.texture_preview_images[cache_key] = image.copy()
            cache.texture_sizes[node.node_id] = full_size
        return image

    def _load_texture_size(
        self,
        node: GraphNode,
        cache: NodeGraphPreviewCache | None = None,
    ) -> tuple[int, int] | None:
        if cache is not None:
            with cache.lock:
                if node.node_id in cache.texture_sizes:
                    return cache.texture_sizes[node.node_id]

        path = Path(str(node.properties.get("path", "")))
        if not path.exists():
            if cache is not None:
                with cache.lock:
                    cache.texture_sizes[node.node_id] = None
            return None
        with Image.open(path) as image:
            size = image.size
        if cache is not None:
            with cache.lock:
                cache.texture_sizes[node.node_id] = size
        return size

    @staticmethod
    def _constant_value(node: GraphNode) -> int:
        try:
            value = int(node.properties.get("value", 255))
        except (TypeError, ValueError):
            value = 255
        return max(0, min(value, 255))

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

        output_image.save(destination, **save_kwargs)

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

    @staticmethod
    def _missing_required_output_inputs(graph: NodeGraph, node: GraphNode) -> tuple[str, ...]:
        missing = []
        for socket_id in ("r", "g", "b"):
            if incoming_connection(
                graph,
                target_node_id=node.node_id,
                target_socket_id=socket_id,
            ) is None:
                missing.append(socket_id.upper())
        return tuple(missing)


def _extract_first_frame(image: Image.Image) -> Image.Image:
    return copy_first_frame_preserving_alpha(image)
