from __future__ import annotations

from PyQt6.QtCore import QPoint, QPointF, QRect, QSize, Qt, pyqtSignal
from PyQt6.QtGui import (
    QColor,
    QCloseEvent,
    QGuiApplication,
    QKeySequence,
    QPainter,
    QPixmap,
    QShortcut,
    QWheelEvent,
)
from PyQt6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from image_converter.domain.models import PreviewChannel, QueueItem, QueueStatus
from image_converter.services.colorspace import recommended_colorspace_for_map_type
from image_converter.services.preview import TexturePreviewService
from image_converter.ui.common import (
    _colorspace_label,
    _map_type_label,
    _preview_channel_label,
    _qimage_from_pil,
)


class PreviewCanvas(QFrame):
    zoom_changed = pyqtSignal(float)
    stage_rect_changed = pyqtSignal(QRect)
    detach_requested = pyqtSignal()

    def __init__(self, parent: QWidget | None = None, *, square_stage: bool = True):
        super().__init__(parent)
        self._source_pixmap: QPixmap | None = None
        self._placeholder_text = "Выберите текстуру из очереди, чтобы открыть preview."
        self._zoom_factor = 1.0
        self._padding = 14
        self._min_zoom = 0.5
        self._max_zoom = 8.0
        self._pan_offset = QPointF(0.0, 0.0)
        self._drag_origin: QPoint | None = None
        self._drag_offset_origin = QPointF(0.0, 0.0)
        self._detach_on_double_click = False
        self._square_stage = square_stage
        self.setMinimumSize(360, 360 if square_stage else 240)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMouseTracking(True)

    def hasHeightForWidth(self) -> bool:
        return self._square_stage

    def heightForWidth(self, width: int) -> int:
        if self._square_stage:
            return width
        return super().heightForWidth(width)

    def sizeHint(self) -> QSize:
        return QSize(460, 460) if self._square_stage else QSize(920, 640)

    def set_detach_on_double_click(self, enabled: bool) -> None:
        self._detach_on_double_click = enabled

    def set_preview_pixmap(self, pixmap: QPixmap, *, preserve_zoom: bool = True) -> None:
        self._source_pixmap = pixmap
        if not preserve_zoom:
            self._zoom_factor = 1.0
            self._pan_offset = QPointF(0.0, 0.0)
        self._normalize_pan_offset()
        self._update_cursor()
        self._emit_zoom_changed()
        self.update()

    def clear_preview(self, text: str = "Выберите текстуру из очереди, чтобы открыть preview.") -> None:
        self._source_pixmap = None
        self._placeholder_text = text
        self._zoom_factor = 1.0
        self._pan_offset = QPointF(0.0, 0.0)
        self._drag_origin = None
        self._update_cursor()
        self._emit_zoom_changed()
        self.update()

    def reset_zoom(self) -> None:
        self._zoom_factor = 1.0
        self._pan_offset = QPointF(0.0, 0.0)
        self._update_cursor()
        self._emit_zoom_changed()
        self.update()

    def change_zoom(self, steps: float) -> None:
        if self._source_pixmap is None or steps == 0:
            return

        scale_step = 1.15 ** steps
        next_zoom = max(self._min_zoom, min(self._max_zoom, self._zoom_factor * scale_step))
        if abs(next_zoom - self._zoom_factor) < 0.001:
            return

        self._zoom_factor = next_zoom
        if self._zoom_factor <= 1.0:
            self._pan_offset = QPointF(0.0, 0.0)
        self._normalize_pan_offset()
        self._update_cursor()
        self._emit_zoom_changed()
        self.update()

    def wheelEvent(self, event: QWheelEvent) -> None:
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.change_zoom(event.angleDelta().y() / 120.0)
            event.accept()
            return
        super().wheelEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._source_pixmap is not None:
            if self._detach_on_double_click:
                self.detach_requested.emit()
                event.accept()
                return
            self.reset_zoom()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.MiddleButton and self._can_pan():
            self._drag_origin = event.position().toPoint()
            self._drag_offset_origin = QPointF(self._pan_offset)
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._drag_origin is not None and self._source_pixmap is not None:
            delta = event.position().toPoint() - self._drag_origin
            self._pan_offset = QPointF(
                self._drag_offset_origin.x() + delta.x(),
                self._drag_offset_origin.y() + delta.y(),
            )
            self._normalize_pan_offset()
            self.update()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.MiddleButton and self._drag_origin is not None:
            self._drag_origin = None
            self._update_cursor()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._normalize_pan_offset()
        self._update_cursor()
        self.stage_rect_changed.emit(self.stage_rect())

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        frame_rect = self.stage_rect()
        painter.setPen(QColor("#31404D"))
        painter.setBrush(QColor("#1C232B"))
        painter.drawRoundedRect(frame_rect, 18, 18)

        viewport_rect = self._viewport_rect()
        if viewport_rect.width() <= 0 or viewport_rect.height() <= 0:
            return

        self._draw_checkerboard(painter, viewport_rect)

        if self._source_pixmap is None:
            painter.setPen(QColor("#8F9CAA"))
            painter.drawText(
                viewport_rect.adjusted(20, 20, -20, -20),
                Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
                self._placeholder_text,
            )
            return

        target_width, target_height = self._scaled_target_size(viewport_rect)
        scaled = self._source_pixmap.scaled(
            target_width,
            target_height,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )

        x = viewport_rect.center().x() - scaled.width() // 2 + round(self._pan_offset.x())
        y = viewport_rect.center().y() - scaled.height() // 2 + round(self._pan_offset.y())
        painter.save()
        painter.setClipRect(viewport_rect)
        painter.drawPixmap(x, y, scaled)
        painter.restore()

    def _square_stage_rect(self, frame_rect: QRect) -> QRect:
        side = max(0, min(frame_rect.width(), frame_rect.height()))
        x = frame_rect.x() + (frame_rect.width() - side) // 2
        y = frame_rect.y() + (frame_rect.height() - side) // 2
        return QRect(x, y, side, side)

    def stage_rect(self) -> QRect:
        frame_rect = self.rect().adjusted(1, 1, -1, -1)
        if self._square_stage:
            return self._square_stage_rect(frame_rect)
        return frame_rect

    def _viewport_rect(self) -> QRect:
        return self.stage_rect().adjusted(
            self._padding,
            self._padding,
            -self._padding,
            -self._padding,
        )

    def _draw_checkerboard(self, painter: QPainter, rect: QRect) -> None:
        tile_size = 18
        light_color = QColor("#34414C")
        dark_color = QColor("#29333D")

        for top in range(rect.top(), rect.bottom() + 1, tile_size):
            row_index = (top - rect.top()) // tile_size
            for left in range(rect.left(), rect.right() + 1, tile_size):
                column_index = (left - rect.left()) // tile_size
                color = light_color if (row_index + column_index) % 2 == 0 else dark_color
                painter.fillRect(left, top, tile_size, tile_size, color)

    def _emit_zoom_changed(self) -> None:
        self.zoom_changed.emit(self._zoom_factor)

    def _scaled_target_size(self, viewport_rect: QRect) -> tuple[int, int]:
        if self._source_pixmap is None:
            return (0, 0)

        base_scale = min(
            viewport_rect.width() / max(self._source_pixmap.width(), 1),
            viewport_rect.height() / max(self._source_pixmap.height(), 1),
        )
        target_width = max(1, round(self._source_pixmap.width() * base_scale * self._zoom_factor))
        target_height = max(1, round(self._source_pixmap.height() * base_scale * self._zoom_factor))
        return (target_width, target_height)

    def _can_pan(self) -> bool:
        if self._source_pixmap is None or self._zoom_factor <= 1.0:
            return False

        viewport_rect = self._viewport_rect()
        target_width, target_height = self._scaled_target_size(viewport_rect)
        return target_width > viewport_rect.width() or target_height > viewport_rect.height()

    def _normalize_pan_offset(self) -> None:
        if self._source_pixmap is None:
            self._pan_offset = QPointF(0.0, 0.0)
            return

        viewport_rect = self._viewport_rect()
        if viewport_rect.width() <= 0 or viewport_rect.height() <= 0:
            self._pan_offset = QPointF(0.0, 0.0)
            return

        target_width, target_height = self._scaled_target_size(viewport_rect)
        max_offset_x = max(0.0, (target_width - viewport_rect.width()) / 2)
        max_offset_y = max(0.0, (target_height - viewport_rect.height()) / 2)
        self._pan_offset = QPointF(
            min(max(self._pan_offset.x(), -max_offset_x), max_offset_x),
            min(max(self._pan_offset.y(), -max_offset_y), max_offset_y),
        )

    def _update_cursor(self) -> None:
        if self._drag_origin is not None:
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            return
        if self._can_pan():
            self.setCursor(Qt.CursorShape.OpenHandCursor)
            return
        self.unsetCursor()


class PreviewPanel(QWidget):
    def __init__(self, parent: QWidget | None = None, *, allow_detach: bool = True):
        super().__init__(parent)
        self._preview_service = TexturePreviewService()
        self._current_item: QueueItem | None = None
        self._selected_channel = PreviewChannel.COMPOSITE
        self._channel_buttons: dict[PreviewChannel, QToolButton] = {}
        self._allow_detach = allow_detach
        self._detached_window: DetachedPreviewWindow | None = None
        self._build_ui()

    def _build_ui(self) -> None:
        self.setObjectName("SectionPanel")
        self.setMinimumHeight(220)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 12)
        layout.setSpacing(8)

        title_label = QLabel("Texture Preview")
        title_label.setObjectName("PanelTitle")
        layout.addWidget(title_label)

        self.preview_canvas = PreviewCanvas()
        self.preview_canvas.zoom_changed.connect(self._update_zoom_label)
        self.preview_canvas.stage_rect_changed.connect(self._layout_canvas_controls)
        self.preview_canvas.set_detach_on_double_click(self._allow_detach)
        if self._allow_detach:
            self.preview_canvas.detach_requested.connect(self._open_detached_preview)
            self.preview_canvas.setToolTip(
                "Double-click: открыть текстуру в отдельном окне.\nCtrl + wheel: zoom, MMB: pan, F: fit."
            )
        else:
            self.preview_canvas.setToolTip("Ctrl + wheel: zoom, MMB: pan, F: fit.")
        layout.addWidget(self.preview_canvas, 1)
        self._build_canvas_controls()

        self.asset_label = QLabel("Ничего не выбрано")
        self.asset_label.setObjectName("PreviewFileName")
        self.asset_label.setWordWrap(False)
        layout.addWidget(self.asset_label)

        self.asset_meta_label = QLabel("Выберите строку в очереди, чтобы открыть превью текстуры.")
        self.asset_meta_label.setObjectName("PreviewMetaText")
        self.asset_meta_label.setWordWrap(False)
        layout.addWidget(self.asset_meta_label)

        self.fit_shortcut = QShortcut(QKeySequence("F"), self)
        self.fit_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self.fit_shortcut.activated.connect(self.preview_reset_requested)

    def _build_canvas_controls(self) -> None:
        self.fit_overlay_button = QPushButton("F", self.preview_canvas)
        self.fit_overlay_button.setObjectName("CanvasControlButton")
        self.fit_overlay_button.setToolTip("Fit: сбросить масштаб до 100%")
        self.fit_overlay_button.clicked.connect(self.preview_reset_requested)

        self.zoom_label = QLabel("100%", self.preview_canvas)
        self.zoom_label.setObjectName("CanvasBadge")

        self.channel_host = QWidget(self.preview_canvas)
        channel_layout = QHBoxLayout(self.channel_host)
        channel_layout.setContentsMargins(0, 0, 0, 0)
        channel_layout.setSpacing(4)

        self.channel_group = QButtonGroup(self)
        self.channel_group.setExclusive(True)
        channel_specs = (
            (PreviewChannel.COMPOSITE, "RGB"),
            (PreviewChannel.RED, "R"),
            (PreviewChannel.GREEN, "G"),
            (PreviewChannel.BLUE, "B"),
            (PreviewChannel.ALPHA, "A"),
        )
        for channel, label in channel_specs:
            button = QToolButton(self.channel_host)
            button.setObjectName("ChannelChip")
            button.setText(label)
            button.setCheckable(True)
            button.clicked.connect(lambda _checked=False, current=channel: self._set_selected_channel(current))
            self.channel_group.addButton(button)
            channel_layout.addWidget(button)
            self._channel_buttons[channel] = button

        self._sync_channel_buttons([])
        self._layout_canvas_controls(self.preview_canvas.stage_rect())

    def preview_reset_requested(self) -> None:
        self.preview_canvas.reset_zoom()

    def set_selected_channel(self, channel: PreviewChannel) -> None:
        self._set_selected_channel(channel)

    def set_queue_item(self, item: QueueItem | None) -> None:
        previous_path = self._current_item.path if self._current_item is not None else None
        next_path = item.path if item is not None else None
        self._current_item = item
        self._rebuild_channels(item)
        if previous_path != next_path:
            self.preview_canvas.reset_zoom()
        self._refresh_preview()
        if self._allow_detach and self._detached_window is not None:
            self._detached_window.set_queue_item(item)

    def _rebuild_channels(self, item: QueueItem | None) -> None:
        channels = self._available_channels(item)
        if self._selected_channel not in channels:
            self._selected_channel = PreviewChannel.COMPOSITE if PreviewChannel.COMPOSITE in channels else (
                channels[0] if channels else PreviewChannel.COMPOSITE
            )
        self._sync_channel_buttons(channels)

    def _available_channels(self, item: QueueItem | None) -> list[PreviewChannel]:
        if item is None or item.metadata is None or item.status is QueueStatus.ERROR:
            return []

        channels = [
            PreviewChannel.COMPOSITE,
            PreviewChannel.RED,
            PreviewChannel.GREEN,
            PreviewChannel.BLUE,
        ]
        if item.metadata.has_alpha:
            channels.append(PreviewChannel.ALPHA)
        return channels

    def _refresh_preview(self) -> None:
        item = self._current_item
        if item is None:
            self.preview_canvas.clear_preview()
            self._update_footer(item)
            self._sync_control_state(False)
            return

        if item.status is QueueStatus.ERROR:
            self.preview_canvas.clear_preview("Предпросмотр недоступен для поврежденного или неподдерживаемого файла.")
            self._update_footer(item)
            self._sync_control_state(False)
            return

        if not self._available_channels(item):
            self.preview_canvas.clear_preview("Нет доступных каналов для предпросмотра.")
            self._update_footer(item)
            self._sync_control_state(False)
            return

        try:
            preview_image = self._preview_service.render(item.path, self._selected_channel)
            preview_pixmap = QPixmap.fromImage(_qimage_from_pil(preview_image))
            self.preview_canvas.set_preview_pixmap(preview_pixmap, preserve_zoom=True)
        except Exception as exc:
            self.preview_canvas.clear_preview(f"Ошибка предпросмотра: {exc}")
            self._sync_control_state(False)
            self._update_footer(item)
            return

        self._sync_control_state(True)
        self._update_footer(item)

    def _update_footer(self, item: QueueItem | None) -> None:
        if item is None:
            self.asset_label.setText("Ничего не выбрано")
            self.asset_meta_label.setText("Выберите строку в очереди, чтобы открыть превью текстуры.")
            return

        self.asset_label.setText(item.path.name)
        self.asset_label.setToolTip(str(item.path))
        metadata = item.metadata
        if metadata is None:
            self.asset_meta_label.setText(item.message or "Метаданные недоступны.")
            return

        channel_name = _preview_channel_label(self._selected_channel)
        alpha_state = "alpha" if metadata.has_alpha else "opaque"
        map_type_name = _map_type_label(item.effective_map_type)
        colorspace_name = _colorspace_label(
            recommended_colorspace_for_map_type(item.effective_map_type)
        )
        self.asset_meta_label.setText(
            f"{map_type_name} · {colorspace_name} · {metadata.resolution_text} · {metadata.mode} · канал: {channel_name} · {alpha_state}"
        )
        self.asset_meta_label.setToolTip(self.asset_meta_label.text())

    def _update_zoom_label(self, zoom_factor: float) -> None:
        self.zoom_label.setText(f"{round(zoom_factor * 100)}%")
        self.zoom_label.adjustSize()
        self._layout_canvas_controls(self.preview_canvas.stage_rect())

    def _set_selected_channel(self, channel: PreviewChannel) -> None:
        if channel == self._selected_channel:
            return
        self._selected_channel = channel
        self._sync_channel_buttons(self._available_channels(self._current_item))
        self._refresh_preview()

    def _sync_channel_buttons(self, channels: list[PreviewChannel]) -> None:
        for channel, button in self._channel_buttons.items():
            available = channel in channels
            button.setVisible(available)
            button.setEnabled(available)
            button.blockSignals(True)
            button.setChecked(available and channel == self._selected_channel)
            button.blockSignals(False)

        self.channel_host.setVisible(bool(channels))
        self.fit_overlay_button.setEnabled(bool(channels))
        self._layout_canvas_controls(self.preview_canvas.stage_rect())

    def _sync_control_state(self, enabled: bool) -> None:
        self.fit_overlay_button.setEnabled(enabled)
        if not enabled:
            self.preview_canvas.reset_zoom()

    def _open_detached_preview(self) -> None:
        item = self._current_item
        if item is None or item.status is QueueStatus.ERROR:
            return

        if self._detached_window is None:
            self._detached_window = DetachedPreviewWindow()

        self._detached_window.show_for_item(item, self._selected_channel)

    def _layout_canvas_controls(self, stage_rect: QRect) -> None:
        if stage_rect.width() <= 0 or stage_rect.height() <= 0:
            return

        margin = 14
        top = stage_rect.top() + margin
        left = stage_rect.left() + margin

        self.fit_overlay_button.adjustSize()
        fit_height = 30
        fit_width = max(32, self.fit_overlay_button.sizeHint().width())
        self.fit_overlay_button.resize(fit_width, fit_height)
        self.fit_overlay_button.move(left, top)

        self.zoom_label.adjustSize()
        zoom_x = self.fit_overlay_button.x() + self.fit_overlay_button.width() + 8
        zoom_y = top + max(0, (fit_height - self.zoom_label.height()) // 2)
        self.zoom_label.move(zoom_x, zoom_y)

        self.channel_host.adjustSize()
        channel_x = stage_rect.right() - margin - self.channel_host.width()
        channel_y = top
        self.channel_host.move(max(left, channel_x), channel_y)


class DetachedPreviewWindow(QWidget):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self.setWindowFlag(Qt.WindowType.Window, True)
        self.setWindowTitle("Texture Preview")
        self.resize(1180, 820)
        self.setMinimumSize(720, 560)
        self._current_item: QueueItem | None = None
        self._selected_channel = PreviewChannel.COMPOSITE
        self._preview_service = TexturePreviewService()
        self._channel_buttons: dict[PreviewChannel, QToolButton] = {}
        self._build_ui()

    def _build_ui(self) -> None:
        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(18, 18, 18, 18)
        root_layout.setSpacing(12)

        header_card = QFrame()
        header_card.setObjectName("ViewerHeaderCard")
        header_layout = QVBoxLayout(header_card)
        header_layout.setContentsMargins(18, 16, 18, 16)
        header_layout.setSpacing(6)

        self.asset_name_label = QLabel("Texture Preview")
        self.asset_name_label.setObjectName("AssetName")
        self.asset_name_label.setWordWrap(True)
        header_layout.addWidget(self.asset_name_label)

        self.asset_meta_label = QLabel(
            "Двойной клик по строке открывает этот viewer. Дальше он синхронизируется с текущим выбором."
        )
        self.asset_meta_label.setObjectName("ViewerMetaText")
        self.asset_meta_label.setWordWrap(True)
        header_layout.addWidget(self.asset_meta_label)
        root_layout.addWidget(header_card)

        canvas_card = QFrame()
        canvas_card.setObjectName("ViewerCanvasCard")
        canvas_layout = QVBoxLayout(canvas_card)
        canvas_layout.setContentsMargins(14, 14, 14, 14)
        canvas_layout.setSpacing(0)

        self.preview_canvas = PreviewCanvas(square_stage=False)
        self.preview_canvas.set_detach_on_double_click(False)
        self.preview_canvas.setMinimumSize(520, 360)
        self.preview_canvas.zoom_changed.connect(self._update_zoom_label)
        canvas_layout.addWidget(self.preview_canvas, 1)
        root_layout.addWidget(canvas_card, 1)

        toolbar_card = QFrame()
        toolbar_card.setObjectName("ViewerToolbarCard")
        toolbar_layout = QHBoxLayout(toolbar_card)
        toolbar_layout.setContentsMargins(14, 12, 14, 12)
        toolbar_layout.setSpacing(10)

        self.channel_group = QButtonGroup(self)
        self.channel_group.setExclusive(True)
        channel_specs = (
            (PreviewChannel.COMPOSITE, "RGB"),
            (PreviewChannel.RED, "R"),
            (PreviewChannel.GREEN, "G"),
            (PreviewChannel.BLUE, "B"),
            (PreviewChannel.ALPHA, "A"),
        )
        for channel, label in channel_specs:
            button = QToolButton(toolbar_card)
            button.setObjectName("ChannelChip")
            button.setText(label)
            button.setCheckable(True)
            button.clicked.connect(lambda _checked=False, current=channel: self._set_selected_channel(current))
            self.channel_group.addButton(button)
            toolbar_layout.addWidget(button)
            self._channel_buttons[channel] = button

        toolbar_layout.addSpacing(8)

        self.fit_button = QPushButton("F")
        self.fit_button.setObjectName("CanvasControlButton")
        self.fit_button.setToolTip("Fit: сбросить масштаб до 100%")
        self.fit_button.clicked.connect(self.preview_canvas.reset_zoom)
        toolbar_layout.addWidget(self.fit_button)

        self.zoom_label = QLabel("100%")
        self.zoom_label.setObjectName("CanvasBadge")
        toolbar_layout.addWidget(self.zoom_label)

        toolbar_layout.addStretch(1)

        self.hint_label = QLabel("Ctrl + wheel: zoom  •  MMB: pan  •  F: fit  •  Esc: close")
        self.hint_label.setObjectName("ViewerHintText")
        toolbar_layout.addWidget(self.hint_label)

        self.close_button = QPushButton("Закрыть")
        self.close_button.setObjectName("GhostButton")
        self.close_button.clicked.connect(self.hide)
        toolbar_layout.addWidget(self.close_button)
        root_layout.addWidget(toolbar_card)

        self.fit_shortcut = QShortcut(QKeySequence("F"), self)
        self.fit_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self.fit_shortcut.activated.connect(self.preview_canvas.reset_zoom)

        self.close_shortcut = QShortcut(QKeySequence("Esc"), self)
        self.close_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self.close_shortcut.activated.connect(self.hide)
        self._sync_channel_buttons([])

    def show_for_item(self, item: QueueItem, channel: PreviewChannel | None = None) -> None:
        if channel is not None:
            self._selected_channel = channel
        self.set_queue_item(item)
        self._ensure_visible_geometry()
        self.show()
        self.raise_()
        self.activateWindow()

    def set_queue_item(self, item: QueueItem | None) -> None:
        previous_path = self._current_item.path if self._current_item is not None else None
        next_path = item.path if item is not None else None
        self._current_item = item
        self._rebuild_channels(item)
        if previous_path != next_path:
            self.preview_canvas.reset_zoom()
        self._refresh_preview()

    def _rebuild_channels(self, item: QueueItem | None) -> None:
        channels = self._available_channels(item)
        if self._selected_channel not in channels:
            self._selected_channel = PreviewChannel.COMPOSITE if PreviewChannel.COMPOSITE in channels else (
                channels[0] if channels else PreviewChannel.COMPOSITE
            )
        self._sync_channel_buttons(channels)

    def _available_channels(self, item: QueueItem | None) -> list[PreviewChannel]:
        if item is None or item.metadata is None or item.status is QueueStatus.ERROR:
            return []

        channels = [
            PreviewChannel.COMPOSITE,
            PreviewChannel.RED,
            PreviewChannel.GREEN,
            PreviewChannel.BLUE,
        ]
        if item.metadata.has_alpha:
            channels.append(PreviewChannel.ALPHA)
        return channels

    def _refresh_preview(self) -> None:
        item = self._current_item
        if item is None:
            self.setWindowTitle("Texture Preview")
            self.asset_name_label.setText("Texture Preview")
            self.asset_meta_label.setText(
                "Двойной клик по строке открывает viewer. Выбор в очереди синхронизирует активную текстуру."
            )
            self.preview_canvas.clear_preview("Выберите ассет в очереди, чтобы открыть texture viewer.")
            self._sync_control_state(False)
            return

        self.setWindowTitle(f"Texture Preview - {item.path.name}")
        if item.status is QueueStatus.ERROR:
            self.asset_name_label.setText(item.path.name)
            self.asset_meta_label.setText(item.message or "Файл поврежден или не читается.")
            self.preview_canvas.clear_preview("Предпросмотр недоступен для поврежденного или неподдерживаемого файла.")
            self._sync_control_state(False)
            return

        try:
            preview_image = self._preview_service.render(item.path, self._selected_channel, max_size=None)
            preview_pixmap = QPixmap.fromImage(_qimage_from_pil(preview_image))
            self.preview_canvas.set_preview_pixmap(preview_pixmap, preserve_zoom=True)
        except Exception as exc:
            self.asset_name_label.setText(item.path.name)
            self.asset_meta_label.setText(f"Не удалось отрисовать preview: {exc}")
            self.preview_canvas.clear_preview(f"Ошибка предпросмотра: {exc}")
            self._sync_control_state(False)
            return

        metadata = item.metadata
        if metadata is None:
            self.asset_name_label.setText(item.path.name)
            self.asset_meta_label.setText("Метаданные недоступны.")
            self._sync_control_state(True)
            return

        map_type_name = _map_type_label(item.effective_map_type)
        colorspace_name = _colorspace_label(recommended_colorspace_for_map_type(item.effective_map_type))
        channel_name = _preview_channel_label(self._selected_channel)
        alpha_state = "alpha" if metadata.has_alpha else "opaque"
        self.asset_name_label.setText(item.path.name)
        self.asset_meta_label.setText(
            f"{map_type_name}  •  {colorspace_name}  •  {metadata.resolution_text}  •  {metadata.mode}  •  канал: {channel_name}  •  {alpha_state}"
        )
        self._sync_control_state(True)

    def _sync_channel_buttons(self, channels: list[PreviewChannel]) -> None:
        for channel, button in self._channel_buttons.items():
            available = channel in channels
            button.setVisible(available)
            button.setEnabled(available)
            button.blockSignals(True)
            button.setChecked(available and channel == self._selected_channel)
            button.blockSignals(False)

    def _sync_control_state(self, enabled: bool) -> None:
        self.fit_button.setEnabled(enabled)
        if enabled:
            self._sync_channel_buttons(self._available_channels(self._current_item))
        else:
            for button in self._channel_buttons.values():
                button.setEnabled(False)
        if not enabled:
            self.preview_canvas.reset_zoom()

    def _set_selected_channel(self, channel: PreviewChannel) -> None:
        if channel == self._selected_channel:
            return
        self._selected_channel = channel
        self._refresh_preview()
        self._sync_channel_buttons(self._available_channels(self._current_item))

    def _update_zoom_label(self, zoom_factor: float) -> None:
        self.zoom_label.setText(f"{round(zoom_factor * 100)}%")

    def _ensure_visible_geometry(self) -> None:
        screen = self.screen() or QGuiApplication.primaryScreen()
        if screen is None:
            return

        available = screen.availableGeometry().adjusted(20, 20, -20, -20)
        width = min(max(self.width(), 860), available.width())
        height = min(max(self.height(), 620), available.height())
        self.resize(width, height)

        geometry = self.frameGeometry()
        if not available.intersects(geometry) or not self.isVisible():
            x = available.left() + (available.width() - geometry.width()) // 2
            y = available.top() + (available.height() - geometry.height()) // 2
        else:
            x = min(max(geometry.x(), available.left()), max(available.left(), available.right() - geometry.width()))
            y = min(max(geometry.y(), available.top()), max(available.top(), available.bottom() - geometry.height()))
        self.move(x, y)

    def closeEvent(self, event: QCloseEvent) -> None:
        self.hide()
        event.ignore()
