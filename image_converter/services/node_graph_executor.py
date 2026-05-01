from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps, ImageSequence

from image_converter.domain.models import ConversionOptions, ConversionStatus
from image_converter.domain.node_graph import (
    GraphConnection,
    GraphNode,
    NodeGraph,
    NodeGraphProject,
    NodeType,
    OutputMode,
    find_node,
    incoming_connection,
)
from image_converter.services.pipeline import RESAMPLING_LANCZOS

Logger = Callable[[str], None]


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


class NodeGraphExecutor:
    def validate(self, project: NodeGraphProject) -> tuple[str, ...]:
        warnings: list[str] = []
        graph = project.graph
        node_ids = {node.node_id for node in graph.nodes}

        for node in graph.nodes:
            if not node.node_id:
                warnings.append("Graph содержит node без id.")
            if node.node_type is NodeType.TEXTURE_INPUT:
                path = Path(str(node.properties.get("path", "")))
                if not str(path):
                    warnings.append(f"{node.title}: texture path не задан.")
                elif not path.exists():
                    warnings.append(f"{node.title}: texture не найден: {path}")
            if node.node_type is NodeType.OUTPUT_RGBA and node.properties.get("enabled", True):
                missing = self._missing_required_output_inputs(graph, node)
                if missing:
                    warnings.append(f"{node.title}: не подключены каналы {', '.join(missing)}.")

        for connection in graph.connections:
            if connection.source_node_id not in node_ids:
                warnings.append(f"Broken connection: source node {connection.source_node_id} не найден.")
            if connection.target_node_id not in node_ids:
                warnings.append(f"Broken connection: target node {connection.target_node_id} не найден.")

        if not self._enabled_output_nodes(graph):
            warnings.append("Нет включенных Output nodes для экспорта.")

        return tuple(dict.fromkeys(warnings))

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

        for output_node in self._enabled_output_nodes(project.graph):
            destination = self._build_output_path(output_node, output_root)
            try:
                result = self._export_output_node(project.graph, output_node, destination, options)
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

    def _export_output_node(
        self,
        graph: NodeGraph,
        output_node: GraphNode,
        destination: Path,
        options: ConversionOptions,
    ) -> GraphExportResult:
        mode = self._output_mode(output_node)
        required_inputs = ("r", "g", "b")
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

        target_size = self._determine_output_size(graph, output_node)
        if target_size is None:
            return GraphExportResult(
                output_node_id=output_node.node_id,
                output_name=output_node.title,
                destination=destination,
                status=ConversionStatus.FAILED,
                message="ОШИБКА: output должен зависеть хотя бы от одной texture-ноды",
            )

        channels = [
            self._evaluate_output_input(graph, output_node, socket_id, target_size)
            for socket_id in required_inputs
        ]
        if mode is OutputMode.RGBA:
            alpha = self._evaluate_optional_output_input(graph, output_node, "a", target_size)
            channels.append(alpha)

        merged = Image.merge("RGBA" if mode is OutputMode.RGBA else "RGB", tuple(channels))
        destination.parent.mkdir(parents=True, exist_ok=True)
        merged.save(
            destination,
            format="PNG",
            optimize=options.optimize,
            compress_level=options.compress_level,
        )
        return GraphExportResult(
            output_node_id=output_node.node_id,
            output_name=output_node.title,
            destination=destination,
            status=ConversionStatus.SUCCESS,
            message=f"exported: {destination.name}",
        )

    def _evaluate_output_input(
        self,
        graph: NodeGraph,
        output_node: GraphNode,
        socket_id: str,
        target_size: tuple[int, int],
    ) -> Image.Image:
        connection = incoming_connection(
            graph,
            target_node_id=output_node.node_id,
            target_socket_id=socket_id,
        )
        if connection is None:
            raise GraphExecutionError(f"Output channel {socket_id.upper()} is not connected.")
        return self._evaluate_channel_socket(graph, connection, target_size, set())

    def _evaluate_optional_output_input(
        self,
        graph: NodeGraph,
        output_node: GraphNode,
        socket_id: str,
        target_size: tuple[int, int],
    ) -> Image.Image:
        connection = incoming_connection(
            graph,
            target_node_id=output_node.node_id,
            target_socket_id=socket_id,
        )
        if connection is None:
            return Image.new("L", target_size, 255)
        return self._evaluate_channel_socket(graph, connection, target_size, set())

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
            channel = ImageOps.invert(
                self._evaluate_channel_socket(graph, input_connection, target_size, visiting)
            )
        else:
            raise GraphExecutionError(f"Unsupported source node: {source_node.node_type.value}")

        visiting.remove(key)
        if channel.size != target_size:
            channel = channel.resize(target_size, RESAMPLING_LANCZOS)
        return channel.copy()

    def _determine_output_size(
        self,
        graph: NodeGraph,
        output_node: GraphNode,
    ) -> tuple[int, int] | None:
        for socket_id in ("r", "g", "b", "a"):
            connection = incoming_connection(
                graph,
                target_node_id=output_node.node_id,
                target_socket_id=socket_id,
            )
            if connection is None:
                continue
            target_size = self._find_upstream_texture_size(graph, connection, set())
            if target_size is not None:
                return target_size
        return None

    def _find_upstream_texture_size(
        self,
        graph: NodeGraph,
        connection: GraphConnection,
        visiting: set[tuple[str, str]],
    ) -> tuple[int, int] | None:
        key = (connection.source_node_id, connection.source_socket_id)
        if key in visiting:
            return None
        visiting.add(key)

        source_node = find_node(graph, connection.source_node_id)
        if source_node is None:
            return None
        if source_node.node_type is NodeType.TEXTURE_INPUT:
            path = Path(str(source_node.properties.get("path", "")))
            if not path.exists():
                return None
            with Image.open(path) as image:
                return image.size
        if source_node.node_type is NodeType.INVERT_CHANNEL:
            input_connection = incoming_connection(
                graph,
                target_node_id=source_node.node_id,
                target_socket_id="in",
            )
            if input_connection is not None:
                return self._find_upstream_texture_size(graph, input_connection, visiting)
        return None

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
    def _constant_value(node: GraphNode) -> int:
        try:
            value = int(node.properties.get("value", 255))
        except (TypeError, ValueError):
            value = 255
        return max(0, min(value, 255))

    @staticmethod
    def _output_mode(node: GraphNode) -> OutputMode:
        try:
            return OutputMode(str(node.properties.get("mode", OutputMode.RGBA.value)))
        except ValueError:
            return OutputMode.RGBA

    @staticmethod
    def _build_output_path(node: GraphNode, output_root: Path) -> Path:
        raw_output_path = str(node.properties.get("output_path", "")).strip()
        if raw_output_path:
            destination = Path(raw_output_path)
            if not destination.is_absolute():
                destination = output_root / destination
            if destination.suffix.lower() != ".png":
                destination = destination.with_suffix(".png")
            return destination

        raw_filename = str(node.properties.get("filename", "")).strip() or f"{node.title}.png"
        destination = output_root / raw_filename
        if destination.suffix.lower() != ".png":
            destination = destination.with_suffix(".png")
        return destination

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
    if getattr(image, "is_animated", False) or getattr(image, "n_frames", 1) > 1:
        return next(ImageSequence.Iterator(image)).copy()
    return image.copy()
