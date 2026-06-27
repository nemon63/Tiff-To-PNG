from __future__ import annotations

from collections.abc import Iterable
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path

from PIL import Image
from PyQt6.QtCore import (
    QFileSystemWatcher,
    QObject,
    QPoint,
    QPointF,
    QRectF,
    QSize,
    Qt,
    QThread,
    QTimer,
    QUrl,
    pyqtSignal,
)
from PyQt6.QtGui import (
    QColor,
    QFont,
    QKeySequence,
    QPainter,
    QPainterPath,
    QPainterPathStroker,
    QPen,
    QPixmap,
    QShortcut,
    QUndoStack,
)
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGraphicsEllipseItem,
    QGraphicsItem,
    QGraphicsPathItem,
    QGraphicsPixmapItem,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsSimpleTextItem,
    QGraphicsView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QSlider,
    QSpinBox,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from image_converter.domain.models import (
    ChannelPackLayout,
    ConversionOptions,
    ConversionStatus,
    QueueItem,
    TextureMapType,
)
from image_converter.domain.node_graph import (
    GraphConnection,
    GraphNode,
    NodeGraphProject,
    NodeType,
    OutputMode,
    OutputProfile,
    SocketDirection,
    TextureDataRole,
    TextureNodeColorSpace,
    create_graph_node,
    incoming_connection,
    make_connection_id,
    make_node_id,
    node_has_enable_flag,
    node_has_resettable_parameters,
    node_type_label,
    reset_node_parameters,
    socket_definitions,
)
from image_converter.ui.graph_commands import (
    AddNodesCommand,
    DeleteItemsCommand,
    InsertNodeInConnectionCommand,
    MoveNodesCommand,
    RemoveConnectionsCommand,
    ReplaceInputConnectionCommand,
    SetDisplayFlagCommand,
    SetNodeStateCommand,
    clone_graph_connection,
    clone_graph_node,
)
from image_converter.services.node_graph_executor import (
    GraphExecutionError,
    GraphExportSummary,
    NodeGraphExecutor,
    NodeGraphPreviewCache,
)
from image_converter.services.node_graph_project import NodeGraphProjectRepository
from image_converter.services.image_loading import copy_first_frame_preserving_alpha
from image_converter.services.map_types import detect_texture_map_type
from image_converter.services.packing import PACK_LAYOUTS, PackSourceCandidate

NODE_WIDTH = 190
TEXTURE_NODE_WIDTH = 260
TITLE_HEIGHT = 28
ROW_HEIGHT = 22
PORT_RADIUS = 6
FLAG_SIZE = 14
FLAG_TOP = 7
FLAG_GAP = 6
TEXTURE_BODY_HEIGHT = 108
TEXTURE_THUMBNAIL_SIZE = 72
TEXTURE_THUMBNAIL_X = 10
TEXTURE_THUMBNAIL_Y = TITLE_HEIGHT + 14
TEXTURE_META_X = 94
TEXTURE_PORT_CENTER_Y = TITLE_HEIGHT + 34
TEXTURE_PORT_SPACING = 20
TEXTURE_PORT_LABEL_X_PAD = 34
GRAPH_LAYOUT_X_SPACING = 300
GRAPH_LAYOUT_Y_SPACING = 36
GRAPH_LAYOUT_GRID = 20

OUTPUT_PROFILE_FILENAMES = {
    OutputProfile.GENERIC_RGBA: "packed_rgba.png",
    OutputProfile.UNITY_URP: "unity_urp_mask.png",
    OutputProfile.UNITY_HDRP: "unity_hdrp_mask.png",
    OutputProfile.UNREAL_ORM: "unreal_orm.png",
    OutputProfile.METAHUMAN_REPACK: "metahuman_repack.png",
}
OUTPUT_PROFILE_MODES = {
    OutputProfile.GENERIC_RGBA: OutputMode.RGBA,
    OutputProfile.UNITY_URP: OutputMode.RGBA,
    OutputProfile.UNITY_HDRP: OutputMode.RGBA,
    OutputProfile.UNREAL_ORM: OutputMode.RGB,
    OutputProfile.METAHUMAN_REPACK: OutputMode.RGBA,
}
OUTPUT_PROFILE_PACK_LAYOUTS = {
    OutputProfile.UNITY_URP: ChannelPackLayout.UNITY_URP,
    OutputProfile.UNITY_HDRP: ChannelPackLayout.UNITY_HDRP,
    OutputProfile.UNREAL_ORM: ChannelPackLayout.ORM,
}
OUTPUT_PROFILE_LABELS = {
    OutputProfile.GENERIC_RGBA: "Generic RGBA",
    OutputProfile.UNITY_URP: "Unity URP",
    OutputProfile.UNITY_HDRP: "Unity HDRP",
    OutputProfile.UNREAL_ORM: "Unreal ORM",
    OutputProfile.METAHUMAN_REPACK: "MetaHuman Repack",
}
DEFAULT_PROFILE_FILL = {
    TextureMapType.AO: 255,
    TextureMapType.ROUGHNESS: 255,
    TextureMapType.SMOOTHNESS: 0,
    TextureMapType.METALLIC: 0,
    TextureMapType.OPACITY: 255,
}
METAHUMAN_REPACK_RULES = (
    ("r", (PackSourceCandidate(TextureMapType.AO),), 255),
    ("g", (PackSourceCandidate(TextureMapType.ROUGHNESS),), 255),
    ("b", (PackSourceCandidate(TextureMapType.METALLIC),), 0),
    ("a", (PackSourceCandidate(TextureMapType.OPACITY),), 255),
)
PACKED_DETAIL_MASK = "detail_mask"
PACKED_SOURCE_CHANNELS = {
    "unity_hdrp_mask": {
        TextureMapType.METALLIC: ("r", False),
        TextureMapType.AO: ("g", False),
        PACKED_DETAIL_MASK: ("b", False),
        TextureMapType.SMOOTHNESS: ("a", False),
        TextureMapType.ROUGHNESS: ("a", True),
    },
    "unity_urp_mask": {
        TextureMapType.METALLIC: ("r", False),
        TextureMapType.SMOOTHNESS: ("a", False),
        TextureMapType.ROUGHNESS: ("a", True),
    },
    "unreal_orm": {
        TextureMapType.AO: ("r", False),
        TextureMapType.ROUGHNESS: ("g", False),
        TextureMapType.SMOOTHNESS: ("g", True),
        TextureMapType.METALLIC: ("b", False),
    },
    "unreal_mra": {
        TextureMapType.METALLIC: ("r", False),
        TextureMapType.ROUGHNESS: ("g", False),
        TextureMapType.SMOOTHNESS: ("g", True),
        TextureMapType.AO: ("b", False),
    },
    "unreal_rma": {
        TextureMapType.ROUGHNESS: ("r", False),
        TextureMapType.SMOOTHNESS: ("r", True),
        TextureMapType.METALLIC: ("g", False),
        TextureMapType.AO: ("b", False),
    },
}
GRAPH_EXPORT_FILE_FILTER = (
    "PNG (*.png);;"
    "JPEG (*.jpg *.jpeg);;"
    "WebP (*.webp);;"
    "TIFF (*.tif *.tiff);;"
    "BMP (*.bmp);;"
    "TGA (*.tga);;"
    "All files (*)"
)


@dataclass(slots=True)
class OutputProfilePlan:
    profile: OutputProfile
    mode: OutputMode
    properties: dict
    utility_nodes: list[GraphNode]
    utility_connections: list[GraphConnection]
    output_connections: list[GraphConnection]
    summary_lines: tuple[str, ...]


class GraphPreviewWorker(QObject):
    finished = pyqtSignal(int, object, str, str, str)
    failed = pyqtSignal(int, str, str)
    completed = pyqtSignal()

    def __init__(
        self,
        generation: int,
        graph_project: NodeGraphProject,
        node: GraphNode,
        *,
        mode_label: str = "",
        max_side: int = 1024,
        preview_cache: NodeGraphPreviewCache | None = None,
    ):
        super().__init__()
        self._generation = generation
        self._project = graph_project
        self._node = node
        self._mode_label = mode_label
        self._max_side = max_side
        self._preview_cache = preview_cache

    def run(self) -> None:
        try:
            executor = NodeGraphExecutor()
            preview_cache = self._preview_cache or NodeGraphPreviewCache(max_side=self._max_side)
            preview_cache.max_side = self._max_side
            image, meta = executor.render_display_node(
                self._project.graph,
                self._node,
                preview_cache=preview_cache,
            )
            if self._mode_label:
                filename = str(self._node.properties.get("filename", "")).strip()
                if self._node.properties.get("output_path"):
                    filename = Path(str(self._node.properties.get("output_path"))).name
                suffix = f" · {filename}" if filename else ""
                meta = f"Output preview · {image.width}x{image.height} · {self._mode_label}{suffix}"
            self.finished.emit(self._generation, image, self._node.title, meta, self._node.node_id)
        except (GraphExecutionError, OSError, ValueError) as exc:
            self.failed.emit(self._generation, self._node.title, str(exc))
        finally:
            self.completed.emit()


class ConnectionItem(QGraphicsPathItem):
    def __init__(
        self,
        connection: GraphConnection,
        source_port: "PortItem",
        target_port: "PortItem",
    ):
        super().__init__()
        self.connection = connection
        self.source_port = source_port
        self.target_port = target_port
        self.setZValue(-10)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)
        self.setPen(QPen(QColor("#5BA7FF"), 2.0))
        self.update_path()

    def update_path(self) -> None:
        start = self.source_port.scene_center()
        end = self.target_port.scene_center()
        dx = max(60.0, abs(end.x() - start.x()) * 0.5)
        path = QPainterPath(start)
        path.cubicTo(
            QPointF(start.x() + dx, start.y()),
            QPointF(end.x() - dx, end.y()),
            end,
        )
        self.setPath(path)

    def shape(self) -> QPainterPath:
        stroker = QPainterPathStroker()
        stroker.setWidth(10.0)
        return stroker.createStroke(self.path())

    def mousePressEvent(self, event) -> None:
        if (
            event.button() == Qt.MouseButton.LeftButton
            and event.modifiers() & Qt.KeyboardModifier.AltModifier
        ):
            scene = self.scene()
            if isinstance(scene, GraphScene):
                scene.connection_delete_requested.emit(self.connection)
                event.accept()
                return
        super().mousePressEvent(event)


class PortItem(QGraphicsEllipseItem):
    def __init__(self, node_item: "GraphNodeItem", socket_id: str, label: str, direction: SocketDirection):
        super().__init__(-PORT_RADIUS, -PORT_RADIUS, PORT_RADIUS * 2, PORT_RADIUS * 2, node_item)
        self.node_item = node_item
        self.socket_id = socket_id
        self.direction = direction
        self.setBrush(QColor("#6EA8FE") if direction is SocketDirection.OUTPUT else QColor("#D9964A"))
        self.setPen(QPen(QColor("#111820"), 1.0))
        self.setAcceptHoverEvents(True)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setToolTip(f"{node_item.node.title}.{label}")

    def scene_center(self) -> QPointF:
        return self.mapToScene(self.rect().center())

    def mousePressEvent(self, event) -> None:
        scene = self.scene()
        if isinstance(scene, GraphScene):
            scene.start_wire_drag(self, event.scenePos())
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        scene = self.scene()
        if isinstance(scene, GraphScene):
            scene.update_wire_drag(event.scenePos())
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        scene = self.scene()
        if isinstance(scene, GraphScene):
            scene.finish_wire_drag(event.scenePos())
            event.accept()
            return
        super().mouseReleaseEvent(event)


class GraphNodeItem(QGraphicsRectItem):
    def __init__(self, node: GraphNode):
        super().__init__()
        self.node = node
        self.node_width = TEXTURE_NODE_WIDTH if node.node_type is NodeType.TEXTURE_INPUT else NODE_WIDTH
        self.title_item: QGraphicsSimpleTextItem | None = None
        self.subtitle_item: QGraphicsSimpleTextItem | None = None
        self.path_label_item: QGraphicsSimpleTextItem | None = None
        self.metadata_line_item: QGraphicsSimpleTextItem | None = None
        self.size_line_item: QGraphicsSimpleTextItem | None = None
        self.thumbnail_bg_item: QGraphicsRectItem | None = None
        self.thumbnail_item: QGraphicsPixmapItem | None = None
        self.port_items: dict[str, PortItem] = {}
        self.port_label_items: dict[str, QGraphicsSimpleTextItem] = {}
        self.display_flag_item: QGraphicsRectItem | None = None
        self.display_flag_label: QGraphicsSimpleTextItem | None = None
        self.enable_flag_item: QGraphicsRectItem | None = None
        self.enable_flag_label: QGraphicsSimpleTextItem | None = None
        self.render_flag_item: QGraphicsRectItem | None = None
        self.render_flag_label: QGraphicsSimpleTextItem | None = None
        self.reset_button_item: QGraphicsRectItem | None = None
        self.reset_button_label: QGraphicsSimpleTextItem | None = None
        self._drag_start_position: tuple[float, float] | None = None
        sockets = socket_definitions(node.node_type)
        input_count = sum(1 for socket in sockets if socket.direction is SocketDirection.INPUT)
        output_count = sum(1 for socket in sockets if socket.direction is SocketDirection.OUTPUT)
        row_count = max(input_count, output_count, 2)
        body_height = row_count * ROW_HEIGHT + 16
        if node.node_type is NodeType.TEXTURE_INPUT:
            body_height = TEXTURE_BODY_HEIGHT
        self.setRect(0, 0, self.node_width, TITLE_HEIGHT + body_height)
        self.setPos(*node.position)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges, True)
        self.setPen(QPen(QColor("#3A434D"), 1.0))
        self.setBrush(QColor("#232A32"))
        self._build_contents()

    def _build_contents(self) -> None:
        title_bg = QGraphicsRectItem(0, 0, self.node_width, TITLE_HEIGHT, self)
        title_bg.setPen(QPen(Qt.PenStyle.NoPen))
        title_bg.setBrush(QColor("#2F3741"))

        self.title_item = QGraphicsSimpleTextItem(self._elide_text(self.node.title, self._title_max_chars()), self)
        self.title_item.setBrush(QColor("#E4EAF1"))
        self.title_item.setPos(10, 6)
        self.title_item.setToolTip(self.node.title)

        self._build_flag_items()

        y_offset = TITLE_HEIGHT + 30
        if self.node.node_type is NodeType.TEXTURE_INPUT:
            thumbnail_rect = QRectF(
                TEXTURE_THUMBNAIL_X,
                TEXTURE_THUMBNAIL_Y,
                TEXTURE_THUMBNAIL_SIZE,
                TEXTURE_THUMBNAIL_SIZE,
            )
            self.thumbnail_bg_item = QGraphicsRectItem(thumbnail_rect, self)
            self.thumbnail_bg_item.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
            self.thumbnail_bg_item.setPen(QPen(QColor("#303946"), 1.0))
            self.thumbnail_bg_item.setBrush(QColor("#111820"))
            self.thumbnail_item = QGraphicsPixmapItem(self)
            self.thumbnail_item.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
            self.thumbnail_item.setVisible(False)
            thumbnail = self._thumbnail_pixmap()
            if thumbnail is not None:
                self.thumbnail_item.setPixmap(thumbnail)
                self.thumbnail_item.setVisible(True)
                self._position_thumbnail_item()
            path_text = Path(str(self.node.properties.get("path", ""))).name or "no texture"
            self.path_label_item = QGraphicsSimpleTextItem(self._elide_text(path_text, 16), self)
            self.path_label_item.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
            self.path_label_item.setFont(self._pixel_font(9))
            self.path_label_item.setBrush(QColor("#D6DEE8"))
            self.path_label_item.setPos(TEXTURE_META_X, TITLE_HEIGHT + 15)
            self.path_label_item.setToolTip(str(self.node.properties.get("path", "")) or path_text)
            self.metadata_line_item = QGraphicsSimpleTextItem(self._texture_metadata_line(), self)
            self.metadata_line_item.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
            self.metadata_line_item.setFont(self._pixel_font(8))
            self.metadata_line_item.setBrush(QColor("#8EA0B2"))
            self.metadata_line_item.setPos(TEXTURE_META_X, TITLE_HEIGHT + 35)
            size_line_text = self._texture_size_line()
            self.size_line_item = QGraphicsSimpleTextItem(size_line_text, self)
            self.size_line_item.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
            self.size_line_item.setFont(self._pixel_font(8))
            self.size_line_item.setBrush(QColor("#667484"))
            self.size_line_item.setPos(TEXTURE_META_X, TITLE_HEIGHT + 55)
            self.size_line_item.setVisible(bool(size_line_text))
            y_offset = TEXTURE_PORT_CENTER_Y - 10
        else:
            self.subtitle_item = QGraphicsSimpleTextItem(self.node.node_type.value, self)
            self.subtitle_item.setBrush(QColor("#8E9AA8"))
            self.subtitle_item.setPos(10, TITLE_HEIGHT + 6)

        inputs = [socket for socket in socket_definitions(self.node.node_type) if socket.direction is SocketDirection.INPUT]
        outputs = [socket for socket in socket_definitions(self.node.node_type) if socket.direction is SocketDirection.OUTPUT]
        for index, socket in enumerate(inputs):
            y = y_offset + index * ROW_HEIGHT
            port = PortItem(self, socket.socket_id, socket.name, socket.direction)
            port.setPos(0, y + 10)
            self.port_items[socket.socket_id] = port
            label = QGraphicsSimpleTextItem(socket.name, self)
            label.setBrush(QColor("#C9D2DD"))
            label.setPos(14, y)
            self.port_label_items[socket.socket_id] = label

        for index, socket in enumerate(outputs):
            spacing = TEXTURE_PORT_SPACING if self.node.node_type is NodeType.TEXTURE_INPUT else ROW_HEIGHT
            y = y_offset + index * spacing
            port = PortItem(self, socket.socket_id, socket.name, socket.direction)
            port.setPos(self.node_width, y + 10)
            self.port_items[socket.socket_id] = port
            label = QGraphicsSimpleTextItem(socket.name, self)
            label.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
            label.setBrush(self._socket_label_color(socket.socket_id))
            label_x = (
                self.node_width - TEXTURE_PORT_LABEL_X_PAD
                if self.node.node_type is NodeType.TEXTURE_INPUT
                else self.node_width - 28
            )
            label.setPos(label_x, y)
            self.port_label_items[socket.socket_id] = label

    def _thumbnail_pixmap(self) -> QPixmap | None:
        path = Path(str(self.node.properties.get("path", "")))
        if not path.exists():
            return None
        try:
            with Image.open(path) as image:
                thumbnail_image = copy_first_frame_preserving_alpha(image)
                thumbnail_image.thumbnail((TEXTURE_THUMBNAIL_SIZE, TEXTURE_THUMBNAIL_SIZE))
                rgba = thumbnail_image.convert("RGBA")
                data = rgba.tobytes("raw", "RGBA")
        except Exception:
            return None
        from PyQt6.QtGui import QImage

        qimage = QImage(
            data,
            rgba.width,
            rgba.height,
            rgba.width * 4,
            QImage.Format.Format_RGBA8888,
        ).copy()
        return QPixmap.fromImage(qimage)

    def _title_max_chars(self) -> int:
        return 28 if self.node.node_type is NodeType.TEXTURE_INPUT else 22

    def _texture_metadata_line(self) -> str:
        path = Path(str(self.node.properties.get("path", "")))
        map_type = detect_texture_map_type(path)
        map_label = map_type.label if map_type is not TextureMapType.UNKNOWN else "Texture"
        data_role = str(self.node.properties.get("data_role", TextureDataRole.DATA.value))
        role_label = {
            TextureDataRole.COLOR.value: "Color",
            TextureDataRole.DATA.value: "Data",
            TextureDataRole.NORMAL.value: "Normal",
            TextureDataRole.MASK.value: "Mask",
        }.get(data_role, "Data")
        return self._elide_text(f"{map_label} · {role_label}", 14)

    def _texture_size_line(self) -> str:
        path = Path(str(self.node.properties.get("path", "")))
        if not path.exists():
            return "missing"
        try:
            with Image.open(path) as image:
                width, height = image.size
        except Exception:
            return "unreadable"
        return self._elide_text(f"{width}x{height}", 14)

    def _socket_label_color(self, socket_id: str) -> QColor:
        if self.node.node_type is not NodeType.TEXTURE_INPUT:
            return QColor("#C9D2DD")
        primary_socket = self._primary_texture_socket()
        if primary_socket is None or socket_id == primary_socket:
            return QColor("#E2E9F2")
        return QColor("#7B8794")

    def _primary_texture_socket(self) -> str | None:
        path = Path(str(self.node.properties.get("path", "")))
        map_type = detect_texture_map_type(path)
        if map_type in {
            TextureMapType.AO,
            TextureMapType.ROUGHNESS,
            TextureMapType.SMOOTHNESS,
            TextureMapType.METALLIC,
            TextureMapType.OPACITY,
            TextureMapType.HEIGHT,
        }:
            return "r"
        return None

    @staticmethod
    def _pixel_font(pixel_size: int) -> QFont:
        font = QFont()
        font.setPixelSize(pixel_size)
        return font

    @staticmethod
    def _elide_text(text: str, max_chars: int) -> str:
        if len(text) <= max_chars:
            return text
        if max_chars <= 3:
            return text[:max_chars]
        keep = max_chars - 3
        head = max(1, keep // 2)
        tail = max(1, keep - head)
        return f"{text[:head]}...{text[-tail:]}"

    def _build_flag_items(self) -> None:
        if node_has_resettable_parameters(self.node.node_type):
            reset_rect = self._reset_button_rect()
            self.reset_button_item = QGraphicsRectItem(reset_rect, self)
            self.reset_button_item.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
            self.reset_button_label = QGraphicsSimpleTextItem("0", self)
            self.reset_button_label.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
            self.reset_button_label.setPos(reset_rect.x() + 4, reset_rect.y() - 1)

        if node_has_enable_flag(self.node.node_type):
            enable_rect = self._enable_flag_rect()
            self.enable_flag_item = QGraphicsRectItem(enable_rect, self)
            self.enable_flag_item.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
            self.enable_flag_label = QGraphicsSimpleTextItem("E", self)
            self.enable_flag_label.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
            self.enable_flag_label.setPos(enable_rect.x() + 4, enable_rect.y() - 1)

        display_rect = self._display_flag_rect()
        self.display_flag_item = QGraphicsRectItem(display_rect, self)
        self.display_flag_item.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self.display_flag_label = QGraphicsSimpleTextItem("D", self)
        self.display_flag_label.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self.display_flag_label.setPos(display_rect.x() + 4, display_rect.y() - 1)

        if self.node.node_type is NodeType.OUTPUT_RGBA:
            render_rect = self._render_flag_rect()
            self.render_flag_item = QGraphicsRectItem(render_rect, self)
            self.render_flag_item.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
            self.render_flag_label = QGraphicsSimpleTextItem("R", self)
            self.render_flag_label.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
            self.render_flag_label.setPos(render_rect.x() + 4, render_rect.y() - 1)

        self.refresh_flags()

    def refresh_flags(self) -> None:
        self._sync_flag_item(
            self.reset_button_item,
            self.reset_button_label,
            "Reset parameters",
            False,
            QColor("#3A434D"),
        )

        operation_enabled = bool(self.node.properties.get("enabled", True))
        self._sync_flag_item(
            self.enable_flag_item,
            self.enable_flag_label,
            "Enable node",
            operation_enabled,
            QColor("#C4932E"),
        )

        display_enabled = bool(self.node.properties.get("display", False))
        self._sync_flag_item(
            self.display_flag_item,
            self.display_flag_label,
            "Display flag",
            display_enabled,
            QColor("#2F82FF"),
        )

        if self.node.node_type is NodeType.OUTPUT_RGBA:
            render_enabled = bool(self.node.properties.get("enabled", True))
            self._sync_flag_item(
                self.render_flag_item,
                self.render_flag_label,
                "Render flag",
                render_enabled,
                QColor("#34A853"),
            )

    def refresh_content(self) -> None:
        if self.title_item is not None:
            self.title_item.setText(self._elide_text(self.node.title, self._title_max_chars()))
            self.title_item.setToolTip(self.node.title)

        if self.node.node_type is NodeType.TEXTURE_INPUT:
            if self.path_label_item is not None:
                path_text = Path(str(self.node.properties.get("path", ""))).name or "no texture"
                self.path_label_item.setText(self._elide_text(path_text, 16))
                self.path_label_item.setToolTip(str(self.node.properties.get("path", "")) or path_text)
            if self.metadata_line_item is not None:
                self.metadata_line_item.setText(self._texture_metadata_line())
            if self.size_line_item is not None:
                size_line_text = self._texture_size_line()
                self.size_line_item.setText(size_line_text)
                self.size_line_item.setVisible(bool(size_line_text))
            if self.thumbnail_item is not None:
                thumbnail = self._thumbnail_pixmap()
                if thumbnail is None:
                    self.thumbnail_item.setVisible(False)
                    self.thumbnail_item.setPixmap(QPixmap())
                else:
                    self.thumbnail_item.setPixmap(thumbnail)
                    self.thumbnail_item.setVisible(True)
                    self._position_thumbnail_item()
            for socket_id, label_item in self.port_label_items.items():
                label_item.setBrush(self._socket_label_color(socket_id))
        elif self.subtitle_item is not None:
            self.subtitle_item.setText(self.node.node_type.value)

        self.refresh_flags()
        self.update()

    def _position_thumbnail_item(self) -> None:
        if self.thumbnail_item is None or self.thumbnail_item.pixmap().isNull():
            return
        pixmap = self.thumbnail_item.pixmap()
        self.thumbnail_item.setPos(
            TEXTURE_THUMBNAIL_X + (TEXTURE_THUMBNAIL_SIZE - pixmap.width()) / 2,
            TEXTURE_THUMBNAIL_Y + (TEXTURE_THUMBNAIL_SIZE - pixmap.height()) / 2,
        )

    def _sync_flag_item(
        self,
        flag_item: QGraphicsRectItem | None,
        flag_label: QGraphicsSimpleTextItem | None,
        tooltip: str,
        enabled: bool,
        active_color: QColor,
    ) -> None:
        if flag_item is None or flag_label is None:
            return
        flag_item.setBrush(active_color if enabled else QColor("#3A434D"))
        flag_item.setPen(QPen(QColor("#89B7FF") if enabled else QColor("#111820"), 1.0))
        flag_item.setToolTip(tooltip)
        flag_label.setBrush(QColor("#FFFFFF") if enabled else QColor("#9AA6B2"))
        flag_label.setToolTip(tooltip)

    def _display_flag_rect(self) -> QRectF:
        if self.node.node_type is NodeType.OUTPUT_RGBA:
            return self._flag_rect_from_right(1)
        return self._flag_rect_from_right(0)

    def _enable_flag_rect(self) -> QRectF:
        return self._flag_rect_from_right(1)

    def _reset_button_rect(self) -> QRectF:
        if self.node.node_type is NodeType.OUTPUT_RGBA:
            return self._flag_rect_from_right(2)
        if node_has_enable_flag(self.node.node_type):
            return self._flag_rect_from_right(2)
        return self._flag_rect_from_right(1)

    def _flag_rect_from_right(self, index: int) -> QRectF:
        x = self.node_width - FLAG_SIZE - 8 - index * (FLAG_SIZE + FLAG_GAP)
        return QRectF(x, FLAG_TOP, FLAG_SIZE, FLAG_SIZE)

    def _render_flag_rect(self) -> QRectF:
        return QRectF(self.node_width - FLAG_SIZE - 8, FLAG_TOP, FLAG_SIZE, FLAG_SIZE)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            scene = self.scene()
            if isinstance(scene, GraphScene):
                if (
                    node_has_resettable_parameters(self.node.node_type)
                    and self._reset_button_rect().contains(event.pos())
                ):
                    scene.node_reset_clicked.emit(self.node)
                    event.accept()
                    return
                if (
                    node_has_enable_flag(self.node.node_type)
                    and self._enable_flag_rect().contains(event.pos())
                ):
                    scene.node_enable_flag_clicked.emit(self.node)
                    event.accept()
                    return
                if self._display_flag_rect().contains(event.pos()):
                    scene.node_display_flag_clicked.emit(self.node)
                    event.accept()
                    return
                if (
                    self.node.node_type is NodeType.OUTPUT_RGBA
                    and self._render_flag_rect().contains(event.pos())
                ):
                    scene.node_render_flag_clicked.emit(self.node)
                    event.accept()
                    return
            self._drag_start_position = self.node.position
            if isinstance(scene, GraphScene):
                scene.begin_node_move(self.node.node_id)
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        super().mouseReleaseEvent(event)
        if event.button() != Qt.MouseButton.LeftButton:
            return
        if self._drag_start_position is None:
            return
        next_position = (float(self.pos().x()), float(self.pos().y()))
        previous_position = self._drag_start_position
        self._drag_start_position = None
        scene = self.scene()
        if isinstance(scene, GraphScene):
            scene.finish_node_move()
            return
        if next_position == previous_position:
            return

    def mouseDoubleClickEvent(self, event) -> None:
        scene = self.scene()
        if isinstance(scene, GraphScene):
            self.setSelected(True)
            scene.node_double_clicked.emit(self.node)
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def itemChange(self, change: QGraphicsItem.GraphicsItemChange, value):
        result = super().itemChange(change, value)
        if change is QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged:
            scene = self.scene()
            if isinstance(scene, GraphScene):
                scene.update_connections_for_node(self.node.node_id)
        return result

    def paint(self, painter: QPainter, option, widget=None) -> None:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = self.rect()
        color = QColor("#2B3540") if self.isSelected() else QColor("#232A32")
        painter.setBrush(color)
        painter.setPen(QPen(QColor("#5BA7FF") if self.isSelected() else QColor("#3A434D"), 1.2))
        painter.drawRoundedRect(rect, 5, 5)


class GraphScene(QGraphicsScene):
    connection_delete_requested = pyqtSignal(object)
    connection_insert_node_requested = pyqtSignal(object, object, object)
    connection_requested = pyqtSignal(object, object)
    connections_delete_requested = pyqtSignal(object)
    graph_changed = pyqtSignal()
    node_display_flag_clicked = pyqtSignal(object)
    node_double_clicked = pyqtSignal(object)
    node_enable_flag_clicked = pyqtSignal(object)
    node_moved = pyqtSignal(object, object, object)
    nodes_moved = pyqtSignal(object, object)
    node_render_flag_clicked = pyqtSignal(object)
    node_reset_clicked = pyqtSignal(object)
    node_selection_changed = pyqtSignal(object)
    status_message = pyqtSignal(str)
    wire_node_requested = pyqtSignal(object, object)

    def __init__(self, project: NodeGraphProject, parent: QWidget | None = None):
        super().__init__(parent)
        self.project = project
        self.node_items: dict[str, GraphNodeItem] = {}
        self.connection_items: dict[str, ConnectionItem] = {}
        self.drag_port: PortItem | None = None
        self.drag_rewire_connection: GraphConnection | None = None
        self.drag_start_scene_pos: QPointF | None = None
        self.drag_wire: QGraphicsPathItem | None = None
        self.move_start_positions: dict[str, tuple[float, float]] = {}
        self.selectionChanged.connect(self._emit_selection)
        self.setSceneRect(-3000, -3000, 6000, 6000)

    def rebuild(self) -> None:
        self.clear()
        self.node_items = {}
        self.connection_items = {}
        self.drag_port = None
        self.drag_rewire_connection = None
        self.drag_start_scene_pos = None
        self.drag_wire = None
        self.move_start_positions = {}
        for node in self.project.graph.nodes:
            item = GraphNodeItem(node)
            self.addItem(item)
            self.node_items[node.node_id] = item
        for connection in self.project.graph.connections:
            self._add_connection_item(connection)

    def add_node(self, node: GraphNode) -> None:
        self.project.graph.nodes.append(node)
        item = GraphNodeItem(node)
        self.addItem(item)
        self.node_items[node.node_id] = item
        self.clearSelection()
        item.setSelected(True)
        self.graph_changed.emit()

    def delete_selected(self) -> None:
        selected = list(self.selectedItems())
        if not selected:
            return
        node_ids = [
            item.node.node_id
            for item in selected
            if isinstance(item, GraphNodeItem)
        ]
        connection_ids = [
            item.connection.connection_id
            for item in selected
            if isinstance(item, ConnectionItem)
        ]
        self.connections_delete_requested.emit((node_ids, connection_ids))

    def start_wire_drag(self, port: PortItem, scene_pos: QPointF) -> None:
        self.drag_rewire_connection = None
        self.drag_port = port
        self.drag_start_scene_pos = QPointF(scene_pos)
        if port.direction is SocketDirection.INPUT:
            existing_connection = incoming_connection(
                self.project.graph,
                target_node_id=port.node_item.node.node_id,
                target_socket_id=port.socket_id,
            )
            if existing_connection is not None:
                source_item = self.node_items.get(existing_connection.source_node_id)
                source_port = (
                    source_item.port_items.get(existing_connection.source_socket_id)
                    if source_item is not None
                    else None
                )
                if source_port is not None:
                    self.drag_port = source_port
                    self.drag_rewire_connection = existing_connection
        self.drag_wire = QGraphicsPathItem()
        self.drag_wire.setZValue(-5)
        pen = QPen(QColor("#9DC7FF"), 2.0)
        pen.setStyle(Qt.PenStyle.DashLine)
        self.drag_wire.setPen(pen)
        self.addItem(self.drag_wire)
        self.update_wire_drag(scene_pos)

    def update_wire_drag(self, scene_pos: QPointF) -> None:
        if self.drag_port is None or self.drag_wire is None:
            return
        start = self.drag_port.scene_center()
        end = scene_pos
        if self.drag_port.direction is SocketDirection.INPUT:
            start, end = end, start
        dx = max(60.0, abs(end.x() - start.x()) * 0.5)
        path = QPainterPath(start)
        path.cubicTo(
            QPointF(start.x() + dx, start.y()),
            QPointF(end.x() - dx, end.y()),
            end,
        )
        self.drag_wire.setPath(path)

    def finish_wire_drag(self, scene_pos: QPointF) -> None:
        started_port = self.drag_port
        rewire_connection = self.drag_rewire_connection
        start_scene_pos = self.drag_start_scene_pos
        source_port, target_port = self._resolve_drag_ports(scene_pos)
        if self.drag_wire is not None:
            self.removeItem(self.drag_wire)
        self.drag_wire = None
        self.drag_port = None
        self.drag_rewire_connection = None
        self.drag_start_scene_pos = None

        if source_port is None or target_port is None:
            moved_far_enough = False
            if start_scene_pos is not None:
                moved_far_enough = (
                    abs(scene_pos.x() - start_scene_pos.x())
                    + abs(scene_pos.y() - start_scene_pos.y())
                    > 8.0
                )
            if started_port is not None and moved_far_enough:
                self.wire_node_requested.emit(started_port, scene_pos)
            else:
                self.status_message.emit("Connection canceled.")
            return
        connection = GraphConnection(
            connection_id=make_connection_id(),
            source_node_id=source_port.node_item.node.node_id,
            source_socket_id=source_port.socket_id,
            target_node_id=target_port.node_item.node.node_id,
            target_socket_id=target_port.socket_id,
        )
        if (
            rewire_connection is not None
            and rewire_connection.source_node_id == connection.source_node_id
            and rewire_connection.source_socket_id == connection.source_socket_id
            and rewire_connection.target_node_id == connection.target_node_id
            and rewire_connection.target_socket_id == connection.target_socket_id
        ):
            self.status_message.emit("Connection unchanged.")
            return
        self.connection_requested.emit(connection, rewire_connection)

    def cut_connections_by_path(self, cut_path: QPainterPath) -> int:
        if not self.connection_items:
            return 0
        stroker = QPainterPathStroker()
        stroker.setWidth(10.0)
        cut_area = stroker.createStroke(cut_path)
        cut_ids = [
            connection_id
            for connection_id, item in self.connection_items.items()
            if cut_area.intersects(item.path())
        ]
        if not cut_ids:
            return 0
        self.connections_delete_requested.emit(([], cut_ids))
        return len(cut_ids)

    def _resolve_drag_ports(self, scene_pos: QPointF) -> tuple[PortItem | None, PortItem | None]:
        if self.drag_port is None:
            return None, None
        target_port = self._port_at(scene_pos)
        if target_port is None or target_port is self.drag_port:
            return None, None
        if self.drag_port.direction is target_port.direction:
            return None, None
        if self.drag_port.direction is SocketDirection.OUTPUT:
            return self.drag_port, target_port
        return target_port, self.drag_port

    def _port_at(self, scene_pos: QPointF) -> PortItem | None:
        for item in self.items(scene_pos):
            if isinstance(item, PortItem):
                return item
        return None

    def update_connections_for_node(self, node_id: str) -> None:
        for connection_item in self.connection_items.values():
            connection = connection_item.connection
            if connection.source_node_id == node_id or connection.target_node_id == node_id:
                connection_item.update_path()

    def begin_node_move(self, active_node_id: str) -> None:
        selected_ids = set(self.selected_node_ids())
        if active_node_id not in selected_ids:
            selected_ids = {active_node_id}
        self.move_start_positions = {
            node_id: item.node.position
            for node_id, item in self.node_items.items()
            if node_id in selected_ids
        }

    def finish_node_move(self) -> None:
        if not self.move_start_positions:
            return
        before_positions = dict(self.move_start_positions)
        self.move_start_positions = {}
        after_positions = {}
        for node_id in before_positions:
            item = self.node_items.get(node_id)
            if item is not None:
                after_positions[node_id] = (float(item.pos().x()), float(item.pos().y()))
        changed_after = {
            node_id: position
            for node_id, position in after_positions.items()
            if before_positions.get(node_id) != position
        }
        if changed_after:
            changed_before = {
                node_id: before_positions[node_id]
                for node_id in changed_after
            }
            self.nodes_moved.emit(changed_before, changed_after)

    def selected_node(self) -> GraphNode | None:
        for item in self.selectedItems():
            if isinstance(item, GraphNodeItem):
                return item.node
        return None

    def selected_node_ids(self) -> list[str]:
        return [
            item.node.node_id
            for item in self.selectedItems()
            if isinstance(item, GraphNodeItem)
        ]

    def selected_connection_ids(self) -> list[str]:
        return [
            item.connection.connection_id
            for item in self.selectedItems()
            if isinstance(item, ConnectionItem)
        ]

    def select_node_ids(self, node_ids: Iterable[str]) -> None:
        target_ids = set(node_ids)
        self.clearSelection()
        for node_id in target_ids:
            item = self.node_items.get(node_id)
            if item is not None:
                item.setSelected(True)

    def _add_connection_item(self, connection: GraphConnection) -> None:
        source_node_item = self.node_items.get(connection.source_node_id)
        target_node_item = self.node_items.get(connection.target_node_id)
        if source_node_item is None or target_node_item is None:
            return
        source_port = source_node_item.port_items.get(connection.source_socket_id)
        target_port = target_node_item.port_items.get(connection.target_socket_id)
        if source_port is None or target_port is None:
            return
        item = ConnectionItem(connection, source_port, target_port)
        self.addItem(item)
        self.connection_items[connection.connection_id] = item

    def _emit_selection(self) -> None:
        self.node_selection_changed.emit(self.selected_node())


class GraphView(QGraphicsView):
    connection_delete_requested = pyqtSignal(object)
    connection_insert_node_requested = pyqtSignal(object, object, object)
    texture_dropped = pyqtSignal(str, object)
    node_add_requested = pyqtSignal(object, object, object)
    layout_requested = pyqtSignal()

    def __init__(self, scene: GraphScene, parent: QWidget | None = None):
        super().__init__(scene, parent)
        self._pan_start: QPoint | None = None
        self._pan_scroll: tuple[int, int] = (0, 0)
        self._knife_active = False
        self._knife_start: QPointF | None = None
        self.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        self.setDragMode(QGraphicsView.DragMode.RubberBandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.FullViewportUpdate)
        self.setBackgroundBrush(QColor("#15191E"))
        self.setAcceptDrops(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def wheelEvent(self, event) -> None:
        delta = event.angleDelta().y() or event.pixelDelta().y()
        if delta == 0:
            event.accept()
            return
        factor = 1.12 if delta > 0 else 1 / 1.12
        current_zoom = self.transform().m11()
        next_zoom = current_zoom * factor
        if 0.15 <= next_zoom <= 4.0:
            self.scale(factor, factor)
        event.accept()

    def mousePressEvent(self, event) -> None:
        if self._knife_active and event.button() == Qt.MouseButton.LeftButton:
            self._knife_start = self.mapToScene(event.position().toPoint())
            event.accept()
            return
        if event.button() == Qt.MouseButton.MiddleButton:
            self._pan_start = event.position().toPoint()
            self._pan_scroll = (
                self.horizontalScrollBar().value(),
                self.verticalScrollBar().value(),
            )
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._knife_start is not None:
            event.accept()
            return
        if self._pan_start is not None:
            delta = event.position().toPoint() - self._pan_start
            self.horizontalScrollBar().setValue(self._pan_scroll[0] - delta.x())
            self.verticalScrollBar().setValue(self._pan_scroll[1] - delta.y())
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if self._knife_start is not None and event.button() == Qt.MouseButton.LeftButton:
            end = self.mapToScene(event.position().toPoint())
            path = QPainterPath(self._knife_start)
            path.lineTo(end)
            scene = self.scene()
            if isinstance(scene, GraphScene):
                scene.cut_connections_by_path(path)
            self._knife_start = None
            self._knife_active = False
            self.unsetCursor()
            event.accept()
            return
        if event.button() == Qt.MouseButton.MiddleButton and self._pan_start is not None:
            self._pan_start = None
            self.unsetCursor()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Delete:
            scene = self.scene()
            if isinstance(scene, GraphScene):
                scene.delete_selected()
                event.accept()
                return
        if event.key() == Qt.Key.Key_Y:
            self._knife_active = True
            self.setCursor(Qt.CursorShape.CrossCursor)
            scene = self.scene()
            if isinstance(scene, GraphScene):
                scene.status_message.emit("Y-drag: cut wires.")
            event.accept()
            return
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Y and self._knife_start is None:
            self._knife_active = False
            self.unsetCursor()
            event.accept()
            return
        super().keyReleaseEvent(event)

    def dragEnterEvent(self, event) -> None:
        if self._extract_texture_path(event.mimeData()):
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event) -> None:
        if self._extract_texture_path(event.mimeData()):
            event.acceptProposedAction()
            return
        super().dragMoveEvent(event)

    def dropEvent(self, event) -> None:
        texture_path = self._extract_texture_path(event.mimeData())
        if texture_path:
            self.texture_dropped.emit(texture_path, self.mapToScene(event.position().toPoint()))
            event.acceptProposedAction()
            return
        super().dropEvent(event)

    def contextMenuEvent(self, event) -> None:
        scene_position = self.mapToScene(event.pos())
        connection_item = self._connection_item_at(scene_position)
        if connection_item is not None:
            self._open_connection_menu(connection_item.connection, scene_position, event.globalPos())
            event.accept()
            return

        self.open_node_menu(scene_position, event.globalPos())
        event.accept()

    def open_node_menu(
        self,
        scene_position: QPointF,
        global_position: QPoint | None = None,
        wire_port: PortItem | None = None,
    ) -> None:
        menu = QMenu(self)

        if wire_port is None or wire_port.direction is SocketDirection.INPUT:
            input_menu = menu.addMenu("Input")
            self._add_node_menu_action(input_menu, "Texture", NodeType.TEXTURE_INPUT, scene_position, wire_port)
            self._add_node_menu_action(input_menu, "Constant", NodeType.CONSTANT_CHANNEL, scene_position, wire_port)

        channel_menu = menu.addMenu("Channel")
        self._add_node_menu_action(channel_menu, "Invert", NodeType.INVERT_CHANNEL, scene_position, wire_port)
        self._add_node_menu_action(channel_menu, "Levels", NodeType.LEVELS_CHANNEL, scene_position, wire_port)
        self._add_node_menu_action(channel_menu, "Clamp", NodeType.CLAMP_CHANNEL, scene_position, wire_port)
        self._add_node_menu_action(channel_menu, "Threshold", NodeType.THRESHOLD_CHANNEL, scene_position, wire_port)
        self._add_node_menu_action(channel_menu, "Luminance", NodeType.LUMINANCE, scene_position, wire_port)

        math_menu = menu.addMenu("Math")
        self._add_node_menu_action(math_menu, "Blend", NodeType.BLEND_CHANNEL, scene_position, wire_port)

        if wire_port is None or wire_port.direction is SocketDirection.OUTPUT:
            utility_menu = menu.addMenu("Utility")
            self._add_node_menu_action(utility_menu, "View", NodeType.VIEW, scene_position, wire_port)

            output_menu = menu.addMenu("Output")
            self._add_node_menu_action(output_menu, "Output RGBA", NodeType.OUTPUT_RGBA, scene_position, wire_port)

        menu.addSeparator()
        fit_action = menu.addAction("Fit View")
        fit_action.triggered.connect(self.fit_graph)
        layout_action = menu.addAction("Auto Layout")
        layout_action.triggered.connect(self.layout_requested.emit)
        if global_position is None:
            global_position = self.viewport().mapToGlobal(self.mapFromScene(scene_position))
        menu.exec(global_position)

    def _open_connection_menu(
        self,
        connection: GraphConnection,
        scene_position: QPointF,
        global_position: QPoint,
    ) -> None:
        menu = QMenu(self)
        insert_menu = menu.addMenu("Insert Node")
        for label, node_type in (
            ("Invert", NodeType.INVERT_CHANNEL),
            ("Levels", NodeType.LEVELS_CHANNEL),
            ("Clamp", NodeType.CLAMP_CHANNEL),
            ("Threshold", NodeType.THRESHOLD_CHANNEL),
        ):
            action = insert_menu.addAction(label)
            action.triggered.connect(
                lambda _checked=False, current_type=node_type: self.connection_insert_node_requested.emit(
                    connection,
                    current_type,
                    scene_position,
                )
            )

        delete_action = menu.addAction("Delete Connection")
        delete_action.triggered.connect(lambda: self.connection_delete_requested.emit(connection))
        menu.exec(global_position)

    def _add_node_menu_action(
        self,
        menu: QMenu,
        label: str,
        node_type: NodeType,
        scene_position: QPointF,
        context: object | None = None,
    ) -> None:
        action = menu.addAction(label)
        action.triggered.connect(
            lambda _checked=False, current_type=node_type: self.node_add_requested.emit(
                current_type,
                scene_position,
                context,
            )
        )

    def _connection_item_at(self, scene_position: QPointF) -> ConnectionItem | None:
        scene = self.scene()
        if scene is None:
            return None
        for item in scene.items(scene_position):
            if isinstance(item, ConnectionItem):
                return item
        return None

    def fit_graph(self) -> None:
        items_rect = self.scene().itemsBoundingRect()
        if items_rect.isNull():
            self.resetTransform()
            self.centerOn(0, 0)
            return
        self.fitInView(items_rect.adjusted(-120, -120, 120, 120), Qt.AspectRatioMode.KeepAspectRatio)

    @staticmethod
    def _extract_texture_path(mime_data) -> str:
        if mime_data.hasFormat("application/x-texture-path"):
            return bytes(mime_data.data("application/x-texture-path")).decode("utf-8")
        if mime_data.hasUrls():
            for url in mime_data.urls():
                if isinstance(url, QUrl) and url.isLocalFile():
                    return url.toLocalFile()
        if mime_data.hasText():
            text = mime_data.text().strip()
            if text:
                return text
        return ""


class NodePropertiesPanel(QWidget):
    node_changed = pyqtSignal(object, object, object, bool)
    output_profile_apply_requested = pyqtSignal(object)
    output_inputs_clear_requested = pyqtSignal(object)
    node_reset_requested = pyqtSignal(object)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._node: GraphNode | None = None
        self._suppress = False
        self._build_ui()
        self.set_node(None)

    def _make_byte_spin(self) -> QSpinBox:
        spin = QSpinBox()
        spin.setRange(0, 255)
        spin.setKeyboardTracking(False)
        spin.valueChanged.connect(self._apply_changes)
        return spin

    def _make_int_spin(self, minimum: int, maximum: int, *, suffix: str = "") -> QSpinBox:
        spin = QSpinBox()
        spin.setRange(minimum, maximum)
        spin.setKeyboardTracking(False)
        if suffix:
            spin.setSuffix(suffix)
        spin.valueChanged.connect(self._apply_changes)
        return spin

    def _make_slider_spin_pair(
        self,
        minimum: int,
        maximum: int,
        *,
        initial: int = 0,
        suffix: str = "",
    ) -> tuple[QSlider, QSpinBox]:
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(minimum, maximum)
        slider.setValue(initial)
        slider.valueChanged.connect(self._apply_changes)

        spin = self._make_int_spin(minimum, maximum, suffix=suffix)
        spin.setValue(initial)
        slider.valueChanged.connect(spin.setValue)
        spin.valueChanged.connect(slider.setValue)
        return slider, spin

    def _make_byte_slider_pair(self, initial: int = 0) -> tuple[QSlider, QSpinBox]:
        return self._make_slider_spin_pair(0, 255, initial=initial)

    def _build_ui(self) -> None:
        self.setObjectName("SectionPanel")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        title = QLabel("Node Properties")
        title.setObjectName("PanelTitle")
        layout.addWidget(title)

        self.reset_parameters_button = QPushButton("Reset Params")
        self.reset_parameters_button.clicked.connect(self._reset_parameters)
        layout.addWidget(self.reset_parameters_button)

        self.empty_label = QLabel("Select a node to edit its properties.")
        self.empty_label.setObjectName("SummaryText")
        self.empty_label.setWordWrap(True)
        layout.addWidget(self.empty_label)

        self.form_host = QWidget()
        self.form = QFormLayout(self.form_host)
        self.form.setContentsMargins(0, 0, 0, 0)

        self.title_edit = QLineEdit()
        self.title_edit.editingFinished.connect(self._apply_changes)
        self.form.addRow("Title", self.title_edit)

        self.node_enabled_checkbox = QCheckBox("Enabled")
        self.node_enabled_checkbox.toggled.connect(self._apply_changes)
        self.form.addRow("Node", self.node_enabled_checkbox)

        self.path_edit = QLineEdit()
        self.path_edit.editingFinished.connect(self._apply_changes)
        self.path_button = QPushButton("...")
        self.path_button.clicked.connect(self._browse_texture)
        path_row = QHBoxLayout()
        path_row.setContentsMargins(0, 0, 0, 0)
        path_row.addWidget(self.path_edit, 1)
        path_row.addWidget(self.path_button)
        path_host = QWidget()
        path_host.setLayout(path_row)
        self.path_host = path_host
        self.form.addRow("Texture", path_host)

        self.texture_color_space_combo = QComboBox()
        for label, value in (
            ("Auto", TextureNodeColorSpace.AUTO.value),
            ("sRGB", TextureNodeColorSpace.SRGB.value),
            ("Linear", TextureNodeColorSpace.LINEAR.value),
        ):
            self.texture_color_space_combo.addItem(label, value)
        self.texture_color_space_combo.currentIndexChanged.connect(self._apply_changes)
        self.form.addRow("Color Space", self.texture_color_space_combo)

        self.texture_data_role_combo = QComboBox()
        for label, value in (
            ("Data", TextureDataRole.DATA.value),
            ("Color", TextureDataRole.COLOR.value),
            ("Normal", TextureDataRole.NORMAL.value),
            ("Mask", TextureDataRole.MASK.value),
        ):
            self.texture_data_role_combo.addItem(label, value)
        self.texture_data_role_combo.currentIndexChanged.connect(self._apply_changes)
        self.form.addRow("Data Role", self.texture_data_role_combo)

        self.value_slider, self.value_spin = self._make_byte_slider_pair(initial=255)
        self.value_host = self._byte_row_widget(self.value_slider, self.value_spin)
        self.form.addRow("Value", self.value_host)

        self.level_black_slider, self.level_black_spin = self._make_byte_slider_pair()
        self.level_black_host = self._byte_row_widget(self.level_black_slider, self.level_black_spin)
        self.form.addRow("Black", self.level_black_host)
        self.level_white_slider, self.level_white_spin = self._make_byte_slider_pair()
        self.level_white_host = self._byte_row_widget(self.level_white_slider, self.level_white_spin)
        self.form.addRow("White", self.level_white_host)
        self.level_gamma_spin = QDoubleSpinBox()
        self.level_gamma_spin.setRange(0.05, 8.0)
        self.level_gamma_spin.setSingleStep(0.05)
        self.level_gamma_spin.setDecimals(2)
        self.level_gamma_spin.setKeyboardTracking(False)
        self.level_gamma_spin.valueChanged.connect(self._apply_changes)
        self.form.addRow("Gamma", self.level_gamma_spin)
        self.level_out_min_slider, self.level_out_min_spin = self._make_byte_slider_pair()
        self.level_out_min_host = self._byte_row_widget(self.level_out_min_slider, self.level_out_min_spin)
        self.form.addRow("Output Min", self.level_out_min_host)
        self.level_out_max_slider, self.level_out_max_spin = self._make_byte_slider_pair()
        self.level_out_max_host = self._byte_row_widget(self.level_out_max_slider, self.level_out_max_spin)
        self.form.addRow("Output Max", self.level_out_max_host)

        self.clamp_min_slider, self.clamp_min_spin = self._make_byte_slider_pair()
        self.clamp_min_host = self._byte_row_widget(self.clamp_min_slider, self.clamp_min_spin)
        self.form.addRow("Min", self.clamp_min_host)
        self.clamp_max_slider, self.clamp_max_spin = self._make_byte_slider_pair()
        self.clamp_max_host = self._byte_row_widget(self.clamp_max_slider, self.clamp_max_spin)
        self.form.addRow("Max", self.clamp_max_host)

        self.threshold_slider, self.threshold_spin = self._make_byte_slider_pair()
        self.threshold_host = self._byte_row_widget(self.threshold_slider, self.threshold_spin)
        self.form.addRow("Threshold", self.threshold_host)

        self.blend_mode_combo = QComboBox()
        for label, value in (
            ("Multiply", "multiply"),
            ("Add", "add"),
            ("Subtract", "subtract"),
            ("Max", "max"),
            ("Min", "min"),
            ("Average", "average"),
        ):
            self.blend_mode_combo.addItem(label, value)
        self.blend_mode_combo.currentIndexChanged.connect(self._apply_changes)
        self.form.addRow("Blend Mode", self.blend_mode_combo)
        self.blend_opacity_slider, self.blend_opacity_spin = self._make_slider_spin_pair(
            0,
            100,
            initial=100,
            suffix="%",
        )
        self.blend_opacity_host = self._byte_row_widget(self.blend_opacity_slider, self.blend_opacity_spin)
        self.form.addRow("Opacity", self.blend_opacity_host)

        self.filename_edit = QLineEdit()
        self.filename_edit.setPlaceholderText("packed_rgba.png")
        self.filename_edit.setToolTip("Output filename inside Export Folder. Add an extension to choose format.")
        self.filename_edit.editingFinished.connect(self._on_output_name_finished)
        self.form.addRow("Output Name", self.filename_edit)

        self.output_path_edit = QLineEdit()
        self.output_path_edit.setPlaceholderText(
            "Optional override file path. Relative paths resolve inside Export Folder."
        )
        self.output_path_edit.setToolTip(
            "Optional override output file path. Relative paths resolve inside Export Folder."
        )
        self.output_path_edit.editingFinished.connect(self._on_output_path_finished)
        self.output_path_button = QPushButton("...")
        self.output_path_button.clicked.connect(self._browse_output_path)
        output_path_row = QHBoxLayout()
        output_path_row.setContentsMargins(0, 0, 0, 0)
        output_path_row.addWidget(self.output_path_edit, 1)
        output_path_row.addWidget(self.output_path_button)
        self.output_path_host = QWidget()
        self.output_path_host.setLayout(output_path_row)
        self.form.addRow("Output File", self.output_path_host)

        self.output_profile_combo = QComboBox()
        for label, value in (
            ("Generic RGBA", OutputProfile.GENERIC_RGBA.value),
            ("Unity URP", OutputProfile.UNITY_URP.value),
            ("Unity HDRP", OutputProfile.UNITY_HDRP.value),
            ("Unreal ORM", OutputProfile.UNREAL_ORM.value),
            ("MetaHuman Repack", OutputProfile.METAHUMAN_REPACK.value),
        ):
            self.output_profile_combo.addItem(label, value)
        self.output_profile_combo.currentIndexChanged.connect(self._apply_changes)
        self.form.addRow("Profile", self.output_profile_combo)

        self.apply_profile_button = QPushButton("Auto Connect Profile")
        self.apply_profile_button.clicked.connect(self._apply_output_profile)
        self.clear_output_inputs_button = QPushButton("Clear Inputs")
        self.clear_output_inputs_button.clicked.connect(self._clear_output_inputs)
        profile_actions = QHBoxLayout()
        profile_actions.setContentsMargins(0, 0, 0, 0)
        profile_actions.addWidget(self.apply_profile_button, 1)
        profile_actions.addWidget(self.clear_output_inputs_button)
        self.profile_actions_host = QWidget()
        self.profile_actions_host.setLayout(profile_actions)
        self.form.addRow("Auto Connect", self.profile_actions_host)

        self.profile_summary_label = QLabel("")
        self.profile_summary_label.setObjectName("SummaryText")
        self.profile_summary_label.setWordWrap(True)
        self.form.addRow("", self.profile_summary_label)

        self.mode_combo = QComboBox()
        self.mode_combo.addItem("RGB", OutputMode.RGB.value)
        self.mode_combo.addItem("RGBA", OutputMode.RGBA.value)
        self.mode_combo.currentIndexChanged.connect(self._apply_changes)
        self.form.addRow("Mode", self.mode_combo)

        self.enabled_checkbox = QCheckBox("Enabled")
        self.enabled_checkbox.toggled.connect(self._apply_changes)
        self.form.addRow("Export", self.enabled_checkbox)

        layout.addWidget(self.form_host)
        layout.addStretch(1)

    def set_node(self, node: GraphNode | None) -> None:
        self._node = node
        self._suppress = True
        try:
            self.empty_label.setVisible(node is None)
            self.form_host.setVisible(node is not None)
            self.reset_parameters_button.setVisible(
                node is not None and node_has_resettable_parameters(node.node_type)
            )
            if node is None:
                return
            self.title_edit.setText(node.title)
            self.node_enabled_checkbox.setChecked(bool(node.properties.get("enabled", True)))
            self.path_edit.setText(str(node.properties.get("path", "")))
            self._set_combo_value(
                self.texture_color_space_combo,
                str(node.properties.get("color_space", TextureNodeColorSpace.AUTO.value)),
            )
            self._set_combo_value(
                self.texture_data_role_combo,
                str(node.properties.get("data_role", TextureDataRole.DATA.value)),
            )
            self.value_spin.setValue(self._coerce_int(node.properties.get("value"), 255))
            self.level_black_spin.setValue(self._coerce_int(node.properties.get("black"), 0))
            self.level_white_spin.setValue(self._coerce_int(node.properties.get("white"), 255))
            self.level_gamma_spin.setValue(self._coerce_float(node.properties.get("gamma"), 1.0))
            self.level_out_min_spin.setValue(self._coerce_int(node.properties.get("out_min"), 0))
            self.level_out_max_spin.setValue(self._coerce_int(node.properties.get("out_max"), 255))
            self.clamp_min_spin.setValue(self._coerce_int(node.properties.get("min"), 0))
            self.clamp_max_spin.setValue(self._coerce_int(node.properties.get("max"), 255))
            self.threshold_spin.setValue(self._coerce_int(node.properties.get("threshold"), 128))
            blend_mode = str(node.properties.get("mode", "multiply"))
            self.blend_mode_combo.setCurrentIndex(0)
            for index in range(self.blend_mode_combo.count()):
                if self.blend_mode_combo.itemData(index) == blend_mode:
                    self.blend_mode_combo.setCurrentIndex(index)
                    break
            self.blend_opacity_spin.setValue(self._coerce_int(node.properties.get("opacity"), 100))
            self.filename_edit.setText(str(node.properties.get("filename", "packed.png")))
            self.output_path_edit.setText(str(node.properties.get("output_path", "")))
            self._update_output_format_hint()
            self._set_combo_value(
                self.output_profile_combo,
                str(node.properties.get("profile", OutputProfile.GENERIC_RGBA.value)),
            )
            self.enabled_checkbox.setChecked(bool(node.properties.get("enabled", True)))
            mode_value = str(node.properties.get("mode", OutputMode.RGBA.value))
            self._set_combo_value(self.mode_combo, mode_value, fallback_index=1)
            self._sync_visibility(node.node_type)
        finally:
            self._suppress = False

    def _sync_visibility(self, node_type: NodeType) -> None:
        self._set_row_visible(self.node_enabled_checkbox, node_has_enable_flag(node_type))
        self._set_row_visible(self.path_host, node_type is NodeType.TEXTURE_INPUT)
        self._set_row_visible(self.texture_color_space_combo, node_type is NodeType.TEXTURE_INPUT)
        self._set_row_visible(self.texture_data_role_combo, node_type is NodeType.TEXTURE_INPUT)
        self._set_row_visible(self.value_host, node_type is NodeType.CONSTANT_CHANNEL)
        self._set_row_visible(self.level_black_host, node_type is NodeType.LEVELS_CHANNEL)
        self._set_row_visible(self.level_white_host, node_type is NodeType.LEVELS_CHANNEL)
        self._set_row_visible(self.level_gamma_spin, node_type is NodeType.LEVELS_CHANNEL)
        self._set_row_visible(self.level_out_min_host, node_type is NodeType.LEVELS_CHANNEL)
        self._set_row_visible(self.level_out_max_host, node_type is NodeType.LEVELS_CHANNEL)
        self._set_row_visible(self.clamp_min_host, node_type is NodeType.CLAMP_CHANNEL)
        self._set_row_visible(self.clamp_max_host, node_type is NodeType.CLAMP_CHANNEL)
        self._set_row_visible(self.threshold_host, node_type is NodeType.THRESHOLD_CHANNEL)
        self._set_row_visible(self.blend_mode_combo, node_type is NodeType.BLEND_CHANNEL)
        self._set_row_visible(self.blend_opacity_host, node_type is NodeType.BLEND_CHANNEL)
        self._set_row_visible(self.filename_edit, node_type is NodeType.OUTPUT_RGBA)
        self._set_row_visible(self.output_path_host, node_type is NodeType.OUTPUT_RGBA)
        self._set_row_visible(self.output_profile_combo, node_type is NodeType.OUTPUT_RGBA)
        self._set_row_visible(self.profile_actions_host, node_type is NodeType.OUTPUT_RGBA)
        self._set_row_visible(self.profile_summary_label, node_type is NodeType.OUTPUT_RGBA)
        self._set_row_visible(self.mode_combo, node_type is NodeType.OUTPUT_RGBA)
        self._set_row_visible(self.enabled_checkbox, node_type is NodeType.OUTPUT_RGBA)

    def _set_row_visible(self, widget: QWidget, visible: bool) -> None:
        widget.setVisible(visible)
        label = self.form.labelForField(widget)
        if label is not None:
            label.setVisible(visible)

    @staticmethod
    def _byte_row_widget(slider: QSlider, spin: QSpinBox) -> QWidget:
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        row.addWidget(slider, 1)
        row.addWidget(spin)
        host = QWidget()
        host.setLayout(row)
        return host

    def _browse_texture(self) -> None:
        if self._node is None:
            return
        path, _ = QFileDialog.getOpenFileName(self, "Select texture")
        if path:
            self.path_edit.setText(path)
            self._apply_changes()

    def _browse_output_path(self) -> None:
        if self._node is None:
            return
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Select output file",
            self.output_path_edit.text().strip() or self.filename_edit.text().strip(),
            GRAPH_EXPORT_FILE_FILTER,
        )
        if path:
            selected_path = Path(path)
            self.output_path_edit.setText(str(selected_path))
            self._sync_output_name_from_output_path(selected_path)
            self._apply_changes()

    def _on_output_name_finished(self) -> None:
        if self._node is None or self._node.node_type is not NodeType.OUTPUT_RGBA:
            self._apply_changes()
            return
        self._sync_output_path_from_output_name(self.filename_edit.text().strip())
        self._apply_changes()

    def _on_output_path_finished(self) -> None:
        if self._node is None or self._node.node_type is not NodeType.OUTPUT_RGBA:
            self._apply_changes()
            return
        raw_path = self.output_path_edit.text().strip()
        if raw_path:
            self._sync_output_name_from_output_path(Path(raw_path))
        self._update_output_format_hint()
        self._apply_changes()

    def _sync_output_name_from_output_path(self, path: Path) -> None:
        if self._node is None or self._node.node_type is not NodeType.OUTPUT_RGBA:
            return
        filename = path.name.strip()
        if filename and self.filename_edit.text().strip() != filename:
            self.filename_edit.setText(filename)
        self._update_output_format_hint()

    def _sync_output_path_from_output_name(self, filename: str) -> None:
        if self._node is None or self._node.node_type is not NodeType.OUTPUT_RGBA:
            return
        filename = filename.strip() or "packed.png"
        current_output = self.output_path_edit.text().strip()
        if not current_output:
            self.output_path_edit.clear()
            self._update_output_format_hint()
            return

        output_path = Path(current_output)
        updated_path = output_path.with_name(filename)
        if str(updated_path) != current_output:
            self.output_path_edit.setText(str(updated_path))
        self._update_output_format_hint()

    def _update_output_format_hint(self) -> None:
        if self._node is None or self._node.node_type is not NodeType.OUTPUT_RGBA:
            self.filename_edit.setPlaceholderText("packed_rgba.png")
            self.filename_edit.setToolTip("Output filename inside Export Folder. Add an extension to choose format.")
            self.output_path_edit.setToolTip(
                "Optional explicit output file path. Leave empty to export into Export Folder using Output Name."
            )
            return

        filename = self.filename_edit.text().strip()
        output_path = self.output_path_edit.text().strip()
        suffix = Path(filename).suffix or Path(output_path).suffix or ".png"
        format_hint = suffix.lower()
        self.filename_edit.setPlaceholderText(f"packed_rgba{format_hint}")
        self.filename_edit.setToolTip(
            f"Output filename inside Export Folder. Current format: {format_hint}. "
            "Change the extension to choose another format."
        )
        self.output_path_edit.setToolTip(
            f"Optional explicit output file path. Relative paths resolve inside Export Folder. "
            "Leave empty to use Output Name. "
            f"Current format: {format_hint}."
        )

    def _reset_parameters(self) -> None:
        if self._node is None or not node_has_resettable_parameters(self._node.node_type):
            return
        self.node_reset_requested.emit(self._node)

    def _apply_output_profile(self) -> None:
        if self._node is None or self._node.node_type is not NodeType.OUTPUT_RGBA:
            return
        self.output_profile_apply_requested.emit(self._node)

    def _clear_output_inputs(self) -> None:
        if self._node is None or self._node.node_type is not NodeType.OUTPUT_RGBA:
            return
        self.output_inputs_clear_requested.emit(self._node)

    def set_profile_summary(self, text: str) -> None:
        self.profile_summary_label.setText(text)

    def _apply_changes(self, *_args: object) -> None:
        if self._suppress or self._node is None:
            return
        previous_title = self._node.title
        previous_path = str(self._node.properties.get("path", ""))
        next_title = self.title_edit.text().strip() or self._node.title
        next_properties = dict(self._node.properties)
        if node_has_enable_flag(self._node.node_type):
            next_properties["enabled"] = self.node_enabled_checkbox.isChecked()
        if self._node.node_type is NodeType.TEXTURE_INPUT:
            next_properties["path"] = self.path_edit.text().strip()
            next_properties["color_space"] = str(
                self.texture_color_space_combo.currentData()
                or TextureNodeColorSpace.AUTO.value
            )
            next_properties["data_role"] = str(
                self.texture_data_role_combo.currentData()
                or TextureDataRole.DATA.value
            )
        elif self._node.node_type is NodeType.CONSTANT_CHANNEL:
            next_properties["value"] = self.value_spin.value()
        elif self._node.node_type is NodeType.LEVELS_CHANNEL:
            next_properties["black"] = self.level_black_spin.value()
            next_properties["white"] = self.level_white_spin.value()
            next_properties["gamma"] = self.level_gamma_spin.value()
            next_properties["out_min"] = self.level_out_min_spin.value()
            next_properties["out_max"] = self.level_out_max_spin.value()
        elif self._node.node_type is NodeType.CLAMP_CHANNEL:
            next_properties["min"] = self.clamp_min_spin.value()
            next_properties["max"] = self.clamp_max_spin.value()
        elif self._node.node_type is NodeType.THRESHOLD_CHANNEL:
            next_properties["threshold"] = self.threshold_spin.value()
        elif self._node.node_type is NodeType.BLEND_CHANNEL:
            next_properties["mode"] = str(self.blend_mode_combo.currentData() or "multiply")
            next_properties["opacity"] = self.blend_opacity_spin.value()
        elif self._node.node_type is NodeType.OUTPUT_RGBA:
            filename_text = self.filename_edit.text().strip() or "packed.png"
            output_path_text = self.output_path_edit.text().strip()
            next_properties["filename"] = filename_text
            next_properties["output_path"] = output_path_text
            next_properties["profile"] = str(
                self.output_profile_combo.currentData()
                or OutputProfile.GENERIC_RGBA.value
            )
            next_properties["mode"] = str(self.mode_combo.currentData() or OutputMode.RGBA.value)
            next_properties["enabled"] = self.enabled_checkbox.isChecked()
        if next_title == self._node.title and next_properties == self._node.properties:
            return
        needs_rebuild = previous_title != next_title
        if self._node.node_type is NodeType.TEXTURE_INPUT:
            needs_rebuild = needs_rebuild or previous_path != str(next_properties.get("path", ""))
        self.node_changed.emit(self._node, next_title, next_properties, needs_rebuild)

    @staticmethod
    def _coerce_int(value: object, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _coerce_float(value: object, default: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _set_combo_value(combo: QComboBox, value: str, *, fallback_index: int = 0) -> None:
        combo.setCurrentIndex(fallback_index)
        for index in range(combo.count()):
            if combo.itemData(index) == value:
                combo.setCurrentIndex(index)
                return


class GraphWorkspace(QWidget):
    export_requested = pyqtSignal()
    preview_image_requested = pyqtSignal(object, str, str, str)
    status_message = pyqtSignal(str)
    watched_paths_changed = pyqtSignal(tuple)
    assets_changed = pyqtSignal(tuple)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.project = NodeGraphProject()
        self.project_dir: Path | None = None
        self._assets: list[QueueItem] = []
        self._clipboard_nodes: list[GraphNode] = []
        self._clipboard_connections: list[GraphConnection] = []
        self._last_preview_node_id: str | None = None
        self._last_preview_mode_label = ""
        self._preview_generation = 0
        self._preview_threads: list[QThread] = []
        self._preview_workers: list[GraphPreviewWorker] = []
        self._preview_inflight_generation = 0
        self._pending_preview_request: tuple[str, str] | None = None
        self._recent_project_dirs: list[Path] = []
        self._shortcuts = []
        self._repository = NodeGraphProjectRepository()
        self._executor = NodeGraphExecutor()
        self._preview_cache = NodeGraphPreviewCache(max_side=1024)
        self._autosave_timer = QTimer(self)
        self._autosave_timer.setInterval(30000)
        self._autosave_timer.setSingleShot(True)
        self._autosave_timer.timeout.connect(self.autosave_project)
        self._auto_watch_enabled = False
        self._asset_watch_timer = QTimer(self)
        self._asset_watch_timer.setInterval(350)
        self._asset_watch_timer.setSingleShot(True)
        self._asset_watch_timer.timeout.connect(self._emit_pending_asset_changes)
        self._asset_watcher = QFileSystemWatcher(self)
        self._asset_watcher.directoryChanged.connect(self._on_watched_directory_changed)
        self._asset_watcher.fileChanged.connect(self._on_watched_file_changed)
        self._watched_texture_keys: dict[str, str] = {}
        self._watched_file_keys: dict[str, str] = {}
        self._watched_directory_paths: dict[str, str] = {}
        self._watched_directory_keys: dict[str, set[str]] = {}
        self._pending_asset_change_keys: set[str] = set()
        self.undo_stack = QUndoStack(self)
        self.undo_stack.cleanChanged.connect(self._on_undo_stack_changed)
        self.undo_stack.indexChanged.connect(self._on_undo_stack_changed)
        self._scene = GraphScene(self.project, self)
        self._scene.connection_delete_requested.connect(self._on_connection_delete_requested)
        self._scene.connection_insert_node_requested.connect(self._on_connection_insert_node_requested)
        self._scene.connection_requested.connect(self._on_connection_requested)
        self._scene.connections_delete_requested.connect(self._on_delete_items_requested)
        self._scene.graph_changed.connect(self._on_graph_changed)
        self._scene.node_display_flag_clicked.connect(self._on_display_flag_clicked)
        self._scene.node_double_clicked.connect(self._on_node_double_clicked)
        self._scene.node_enable_flag_clicked.connect(self._on_enable_flag_clicked)
        self._scene.node_moved.connect(self._on_node_moved)
        self._scene.nodes_moved.connect(self._on_nodes_moved)
        self._scene.node_render_flag_clicked.connect(self._on_render_flag_clicked)
        self._scene.node_reset_clicked.connect(self._on_node_reset_clicked)
        self._scene.node_selection_changed.connect(self._on_node_selected)
        self._scene.status_message.connect(self.status_message.emit)
        self._scene.wire_node_requested.connect(self._on_wire_node_requested)
        self._build_ui()
        self._scene.rebuild()
        self._refresh_validation()
        self.undo_stack.setClean()
        self._update_project_label()

    def _build_ui(self) -> None:
        self.setObjectName("SectionPanel")
        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(10, 10, 10, 10)
        root_layout.setSpacing(8)

        toolbar = QVBoxLayout()
        toolbar.setSpacing(6)
        project_row = QHBoxLayout()
        project_row.setSpacing(6)
        self.project_label = QLabel("Untitled Graph")
        self.project_label.setObjectName("PanelTitle")
        project_row.addWidget(self.project_label)

        menu_hint = QLabel("Right-click graph to add nodes")
        menu_hint.setObjectName("SummaryText")
        project_row.addWidget(menu_hint)
        project_row.addStretch(1)

        self.new_button = QPushButton("New")
        self.new_button.clicked.connect(self.new_project)
        project_row.addWidget(self.new_button)
        self.load_button = QPushButton("Load")
        self.load_button.clicked.connect(self.load_project_dialog)
        project_row.addWidget(self.load_button)
        self.save_button = QPushButton("Save")
        self.save_button.clicked.connect(self.save_project_dialog)
        project_row.addWidget(self.save_button)
        self.recent_button = QToolButton()
        self.recent_button.setText("Recent")
        self.recent_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        project_row.addWidget(self.recent_button)
        self.export_button = QPushButton("Export Graph")
        self.export_button.setObjectName("PrimaryButton")
        self.export_button.clicked.connect(self.export_requested.emit)
        project_row.addWidget(self.export_button)
        toolbar.addLayout(project_row)

        tools_row = QHBoxLayout()
        tools_row.setSpacing(6)
        self.delete_button = QPushButton("Delete")
        self.delete_button.setObjectName("DangerButton")
        self.delete_button.clicked.connect(self._delete_selection)
        tools_row.addWidget(self.delete_button)
        self.fit_button = QPushButton("Fit")
        self.fit_button.clicked.connect(lambda: self.view.fit_graph())
        tools_row.addWidget(self.fit_button)
        self.layout_button = QPushButton("Layout")
        self.layout_button.clicked.connect(self.auto_layout_nodes)
        tools_row.addWidget(self.layout_button)
        tools_row.addWidget(QLabel("Preview"))
        self.preview_quality_combo = QComboBox()
        self.preview_quality_combo.setToolTip("Preview max side")
        for label, value in (("512", 512), ("1024", 1024), ("2048", 2048)):
            self.preview_quality_combo.addItem(label, value)
        self.preview_quality_combo.setCurrentIndex(1)
        self.preview_quality_combo.currentIndexChanged.connect(self._set_preview_quality)
        tools_row.addWidget(self.preview_quality_combo)
        self.remap_button = QPushButton("Remap Missing")
        self.remap_button.clicked.connect(self.remap_missing_texture_paths)
        tools_row.addWidget(self.remap_button)
        tools_row.addStretch(1)
        toolbar.addLayout(tools_row)
        root_layout.addLayout(toolbar)

        self.view = GraphView(self._scene)
        self.view.connection_delete_requested.connect(self._on_connection_delete_requested)
        self.view.connection_insert_node_requested.connect(self._on_connection_insert_node_requested)
        self.view.layout_requested.connect(self.auto_layout_nodes)
        self.view.texture_dropped.connect(self.add_texture_node_for_path)
        self.view.node_add_requested.connect(self.add_node_of_type)
        self.properties_panel = NodePropertiesPanel()
        self.properties_panel.node_changed.connect(self._on_node_properties_changed)
        self.properties_panel.output_profile_apply_requested.connect(self._apply_output_profile)
        self.properties_panel.output_inputs_clear_requested.connect(self._clear_output_inputs)
        self.properties_panel.node_reset_requested.connect(self._on_node_reset_clicked)
        self._install_shortcuts()

        self.result_table = QTableWidget()
        self.result_table.setColumnCount(4)
        self.result_table.setHorizontalHeaderLabels(("Output", "Status", "Destination", "Message"))
        self.result_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.result_table.verticalHeader().setVisible(False)
        self.result_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.result_table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.result_table.horizontalHeader().setStretchLastSection(True)
        self.result_table.setMinimumHeight(120)
        self.result_table.setMinimumWidth(0)

        self.validation_table = QTableWidget()
        self.validation_table.setColumnCount(3)
        self.validation_table.setHorizontalHeaderLabels(("Severity", "Node", "Message"))
        self.validation_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.validation_table.verticalHeader().setVisible(False)
        self.validation_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.validation_table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.validation_table.horizontalHeader().setStretchLastSection(True)
        self.validation_table.setMinimumHeight(120)
        self.validation_table.itemSelectionChanged.connect(self._select_validation_issue_node)

        root_layout.addWidget(self.view, 1)
        self._rebuild_recent_menu()

    def build_properties_widget(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("SidebarPanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        tabs = QTabWidget()
        tabs.addTab(self.properties_panel, "Properties")

        validation_tab = QWidget()
        validation_layout = QVBoxLayout(validation_tab)
        validation_layout.setContentsMargins(8, 8, 8, 8)
        validation_layout.setSpacing(8)
        validation_layout.addWidget(QLabel("Graph Validation"))
        validation_layout.addWidget(self.validation_table, 1)
        tabs.addTab(validation_tab, "Validation")

        export_tab = QWidget()
        export_layout = QVBoxLayout(export_tab)
        export_layout.setContentsMargins(8, 8, 8, 8)
        export_layout.setSpacing(8)
        export_layout.addWidget(QLabel("Graph Export Queue"))
        export_layout.addWidget(self.result_table, 1)
        tabs.addTab(export_tab, "Export")

        layout.addWidget(tabs, 1)
        return panel

    def _install_shortcuts(self) -> None:
        shortcuts = (
            ("Ctrl+Z", self.undo_stack.undo),
            ("Ctrl+Y", self.undo_stack.redo),
            ("Ctrl+C", self._copy_selection),
            ("Ctrl+V", self._paste_clipboard),
            ("Ctrl+D", self._duplicate_selection),
            ("Delete", self._delete_selection),
        )
        for sequence, slot in shortcuts:
            shortcut = QShortcut(QKeySequence(sequence), self.view)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.activated.connect(slot)
            self._shortcuts.append(shortcut)

    def set_assets(self, items: list[QueueItem]) -> None:
        self._assets = [item for item in items if item.metadata is not None]

    def refresh_asset_paths(self, paths: Iterable[Path]) -> None:
        changed_keys = {
            self._path_key(path)
            for path in paths
            if str(path).strip()
        }
        if not changed_keys:
            return

        selected_ids = self._scene.selected_node_ids()
        if not selected_ids and self.properties_panel._node is not None:
            selected_ids = [self.properties_panel._node.node_id]

        changed_node_ids = [
            node.node_id
            for node in self.project.graph.nodes
            if node.node_type is NodeType.TEXTURE_INPUT
            and self._path_key(Path(str(node.properties.get("path", "")))) in changed_keys
        ]
        if not changed_node_ids:
            return

        self._preview_generation += 1
        for node_id in changed_node_ids:
            self._preview_cache.invalidate_node_and_downstream(self.project.graph, node_id)

        for node_id in changed_node_ids:
            item = self._scene.node_items.get(node_id)
            if item is not None:
                item.refresh_content()

        if selected_ids:
            self._scene.select_node_ids(selected_ids)
        self._refresh_validation()
        self.properties_panel.set_node(self._scene.selected_node())
        self._refresh_properties_profile_summary()
        if not self._refresh_last_preview_request():
            self._preview_active_display_node()

    def set_auto_watch_enabled(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if self._auto_watch_enabled == enabled:
            if enabled:
                self._rebuild_asset_watchers()
            return
        self._auto_watch_enabled = enabled
        if enabled:
            self._rebuild_asset_watchers()
            self.status_message.emit("Graph Auto Watch enabled.")
        else:
            self._clear_asset_watchers()
            self.status_message.emit("Graph Auto Watch disabled.")

    def auto_watch_enabled(self) -> bool:
        return self._auto_watch_enabled

    def watched_texture_paths(self) -> tuple[Path, ...]:
        return tuple(
            Path(path)
            for path in sorted(self._watched_texture_keys.values())
        )

    def export_destinations(self, output_root: Path) -> list[Path]:
        return self._executor.resolve_output_paths(self.project.graph, output_root)

    def apply_recent_projects(self, paths: Iterable[str]) -> None:
        self._recent_project_dirs = []
        for raw_path in paths:
            path = Path(raw_path)
            if path.exists() and path not in self._recent_project_dirs:
                self._recent_project_dirs.append(path)
        self._rebuild_recent_menu()

    def recent_project_paths(self) -> tuple[str, ...]:
        return tuple(str(path) for path in self._recent_project_dirs[:8])

    def new_project(self) -> None:
        self.project = NodeGraphProject()
        self.project_dir = None
        self._last_preview_node_id = None
        self._last_preview_mode_label = ""
        self._preview_generation += 1
        self._preview_inflight_generation = 0
        self._pending_preview_request = None
        self._preview_cache.clear()
        self.undo_stack.clear()
        self._scene.project = self.project
        self._scene.rebuild()
        self.undo_stack.setClean()
        self._update_project_label()
        self.result_table.setRowCount(0)
        self._refresh_validation()
        self._rebuild_asset_watchers()
        self.status_message.emit("New graph project.")

    def load_project_dialog(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Load .texturegraph project")
        if not path:
            return
        self.load_project(Path(path))

    def save_project_dialog(self) -> None:
        path = str(self.project_dir or "")
        if not path:
            path = QFileDialog.getExistingDirectory(self, "Save .texturegraph project")
        if not path:
            return
        self.save_project(Path(path))

    def save_project(self, bundle_dir: Path) -> None:
        try:
            self._repository.save(self.project, bundle_dir)
        except Exception as exc:
            self.status_message.emit(f"Graph save failed: {exc}")
            return
        self.project_dir = bundle_dir
        self.project.name = bundle_dir.stem
        self._remember_recent_project(bundle_dir)
        self.undo_stack.setClean()
        self._update_project_label()
        self.status_message.emit(f"Saved graph: {bundle_dir}")

    def autosave_project(self) -> None:
        if not self.has_unsaved_changes() or self.project_dir is None:
            return
        autosave_dir = self.project_dir / ".autosave.texturegraph"
        try:
            self._repository.save(self.project, autosave_dir)
        except Exception as exc:
            self.status_message.emit(f"Graph autosave failed: {exc}")
            return
        self.status_message.emit(f"Autosaved graph: {autosave_dir}")

    def load_project(self, bundle_dir: Path) -> None:
        try:
            self.project = self._repository.load(bundle_dir)
        except Exception as exc:
            self.status_message.emit(f"Graph load failed: {exc}")
            return
        self.project_dir = bundle_dir
        self._remember_recent_project(bundle_dir)
        self._last_preview_node_id = None
        self._last_preview_mode_label = ""
        self._preview_generation += 1
        self._preview_inflight_generation = 0
        self._pending_preview_request = None
        self._preview_cache.clear()
        self.undo_stack.clear()
        self._scene.project = self.project
        self._scene.rebuild()
        self.undo_stack.setClean()
        self._update_project_label()
        self._refresh_validation()
        self._rebuild_asset_watchers()
        self.status_message.emit(f"Loaded graph: {self.project.name}")

    def remap_missing_texture_paths(self) -> None:
        missing_nodes = [
            node
            for node in self.project.graph.nodes
            if node.node_type is NodeType.TEXTURE_INPUT
            and (
                not str(node.properties.get("path", "")).strip()
                or not Path(str(node.properties.get("path", ""))).exists()
            )
        ]
        if not missing_nodes:
            self.status_message.emit("No missing texture paths.")
            return
        root = QFileDialog.getExistingDirectory(self, "Select folder to remap missing textures")
        if not root:
            return
        root_path = Path(root)
        indexed_paths: dict[str, Path] = {}
        for candidate in root_path.rglob("*"):
            if candidate.is_file():
                indexed_paths.setdefault(candidate.name.casefold(), candidate)

        updated = 0
        for node in missing_nodes:
            old_path = Path(str(node.properties.get("path", "")))
            replacement = indexed_paths.get(old_path.name.casefold())
            if replacement is None:
                continue
            properties = dict(node.properties)
            properties["path"] = str(replacement)
            self._push_graph_command(
                SetNodeStateCommand(
                    self.project.graph,
                    self._on_graph_command_changed,
                    node,
                    title=node.title,
                    properties=properties,
                    text="Remap texture path",
                    needs_rebuild=True,
                )
            )
            updated += 1
        self.status_message.emit(f"Remapped missing textures: {updated}/{len(missing_nodes)}")

    def _set_preview_quality(self) -> None:
        max_side = int(self.preview_quality_combo.currentData() or 1024)
        self._preview_cache.max_side = max_side
        self._preview_cache.clear()
        self._preview_active_display_node()

    def _schedule_autosave(self) -> None:
        if self.project_dir is not None and self.has_unsaved_changes():
            self._autosave_timer.start()

    def _remember_recent_project(self, bundle_dir: Path) -> None:
        try:
            resolved = bundle_dir.resolve()
        except OSError:
            resolved = bundle_dir
        self._recent_project_dirs = [
            path for path in self._recent_project_dirs if path != resolved
        ]
        self._recent_project_dirs.insert(0, resolved)
        self._recent_project_dirs = self._recent_project_dirs[:8]
        self._rebuild_recent_menu()

    def _rebuild_recent_menu(self) -> None:
        if not hasattr(self, "recent_button"):
            return
        menu = QMenu(self.recent_button)
        if not self._recent_project_dirs:
            empty_action = menu.addAction("No recent projects")
            empty_action.setEnabled(False)
        for bundle_dir in self._recent_project_dirs:
            action = menu.addAction(bundle_dir.name)
            action.setToolTip(str(bundle_dir))
            action.triggered.connect(
                lambda _checked=False, path=bundle_dir: self.load_project(path)
            )
        self.recent_button.setMenu(menu)

    def add_texture_node_from_selected_asset(self) -> None:
        if not self._assets:
            self.status_message.emit("Add assets to Queue first, then create Texture nodes.")
            return
        self.add_texture_node_for_path(str(self._assets[0].path), None)

    def add_texture_node_for_path(self, path: str, scene_position: object | None = None) -> None:
        name = Path(str(path)).stem[:28]
        if isinstance(scene_position, QPointF):
            position = (scene_position.x(), scene_position.y())
        else:
            position = self._next_node_position()
        node = create_graph_node(
            NodeType.TEXTURE_INPUT,
            title=name,
            position=position,
            properties={"path": str(path)},
        )
        self._push_graph_command(
            AddNodesCommand(self.project.graph, self._on_graph_command_changed, [node], text="Add texture"),
            select_node_ids=[node.node_id],
        )

    def add_node_of_type(
        self,
        node_type: object,
        scene_position: object | None = None,
        context: object | None = None,
    ) -> None:
        try:
            resolved_type = node_type if isinstance(node_type, NodeType) else NodeType(str(node_type))
        except ValueError:
            self.status_message.emit(f"Unknown node type: {node_type}")
            return

        position = self._node_position_for(scene_position)
        count = sum(1 for node in self.project.graph.nodes if node.node_type is resolved_type)
        properties: dict[str, object] = {}
        title = f"{node_type_label(resolved_type)} {count + 1}"

        if resolved_type is NodeType.OUTPUT_RGBA:
            title = f"Output {count + 1}"
            properties["filename"] = f"graph_output_{count + 1}.png"
        elif resolved_type is NodeType.TEXTURE_INPUT:
            title = f"Texture {count + 1}"
        elif resolved_type is NodeType.VIEW:
            title = f"View {count + 1}"

        node = create_graph_node(
            resolved_type,
            title=title,
            position=position,
            properties=properties,
        )
        connections = self._connections_for_new_node_context(node, context)
        self._push_graph_command(
            AddNodesCommand(
                self.project.graph,
                self._on_graph_command_changed,
                [node],
                connections,
                text=f"Add {node_type_label(resolved_type)}",
            ),
            select_node_ids=[node.node_id],
        )

    def add_constant_node(self) -> None:
        self.add_node_of_type(NodeType.CONSTANT_CHANNEL)

    def add_invert_node(self) -> None:
        self.add_node_of_type(NodeType.INVERT_CHANNEL)

    def add_view_node(self) -> None:
        self.add_node_of_type(NodeType.VIEW)

    def add_output_node(self) -> None:
        self.add_node_of_type(NodeType.OUTPUT_RGBA)

    def auto_layout_nodes(self) -> None:
        nodes = list(self.project.graph.nodes)
        if not nodes:
            self.status_message.emit("Graph is empty.")
            return
        before_positions = {node.node_id: node.position for node in nodes}
        after_positions = self._auto_layout_positions(nodes)
        if after_positions == before_positions:
            self.status_message.emit("Graph layout already looks clean.")
            return
        selected_ids = self._scene.selected_node_ids()
        self._push_graph_command(
            MoveNodesCommand(
                self.project.graph,
                self._on_graph_command_changed,
                before_positions,
                after_positions,
            ),
            select_node_ids=selected_ids,
        )
        self.view.fit_graph()
        self.status_message.emit(f"Auto layout: arranged {len(nodes)} node(s).")

    def _auto_layout_positions(self, nodes: list[GraphNode]) -> dict[str, tuple[float, float]]:
        node_ids = {node.node_id for node in nodes}
        depth_by_id = self._layout_depth_by_node(nodes)
        columns: dict[int, list[GraphNode]] = {}
        for node in nodes:
            columns.setdefault(depth_by_id.get(node.node_id, 0), []).append(node)

        depths = sorted(columns)
        center = self.view.mapToScene(self.view.viewport().rect().center())
        total_width = max(0, (len(depths) - 1) * GRAPH_LAYOUT_X_SPACING)
        left = center.x() - total_width / 2
        positions: dict[str, tuple[float, float]] = {}

        for column_index, depth in enumerate(depths):
            column_nodes = sorted(
                columns[depth],
                key=lambda item: (self._layout_type_rank(item.node_type), item.position[1], item.title),
            )
            heights = [self._estimated_node_height(node) for node in column_nodes]
            total_height = sum(heights) + max(0, len(heights) - 1) * GRAPH_LAYOUT_Y_SPACING
            x = self._snap_to_layout_grid(left + column_index * GRAPH_LAYOUT_X_SPACING)
            y = self._snap_to_layout_grid(center.y() - total_height / 2)
            for node, height in zip(column_nodes, heights, strict=True):
                if node.node_id in node_ids:
                    positions[node.node_id] = (x, y)
                y = self._snap_to_layout_grid(y + height + GRAPH_LAYOUT_Y_SPACING)
        return positions

    def _layout_depth_by_node(self, nodes: list[GraphNode]) -> dict[str, int]:
        node_by_id = {node.node_id: node for node in nodes}
        depth_by_id = {
            node.node_id: self._layout_type_rank(node.node_type)
            for node in nodes
        }
        incoming_count = {node.node_id: 0 for node in nodes}
        outgoing: dict[str, list[str]] = {node.node_id: [] for node in nodes}
        for connection in self.project.graph.connections:
            if connection.source_node_id not in node_by_id or connection.target_node_id not in node_by_id:
                continue
            outgoing[connection.source_node_id].append(connection.target_node_id)
            incoming_count[connection.target_node_id] += 1

        queue = sorted(
            (node_id for node_id, count in incoming_count.items() if count == 0),
            key=lambda node_id: (
                depth_by_id.get(node_id, 0),
                node_by_id[node_id].position[0],
                node_by_id[node_id].position[1],
            ),
        )
        while queue:
            node_id = queue.pop(0)
            for target_id in outgoing[node_id]:
                depth_by_id[target_id] = max(depth_by_id[target_id], depth_by_id[node_id] + 1)
                incoming_count[target_id] -= 1
                if incoming_count[target_id] == 0:
                    queue.append(target_id)
                    queue.sort(
                        key=lambda item: (
                            depth_by_id.get(item, 0),
                            node_by_id[item].position[0],
                            node_by_id[item].position[1],
                        )
                    )
        return depth_by_id

    @staticmethod
    def _layout_type_rank(node_type: NodeType) -> int:
        if node_type in (NodeType.TEXTURE_INPUT, NodeType.CONSTANT_CHANNEL):
            return 0
        if node_type in (
            NodeType.INVERT_CHANNEL,
            NodeType.LEVELS_CHANNEL,
            NodeType.CLAMP_CHANNEL,
            NodeType.THRESHOLD_CHANNEL,
            NodeType.BLEND_CHANNEL,
            NodeType.LUMINANCE,
        ):
            return 1
        return 2

    @staticmethod
    def _estimated_node_height(node: GraphNode) -> float:
        sockets = socket_definitions(node.node_type)
        input_count = sum(1 for socket in sockets if socket.direction is SocketDirection.INPUT)
        output_count = sum(1 for socket in sockets if socket.direction is SocketDirection.OUTPUT)
        row_count = max(input_count, output_count, 2)
        body_height = row_count * ROW_HEIGHT + 16
        if node.node_type is NodeType.TEXTURE_INPUT:
            body_height = TEXTURE_BODY_HEIGHT
        return float(TITLE_HEIGHT + body_height)

    @staticmethod
    def _snap_to_layout_grid(value: float) -> float:
        return float(round(value / GRAPH_LAYOUT_GRID) * GRAPH_LAYOUT_GRID)

    def export_graph(
        self,
        output_root: Path,
        options: ConversionOptions,
        logger,
    ) -> GraphExportSummary:
        self._refresh_validation()
        warnings = self._executor.validate(self.project)
        if warnings:
            for warning in warnings:
                logger(f"GRAPH VALIDATION -> {warning}")
        summary = self._executor.export_enabled_outputs(
            self.project,
            output_root,
            options,
            logger=logger,
        )
        self.set_export_summary(summary)
        return summary

    def set_export_summary(self, summary: GraphExportSummary) -> None:
        self.result_table.setRowCount(len(summary.results))
        for row, result in enumerate(summary.results):
            values = (
                result.output_name,
                self._status_text(result.status),
                str(result.destination),
                result.message,
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                self.result_table.setItem(row, column, item)

    def _refresh_validation(self) -> None:
        issues = self._executor.validate_issues(self.project)
        self.validation_table.blockSignals(True)
        try:
            self.validation_table.setRowCount(len(issues))
            for row, issue in enumerate(issues):
                node_title = self._node_title(issue.node_id)
                values = (
                    issue.severity.value.upper(),
                    node_title,
                    issue.message,
                )
                for column, value in enumerate(values):
                    item = QTableWidgetItem(value)
                    item.setData(Qt.ItemDataRole.UserRole, issue.node_id)
                    item.setToolTip(issue.message)
                    self.validation_table.setItem(row, column, item)
        finally:
            self.validation_table.blockSignals(False)

    def _select_validation_issue_node(self) -> None:
        selected_rows = self.validation_table.selectionModel().selectedRows()
        if not selected_rows:
            return
        row = selected_rows[0].row()
        item = self.validation_table.item(row, 0)
        if item is None:
            return
        node_id = str(item.data(Qt.ItemDataRole.UserRole) or "")
        if node_id:
            self._scene.select_node_ids([node_id])

    def _node_title(self, node_id: str) -> str:
        for node in self.project.graph.nodes:
            if node.node_id == node_id:
                return node.title
        return "-"

    def _next_node_position(self) -> tuple[float, float]:
        view_center = self.view.mapToScene(self.view.viewport().rect().center())
        offset = 28 * len(self.project.graph.nodes)
        return (view_center.x() + offset, view_center.y() + offset)

    def _node_position_for(self, scene_position: object | None) -> tuple[float, float]:
        if isinstance(scene_position, QPointF):
            return (scene_position.x(), scene_position.y())
        return self._next_node_position()

    def _push_graph_command(self, command, *, select_node_ids: Iterable[str] = ()) -> None:
        selected_ids = list(select_node_ids)
        self.undo_stack.push(command)
        if selected_ids:
            self._scene.select_node_ids(selected_ids)
        self._update_project_label()

    def _on_graph_command_changed(self, needs_rebuild: bool = True) -> None:
        selected_ids = self._scene.selected_node_ids()
        if not selected_ids and self.properties_panel._node is not None:
            selected_ids = [self.properties_panel._node.node_id]

        if needs_rebuild:
            self._preview_cache.clear()
        else:
            selected_node = self._scene.selected_node() or self.properties_panel._node
            if selected_node is not None:
                self._invalidate_preview_from_node(selected_node)
            else:
                self._preview_cache.clear()
        if needs_rebuild:
            self._scene.rebuild()
            self._scene.select_node_ids(selected_ids)
        else:
            self._refresh_node_flags()

        self._refresh_validation()
        self.properties_panel.set_node(self._scene.selected_node())
        self._refresh_properties_profile_summary()
        self._preview_active_display_node()
        self._update_project_label()
        self._schedule_autosave()
        if needs_rebuild:
            self._rebuild_asset_watchers()

    def _delete_selection(self) -> None:
        self._scene.delete_selected()

    def _on_delete_items_requested(self, payload: object) -> None:
        try:
            node_ids, connection_ids = payload
        except (TypeError, ValueError):
            return
        if not node_ids and not connection_ids:
            return
        self._push_graph_command(
            DeleteItemsCommand(
                self.project.graph,
                self._on_graph_command_changed,
                node_ids=node_ids,
                connection_ids=connection_ids,
            )
        )

    def _on_connection_requested(
        self,
        connection: GraphConnection,
        rewire_connection: object | None = None,
    ) -> None:
        remove_connections = (
            [rewire_connection]
            if isinstance(rewire_connection, GraphConnection)
            else []
        )
        self._push_graph_command(
            ReplaceInputConnectionCommand(
                self.project.graph,
                self._on_graph_command_changed,
                connection,
                remove_connections=remove_connections,
            ),
            select_node_ids=[connection.target_node_id],
        )
        self.status_message.emit("Connection created.")

    def _on_connection_delete_requested(self, connection: GraphConnection) -> None:
        self._push_graph_command(
            RemoveConnectionsCommand(
                self.project.graph,
                self._on_graph_command_changed,
                [connection],
                text="Delete connection",
            )
        )

    def _on_connection_insert_node_requested(
        self,
        connection: GraphConnection,
        node_type: object,
        scene_position: object,
    ) -> None:
        try:
            resolved_type = node_type if isinstance(node_type, NodeType) else NodeType(str(node_type))
        except ValueError:
            return
        input_socket_id = self._first_channel_input_socket_id(resolved_type)
        output_socket_id = self._first_channel_output_socket_id(resolved_type)
        if input_socket_id is None or output_socket_id is None:
            self.status_message.emit(f"{node_type_label(resolved_type)} cannot be inserted in a wire.")
            return

        position = self._node_position_for(scene_position)
        count = sum(1 for node in self.project.graph.nodes if node.node_type is resolved_type)
        node = create_graph_node(
            resolved_type,
            title=f"{node_type_label(resolved_type)} {count + 1}",
            position=position,
        )
        input_connection = GraphConnection(
            connection_id=make_connection_id(),
            source_node_id=connection.source_node_id,
            source_socket_id=connection.source_socket_id,
            target_node_id=node.node_id,
            target_socket_id=input_socket_id,
        )
        output_connection = GraphConnection(
            connection_id=make_connection_id(),
            source_node_id=node.node_id,
            source_socket_id=output_socket_id,
            target_node_id=connection.target_node_id,
            target_socket_id=connection.target_socket_id,
        )
        self._push_graph_command(
            InsertNodeInConnectionCommand(
                self.project.graph,
                self._on_graph_command_changed,
                connection,
                node,
                input_connection,
                output_connection,
            ),
            select_node_ids=[node.node_id],
        )

    def _on_wire_node_requested(self, port: PortItem, scene_position: object) -> None:
        if isinstance(scene_position, QPointF):
            self.view.open_node_menu(scene_position, wire_port=port)

    def _on_node_moved(
        self,
        node: GraphNode,
        previous_position: object,
        next_position: object,
    ) -> None:
        if not isinstance(previous_position, tuple) or not isinstance(next_position, tuple):
            return
        if previous_position == next_position:
            return
        self._push_graph_command(
            MoveNodesCommand(
                self.project.graph,
                self._on_graph_command_changed,
                {node.node_id: previous_position},
                {node.node_id: next_position},
            ),
            select_node_ids=[node.node_id],
        )

    def _on_nodes_moved(self, previous_positions: object, next_positions: object) -> None:
        if not isinstance(previous_positions, dict) or not isinstance(next_positions, dict):
            return
        if not previous_positions or not next_positions:
            return
        self._push_graph_command(
            MoveNodesCommand(
                self.project.graph,
                self._on_graph_command_changed,
                previous_positions,
                next_positions,
            ),
            select_node_ids=list(next_positions.keys()),
        )

    def _on_node_properties_changed(
        self,
        node: GraphNode,
        title: object,
        properties: object,
        needs_rebuild: bool,
    ) -> None:
        if not isinstance(title, str) or not isinstance(properties, dict):
            return
        self._push_graph_command(
            SetNodeStateCommand(
                self.project.graph,
                self._on_graph_command_changed,
                node,
                title=title,
                properties=properties,
                text="Edit node properties",
                needs_rebuild=needs_rebuild,
            ),
            select_node_ids=[node.node_id],
        )

    def _apply_output_profile(self, node: GraphNode | None) -> None:
        if node is None or node.node_type is not NodeType.OUTPUT_RGBA:
            return
        plan = self._build_output_profile_plan(node)

        self.undo_stack.beginMacro("Auto connect output profile")
        try:
            self.undo_stack.push(
                SetNodeStateCommand(
                    self.project.graph,
                    self._on_graph_command_changed,
                    node,
                    title=node.title,
                    properties=plan.properties,
                    text="Set output profile",
                    needs_rebuild=False,
                )
            )
            if plan.utility_nodes:
                self.undo_stack.push(
                    AddNodesCommand(
                        self.project.graph,
                        self._on_graph_command_changed,
                        plan.utility_nodes,
                        plan.utility_connections,
                        text="Add profile nodes",
                    )
                )
            for connection in plan.output_connections:
                self.undo_stack.push(
                    ReplaceInputConnectionCommand(
                        self.project.graph,
                        self._on_graph_command_changed,
                        connection,
                    )
                )
        finally:
            self.undo_stack.endMacro()

        self._scene.select_node_ids([node.node_id])
        self._refresh_properties_profile_summary(node)
        self._schedule_autosave()
        self.status_message.emit(f"{node.title}: {self._profile_status_text(plan)}")

    def _clear_output_inputs(self, node: GraphNode | None) -> None:
        if node is None or node.node_type is not NodeType.OUTPUT_RGBA:
            return
        input_socket_ids = {
            socket.socket_id
            for socket in socket_definitions(NodeType.OUTPUT_RGBA)
            if socket.direction is SocketDirection.INPUT
        }
        connections = [
            connection
            for connection in self.project.graph.connections
            if connection.target_node_id == node.node_id
            and connection.target_socket_id in input_socket_ids
        ]
        if not connections:
            self.status_message.emit(f"{node.title}: output inputs are already clear.")
            return
        self._push_graph_command(
            RemoveConnectionsCommand(
                self.project.graph,
                self._on_graph_command_changed,
                connections,
                text="Clear output inputs",
            ),
            select_node_ids=[node.node_id],
        )
        self.status_message.emit(f"{node.title}: cleared {len(connections)} input wire(s).")

    def _build_output_profile_plan(self, node: GraphNode) -> OutputProfilePlan:
        profile = self._output_profile(node)
        mode = OUTPUT_PROFILE_MODES.get(profile, OutputMode.RGBA)
        properties = dict(node.properties)
        properties["profile"] = profile.value
        properties["mode"] = mode.value
        properties["filename"] = OUTPUT_PROFILE_FILENAMES.get(profile, "packed_rgba.png")

        utility_nodes: list[GraphNode] = []
        utility_connections: list[GraphConnection] = []
        output_connections: list[GraphConnection] = []
        texture_nodes_by_map_type = self._texture_nodes_by_map_type()
        packed_texture_nodes = self._packed_texture_nodes()
        output_x, output_y = node.position

        if profile is OutputProfile.GENERIC_RGBA:
            generic_connections, generic_utility_nodes = self._generic_rgba_profile_connections(
                node,
                texture_nodes_by_map_type,
            )
            output_connections.extend(generic_connections)
            utility_nodes.extend(generic_utility_nodes)
        else:
            for index, (socket_id, candidates, fill_value) in enumerate(self._profile_rules(profile)):
                if mode is OutputMode.RGB and socket_id == "a":
                    continue
                source_node, source_socket_id, invert = self._resolve_separate_profile_source(
                    candidates,
                    texture_nodes_by_map_type,
                )
                if source_node is None:
                    source_node, source_socket_id, invert = self._resolve_packed_profile_source(
                        profile,
                        socket_id,
                        candidates,
                        packed_texture_nodes,
                    )
                resolved_fill = self._profile_fill_value(
                    profile,
                    socket_id,
                    candidates,
                    fill_value,
                )
                if source_node is not None:
                    if invert:
                        invert_node = create_graph_node(
                            NodeType.INVERT_CHANNEL,
                            title=f"Invert {socket_id.upper()}",
                            position=(output_x - 240.0, output_y + 58.0 * index),
                        )
                        utility_nodes.append(invert_node)
                        utility_connections.append(
                            GraphConnection(
                                connection_id=make_connection_id(),
                                source_node_id=source_node.node_id,
                                source_socket_id=source_socket_id,
                                target_node_id=invert_node.node_id,
                                target_socket_id="in",
                            )
                        )
                        source_node = invert_node
                        source_socket_id = "out"
                else:
                    constant_node = create_graph_node(
                        NodeType.CONSTANT_CHANNEL,
                        title=f"{socket_id.upper()} {resolved_fill}",
                        position=(output_x - 240.0, output_y + 58.0 * index),
                        properties={"value": resolved_fill},
                    )
                    utility_nodes.append(constant_node)
                    source_node = constant_node
                    source_socket_id = "out"

                output_connections.append(
                    GraphConnection(
                        connection_id=make_connection_id(),
                        source_node_id=source_node.node_id,
                        source_socket_id=source_socket_id,
                        target_node_id=node.node_id,
                        target_socket_id=socket_id,
                    )
                )

        return OutputProfilePlan(
            profile=profile,
            mode=mode,
            properties=properties,
            utility_nodes=utility_nodes,
            utility_connections=utility_connections,
            output_connections=output_connections,
            summary_lines=self._profile_plan_summary_lines(
                utility_nodes,
                utility_connections,
                output_connections,
            ),
        )

    def _refresh_properties_profile_summary(self, node: GraphNode | None = None) -> None:
        selected_node = node or self._scene.selected_node()
        if selected_node is None or selected_node.node_type is not NodeType.OUTPUT_RGBA:
            self.properties_panel.set_profile_summary("")
            return
        self.properties_panel.set_profile_summary(self._profile_summary_text(selected_node))

    def _profile_summary_text(self, node: GraphNode) -> str:
        plan = self._build_output_profile_plan(node)
        profile_label = OUTPUT_PROFILE_LABELS.get(plan.profile, plan.profile.value)
        filename = str(plan.properties.get("filename", "packed_rgba.png"))
        lines = [f"{profile_label} -> {plan.mode.value.upper()} / {filename}"]
        if any(connection.target_node_id == node.node_id for connection in self.project.graph.connections):
            lines.append("Existing input wires will be replaced.")
        if plan.summary_lines:
            lines.extend(plan.summary_lines)
        else:
            lines.append("No compatible output channel rules.")
        return "\n".join(lines)

    def _profile_status_text(self, plan: OutputProfilePlan) -> str:
        profile_label = OUTPUT_PROFILE_LABELS.get(plan.profile, plan.profile.value)
        mappings = "; ".join(plan.summary_lines)
        if len(mappings) > 180:
            mappings = f"{mappings[:177]}..."
        return f"{profile_label}: {mappings}" if mappings else f"{profile_label}: no channel mappings."

    def _profile_plan_summary_lines(
        self,
        utility_nodes: list[GraphNode],
        utility_connections: list[GraphConnection],
        output_connections: list[GraphConnection],
    ) -> tuple[str, ...]:
        node_by_id = {node.node_id: node for node in self.project.graph.nodes}
        node_by_id.update({node.node_id: node for node in utility_nodes})
        utility_input_by_target = {
            (connection.target_node_id, connection.target_socket_id): connection
            for connection in utility_connections
        }
        channel_order = {"r": 0, "g": 1, "b": 2, "a": 3}
        lines: list[str] = []
        for connection in sorted(
            output_connections,
            key=lambda item: channel_order.get(item.target_socket_id, 99),
        ):
            source_text = self._profile_source_text(
                connection.source_node_id,
                connection.source_socket_id,
                node_by_id,
                utility_input_by_target,
            )
            lines.append(f"{connection.target_socket_id.upper()} <- {source_text}")
        return tuple(lines)

    def _profile_source_text(
        self,
        node_id: str,
        socket_id: str,
        node_by_id: dict[str, GraphNode],
        utility_input_by_target: dict[tuple[str, str], GraphConnection],
    ) -> str:
        node = node_by_id.get(node_id)
        socket_label = socket_id.upper() if socket_id in {"r", "g", "b", "a"} else socket_id
        if node is None:
            return f"{node_id}.{socket_label}"
        if node.node_type is NodeType.CONSTANT_CHANNEL:
            return f"Constant {self._coerce_channel_value(node.properties.get('value'), 255)}"
        if node.node_type is NodeType.INVERT_CHANNEL:
            input_connection = utility_input_by_target.get((node.node_id, "in"))
            if input_connection is None:
                return "Invert"
            inner_text = self._profile_source_text(
                input_connection.source_node_id,
                input_connection.source_socket_id,
                node_by_id,
                utility_input_by_target,
            )
            return f"Invert({inner_text})"
        if node.node_type is NodeType.TEXTURE_INPUT:
            path_name = Path(str(node.properties.get("path", ""))).name
            source_name = path_name or node.title
            return f"{source_name}.{socket_label}"
        return f"{node.title}.{socket_label}"

    @staticmethod
    def _coerce_channel_value(value: object, default: int) -> int:
        try:
            return max(0, min(255, int(value)))
        except (TypeError, ValueError):
            return default

    def _profile_rules(
        self,
        profile: OutputProfile,
    ) -> tuple[tuple[str, tuple[PackSourceCandidate, ...], int | None], ...]:
        if profile is OutputProfile.GENERIC_RGBA:
            return ()
        if profile is OutputProfile.METAHUMAN_REPACK:
            return METAHUMAN_REPACK_RULES
        layout = OUTPUT_PROFILE_PACK_LAYOUTS.get(profile)
        if layout is None:
            return ()
        return tuple(
            (
                rule.channel.lower(),
                rule.candidates,
                rule.fill_value,
            )
            for rule in PACK_LAYOUTS[layout]
        )

    def _generic_rgba_profile_connections(
        self,
        output_node: GraphNode,
        texture_nodes_by_map_type: dict[TextureMapType, GraphNode],
    ) -> tuple[list[GraphConnection], list[GraphNode]]:
        color_node = (
            texture_nodes_by_map_type.get(TextureMapType.BASECOLOR)
            or texture_nodes_by_map_type.get(TextureMapType.EMISSIVE)
            or self._first_generic_texture_node()
        )
        opacity_node = texture_nodes_by_map_type.get(TextureMapType.OPACITY)
        utility_nodes: list[GraphNode] = []

        if color_node is None:
            output_x, output_y = output_node.position
            constants = [
                create_graph_node(
                    NodeType.CONSTANT_CHANNEL,
                    title=f"{socket.upper()} {value}",
                    position=(output_x - 240.0, output_y + 58.0 * index),
                    properties={"value": value},
                )
                for index, (socket, value) in enumerate((("r", 0), ("g", 0), ("b", 0), ("a", 255)))
            ]
            utility_nodes.extend(constants)
            return (
                [
                    GraphConnection(
                        connection_id=make_connection_id(),
                        source_node_id=constant.node_id,
                        source_socket_id="out",
                        target_node_id=output_node.node_id,
                        target_socket_id=socket_id,
                    )
                    for constant, socket_id in zip(constants, ("r", "g", "b", "a"), strict=True)
                ],
                utility_nodes,
            )

        alpha_source_node = opacity_node or color_node
        alpha_socket_id = "r" if opacity_node is not None else "a"
        source_specs = (
            ("r", color_node, "r"),
            ("g", color_node, "g"),
            ("b", color_node, "b"),
            ("a", alpha_source_node, alpha_socket_id),
        )
        return (
            [
                GraphConnection(
                    connection_id=make_connection_id(),
                    source_node_id=source_node.node_id,
                    source_socket_id=source_socket_id,
                    target_node_id=output_node.node_id,
                    target_socket_id=target_socket_id,
                )
                for target_socket_id, source_node, source_socket_id in source_specs
            ],
            utility_nodes,
        )

    def _first_generic_texture_node(self) -> GraphNode | None:
        for node in self.project.graph.nodes:
            if node.node_type is not NodeType.TEXTURE_INPUT:
                continue
            path = Path(str(node.properties.get("path", "")))
            map_type = detect_texture_map_type(path)
            if map_type in (TextureMapType.NORMAL, TextureMapType.HEIGHT):
                continue
            return node
        return next(
            (
                node
                for node in self.project.graph.nodes
                if node.node_type is NodeType.TEXTURE_INPUT
            ),
            None,
        )

    def _resolve_separate_profile_source(
        self,
        candidates: tuple[PackSourceCandidate, ...],
        texture_nodes_by_map_type: dict[TextureMapType, GraphNode],
    ) -> tuple[GraphNode | None, str, bool]:
        for candidate in candidates:
            source_node = texture_nodes_by_map_type.get(candidate.map_type)
            if source_node is not None:
                return source_node, "r", candidate.invert
        return None, "", False

    def _resolve_packed_profile_source(
        self,
        profile: OutputProfile,
        socket_id: str,
        candidates: tuple[PackSourceCandidate, ...],
        packed_texture_nodes: list[tuple[str, GraphNode]],
    ) -> tuple[GraphNode | None, str, bool]:
        semantic = self._profile_socket_semantic(profile, socket_id, candidates)
        if semantic is None:
            return None, "", False
        for pack_kind, texture_node in packed_texture_nodes:
            mapping = PACKED_SOURCE_CHANNELS.get(pack_kind, {})
            source = mapping.get(semantic)
            if source is None:
                continue
            source_socket_id, invert = source
            return texture_node, source_socket_id, invert
        return None, "", False

    def _profile_fill_value(
        self,
        profile: OutputProfile,
        socket_id: str,
        candidates: tuple[PackSourceCandidate, ...],
        fill_value: int | None,
    ) -> int:
        if fill_value is not None:
            return fill_value
        if candidates:
            return DEFAULT_PROFILE_FILL.get(candidates[0].map_type, 255)
        if profile is OutputProfile.UNITY_URP and socket_id in {"g", "b"}:
            return 0
        return 255

    @staticmethod
    def _profile_socket_semantic(
        profile: OutputProfile,
        socket_id: str,
        candidates: tuple[PackSourceCandidate, ...],
    ) -> TextureMapType | str | None:
        if candidates:
            return candidates[0].map_type
        if profile is OutputProfile.UNITY_HDRP and socket_id == "b":
            return PACKED_DETAIL_MASK
        return None

    def _texture_nodes_by_map_type(self) -> dict[TextureMapType, GraphNode]:
        asset_map_types = {
            self._path_key(item.path): item.effective_map_type
            for item in self._assets
        }
        nodes_by_map_type: dict[TextureMapType, GraphNode] = {}
        for node in self.project.graph.nodes:
            if node.node_type is not NodeType.TEXTURE_INPUT:
                continue
            path = Path(str(node.properties.get("path", "")))
            map_type = asset_map_types.get(self._path_key(path))
            if map_type is None:
                map_type = detect_texture_map_type(path)
            if map_type is TextureMapType.UNKNOWN:
                continue
            nodes_by_map_type.setdefault(map_type, node)
        return nodes_by_map_type

    def _packed_texture_nodes(self) -> list[tuple[str, GraphNode]]:
        packed_nodes: list[tuple[str, GraphNode]] = []
        for node in self.project.graph.nodes:
            if node.node_type is not NodeType.TEXTURE_INPUT:
                continue
            pack_kind = self._detect_packed_texture_kind(
                Path(str(node.properties.get("path", ""))).stem
            )
            if pack_kind:
                packed_nodes.append((pack_kind, node))
        return packed_nodes

    @staticmethod
    def _detect_packed_texture_kind(stem: str) -> str:
        normalized = stem.lower().replace("-", "_").replace(" ", "_")
        collapsed = normalized.replace("_", "")
        if "maskmap" in collapsed or "hdrpmask" in collapsed:
            return "unity_hdrp_mask"
        if "orm" in collapsed or "occlusionroughnessmetallic" in collapsed:
            return "unreal_orm"
        if "mra" in collapsed or "metallicroughnessao" in collapsed:
            return "unreal_mra"
        if "rma" in collapsed or "roughnessmetallicao" in collapsed:
            return "unreal_rma"
        if "metallicsmoothness" in collapsed or "urpmask" in collapsed:
            return "unity_urp_mask"
        return ""

    @staticmethod
    def _path_key(path: Path) -> str:
        try:
            return str(path.resolve()).lower()
        except OSError:
            return str(path).lower()

    def _rebuild_asset_watchers(self) -> None:
        pending_change_keys = set(self._pending_asset_change_keys)
        texture_paths: dict[str, str] = {}
        directory_keys: dict[str, set[str]] = {}
        directory_paths: dict[str, str] = {}
        file_keys: dict[str, str] = {}

        if self._auto_watch_enabled:
            for node in self.project.graph.nodes:
                if node.node_type is not NodeType.TEXTURE_INPUT:
                    continue
                raw_path = str(node.properties.get("path", "")).strip()
                if not raw_path:
                    continue
                texture_path = Path(raw_path)
                path_key = self._path_key(texture_path)
                texture_paths[path_key] = str(texture_path)
                file_keys[path_key] = str(texture_path)
                directory = texture_path.parent if str(texture_path.parent) else texture_path
                directory_key = self._path_key(directory)
                directory_paths[directory_key] = str(directory)
                directory_keys.setdefault(directory_key, set()).add(path_key)

        self._replace_watched_paths(self._asset_watcher.files(), file_keys.values(), is_file=True)
        self._replace_watched_paths(
            self._asset_watcher.directories(),
            directory_paths.values(),
            is_file=False,
        )
        self._watched_texture_keys = texture_paths
        self._watched_file_keys = file_keys
        self._watched_directory_paths = directory_paths
        self._watched_directory_keys = directory_keys
        self._pending_asset_change_keys = {
            path_key
            for path_key in pending_change_keys
            if path_key in self._watched_texture_keys
        }
        self.watched_paths_changed.emit(self.watched_texture_paths())

    def _clear_asset_watchers(self) -> None:
        self._asset_watch_timer.stop()
        self._pending_asset_change_keys.clear()
        existing_files = self._asset_watcher.files()
        existing_directories = self._asset_watcher.directories()
        if existing_files:
            self._asset_watcher.removePaths(existing_files)
        if existing_directories:
            self._asset_watcher.removePaths(existing_directories)
        self._watched_texture_keys.clear()
        self._watched_file_keys.clear()
        self._watched_directory_paths.clear()
        self._watched_directory_keys.clear()
        self.watched_paths_changed.emit(())

    def _replace_watched_paths(
        self,
        existing_paths: list[str],
        target_paths: Iterable[str],
        *,
        is_file: bool,
    ) -> None:
        target_list = list(dict.fromkeys(path for path in target_paths if str(path).strip()))
        existing_keys = {self._path_key(Path(path)): path for path in existing_paths if str(path).strip()}
        target_keys = {self._path_key(Path(path)): path for path in target_list}
        remove_paths = [
            existing_keys[key]
            for key in existing_keys.keys() - target_keys.keys()
        ]
        add_paths = [
            path
            for key, path in target_keys.items()
            if key not in existing_keys
        ]
        if remove_paths:
            self._asset_watcher.removePaths(remove_paths)
        if add_paths:
            filtered_paths = [
                path
                for path in add_paths
                if (Path(path).is_file() if is_file else Path(path).is_dir())
            ]
            if filtered_paths:
                self._asset_watcher.addPaths(filtered_paths)

    def _on_watched_directory_changed(self, directory: str) -> None:
        directory_key = self._path_key(Path(directory))
        changed_keys = self._watched_directory_keys.get(directory_key, set())
        if not changed_keys:
            return
        self._pending_asset_change_keys.update(changed_keys)
        self._asset_watch_timer.start()
        if self._auto_watch_enabled:
            self._rebuild_asset_watchers()

    def _on_watched_file_changed(self, path: str) -> None:
        path_key = self._path_key(Path(path))
        if path_key in self._watched_file_keys:
            self._pending_asset_change_keys.add(path_key)
            self._asset_watch_timer.start()
        if self._auto_watch_enabled:
            self._rebuild_asset_watchers()

    def _emit_pending_asset_changes(self) -> None:
        if not self._pending_asset_change_keys:
            return
        changed_paths = tuple(
            Path(self._watched_texture_keys[path_key])
            for path_key in sorted(self._pending_asset_change_keys)
            if path_key in self._watched_texture_keys
        )
        self._pending_asset_change_keys.clear()
        if changed_paths:
            self.assets_changed.emit(changed_paths)

    def _connections_for_new_node_context(
        self,
        node: GraphNode,
        context: object | None,
    ) -> list[GraphConnection]:
        if not isinstance(context, PortItem):
            return []
        if context.direction is SocketDirection.OUTPUT:
            target_socket_id = self._first_channel_input_socket_id(node.node_type)
            if target_socket_id is None:
                return []
            return [
                GraphConnection(
                    connection_id=make_connection_id(),
                    source_node_id=context.node_item.node.node_id,
                    source_socket_id=context.socket_id,
                    target_node_id=node.node_id,
                    target_socket_id=target_socket_id,
                )
            ]

        source_socket_id = self._first_channel_output_socket_id(node.node_type)
        if source_socket_id is None:
            return []
        return [
            GraphConnection(
                connection_id=make_connection_id(),
                source_node_id=node.node_id,
                source_socket_id=source_socket_id,
                target_node_id=context.node_item.node.node_id,
                target_socket_id=context.socket_id,
            )
        ]

    def _copy_selection(self) -> None:
        node_ids = set(self._scene.selected_node_ids())
        if not node_ids:
            self.status_message.emit("No graph nodes selected.")
            return
        self._clipboard_nodes = [
            clone_graph_node(node)
            for node in self.project.graph.nodes
            if node.node_id in node_ids
        ]
        self._clipboard_connections = [
            clone_graph_connection(connection)
            for connection in self.project.graph.connections
            if connection.source_node_id in node_ids and connection.target_node_id in node_ids
        ]
        self.status_message.emit(f"Copied nodes: {len(self._clipboard_nodes)}")

    def _paste_clipboard(self) -> None:
        if not self._clipboard_nodes:
            self.status_message.emit("Clipboard is empty.")
            return
        id_map = {node.node_id: make_node_id() for node in self._clipboard_nodes}
        pasted_nodes: list[GraphNode] = []
        for node in self._clipboard_nodes:
            pasted = clone_graph_node(node, node_id=id_map[node.node_id])
            pasted.position = (pasted.position[0] + 36.0, pasted.position[1] + 36.0)
            pasted_nodes.append(pasted)
        pasted_connections = [
            clone_graph_connection(
                connection,
                connection_id=make_connection_id(),
                source_node_id=id_map[connection.source_node_id],
                target_node_id=id_map[connection.target_node_id],
            )
            for connection in self._clipboard_connections
            if connection.source_node_id in id_map and connection.target_node_id in id_map
        ]
        self._push_graph_command(
            AddNodesCommand(
                self.project.graph,
                self._on_graph_command_changed,
                pasted_nodes,
                pasted_connections,
                text="Paste nodes",
            ),
            select_node_ids=[node.node_id for node in pasted_nodes],
        )
        self._clipboard_nodes = [clone_graph_node(node) for node in pasted_nodes]
        self._clipboard_connections = [
            clone_graph_connection(connection)
            for connection in pasted_connections
        ]

    def _duplicate_selection(self) -> None:
        self._copy_selection()
        self._paste_clipboard()

    def has_unsaved_changes(self) -> bool:
        try:
            return not self.undo_stack.isClean()
        except RuntimeError:
            return False

    def _on_undo_stack_changed(self, *_args: object) -> None:
        self._update_project_label()

    def _update_project_label(self) -> None:
        try:
            suffix = "*" if self.has_unsaved_changes() else ""
            self.project_label.setText(f"{self.project.name}{suffix}")
        except RuntimeError:
            return

    @staticmethod
    def _first_channel_input_socket_id(node_type: NodeType) -> str | None:
        return next(
            (
                socket.socket_id
                for socket in socket_definitions(node_type)
                if socket.direction is SocketDirection.INPUT
            ),
            None,
        )

    @staticmethod
    def _first_channel_output_socket_id(node_type: NodeType) -> str | None:
        return next(
            (
                socket.socket_id
                for socket in socket_definitions(node_type)
                if socket.direction is SocketDirection.OUTPUT
            ),
            None,
        )

    def _on_graph_changed(self) -> None:
        self._on_graph_command_changed(True)

    def _on_node_selected(self, node: GraphNode | None) -> None:
        self.properties_panel.set_node(node)
        self._refresh_properties_profile_summary(node)
        if self._active_display_node() is None:
            self._preview_view_node(node)

    def _on_node_double_clicked(self, node: GraphNode | None) -> None:
        if node is None:
            return
        if node.node_type is NodeType.OUTPUT_RGBA:
            self._preview_output_node(node)
            return
        if node.node_type is NodeType.VIEW:
            self._preview_view_node(node)

    def _on_display_flag_clicked(self, node: GraphNode | None) -> None:
        if node is None:
            return
        self._push_graph_command(
            SetDisplayFlagCommand(
                self.project.graph,
                self._on_graph_command_changed,
                node.node_id,
            ),
            select_node_ids=[node.node_id],
        )

    def _on_render_flag_clicked(self, node: GraphNode | None) -> None:
        if node is None or node.node_type is not NodeType.OUTPUT_RGBA:
            return
        properties = dict(node.properties)
        properties["enabled"] = not bool(properties.get("enabled", True))
        self._push_graph_command(
            SetNodeStateCommand(
                self.project.graph,
                self._on_graph_command_changed,
                node,
                title=node.title,
                properties=properties,
                text="Toggle render flag",
                needs_rebuild=False,
            ),
            select_node_ids=[node.node_id],
        )
        state = "on" if properties.get("enabled", True) else "off"
        self.status_message.emit(f"{node.title}: render flag {state}.")

    def _on_enable_flag_clicked(self, node: GraphNode | None) -> None:
        if node is None or not node_has_enable_flag(node.node_type):
            return
        properties = dict(node.properties)
        properties["enabled"] = not bool(properties.get("enabled", True))
        self._push_graph_command(
            SetNodeStateCommand(
                self.project.graph,
                self._on_graph_command_changed,
                node,
                title=node.title,
                properties=properties,
                text="Toggle node enabled",
                needs_rebuild=False,
            ),
            select_node_ids=[node.node_id],
        )
        state = "enabled" if properties.get("enabled", True) else "bypassed"
        self.status_message.emit(f"{node.title}: {state}.")

    def _on_node_reset_clicked(self, node: GraphNode | None) -> None:
        if node is None or not node_has_resettable_parameters(node.node_type):
            return
        shadow = clone_graph_node(node)
        reset_node_parameters(shadow)
        self._push_graph_command(
            SetNodeStateCommand(
                self.project.graph,
                self._on_graph_command_changed,
                node,
                title=node.title,
                properties=shadow.properties,
                text="Reset node parameters",
                needs_rebuild=False,
            ),
            select_node_ids=[node.node_id],
        )
        self.status_message.emit(f"{node.title}: parameters reset.")

    def _refresh_node_flags(self) -> None:
        for item in self._scene.node_items.values():
            item.refresh_flags()

    def _invalidate_preview_from_node(self, node: GraphNode) -> None:
        self._preview_cache.invalidate_node_and_downstream(self.project.graph, node.node_id)

    def _active_display_node(self) -> GraphNode | None:
        return next(
            (
                node
                for node in self.project.graph.nodes
                if bool(node.properties.get("display", False))
            ),
            None,
        )

    def _preview_active_display_node(self) -> None:
        display_node = self._active_display_node()
        if display_node is not None:
            self._preview_display_node(display_node)

    def _preview_display_node(self, node: GraphNode) -> None:
        self._request_preview_node(node)

    def _preview_view_node(self, node: GraphNode | None) -> None:
        if node is None or node.node_type is not NodeType.VIEW:
            return
        if incoming_connection(
            self.project.graph,
            target_node_id=node.node_id,
            target_socket_id="in",
        ) is None:
            return
        self._request_preview_node(node)

    def _preview_output_node(self, node: GraphNode) -> None:
        mode = self._output_mode_label(node)
        self._request_preview_node(node, mode_label=mode)

    def _request_preview_node(self, node: GraphNode, *, mode_label: str = "") -> None:
        self._last_preview_node_id = node.node_id
        self._last_preview_mode_label = mode_label
        self._preview_generation += 1
        generation = self._preview_generation
        if self._preview_inflight_generation:
            self._pending_preview_request = (node.node_id, mode_label)
            return
        self._start_preview_request(node, mode_label, generation)

    def _start_preview_request(self, node: GraphNode, mode_label: str, generation: int) -> None:
        self._preview_inflight_generation = generation
        snapshot = deepcopy(self.project)
        node_snapshot = next(
            (
                graph_node
                for graph_node in snapshot.graph.nodes
                if graph_node.node_id == node.node_id
            ),
            None,
        )
        if node_snapshot is None:
            self._preview_inflight_generation = 0
            return
        thread = QThread(self)
        worker = GraphPreviewWorker(
            generation,
            snapshot,
            node_snapshot,
            mode_label=mode_label,
            max_side=self._preview_cache.max_side,
            preview_cache=self._preview_cache,
        )
        worker.moveToThread(thread)
        self._preview_threads.append(thread)
        self._preview_workers.append(worker)
        thread.started.connect(worker.run)
        worker.finished.connect(self._on_preview_worker_finished)
        worker.failed.connect(self._on_preview_worker_failed)
        worker.completed.connect(thread.quit)
        worker.completed.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(lambda current=thread: self._forget_preview_thread(current))
        thread.finished.connect(lambda current=worker: self._forget_preview_worker(current))
        thread.start()
        self.status_message.emit(f"{node.title}: rendering preview...")

    def _refresh_last_preview_request(self) -> bool:
        if not self._last_preview_node_id:
            return False
        node = next(
            (
                graph_node
                for graph_node in self.project.graph.nodes
                if graph_node.node_id == self._last_preview_node_id
            ),
            None,
        )
        if node is None:
            self._last_preview_node_id = None
            self._last_preview_mode_label = ""
            return False
        self._request_preview_node(node, mode_label=self._last_preview_mode_label)
        return True

    def _on_preview_worker_finished(
        self,
        generation: int,
        image: object,
        title: str,
        meta: str,
        node_id: str,
    ) -> None:
        self._preview_inflight_generation = 0
        pending = self._pending_preview_request
        self._pending_preview_request = None
        if generation != self._preview_generation:
            if pending is not None:
                self._restart_pending_preview(pending)
            return
        self.preview_image_requested.emit(image, title, meta, node_id)
        if pending is not None:
            self._restart_pending_preview(pending)

    def _on_preview_worker_failed(self, generation: int, title: str, message: str) -> None:
        self._preview_inflight_generation = 0
        pending = self._pending_preview_request
        self._pending_preview_request = None
        if generation != self._preview_generation:
            if pending is not None:
                self._restart_pending_preview(pending)
            return
        self.status_message.emit(f"{title}: preview failed: {message}")
        if pending is not None:
            self._restart_pending_preview(pending)

    def _restart_pending_preview(self, pending: tuple[str, str]) -> None:
        node_id, mode_label = pending
        node = next(
            (
                graph_node
                for graph_node in self.project.graph.nodes
                if graph_node.node_id == node_id
            ),
            None,
        )
        if node is None:
            return
        self._start_preview_request(node, mode_label, self._preview_generation)

    def _forget_preview_thread(self, thread: QThread) -> None:
        if thread in self._preview_threads:
            self._preview_threads.remove(thread)

    def _forget_preview_worker(self, worker: GraphPreviewWorker) -> None:
        if worker in self._preview_workers:
            self._preview_workers.remove(worker)

    @staticmethod
    def _output_mode_label(node: GraphNode) -> str:
        try:
            return OutputMode(str(node.properties.get("mode", OutputMode.RGBA.value))).value.upper()
        except ValueError:
            return OutputMode.RGBA.value.upper()

    @staticmethod
    def _output_profile(node: GraphNode) -> OutputProfile:
        try:
            return OutputProfile(str(node.properties.get("profile", OutputProfile.GENERIC_RGBA.value)))
        except ValueError:
            return OutputProfile.GENERIC_RGBA

    @staticmethod
    def _status_text(status: ConversionStatus) -> str:
        if status is ConversionStatus.SUCCESS:
            return "Success"
        if status is ConversionStatus.SKIPPED:
            return "Skipped"
        return "Error"

    def minimumSizeHint(self) -> QSize:
        return QSize(360, 260)

    def sizeHint(self) -> QSize:
        return QSize(980, 620)

    def closeEvent(self, event) -> None:
        self._preview_generation += 1
        for thread in list(self._preview_threads):
            thread.quit()
            thread.wait(10000)
        super().closeEvent(event)
