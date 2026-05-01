from __future__ import annotations

from pathlib import Path

from PIL import Image
from PyQt6.QtCore import QPoint, QPointF, QRectF, QSize, Qt, QUrl, pyqtSignal
from PyQt6.QtGui import (
    QColor,
    QPainter,
    QPainterPath,
    QPainterPathStroker,
    QPen,
    QPixmap,
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
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from image_converter.domain.models import ConversionOptions, ConversionStatus, QueueItem
from image_converter.domain.node_graph import (
    GraphConnection,
    GraphNode,
    NodeGraphProject,
    NodeType,
    OutputMode,
    SocketDirection,
    create_graph_node,
    incoming_connection,
    make_connection_id,
    node_has_enable_flag,
    node_has_resettable_parameters,
    node_type_label,
    remove_node,
    reset_node_parameters,
    replace_input_connection,
    socket_definitions,
)
from image_converter.services.node_graph_executor import (
    GraphExecutionError,
    GraphExportSummary,
    NodeGraphExecutor,
    NodeGraphPreviewCache,
)
from image_converter.services.node_graph_project import NodeGraphProjectRepository

NODE_WIDTH = 190
TITLE_HEIGHT = 28
ROW_HEIGHT = 22
PORT_RADIUS = 6
FLAG_SIZE = 14
FLAG_TOP = 7
FLAG_GAP = 6


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
        self.port_items: dict[str, PortItem] = {}
        self.display_flag_item: QGraphicsRectItem | None = None
        self.display_flag_label: QGraphicsSimpleTextItem | None = None
        self.enable_flag_item: QGraphicsRectItem | None = None
        self.enable_flag_label: QGraphicsSimpleTextItem | None = None
        self.render_flag_item: QGraphicsRectItem | None = None
        self.render_flag_label: QGraphicsSimpleTextItem | None = None
        self.reset_button_item: QGraphicsRectItem | None = None
        self.reset_button_label: QGraphicsSimpleTextItem | None = None
        sockets = socket_definitions(node.node_type)
        input_count = sum(1 for socket in sockets if socket.direction is SocketDirection.INPUT)
        output_count = sum(1 for socket in sockets if socket.direction is SocketDirection.OUTPUT)
        row_count = max(input_count, output_count, 2)
        body_height = row_count * ROW_HEIGHT + 16
        if node.node_type is NodeType.TEXTURE_INPUT:
            body_height += 58
        self.setRect(0, 0, NODE_WIDTH, TITLE_HEIGHT + body_height)
        self.setPos(*node.position)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges, True)
        self.setPen(QPen(QColor("#3A434D"), 1.0))
        self.setBrush(QColor("#232A32"))
        self._build_contents()

    def _build_contents(self) -> None:
        title_bg = QGraphicsRectItem(0, 0, NODE_WIDTH, TITLE_HEIGHT, self)
        title_bg.setPen(QPen(Qt.PenStyle.NoPen))
        title_bg.setBrush(QColor("#2F3741"))

        title = QGraphicsSimpleTextItem(self.node.title, self)
        title.setBrush(QColor("#E4EAF1"))
        title.setPos(10, 6)

        self._build_flag_items()

        subtitle = QGraphicsSimpleTextItem(self.node.node_type.value, self)
        subtitle.setBrush(QColor("#8E9AA8"))
        subtitle.setPos(10, TITLE_HEIGHT + 6)

        y_offset = TITLE_HEIGHT + 30
        if self.node.node_type is NodeType.TEXTURE_INPUT:
            thumbnail = self._build_thumbnail()
            if thumbnail is not None:
                thumbnail.setParentItem(self)
                thumbnail.setPos(10, TITLE_HEIGHT + 26)
            path_text = Path(str(self.node.properties.get("path", ""))).name or "no texture"
            path_label = QGraphicsSimpleTextItem(path_text[:24], self)
            path_label.setBrush(QColor("#B7C1CC"))
            path_label.setPos(78, TITLE_HEIGHT + 34)
            y_offset += 58

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

        for index, socket in enumerate(outputs):
            y = y_offset + index * ROW_HEIGHT
            port = PortItem(self, socket.socket_id, socket.name, socket.direction)
            port.setPos(NODE_WIDTH, y + 10)
            self.port_items[socket.socket_id] = port
            label = QGraphicsSimpleTextItem(socket.name, self)
            label.setBrush(QColor("#C9D2DD"))
            label.setPos(NODE_WIDTH - 28, y)

    def _build_thumbnail(self) -> QGraphicsPixmapItem | None:
        path = Path(str(self.node.properties.get("path", "")))
        if not path.exists():
            return None
        try:
            with Image.open(path) as image:
                image.thumbnail((54, 54))
                rgba = image.convert("RGBA")
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
        pixmap_item = QGraphicsPixmapItem(QPixmap.fromImage(qimage))
        return pixmap_item

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
        x = NODE_WIDTH - FLAG_SIZE - 8 - index * (FLAG_SIZE + FLAG_GAP)
        return QRectF(x, FLAG_TOP, FLAG_SIZE, FLAG_SIZE)

    def _render_flag_rect(self) -> QRectF:
        return QRectF(NODE_WIDTH - FLAG_SIZE - 8, FLAG_TOP, FLAG_SIZE, FLAG_SIZE)

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
        super().mousePressEvent(event)

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
            self.node.position = (float(self.pos().x()), float(self.pos().y()))
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
    graph_changed = pyqtSignal()
    node_display_flag_clicked = pyqtSignal(object)
    node_double_clicked = pyqtSignal(object)
    node_enable_flag_clicked = pyqtSignal(object)
    node_render_flag_clicked = pyqtSignal(object)
    node_reset_clicked = pyqtSignal(object)
    node_selection_changed = pyqtSignal(object)
    status_message = pyqtSignal(str)

    def __init__(self, project: NodeGraphProject, parent: QWidget | None = None):
        super().__init__(parent)
        self.project = project
        self.node_items: dict[str, GraphNodeItem] = {}
        self.connection_items: dict[str, ConnectionItem] = {}
        self.drag_port: PortItem | None = None
        self.drag_wire: QGraphicsPathItem | None = None
        self.selectionChanged.connect(self._emit_selection)
        self.setSceneRect(-3000, -3000, 6000, 6000)

    def rebuild(self) -> None:
        self.clear()
        self.node_items = {}
        self.connection_items = {}
        self.drag_port = None
        self.drag_wire = None
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
        for item in selected:
            if isinstance(item, GraphNodeItem):
                remove_node(self.project.graph, item.node.node_id)
            elif isinstance(item, ConnectionItem):
                self.project.graph.connections = [
                    connection
                    for connection in self.project.graph.connections
                    if connection.connection_id != item.connection.connection_id
                ]
        self.rebuild()
        self.graph_changed.emit()

    def start_wire_drag(self, port: PortItem, scene_pos: QPointF) -> None:
        self.drag_port = port
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
        source_port, target_port = self._resolve_drag_ports(scene_pos)
        if self.drag_wire is not None:
            self.removeItem(self.drag_wire)
        self.drag_wire = None
        self.drag_port = None

        if source_port is None or target_port is None:
            self.status_message.emit("Connection canceled.")
            return
        connection = GraphConnection(
            connection_id=make_connection_id(),
            source_node_id=source_port.node_item.node.node_id,
            source_socket_id=source_port.socket_id,
            target_node_id=target_port.node_item.node.node_id,
            target_socket_id=target_port.socket_id,
        )
        target_node_id = connection.target_node_id
        replace_input_connection(self.project.graph, connection)
        self.rebuild()
        target_item = self.node_items.get(target_node_id)
        if target_item is not None:
            self.clearSelection()
            target_item.setSelected(True)
        self.graph_changed.emit()
        self.status_message.emit("Connection created.")

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
        self.project.graph.connections = [
            connection
            for connection in self.project.graph.connections
            if connection.connection_id not in cut_ids
        ]
        self.rebuild()
        self.graph_changed.emit()
        self.status_message.emit(f"Cut connections: {len(cut_ids)}")
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

    def selected_node(self) -> GraphNode | None:
        for item in self.selectedItems():
            if isinstance(item, GraphNodeItem):
                return item.node
        return None

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
    texture_dropped = pyqtSignal(str, object)
    node_add_requested = pyqtSignal(object, object)

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
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            factor = 1.12 if event.angleDelta().y() > 0 else 1 / 1.12
            self.scale(factor, factor)
            event.accept()
            return
        super().wheelEvent(event)

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
        menu = QMenu(self)

        input_menu = menu.addMenu("Input")
        self._add_node_menu_action(input_menu, "Texture", NodeType.TEXTURE_INPUT, scene_position)
        self._add_node_menu_action(input_menu, "Constant", NodeType.CONSTANT_CHANNEL, scene_position)

        channel_menu = menu.addMenu("Channel")
        self._add_node_menu_action(channel_menu, "Invert", NodeType.INVERT_CHANNEL, scene_position)
        self._add_node_menu_action(channel_menu, "Levels", NodeType.LEVELS_CHANNEL, scene_position)
        self._add_node_menu_action(channel_menu, "Clamp", NodeType.CLAMP_CHANNEL, scene_position)
        self._add_node_menu_action(channel_menu, "Threshold", NodeType.THRESHOLD_CHANNEL, scene_position)
        self._add_node_menu_action(channel_menu, "Luminance", NodeType.LUMINANCE, scene_position)

        math_menu = menu.addMenu("Math")
        self._add_node_menu_action(math_menu, "Blend", NodeType.BLEND_CHANNEL, scene_position)

        utility_menu = menu.addMenu("Utility")
        self._add_node_menu_action(utility_menu, "View", NodeType.VIEW, scene_position)

        output_menu = menu.addMenu("Output")
        self._add_node_menu_action(output_menu, "Output RGBA", NodeType.OUTPUT_RGBA, scene_position)

        menu.addSeparator()
        fit_action = menu.addAction("Fit View")
        fit_action.triggered.connect(self.fit_graph)
        menu.exec(event.globalPos())
        event.accept()

    def _add_node_menu_action(
        self,
        menu: QMenu,
        label: str,
        node_type: NodeType,
        scene_position: QPointF,
    ) -> None:
        action = menu.addAction(label)
        action.triggered.connect(
            lambda _checked=False, current_type=node_type: self.node_add_requested.emit(
                current_type,
                scene_position,
            )
        )

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
    node_changed = pyqtSignal(bool)

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
        self.title_edit.textEdited.connect(self._apply_changes)
        self.form.addRow("Title", self.title_edit)

        self.node_enabled_checkbox = QCheckBox("Enabled")
        self.node_enabled_checkbox.toggled.connect(self._apply_changes)
        self.form.addRow("Node", self.node_enabled_checkbox)

        self.path_edit = QLineEdit()
        self.path_edit.textEdited.connect(self._apply_changes)
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

        self.value_spin = QSpinBox()
        self.value_spin.setRange(0, 255)
        self.value_spin.setKeyboardTracking(False)
        self.value_spin.valueChanged.connect(self._apply_changes)
        self.form.addRow("Value", self.value_spin)

        self.level_black_spin = self._make_byte_spin()
        self.form.addRow("Black", self.level_black_spin)
        self.level_white_spin = self._make_byte_spin()
        self.form.addRow("White", self.level_white_spin)
        self.level_gamma_spin = QDoubleSpinBox()
        self.level_gamma_spin.setRange(0.05, 8.0)
        self.level_gamma_spin.setSingleStep(0.05)
        self.level_gamma_spin.setDecimals(2)
        self.level_gamma_spin.setKeyboardTracking(False)
        self.level_gamma_spin.valueChanged.connect(self._apply_changes)
        self.form.addRow("Gamma", self.level_gamma_spin)
        self.level_out_min_spin = self._make_byte_spin()
        self.form.addRow("Output Min", self.level_out_min_spin)
        self.level_out_max_spin = self._make_byte_spin()
        self.form.addRow("Output Max", self.level_out_max_spin)

        self.clamp_min_spin = self._make_byte_spin()
        self.form.addRow("Min", self.clamp_min_spin)
        self.clamp_max_spin = self._make_byte_spin()
        self.form.addRow("Max", self.clamp_max_spin)

        self.threshold_spin = self._make_byte_spin()
        self.form.addRow("Threshold", self.threshold_spin)

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
        self.blend_opacity_spin = QSpinBox()
        self.blend_opacity_spin.setRange(0, 100)
        self.blend_opacity_spin.setKeyboardTracking(False)
        self.blend_opacity_spin.setSuffix("%")
        self.blend_opacity_spin.valueChanged.connect(self._apply_changes)
        self.form.addRow("Opacity", self.blend_opacity_spin)

        self.filename_edit = QLineEdit()
        self.filename_edit.textEdited.connect(self._apply_changes)
        self.form.addRow("Filename", self.filename_edit)

        self.output_path_edit = QLineEdit()
        self.output_path_edit.setPlaceholderText("Optional full output PNG path")
        self.output_path_edit.textEdited.connect(self._apply_changes)
        self.output_path_button = QPushButton("...")
        self.output_path_button.clicked.connect(self._browse_output_path)
        output_path_row = QHBoxLayout()
        output_path_row.setContentsMargins(0, 0, 0, 0)
        output_path_row.addWidget(self.output_path_edit, 1)
        output_path_row.addWidget(self.output_path_button)
        self.output_path_host = QWidget()
        self.output_path_host.setLayout(output_path_row)
        self.form.addRow("Output Path", self.output_path_host)

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
            self.enabled_checkbox.setChecked(bool(node.properties.get("enabled", True)))
            mode_value = str(node.properties.get("mode", OutputMode.RGBA.value))
            self.mode_combo.setCurrentIndex(1)
            for index in range(self.mode_combo.count()):
                if self.mode_combo.itemData(index) == mode_value:
                    self.mode_combo.setCurrentIndex(index)
                    break
            self._sync_visibility(node.node_type)
        finally:
            self._suppress = False

    def _sync_visibility(self, node_type: NodeType) -> None:
        self._set_row_visible(self.node_enabled_checkbox, node_has_enable_flag(node_type))
        self._set_row_visible(self.path_host, node_type is NodeType.TEXTURE_INPUT)
        self._set_row_visible(self.value_spin, node_type is NodeType.CONSTANT_CHANNEL)
        self._set_row_visible(self.level_black_spin, node_type is NodeType.LEVELS_CHANNEL)
        self._set_row_visible(self.level_white_spin, node_type is NodeType.LEVELS_CHANNEL)
        self._set_row_visible(self.level_gamma_spin, node_type is NodeType.LEVELS_CHANNEL)
        self._set_row_visible(self.level_out_min_spin, node_type is NodeType.LEVELS_CHANNEL)
        self._set_row_visible(self.level_out_max_spin, node_type is NodeType.LEVELS_CHANNEL)
        self._set_row_visible(self.clamp_min_spin, node_type is NodeType.CLAMP_CHANNEL)
        self._set_row_visible(self.clamp_max_spin, node_type is NodeType.CLAMP_CHANNEL)
        self._set_row_visible(self.threshold_spin, node_type is NodeType.THRESHOLD_CHANNEL)
        self._set_row_visible(self.blend_mode_combo, node_type is NodeType.BLEND_CHANNEL)
        self._set_row_visible(self.blend_opacity_spin, node_type is NodeType.BLEND_CHANNEL)
        self._set_row_visible(self.filename_edit, node_type is NodeType.OUTPUT_RGBA)
        self._set_row_visible(self.output_path_host, node_type is NodeType.OUTPUT_RGBA)
        self._set_row_visible(self.mode_combo, node_type is NodeType.OUTPUT_RGBA)
        self._set_row_visible(self.enabled_checkbox, node_type is NodeType.OUTPUT_RGBA)

    def _set_row_visible(self, widget: QWidget, visible: bool) -> None:
        widget.setVisible(visible)
        label = self.form.labelForField(widget)
        if label is not None:
            label.setVisible(visible)

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
            "Select output PNG",
            self.output_path_edit.text().strip() or self.filename_edit.text().strip(),
            "PNG (*.png)",
        )
        if path:
            self.output_path_edit.setText(path)
            self._apply_changes()

    def _reset_parameters(self) -> None:
        if self._node is None or not node_has_resettable_parameters(self._node.node_type):
            return
        reset_node_parameters(self._node)
        self.set_node(self._node)
        self.node_changed.emit(False)

    def _apply_changes(self, *_args: object) -> None:
        if self._suppress or self._node is None:
            return
        previous_title = self._node.title
        previous_path = str(self._node.properties.get("path", ""))
        self._node.title = self.title_edit.text().strip() or self._node.title
        if node_has_enable_flag(self._node.node_type):
            self._node.properties["enabled"] = self.node_enabled_checkbox.isChecked()
        if self._node.node_type is NodeType.TEXTURE_INPUT:
            self._node.properties["path"] = self.path_edit.text().strip()
        elif self._node.node_type is NodeType.CONSTANT_CHANNEL:
            self._node.properties["value"] = self.value_spin.value()
        elif self._node.node_type is NodeType.LEVELS_CHANNEL:
            self._node.properties["black"] = self.level_black_spin.value()
            self._node.properties["white"] = self.level_white_spin.value()
            self._node.properties["gamma"] = self.level_gamma_spin.value()
            self._node.properties["out_min"] = self.level_out_min_spin.value()
            self._node.properties["out_max"] = self.level_out_max_spin.value()
        elif self._node.node_type is NodeType.CLAMP_CHANNEL:
            self._node.properties["min"] = self.clamp_min_spin.value()
            self._node.properties["max"] = self.clamp_max_spin.value()
        elif self._node.node_type is NodeType.THRESHOLD_CHANNEL:
            self._node.properties["threshold"] = self.threshold_spin.value()
        elif self._node.node_type is NodeType.BLEND_CHANNEL:
            self._node.properties["mode"] = str(self.blend_mode_combo.currentData() or "multiply")
            self._node.properties["opacity"] = self.blend_opacity_spin.value()
        elif self._node.node_type is NodeType.OUTPUT_RGBA:
            self._node.properties["filename"] = self.filename_edit.text().strip() or "packed.png"
            self._node.properties["output_path"] = self.output_path_edit.text().strip()
            self._node.properties["mode"] = str(self.mode_combo.currentData() or OutputMode.RGBA.value)
            self._node.properties["enabled"] = self.enabled_checkbox.isChecked()
        needs_rebuild = previous_title != self._node.title
        if self._node.node_type is NodeType.TEXTURE_INPUT:
            needs_rebuild = needs_rebuild or previous_path != str(self._node.properties.get("path", ""))
        self.node_changed.emit(needs_rebuild)

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


class GraphWorkspace(QWidget):
    export_requested = pyqtSignal()
    preview_image_requested = pyqtSignal(object, str, str)
    status_message = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.project = NodeGraphProject()
        self.project_dir: Path | None = None
        self._assets: list[QueueItem] = []
        self._repository = NodeGraphProjectRepository()
        self._executor = NodeGraphExecutor()
        self._preview_cache = NodeGraphPreviewCache(max_side=1024)
        self._scene = GraphScene(self.project, self)
        self._scene.graph_changed.connect(self._on_graph_changed)
        self._scene.node_display_flag_clicked.connect(self._on_display_flag_clicked)
        self._scene.node_double_clicked.connect(self._on_node_double_clicked)
        self._scene.node_enable_flag_clicked.connect(self._on_enable_flag_clicked)
        self._scene.node_render_flag_clicked.connect(self._on_render_flag_clicked)
        self._scene.node_reset_clicked.connect(self._on_node_reset_clicked)
        self._scene.node_selection_changed.connect(self._on_node_selected)
        self._scene.status_message.connect(self.status_message.emit)
        self._build_ui()
        self._scene.rebuild()

    def _build_ui(self) -> None:
        self.setObjectName("SectionPanel")
        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(10, 10, 10, 10)
        root_layout.setSpacing(8)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(6)
        self.project_label = QLabel("Untitled Graph")
        self.project_label.setObjectName("PanelTitle")
        toolbar.addWidget(self.project_label)

        menu_hint = QLabel("Right-click graph to add nodes")
        menu_hint.setObjectName("SummaryText")
        toolbar.addWidget(menu_hint)

        self.delete_button = QPushButton("Delete")
        self.delete_button.setObjectName("DangerButton")
        self.delete_button.clicked.connect(self._scene.delete_selected)
        toolbar.addWidget(self.delete_button)
        toolbar.addStretch(1)
        self.new_button = QPushButton("New")
        self.new_button.clicked.connect(self.new_project)
        toolbar.addWidget(self.new_button)
        self.load_button = QPushButton("Load")
        self.load_button.clicked.connect(self.load_project_dialog)
        toolbar.addWidget(self.load_button)
        self.save_button = QPushButton("Save")
        self.save_button.clicked.connect(self.save_project_dialog)
        toolbar.addWidget(self.save_button)
        self.fit_button = QPushButton("Fit")
        self.fit_button.clicked.connect(lambda: self.view.fit_graph())
        toolbar.addWidget(self.fit_button)
        self.export_button = QPushButton("Export Graph")
        self.export_button.setObjectName("PrimaryButton")
        self.export_button.clicked.connect(self.export_requested.emit)
        toolbar.addWidget(self.export_button)
        root_layout.addLayout(toolbar)

        self.view = GraphView(self._scene)
        self.view.texture_dropped.connect(self.add_texture_node_for_path)
        self.view.node_add_requested.connect(self.add_node_of_type)
        self.properties_panel = NodePropertiesPanel()
        self.properties_panel.node_changed.connect(self._rebuild_after_property_change)

        self.result_table = QTableWidget()
        self.result_table.setColumnCount(4)
        self.result_table.setHorizontalHeaderLabels(("Output", "Status", "Destination", "Message"))
        self.result_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.result_table.verticalHeader().setVisible(False)
        self.result_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.result_table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.result_table.horizontalHeader().setStretchLastSection(True)
        self.result_table.setMaximumHeight(150)
        self.result_table.setMinimumWidth(0)

        root_layout.addWidget(self.view, 1)

    def build_properties_widget(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("SidebarPanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)
        layout.addWidget(self.properties_panel, 1)
        layout.addWidget(QLabel("Graph Export Queue"))
        layout.addWidget(self.result_table)
        return panel

    def set_assets(self, items: list[QueueItem]) -> None:
        self._assets = [item for item in items if item.metadata is not None]

    def new_project(self) -> None:
        self.project = NodeGraphProject()
        self.project_dir = None
        self._preview_cache.clear()
        self._scene.project = self.project
        self._scene.rebuild()
        self.project_label.setText(self.project.name)
        self.result_table.setRowCount(0)
        self.status_message.emit("New graph project.")

    def load_project_dialog(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Load .texturegraph project")
        if not path:
            return
        try:
            self.project = self._repository.load(Path(path))
        except Exception as exc:
            self.status_message.emit(f"Graph load failed: {exc}")
            return
        self.project_dir = Path(path)
        self._preview_cache.clear()
        self._scene.project = self.project
        self._scene.rebuild()
        self.project_label.setText(self.project.name)
        self.status_message.emit(f"Loaded graph: {self.project.name}")

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
        self.project_label.setText(self.project.name)
        self.status_message.emit(f"Saved graph: {bundle_dir}")

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
        self._scene.add_node(
            create_graph_node(
                NodeType.TEXTURE_INPUT,
                title=name,
                position=position,
                properties={"path": str(path)},
            )
        )

    def add_node_of_type(self, node_type: object, scene_position: object | None = None) -> None:
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

        self._scene.add_node(
            create_graph_node(
                resolved_type,
                title=title,
                position=position,
                properties=properties,
            )
        )

    def add_constant_node(self) -> None:
        self.add_node_of_type(NodeType.CONSTANT_CHANNEL)

    def add_invert_node(self) -> None:
        self.add_node_of_type(NodeType.INVERT_CHANNEL)

    def add_view_node(self) -> None:
        self.add_node_of_type(NodeType.VIEW)

    def add_output_node(self) -> None:
        self.add_node_of_type(NodeType.OUTPUT_RGBA)

    def export_graph(
        self,
        output_root: Path,
        options: ConversionOptions,
        logger,
    ) -> GraphExportSummary:
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

    def _next_node_position(self) -> tuple[float, float]:
        view_center = self.view.mapToScene(self.view.viewport().rect().center())
        offset = 28 * len(self.project.graph.nodes)
        return (view_center.x() + offset, view_center.y() + offset)

    def _node_position_for(self, scene_position: object | None) -> tuple[float, float]:
        if isinstance(scene_position, QPointF):
            return (scene_position.x(), scene_position.y())
        return self._next_node_position()

    def _on_graph_changed(self) -> None:
        self._preview_cache.clear()
        selected_node = self._scene.selected_node()
        self.properties_panel.set_node(selected_node)
        self._preview_active_display_node()

    def _on_node_selected(self, node: GraphNode | None) -> None:
        self.properties_panel.set_node(node)
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
        for graph_node in self.project.graph.nodes:
            graph_node.properties["display"] = graph_node.node_id == node.node_id
        self._refresh_node_flags()
        self._preview_display_node(node)

    def _on_render_flag_clicked(self, node: GraphNode | None) -> None:
        if node is None or node.node_type is not NodeType.OUTPUT_RGBA:
            return
        node.properties["enabled"] = not bool(node.properties.get("enabled", True))
        self._refresh_node_flags()
        if self.properties_panel._node is node:
            self.properties_panel.set_node(node)
        state = "on" if node.properties.get("enabled", True) else "off"
        self.status_message.emit(f"{node.title}: render flag {state}.")

    def _on_enable_flag_clicked(self, node: GraphNode | None) -> None:
        if node is None or not node_has_enable_flag(node.node_type):
            return
        node.properties["enabled"] = not bool(node.properties.get("enabled", True))
        self._invalidate_preview_from_node(node)
        self._refresh_node_flags()
        if self.properties_panel._node is node:
            self.properties_panel.set_node(node)
        self._preview_active_display_node()
        state = "enabled" if node.properties.get("enabled", True) else "bypassed"
        self.status_message.emit(f"{node.title}: {state}.")

    def _on_node_reset_clicked(self, node: GraphNode | None) -> None:
        if node is None or not node_has_resettable_parameters(node.node_type):
            return
        reset_node_parameters(node)
        self._invalidate_preview_from_node(node)
        if self.properties_panel._node is node:
            self.properties_panel.set_node(node)
        self._refresh_node_flags()
        self._preview_active_display_node()
        self.status_message.emit(f"{node.title}: parameters reset.")

    def _rebuild_after_property_change(self, needs_rebuild: bool = False) -> None:
        selected_node = self.properties_panel._node
        selected_id = selected_node.node_id if selected_node is not None else None
        if selected_node is not None:
            self._invalidate_preview_from_node(selected_node)
        if not needs_rebuild:
            self._refresh_node_flags()
            self._preview_active_display_node()
            return
        self._scene.rebuild()
        if selected_id is not None:
            item = self._scene.node_items.get(selected_id)
            if item is not None:
                item.setSelected(True)
        self._preview_active_display_node()

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
        try:
            preview_image, meta = self._executor.render_display_node(
                self.project.graph,
                node,
                preview_cache=self._preview_cache,
            )
        except (GraphExecutionError, OSError, ValueError) as exc:
            self.status_message.emit(f"{node.title}: display failed: {exc}")
            return
        self.preview_image_requested.emit(preview_image, node.title, meta)

    def _preview_view_node(self, node: GraphNode | None) -> None:
        if node is None or node.node_type is not NodeType.VIEW:
            return
        if incoming_connection(
            self.project.graph,
            target_node_id=node.node_id,
            target_socket_id="in",
        ) is None:
            return
        try:
            preview_image = self._executor.render_view_node(
                self.project.graph,
                node,
                preview_cache=self._preview_cache,
            )
        except (GraphExecutionError, OSError, ValueError) as exc:
            self.status_message.emit(f"{node.title}: {exc}")
            return
        meta = f"Graph channel preview · {preview_image.width}x{preview_image.height} · {preview_image.mode}"
        self.preview_image_requested.emit(preview_image, node.title, meta)

    def _preview_output_node(self, node: GraphNode) -> None:
        try:
            preview_image, _meta = self._executor.render_display_node(
                self.project.graph,
                node,
                preview_cache=self._preview_cache,
            )
        except (GraphExecutionError, OSError, ValueError) as exc:
            self.status_message.emit(f"{node.title}: preview failed: {exc}")
            return

        mode = self._output_mode_label(node)
        filename = str(node.properties.get("filename", "")).strip()
        if node.properties.get("output_path"):
            filename = Path(str(node.properties.get("output_path"))).name
        suffix = f" · {filename}" if filename else ""
        meta = f"Output preview · {preview_image.width}x{preview_image.height} · {mode}{suffix}"
        self.preview_image_requested.emit(preview_image, node.title, meta)

    @staticmethod
    def _output_mode_label(node: GraphNode) -> str:
        try:
            return OutputMode(str(node.properties.get("mode", OutputMode.RGBA.value))).value.upper()
        except ValueError:
            return OutputMode.RGBA.value.upper()

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
