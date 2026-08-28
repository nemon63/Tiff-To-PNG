from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from PyQt6.QtCore import QPoint, QPointF, QRect, QRectF, Qt, QUrl, pyqtSignal
from PyQt6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QPainter,
    QPainterPath,
    QPainterPathStroker,
    QPen,
    QPixmap,
)
from PyQt6.QtWidgets import (
    QGraphicsEllipseItem,
    QGraphicsItem,
    QGraphicsPathItem,
    QGraphicsPixmapItem,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsSimpleTextItem,
    QGraphicsView,
    QMenu,
    QWidget,
)

from image_converter.domain.models import TextureMapType
from image_converter.domain.node_graph import (
    GraphConnection,
    GraphNode,
    NodeGraphProject,
    NodeType,
    SocketDirection,
    SocketType,
    TextureDataRole,
    incoming_connection,
    make_connection_id,
    node_has_enable_flag,
    node_has_resettable_parameters,
    socket_definitions,
)
from image_converter.services.map_types import detect_texture_map_type
from image_converter.ui.node_help import NodeHelpPopup, node_help_content
from image_converter.ui.node_visual_cache import TextureVisualCache

NODE_WIDTH = 190
TEXTURE_NODE_WIDTH = 260
TITLE_HEIGHT = 28
ROW_HEIGHT = 22
NODE_BODY_TOP_PADDING = 30
NODE_BODY_BOTTOM_PADDING = 10
PORT_RADIUS = 6
FLAG_SIZE = 14
FLAG_TOP = 7
FLAG_GAP = 6
TEXTURE_BODY_HEIGHT = 122
TEXTURE_THUMBNAIL_SIZE = 72
TEXTURE_THUMBNAIL_X = 10
TEXTURE_THUMBNAIL_Y = TITLE_HEIGHT + 14
TEXTURE_META_X = 94
TEXTURE_PORT_CENTER_Y = TITLE_HEIGHT + 34
TEXTURE_PORT_SPACING = 20
TEXTURE_PORT_LABEL_X_PAD = 34
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
        self._wire_color = (
            QColor("#B678FF")
            if source_port.socket_type is SocketType.IMAGE
            else QColor("#5BA7FF")
        )
        # Keep the item's bounds large enough for the thicker selected wire.
        self.setPen(QPen(self._wire_color, 4.0))
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

    def paint(self, painter: QPainter, option, widget=None) -> None:
        # QGraphicsPathItem draws a dashed bounding rectangle for selected paths.
        # Paint the wire directly so selection is communicated by the wire itself.
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(self._display_pen())
        painter.drawPath(self.path())

    def _display_pen(self) -> QPen:
        pen = QPen(
            QColor("#FFD166") if self.isSelected() else self._wire_color,
            3.5 if self.isSelected() else 2.0,
        )
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        return pen

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
    def __init__(
        self,
        node_item: "GraphNodeItem",
        socket_id: str,
        label: str,
        direction: SocketDirection,
        socket_type: SocketType,
    ):
        super().__init__(-PORT_RADIUS, -PORT_RADIUS, PORT_RADIUS * 2, PORT_RADIUS * 2, node_item)
        self.node_item = node_item
        self.socket_id = socket_id
        self.direction = direction
        self.socket_type = socket_type
        if socket_type is SocketType.IMAGE:
            color = QColor("#B678FF")
        else:
            color = QColor("#6EA8FE") if direction is SocketDirection.OUTPUT else QColor("#D9964A")
        self.setBrush(color)
        self.setPen(QPen(QColor("#111820"), 1.0))
        self.setAcceptHoverEvents(True)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setToolTip(f"{node_item.node.title}.{label} ({socket_type.value})")

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
    def __init__(self, node: GraphNode, texture_visual_cache: TextureVisualCache | None = None):
        super().__init__()
        self.node = node
        self._texture_visual_cache = texture_visual_cache or TextureVisualCache()
        self.node_width = TEXTURE_NODE_WIDTH if node.node_type is NodeType.TEXTURE_INPUT else NODE_WIDTH
        self.title_item: QGraphicsSimpleTextItem | None = None
        self.subtitle_item: QGraphicsSimpleTextItem | None = None
        self.path_label_item: QGraphicsSimpleTextItem | None = None
        self.metadata_line_item: QGraphicsSimpleTextItem | None = None
        self.size_line_item: QGraphicsSimpleTextItem | None = None
        self.thumbnail_bg_item: QGraphicsRectItem | None = None
        self.thumbnail_item: QGraphicsPixmapItem | None = None
        self.color_swatch_item: QGraphicsRectItem | None = None
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
        self.help_button_item: QGraphicsEllipseItem | None = None
        self.help_button_label: QGraphicsSimpleTextItem | None = None
        self._drag_start_position: tuple[float, float] | None = None
        sockets = socket_definitions(node.node_type)
        input_count = sum(1 for socket in sockets if socket.direction is SocketDirection.INPUT)
        output_count = sum(1 for socket in sockets if socket.direction is SocketDirection.OUTPUT)
        row_count = max(input_count, output_count, 2)
        body_height = (
            NODE_BODY_TOP_PADDING
            + row_count * ROW_HEIGHT
            + NODE_BODY_BOTTOM_PADDING
        )
        if node.node_type is NodeType.TEXTURE_INPUT:
            body_height = TEXTURE_BODY_HEIGHT
        self.setRect(0, 0, self.node_width, TITLE_HEIGHT + body_height)
        self.setPos(*node.position)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges, True)
        self.setCacheMode(QGraphicsItem.CacheMode.DeviceCoordinateCache)
        self.setPen(QPen(QColor("#3A434D"), 1.0))
        self.setBrush(QColor("#232A32"))
        self._build_contents()

    def _build_contents(self) -> None:
        self.title_item = QGraphicsSimpleTextItem("", self)
        self.title_item.setBrush(QColor("#E4EAF1"))
        self.title_item.setPos(10, 6)
        self.title_item.setText(self._elided_node_title())
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
            subtitle_text = self.node.node_type.value
            subtitle_x = 10
            if self.node.node_type is NodeType.COLOR:
                self.color_swatch_item = QGraphicsRectItem(10, TITLE_HEIGHT + 8, 40, 16, self)
                self.color_swatch_item.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
                self.color_swatch_item.setPen(QPen(QColor("#667484"), 1.0))
                self.color_swatch_item.setBrush(self._node_color())
                subtitle_text = self._node_color_hex()
                subtitle_x = 60
            self.subtitle_item = QGraphicsSimpleTextItem(subtitle_text, self)
            self.subtitle_item.setBrush(QColor("#8E9AA8"))
            self.subtitle_item.setPos(subtitle_x, TITLE_HEIGHT + 6)

        inputs = [socket for socket in socket_definitions(self.node.node_type) if socket.direction is SocketDirection.INPUT]
        outputs = [socket for socket in socket_definitions(self.node.node_type) if socket.direction is SocketDirection.OUTPUT]
        for index, socket in enumerate(inputs):
            y = y_offset + index * ROW_HEIGHT
            port = PortItem(
                self,
                socket.socket_id,
                socket.name,
                socket.direction,
                socket.socket_type,
            )
            port.setPos(0, y + 10)
            self.port_items[socket.socket_id] = port
            label = QGraphicsSimpleTextItem(socket.name, self)
            label.setBrush(QColor("#C9D2DD"))
            label.setPos(14, y)
            self.port_label_items[socket.socket_id] = label

        for index, socket in enumerate(outputs):
            spacing = TEXTURE_PORT_SPACING if self.node.node_type is NodeType.TEXTURE_INPUT else ROW_HEIGHT
            y = y_offset + index * spacing
            port = PortItem(
                self,
                socket.socket_id,
                socket.name,
                socket.direction,
                socket.socket_type,
            )
            port.setPos(self.node_width, y + 10)
            self.port_items[socket.socket_id] = port
            label = QGraphicsSimpleTextItem(socket.name, self)
            label.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
            label.setBrush(self._socket_label_color(socket.socket_id))
            if self.node.node_type is NodeType.TEXTURE_INPUT:
                label_x = self.node_width - (
                    54 if socket.socket_type is SocketType.IMAGE else TEXTURE_PORT_LABEL_X_PAD
                )
            else:
                label_x = self.node_width - (54 if socket.socket_type is SocketType.IMAGE else 28)
            label.setPos(label_x, y)
            self.port_label_items[socket.socket_id] = label

    def _thumbnail_pixmap(self) -> QPixmap | None:
        path = Path(str(self.node.properties.get("path", "")))
        visual = self._texture_visual_cache.get(path)
        return visual.pixmap if visual is not None else None

    def _elided_node_title(self) -> str:
        right_edge = self.node_width - 8
        if node_help_content(self.node.node_type) is not None:
            right_edge = self._help_button_rect().left()
        available_width = max(20, int(right_edge - 16))
        font = self.title_item.font() if self.title_item is not None else QFont()
        return QFontMetrics(font).elidedText(
            self.node.title,
            Qt.TextElideMode.ElideRight,
            available_width,
        )

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
        visual = self._texture_visual_cache.get(path)
        if visual is None:
            return "loading" if self._texture_visual_cache.is_pending(path) else "unreadable"
        width, height = visual.size
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
        if node_help_content(self.node.node_type) is not None:
            help_rect = self._help_button_rect()
            self.help_button_item = QGraphicsEllipseItem(help_rect, self)
            self.help_button_item.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
            self.help_button_item.setBrush(QColor("#253C52"))
            self.help_button_item.setPen(QPen(QColor("#6EA8FE"), 1.0))
            self.help_button_item.setCursor(Qt.CursorShape.PointingHandCursor)
            self.help_button_item.setToolTip("Описание ноды")
            self.help_button_label = QGraphicsSimpleTextItem("?", self)
            self.help_button_label.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
            self.help_button_label.setBrush(QColor("#DCEBFA"))
            self.help_button_label.setPos(help_rect.x() + 4, help_rect.y() - 1)
            self.help_button_label.setCursor(Qt.CursorShape.PointingHandCursor)
            self.help_button_label.setToolTip("Описание ноды")

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
            self.title_item.setText(self._elided_node_title())
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
            if self.node.node_type is NodeType.COLOR:
                self.subtitle_item.setText(self._node_color_hex())
                if self.color_swatch_item is not None:
                    self.color_swatch_item.setBrush(self._node_color())
            else:
                self.subtitle_item.setText(self.node.node_type.value)

        self.refresh_flags()
        self.update()

    def set_color_preview(self, properties: dict) -> None:
        if self.node.node_type is not NodeType.COLOR:
            return

        def component(name: str) -> int:
            try:
                value = int(properties.get(name, 255))
            except (TypeError, ValueError):
                value = 255
            return max(0, min(value, 255))

        red = component("red")
        green = component("green")
        blue = component("blue")
        alpha = component("alpha")
        if self.color_swatch_item is not None:
            self.color_swatch_item.setBrush(QColor(red, green, blue, alpha))
        if self.subtitle_item is not None:
            self.subtitle_item.setText(f"#{red:02X}{green:02X}{blue:02X}{alpha:02X}")
        self.update()

    def _position_thumbnail_item(self) -> None:
        if self.thumbnail_item is None or self.thumbnail_item.pixmap().isNull():
            return
        pixmap = self.thumbnail_item.pixmap()
        self.thumbnail_item.setPos(
            TEXTURE_THUMBNAIL_X + (TEXTURE_THUMBNAIL_SIZE - pixmap.width()) / 2,
            TEXTURE_THUMBNAIL_Y + (TEXTURE_THUMBNAIL_SIZE - pixmap.height()) / 2,
        )

    def _node_color(self) -> QColor:
        return QColor(
            self._node_color_component("red"),
            self._node_color_component("green"),
            self._node_color_component("blue"),
            self._node_color_component("alpha"),
        )

    def _node_color_hex(self) -> str:
        return "#{:02X}{:02X}{:02X}{:02X}".format(
            self._node_color_component("red"),
            self._node_color_component("green"),
            self._node_color_component("blue"),
            self._node_color_component("alpha"),
        )

    def _node_color_component(self, property_name: str) -> int:
        try:
            value = int(self.node.properties.get(property_name, 255))
        except (TypeError, ValueError):
            value = 255
        return max(0, min(value, 255))

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

    def _help_button_rect(self) -> QRectF:
        occupied_flags = 1
        if self.node.node_type is NodeType.OUTPUT_RGBA:
            occupied_flags += 1
        if node_has_enable_flag(self.node.node_type):
            occupied_flags += 1
        if node_has_resettable_parameters(self.node.node_type):
            occupied_flags += 1
        return self._flag_rect_from_right(occupied_flags)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            scene = self.scene()
            if (
                isinstance(scene, GraphScene)
                and node_help_content(self.node.node_type) is not None
                and self._help_button_rect().contains(event.pos())
            ):
                scene.node_help_requested.emit(self.node, self)
                event.accept()
                return
            if isinstance(scene, GraphScene) and not scene.editing_enabled:
                super().mousePressEvent(event)
                return
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

        node_path = QPainterPath()
        node_path.addRoundedRect(rect, 5, 5)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        painter.drawPath(node_path)

        painter.save()
        painter.setClipPath(node_path)
        painter.fillRect(
            QRectF(rect.left(), rect.top(), rect.width(), TITLE_HEIGHT),
            QColor("#2F3741"),
        )
        painter.restore()

        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(
            QPen(
                QColor("#5BA7FF") if self.isSelected() else QColor("#3A434D"),
                1.2,
            )
        )
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
    node_help_requested = pyqtSignal(object, object)
    node_moved = pyqtSignal(object, object, object)
    nodes_moved = pyqtSignal(object, object)
    node_render_flag_clicked = pyqtSignal(object)
    node_reset_clicked = pyqtSignal(object)
    node_selection_changed = pyqtSignal(object)
    node_properties_selection_changed = pyqtSignal(object)
    status_message = pyqtSignal(str)
    wire_node_requested = pyqtSignal(object, object)

    def __init__(self, project: NodeGraphProject, parent: QWidget | None = None):
        super().__init__(parent)
        self.project = project
        self.node_items: dict[str, GraphNodeItem] = {}
        self.connection_items: dict[str, ConnectionItem] = {}
        self.connection_ids_by_node: dict[str, set[str]] = {}
        self.texture_visual_cache = TextureVisualCache(self)
        self.texture_visual_cache.visual_ready.connect(self._refresh_texture_visual)
        self.drag_port: PortItem | None = None
        self.drag_rewire_connection: GraphConnection | None = None
        self.drag_start_scene_pos: QPointF | None = None
        self.drag_wire: QGraphicsPathItem | None = None
        self.move_start_positions: dict[str, tuple[float, float]] = {}
        self.editing_enabled = True
        self._pending_selection_active = False
        self._pending_selection_node_id: str | None = None
        self.selectionChanged.connect(self._emit_selection)
        self.setSceneRect(-3000, -3000, 6000, 6000)

    def rebuild(self) -> None:
        self.clear()
        self.node_items = {}
        self.connection_items = {}
        self.connection_ids_by_node = {}
        self.drag_port = None
        self.drag_rewire_connection = None
        self.drag_start_scene_pos = None
        self.drag_wire = None
        self.move_start_positions = {}
        for node in self.project.graph.nodes:
            item = GraphNodeItem(node, self.texture_visual_cache)
            item.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, self.editing_enabled)
            self.addItem(item)
            self.node_items[node.node_id] = item
        for connection in self.project.graph.connections:
            self._add_connection_item(connection)

    def _refresh_texture_visual(self, path: Path) -> None:
        path_key = self.texture_visual_cache.path_key(path)
        for item in self.node_items.values():
            if item.node.node_type is not NodeType.TEXTURE_INPUT:
                continue
            item_path = Path(str(item.node.properties.get("path", "")))
            if self.texture_visual_cache.path_key(item_path) == path_key:
                item.refresh_content()

    def add_node(self, node: GraphNode) -> None:
        self.project.graph.nodes.append(node)
        item = GraphNodeItem(node, self.texture_visual_cache)
        self.addItem(item)
        self.node_items[node.node_id] = item
        self.clearSelection()
        item.setSelected(True)
        self.graph_changed.emit()

    def delete_selected(self) -> None:
        if not self.editing_enabled:
            return
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
        if not self.editing_enabled:
            return
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
        if not self.editing_enabled:
            return 0
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
        if self.drag_port.socket_type is not target_port.socket_type:
            self.status_message.emit(
                f"Cannot connect {self.drag_port.socket_type.value} to {target_port.socket_type.value}."
            )
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
        for connection_id in self.connection_ids_by_node.get(node_id, ()):
            connection_item = self.connection_items.get(connection_id)
            if connection_item is not None:
                connection_item.update_path()

    def sync_node_positions(self) -> None:
        for node in self.project.graph.nodes:
            item = self.node_items.get(node.node_id)
            if item is None:
                continue
            position = QPointF(*node.position)
            if item.pos() == position:
                continue
            item.setPos(position)

    def set_editing_enabled(self, enabled: bool) -> None:
        self.editing_enabled = enabled
        for item in self.node_items.values():
            item.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, enabled)

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
        pending_selection_active = self._pending_selection_active
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
        if pending_selection_active:
            self._flush_pending_selection()

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
        self.connection_ids_by_node.setdefault(connection.source_node_id, set()).add(
            connection.connection_id
        )
        self.connection_ids_by_node.setdefault(connection.target_node_id, set()).add(
            connection.connection_id
        )

    def _emit_selection(self) -> None:
        node = self.selected_node()
        self.node_properties_selection_changed.emit(node)
        if self.move_start_positions:
            self._pending_selection_active = True
            self._pending_selection_node_id = node.node_id if node is not None else None
            return
        self._pending_selection_active = False
        self._pending_selection_node_id = None
        self.node_selection_changed.emit(node)

    def _flush_pending_selection(self) -> None:
        node: GraphNode | None = None
        if self._pending_selection_node_id is not None:
            item = self.node_items.get(self._pending_selection_node_id)
            if item is not None:
                node = item.node
        self._pending_selection_active = False
        self._pending_selection_node_id = None
        self.node_selection_changed.emit(node)


class GraphView(QGraphicsView):
    connection_delete_requested = pyqtSignal(object)
    connection_insert_node_requested = pyqtSignal(object, object, object)
    texture_dropped = pyqtSignal(str, object)
    node_add_requested = pyqtSignal(object, object, object)
    layout_requested = pyqtSignal()

    def __init__(self, scene: GraphScene, parent: QWidget | None = None):
        super().__init__(scene, parent)
        self.node_help_popup: NodeHelpPopup | None = None
        self._pan_start: QPoint | None = None
        self._pan_scroll: tuple[int, int] = (0, 0)
        self._knife_active = False
        self._knife_start: QPointF | None = None
        self.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        self.setDragMode(QGraphicsView.DragMode.RubberBandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.BoundingRectViewportUpdate)
        self.setOptimizationFlag(QGraphicsView.OptimizationFlag.DontSavePainterState, True)
        self.setOptimizationFlag(QGraphicsView.OptimizationFlag.DontAdjustForAntialiasing, True)
        self.setBackgroundBrush(QColor("#15191E"))
        self.setAcceptDrops(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        scene.node_help_requested.connect(self._show_node_help)

    def _show_node_help(self, node: GraphNode, node_item: GraphNodeItem) -> None:
        content = node_help_content(node.node_type)
        if content is None:
            return

        if self.node_help_popup is not None:
            self.node_help_popup.close()
            self.node_help_popup.deleteLater()

        popup = NodeHelpPopup(content, self)
        self.node_help_popup = popup

        help_rect = node_item._help_button_rect()
        scene_rect = node_item.mapRectToScene(help_rect)
        top_left = self.viewport().mapToGlobal(self.mapFromScene(scene_rect.topLeft()))
        bottom_right = self.viewport().mapToGlobal(
            self.mapFromScene(scene_rect.bottomRight())
        )
        popup.show_near(QRect(top_left, bottom_right).normalized())

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
            scene = self.scene()
            if isinstance(scene, GraphScene) and not scene.editing_enabled:
                event.ignore()
                return
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
        scene = self.scene()
        if isinstance(scene, GraphScene) and not scene.editing_enabled:
            event.ignore()
            return
        texture_path = self._extract_texture_path(event.mimeData())
        if texture_path:
            self.texture_dropped.emit(texture_path, self.mapToScene(event.position().toPoint()))
            event.acceptProposedAction()
            return
        super().dropEvent(event)

    def contextMenuEvent(self, event) -> None:
        scene = self.scene()
        if isinstance(scene, GraphScene) and not scene.editing_enabled:
            event.ignore()
            return
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
            self._add_node_menu_action(input_menu, "Color", NodeType.COLOR, scene_position, wire_port)
            self._add_node_menu_action(input_menu, "Constant", NodeType.CONSTANT_CHANNEL, scene_position, wire_port)

        channel_menu = menu.addMenu("Channel")
        self._add_node_menu_action(channel_menu, "Invert", NodeType.INVERT_CHANNEL, scene_position, wire_port)
        self._add_node_menu_action(channel_menu, "Levels", NodeType.LEVELS_CHANNEL, scene_position, wire_port)
        self._add_node_menu_action(channel_menu, "Remap", NodeType.REMAP_CHANNEL, scene_position, wire_port)
        self._add_node_menu_action(channel_menu, "Clamp", NodeType.CLAMP_CHANNEL, scene_position, wire_port)
        self._add_node_menu_action(channel_menu, "Threshold", NodeType.THRESHOLD_CHANNEL, scene_position, wire_port)
        self._add_node_menu_action(channel_menu, "Blur", NodeType.BLUR_CHANNEL, scene_position, wire_port)
        self._add_node_menu_action(channel_menu, "Dilate", NodeType.DILATE_CHANNEL, scene_position, wire_port)
        self._add_node_menu_action(channel_menu, "Erode", NodeType.ERODE_CHANNEL, scene_position, wire_port)
        self._add_node_menu_action(channel_menu, "Luminance", NodeType.LUMINANCE, scene_position, wire_port)

        image_menu = menu.addMenu("Image")
        self._add_node_menu_action(image_menu, "Mix Image", NodeType.MIX_IMAGE, scene_position, wire_port)
        self._add_node_menu_action(image_menu, "Blend Image", NodeType.BLEND_IMAGE, scene_position, wire_port)
        self._add_node_menu_action(image_menu, "Split RGBA", NodeType.SPLIT_RGBA, scene_position, wire_port)
        self._add_node_menu_action(image_menu, "Combine RGBA", NodeType.COMBINE_RGBA, scene_position, wire_port)
        self._add_node_menu_action(image_menu, "Apply Mask", NodeType.SET_ALPHA, scene_position, wire_port)

        math_menu = menu.addMenu("Math")
        self._add_node_menu_action(math_menu, "Blend Channel", NodeType.BLEND_CHANNEL, scene_position, wire_port)

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
            ("Remap", NodeType.REMAP_CHANNEL),
            ("Clamp", NodeType.CLAMP_CHANNEL),
            ("Threshold", NodeType.THRESHOLD_CHANNEL),
            ("Blur", NodeType.BLUR_CHANNEL),
            ("Dilate", NodeType.DILATE_CHANNEL),
            ("Erode", NodeType.ERODE_CHANNEL),
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
