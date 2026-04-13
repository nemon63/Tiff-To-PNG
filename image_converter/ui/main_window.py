from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QPoint, QPointF, QRect, QSize, Qt, pyqtSignal
from PyQt6.QtGui import (
    QColor,
    QCloseEvent,
    QDragEnterEvent,
    QDropEvent,
    QImage,
    QKeySequence,
    QPainter,
    QPixmap,
    QShortcut,
    QWheelEvent,
)
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from image_converter.domain.constants import FILE_DIALOG_FILTER
from image_converter.domain.models import (
    AppSettings,
    BatchRequest,
    BatchSource,
    ConversionOptions,
    PreviewChannel,
    QueueItem,
    QueueStatus,
    ResizeMode,
    TextureMapType,
)
from image_converter.services.conversion import BatchConversionService
from image_converter.services.preview import TexturePreviewService

QUEUE_HEADERS = ("Имя", "Карта", "Размер", "Разрешение", "Статус", "Выходной путь")
AUTO_MAP_TYPE_DATA = "__auto__"


def _extract_local_paths(event) -> list[str]:
    mime_data = event.mimeData()
    if not mime_data.hasUrls():
        return []

    paths: list[str] = []
    for url in mime_data.urls():
        if url.isLocalFile():
            local_path = url.toLocalFile()
            if local_path:
                paths.append(local_path)
    return paths


def _status_text(status: QueueStatus) -> str:
    mapping = {
        QueueStatus.PENDING: "Ожидает",
        QueueStatus.READY: "Готово к запуску",
        QueueStatus.RUNNING: "В обработке",
        QueueStatus.DONE: "Успех",
        QueueStatus.SKIPPED: "Пропущено",
        QueueStatus.ERROR: "Ошибка",
    }
    return mapping[status]


def _queue_status_display(item: QueueItem) -> str:
    base_text = _status_text(item.status)
    if item.status in (QueueStatus.READY, QueueStatus.PENDING) and item.metadata and item.metadata.warning_count:
        return f"{base_text} ({item.metadata.warning_count} предупрежд.)"
    return base_text


def _preview_channel_label(channel: PreviewChannel) -> str:
    mapping = {
        PreviewChannel.COMPOSITE: "RGB",
        PreviewChannel.RED: "R",
        PreviewChannel.GREEN: "G",
        PreviewChannel.BLUE: "B",
        PreviewChannel.ALPHA: "A",
        PreviewChannel.LUMA: "Luma",
    }
    return mapping[channel]


def _map_type_label(map_type: TextureMapType) -> str:
    return map_type.label


def _qimage_from_pil(image) -> QImage:
    rgba_image = image.convert("RGBA")
    raw_data = rgba_image.tobytes("raw", "RGBA")
    qimage = QImage(
        raw_data,
        rgba_image.width,
        rgba_image.height,
        rgba_image.width * 4,
        QImage.Format.Format_RGBA8888,
    )
    return qimage.copy()


def _status_colors(item: QueueItem) -> tuple[QColor, QColor]:
    status = item.status
    if status is QueueStatus.RUNNING:
        return QColor("#16314F"), QColor("#D8EEFF")
    if status is QueueStatus.DONE:
        return QColor("#17351C"), QColor("#DCF7DD")
    if status is QueueStatus.SKIPPED:
        return QColor("#2A2A2A"), QColor("#E7E7E7")
    if status is QueueStatus.ERROR:
        return QColor("#4A1717"), QColor("#FFD7D7")
    if item.metadata and item.metadata.warnings:
        return QColor("#4C3B10"), QColor("#FFF0B8")
    return QColor("#1F1F1F"), QColor("#F3F3F3")


class SettingsPanel(QWidget):
    convert_requested = pyqtSignal()
    output_path_changed = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._interactive_widgets: list[QWidget] = []
        self._build_ui()
        self._update_resize_state()
        self._update_png8_state()

    def _build_ui(self) -> None:
        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(18, 18, 18, 18)
        root_layout.setSpacing(16)

        title_label = QLabel("Параметры экспорта")
        title_label.setObjectName("PanelTitle")
        root_layout.addWidget(title_label)

        subtitle_label = QLabel(
            "Соберите batch-проход: источники, выходной путь, формат PNG и масштабирование."
        )
        subtitle_label.setObjectName("PanelSubtitle")
        subtitle_label.setWordWrap(True)
        root_layout.addWidget(subtitle_label)

        root_layout.addWidget(self._build_paths_group())
        root_layout.addWidget(self._build_basic_group())
        root_layout.addWidget(self._build_png_group())
        root_layout.addWidget(self._build_resize_group())
        root_layout.addStretch(1)

        controls_row = QHBoxLayout()
        self.convert_button = QPushButton("Конвертировать")
        self.convert_button.setObjectName("PrimaryButton")
        self.convert_button.setMinimumHeight(42)
        self.convert_button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.convert_button.setDefault(True)
        self.convert_button.clicked.connect(self.convert_requested.emit)
        self.convert_button.setToolTip("Запустить пакетную конвертацию в PNG.")
        controls_row.addWidget(self.convert_button)
        root_layout.addLayout(controls_row)
        self._register_interactive(self.convert_button)

    def _build_paths_group(self) -> QGroupBox:
        group = QGroupBox("Пути")
        layout = QFormLayout(group)
        layout.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        layout.setFormAlignment(Qt.AlignmentFlag.AlignTop)

        self.input_edit = QLineEdit()
        self.input_edit.setPlaceholderText("Файл или папка с исходниками")
        self.input_edit.editingFinished.connect(self._on_input_editing_finished)
        self.input_edit.setToolTip(
            "Путь к файлу или папке.\nПоддерживаются TIFF, TGA, JPEG, BMP, GIF, WEBP и PSD."
        )

        input_row = QHBoxLayout()
        input_row.addWidget(self.input_edit)
        self.input_file_button = QPushButton("Файл")
        self.input_file_button.clicked.connect(self._pick_input_file)
        input_row.addWidget(self.input_file_button)
        self.input_folder_button = QPushButton("Папка")
        self.input_folder_button.clicked.connect(self._pick_input_folder)
        input_row.addWidget(self.input_folder_button)

        input_wrapper = QWidget()
        input_wrapper.setLayout(input_row)
        layout.addRow("Вход:", input_wrapper)

        self.output_edit = QLineEdit()
        self.output_edit.setPlaceholderText("Папка назначения")
        self.output_edit.textChanged.connect(self.output_path_changed.emit)
        self.output_edit.setToolTip(
            "Папка для PNG. Если оставить пустой, файлы будут сохранены рядом с исходниками."
        )

        output_row = QHBoxLayout()
        output_row.addWidget(self.output_edit)
        self.output_button = QPushButton("Выбрать")
        self.output_button.clicked.connect(self._pick_output_folder)
        output_row.addWidget(self.output_button)

        output_wrapper = QWidget()
        output_wrapper.setLayout(output_row)
        layout.addRow("Выход:", output_wrapper)

        self._register_interactive(
            self.input_edit,
            self.output_edit,
            self.input_file_button,
            self.input_folder_button,
            self.output_button,
        )
        return group

    def _build_basic_group(self) -> QGroupBox:
        group = QGroupBox("Основные параметры")
        layout = QVBoxLayout(group)

        self.recursive_checkbox = QCheckBox("Рекурсивно обрабатывать подпапки")
        self.recursive_checkbox.setChecked(True)
        layout.addWidget(self.recursive_checkbox)

        self.force_rgba_checkbox = QCheckBox("Принудительно сохранять как RGBA")
        layout.addWidget(self.force_rgba_checkbox)

        self.overwrite_checkbox = QCheckBox("Перезаписывать существующие PNG")
        layout.addWidget(self.overwrite_checkbox)

        self.delete_source_checkbox = QCheckBox("Удалять исходники после успешной конвертации")
        layout.addWidget(self.delete_source_checkbox)

        self._register_interactive(
            self.recursive_checkbox,
            self.force_rgba_checkbox,
            self.overwrite_checkbox,
            self.delete_source_checkbox,
        )
        return group

    def _build_png_group(self) -> QGroupBox:
        group = QGroupBox("Параметры PNG")
        layout = QVBoxLayout(group)

        self.optimize_checkbox = QCheckBox("Optimize")
        self.optimize_checkbox.setChecked(True)
        layout.addWidget(self.optimize_checkbox)

        compress_row = QHBoxLayout()
        compress_row.addWidget(QLabel("Степень сжатия:"))
        self.compress_spin = QSpinBox()
        self.compress_spin.setRange(0, 9)
        self.compress_spin.setValue(6)
        compress_row.addWidget(self.compress_spin)
        compress_row.addStretch(1)
        layout.addLayout(compress_row)

        palette_row = QHBoxLayout()
        self.png8_checkbox = QCheckBox("PNG-8")
        self.png8_checkbox.toggled.connect(self._update_png8_state)
        palette_row.addWidget(self.png8_checkbox)
        palette_row.addWidget(QLabel("Цветов:"))
        self.png8_colors_spin = QSpinBox()
        self.png8_colors_spin.setRange(2, 256)
        self.png8_colors_spin.setValue(256)
        palette_row.addWidget(self.png8_colors_spin)
        self.dither_checkbox = QCheckBox("Dithering")
        self.dither_checkbox.setChecked(True)
        palette_row.addWidget(self.dither_checkbox)
        palette_row.addStretch(1)
        layout.addLayout(palette_row)

        self._register_interactive(
            self.optimize_checkbox,
            self.compress_spin,
            self.png8_checkbox,
            self.png8_colors_spin,
            self.dither_checkbox,
        )
        return group

    def _build_resize_group(self) -> QGroupBox:
        group = QGroupBox("Масштабирование")
        layout = QVBoxLayout(group)

        self.resize_none_radio = QRadioButton("Без изменения")
        self.resize_none_radio.setChecked(True)
        self.resize_none_radio.toggled.connect(self._update_resize_state)
        layout.addWidget(self.resize_none_radio)

        percent_row = QHBoxLayout()
        self.resize_percent_radio = QRadioButton("Процент от оригинала")
        self.resize_percent_radio.toggled.connect(self._update_resize_state)
        percent_row.addWidget(self.resize_percent_radio)
        self.resize_percent_spin = QSpinBox()
        self.resize_percent_spin.setRange(1, 1000)
        self.resize_percent_spin.setValue(100)
        self.resize_percent_spin.setSuffix(" %")
        percent_row.addWidget(self.resize_percent_spin)
        percent_row.addStretch(1)
        layout.addLayout(percent_row)

        max_side_row = QHBoxLayout()
        self.resize_max_side_radio = QRadioButton("Ограничить длинную сторону")
        self.resize_max_side_radio.toggled.connect(self._update_resize_state)
        max_side_row.addWidget(self.resize_max_side_radio)
        self.max_side_spin = QSpinBox()
        self.max_side_spin.setRange(1, 20000)
        self.max_side_spin.setValue(2048)
        self.max_side_spin.setSuffix(" px")
        max_side_row.addWidget(self.max_side_spin)
        max_side_row.addStretch(1)
        layout.addLayout(max_side_row)

        self._register_interactive(
            self.resize_none_radio,
            self.resize_percent_radio,
            self.resize_percent_spin,
            self.resize_max_side_radio,
            self.max_side_spin,
        )
        return group

    def _register_interactive(self, *widgets: QWidget) -> None:
        self._interactive_widgets.extend(widgets)

    def _set_input_path(self, path: str) -> None:
        self.input_edit.setText(path)
        self._auto_fill_output_from_input(force=True)

    def _on_input_editing_finished(self) -> None:
        self._auto_fill_output_from_input(force=False)

    def _auto_fill_output_from_input(self, force: bool = False) -> None:
        if not force and self.output_edit.text().strip():
            return

        raw_input = self.input_edit.text().strip()
        if not raw_input:
            return

        input_path = Path(raw_input)
        if input_path.exists():
            output_path = input_path if input_path.is_dir() else input_path.parent
        else:
            output_path = input_path.parent if input_path.suffix else input_path

        if str(output_path):
            self.output_edit.setText(str(output_path))

    def _pick_input_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Выберите файл изображения",
            "",
            FILE_DIALOG_FILTER,
        )
        if path:
            self._set_input_path(path)

    def _pick_input_folder(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Выберите входную папку")
        if path:
            self._set_input_path(path)

    def _pick_output_folder(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Выберите выходную папку")
        if path:
            self.output_edit.setText(path)

    def _update_resize_state(self, *_args: object) -> None:
        self.resize_percent_spin.setEnabled(self.resize_percent_radio.isChecked())
        self.max_side_spin.setEnabled(self.resize_max_side_radio.isChecked())

    def _update_png8_state(self, *_args: object) -> None:
        enabled = self.png8_checkbox.isChecked()
        self.png8_colors_spin.setEnabled(enabled)
        self.dither_checkbox.setEnabled(enabled)

    def selected_resize_mode(self) -> ResizeMode:
        if self.resize_percent_radio.isChecked():
            return ResizeMode.PERCENT
        if self.resize_max_side_radio.isChecked():
            return ResizeMode.MAX_SIDE
        return ResizeMode.NONE

    def build_request(self) -> BatchRequest:
        input_text = self.input_edit.text().strip()
        output_text = self.output_edit.text().strip()
        return BatchRequest(
            input_path=Path(input_text) if input_text else None,
            output_root=Path(output_text) if output_text else None,
            options=ConversionOptions(
                recursive=self.recursive_checkbox.isChecked(),
                force_rgba=self.force_rgba_checkbox.isChecked(),
                overwrite=self.overwrite_checkbox.isChecked(),
                delete_source=self.delete_source_checkbox.isChecked(),
                optimize=self.optimize_checkbox.isChecked(),
                compress_level=self.compress_spin.value(),
                resize_mode=self.selected_resize_mode(),
                resize_percent=self.resize_percent_spin.value(),
                max_side=self.max_side_spin.value(),
                png8=self.png8_checkbox.isChecked(),
                png8_colors=self.png8_colors_spin.value(),
                dither=self.dither_checkbox.isChecked(),
            ),
        )

    def apply_app_settings(self, settings: AppSettings) -> None:
        self.input_edit.setText(settings.input_path)
        self.output_edit.setText(settings.output_path)

        options = settings.options
        self.recursive_checkbox.setChecked(options.recursive)
        self.force_rgba_checkbox.setChecked(options.force_rgba)
        self.overwrite_checkbox.setChecked(options.overwrite)
        self.delete_source_checkbox.setChecked(options.delete_source)
        self.optimize_checkbox.setChecked(options.optimize)
        self.compress_spin.setValue(options.compress_level)
        self.png8_checkbox.setChecked(options.png8)
        self.png8_colors_spin.setValue(options.png8_colors)
        self.dither_checkbox.setChecked(options.dither)
        self.resize_percent_spin.setValue(options.resize_percent)
        self.max_side_spin.setValue(options.max_side)

        if options.resize_mode is ResizeMode.PERCENT:
            self.resize_percent_radio.setChecked(True)
        elif options.resize_mode is ResizeMode.MAX_SIDE:
            self.resize_max_side_radio.setChecked(True)
        else:
            self.resize_none_radio.setChecked(True)

        self._update_resize_state()
        self._update_png8_state()

    def set_controls_enabled(self, enabled: bool) -> None:
        for widget in self._interactive_widgets:
            widget.setEnabled(enabled)

        if enabled:
            self._update_resize_state()
            self._update_png8_state()


class QueueTableWidget(QTableWidget):
    paths_dropped = pyqtSignal(list)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if _extract_local_paths(event):
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event) -> None:
        if _extract_local_paths(event):
            event.acceptProposedAction()
            return
        super().dragMoveEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:
        paths = _extract_local_paths(event)
        if paths:
            self.paths_dropped.emit(paths)
            event.acceptProposedAction()
            return
        super().dropEvent(event)


class QueuePanel(QWidget):
    paths_selected = pyqtSignal(list)
    paths_dropped = pyqtSignal(list)
    remove_requested = pyqtSignal()
    clear_requested = pyqtSignal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._interactive_widgets: list[QWidget] = []
        self._build_ui()

    def _build_ui(self) -> None:
        self.setObjectName("SectionPanel")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(12)

        title_label = QLabel("Очередь конвертации")
        title_label.setObjectName("PanelTitle")
        layout.addWidget(title_label)

        subtitle_label = QLabel(
            "Перетащите сюда отдельные текстуры или целые каталоги. Очередь всегда показывает, что реально уйдет в batch."
        )
        subtitle_label.setObjectName("PanelSubtitle")
        subtitle_label.setWordWrap(True)
        layout.addWidget(subtitle_label)

        controls_row = QHBoxLayout()
        self.add_files_button = QPushButton("Добавить файлы")
        self.add_files_button.clicked.connect(self._pick_files)
        controls_row.addWidget(self.add_files_button)

        self.add_folder_button = QPushButton("Добавить папку")
        self.add_folder_button.clicked.connect(self._pick_folder)
        controls_row.addWidget(self.add_folder_button)

        self.remove_selected_button = QPushButton("Удалить выбранные")
        self.remove_selected_button.clicked.connect(self.remove_requested.emit)
        controls_row.addWidget(self.remove_selected_button)

        self.clear_button = QPushButton("Очистить")
        self.clear_button.setObjectName("DangerButton")
        self.clear_button.clicked.connect(self.clear_requested.emit)
        controls_row.addWidget(self.clear_button)
        controls_row.addStretch(1)
        layout.addLayout(controls_row)

        self.drop_hint = QLabel(
            "Перетащите файлы или папки сюда. Можно смешивать отдельные текстуры и каталоги."
        )
        self.drop_hint.setObjectName("DropHint")
        self.drop_hint.setWordWrap(True)
        layout.addWidget(self.drop_hint)

        self.summary_label = QLabel("Очередь пуста")
        self.summary_label.setObjectName("SummaryText")
        layout.addWidget(self.summary_label)

        self.table = QueueTableWidget()
        self.table.setColumnCount(len(QUEUE_HEADERS))
        self.table.setHorizontalHeaderLabels(QUEUE_HEADERS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self.table.paths_dropped.connect(self.paths_dropped.emit)
        layout.addWidget(self.table, 1)

        self._register_interactive(
            self.add_files_button,
            self.add_folder_button,
            self.remove_selected_button,
            self.clear_button,
            self.table,
        )

    def _register_interactive(self, *widgets: QWidget) -> None:
        self._interactive_widgets.extend(widgets)

    def _pick_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "Выберите изображения", "", FILE_DIALOG_FILTER)
        if paths:
            self.paths_selected.emit(paths)

    def _pick_folder(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Выберите папку с текстурами")
        if path:
            self.paths_selected.emit([path])

    def set_controls_enabled(self, enabled: bool) -> None:
        for widget in self._interactive_widgets:
            widget.setEnabled(enabled)


class PreviewCanvas(QFrame):
    zoom_changed = pyqtSignal(float)
    stage_rect_changed = pyqtSignal(QRect)

    def __init__(self, parent: QWidget | None = None):
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
        self.setMinimumSize(360, 360)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMouseTracking(True)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return width

    def sizeHint(self) -> QSize:
        return QSize(460, 460)

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
        if event.button() == Qt.MouseButton.LeftButton:
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

        square_rect = frame_rect.adjusted(self._padding, self._padding, -self._padding, -self._padding)
        if square_rect.width() <= 0 or square_rect.height() <= 0:
            return

        self._draw_checkerboard(painter, square_rect)

        if self._source_pixmap is None:
            painter.setPen(QColor("#8F9CAA"))
            painter.drawText(
                square_rect.adjusted(20, 20, -20, -20),
                Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
                self._placeholder_text,
            )
            return

        target_width, target_height = self._scaled_target_size(square_rect)
        scaled = self._source_pixmap.scaled(
            target_width,
            target_height,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )

        x = square_rect.center().x() - scaled.width() // 2 + round(self._pan_offset.x())
        y = square_rect.center().y() - scaled.height() // 2 + round(self._pan_offset.y())
        painter.save()
        painter.setClipRect(square_rect)
        painter.drawPixmap(x, y, scaled)
        painter.restore()

    def _square_stage_rect(self, frame_rect: QRect) -> QRect:
        side = max(0, min(frame_rect.width(), frame_rect.height()))
        x = frame_rect.x() + (frame_rect.width() - side) // 2
        y = frame_rect.y() + (frame_rect.height() - side) // 2
        return QRect(x, y, side, side)

    def stage_rect(self) -> QRect:
        return self._square_stage_rect(self.rect().adjusted(1, 1, -1, -1))

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

    def _scaled_target_size(self, square_rect: QRect) -> tuple[int, int]:
        if self._source_pixmap is None:
            return (0, 0)

        base_scale = min(
            square_rect.width() / max(self._source_pixmap.width(), 1),
            square_rect.height() / max(self._source_pixmap.height(), 1),
        )
        target_width = max(1, round(self._source_pixmap.width() * base_scale * self._zoom_factor))
        target_height = max(1, round(self._source_pixmap.height() * base_scale * self._zoom_factor))
        return (target_width, target_height)

    def _can_pan(self) -> bool:
        if self._source_pixmap is None or self._zoom_factor <= 1.0:
            return False

        viewport_rect = self._square_stage_rect(self.rect().adjusted(1, 1, -1, -1)).adjusted(
            self._padding,
            self._padding,
            -self._padding,
            -self._padding,
        )
        target_width, target_height = self._scaled_target_size(viewport_rect)
        return target_width > viewport_rect.width() or target_height > viewport_rect.height()

    def _normalize_pan_offset(self) -> None:
        if self._source_pixmap is None:
            self._pan_offset = QPointF(0.0, 0.0)
            return

        viewport_rect = self._square_stage_rect(self.rect().adjusted(1, 1, -1, -1)).adjusted(
            self._padding,
            self._padding,
            -self._padding,
            -self._padding,
        )
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


class InspectorField(QFrame):
    def __init__(
        self,
        title: str,
        parent: QWidget | None = None,
        *,
        compact: bool = False,
        wrap_value: bool = True,
        inline: bool = False,
    ):
        super().__init__(parent)
        self.setObjectName("InspectorCompactRow" if compact else "InspectorRow")
        if inline:
            layout = QHBoxLayout(self)
            if compact:
                layout.setContentsMargins(0, 0, 0, 0)
                layout.setSpacing(6)
            else:
                layout.setContentsMargins(12, 8, 12, 8)
                layout.setSpacing(10)
        else:
            layout = QVBoxLayout(self)
            if compact:
                layout.setContentsMargins(10, 8, 10, 8)
                layout.setSpacing(2)
            else:
                layout.setContentsMargins(12, 10, 12, 10)
                layout.setSpacing(4)

        self.title_label = QLabel(title)
        self.title_label.setObjectName("InspectorInlineKey" if inline else "InspectorKey")
        layout.addWidget(self.title_label)

        self.value_label = QLabel("-")
        self.value_label.setObjectName("InspectorInlineValue" if inline else "InspectorValue")
        self.value_label.setWordWrap(wrap_value)
        self.value_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        if inline:
            layout.addWidget(self.value_label, 1)
        else:
            layout.addWidget(self.value_label)

    def set_value(self, text: str) -> None:
        self.value_label.setText(text)
        self.value_label.setToolTip(text)


class PreviewPanel(QWidget):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._preview_service = TexturePreviewService()
        self._current_item: QueueItem | None = None
        self._selected_channel = PreviewChannel.COMPOSITE
        self._channel_buttons: dict[PreviewChannel, QToolButton] = {}
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
        self.asset_meta_label.setText(
            f"{map_type_name} · {metadata.resolution_text} · {metadata.mode} · канал: {channel_name} · {alpha_state}"
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


class MetadataPanel(QWidget):
    map_type_override_changed = pyqtSignal(object)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._current_item: QueueItem | None = None
        self._build_ui()

    def _build_ui(self) -> None:
        self.setObjectName("SectionPanel")
        self.setMinimumHeight(170)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(8)

        header_row = QHBoxLayout()
        title_label = QLabel("Asset Inspector")
        title_label.setObjectName("PanelTitle")
        header_row.addWidget(title_label)
        header_row.addStretch(1)
        layout.addLayout(header_row)

        self.asset_name_label = QLabel("Ничего не выбрано")
        self.asset_name_label.setObjectName("AssetName")
        self.asset_name_label.setWordWrap(True)
        layout.addWidget(self.asset_name_label)

        map_type_row = QHBoxLayout()
        map_type_row.setSpacing(8)
        map_type_label = QLabel("Map Type")
        map_type_label.setObjectName("SummaryText")
        map_type_row.addWidget(map_type_label)
        self.map_type_combo = QComboBox()
        self.map_type_combo.currentIndexChanged.connect(self._emit_map_type_override)
        map_type_row.addWidget(self.map_type_combo, 1)
        layout.addLayout(map_type_row)

        self.source_field = InspectorField("Исходный файл", compact=True, wrap_value=False, inline=True)
        layout.addWidget(self.source_field)

        metrics_grid = QGridLayout()
        metrics_grid.setHorizontalSpacing(8)
        metrics_grid.setVerticalSpacing(6)
        self.format_field = InspectorField("Формат", compact=True, wrap_value=False, inline=True)
        self.resolution_field = InspectorField("Разрешение", compact=True, wrap_value=False, inline=True)
        self.mode_field = InspectorField("Режим", compact=True, wrap_value=False, inline=True)
        self.alpha_field = InspectorField("Alpha", compact=True, wrap_value=False, inline=True)
        self.frames_field = InspectorField("Кадры", compact=True, wrap_value=False, inline=True)
        self.size_field = InspectorField("Размер", compact=True, wrap_value=False, inline=True)

        metric_fields = (
            self.format_field,
            self.resolution_field,
            self.mode_field,
            self.alpha_field,
            self.frames_field,
            self.size_field,
        )
        for index, field in enumerate(metric_fields):
            metrics_grid.addWidget(field, index // 3, index % 3)
        layout.addLayout(metrics_grid)

        self.output_field = InspectorField("Выходной PNG", compact=True, wrap_value=False, inline=True)
        layout.addWidget(self.output_field)

        self.warning_label = QLabel("")
        self.warning_label.setObjectName("WarningBanner")
        self.warning_label.setWordWrap(True)
        self.warning_label.hide()
        layout.addWidget(self.warning_label)

    def set_queue_item(self, item: QueueItem | None) -> None:
        self._current_item = item
        self._sync_map_type_combo(item)

        if item is None:
            self.asset_name_label.setText("Ничего не выбрано")
            self._set_values(
                source="-",
                format_value="-",
                resolution="-",
                mode="-",
                alpha="-",
                frames="-",
                size="-",
                output="Будет рассчитан после выбора выходной папки.",
            )
            self.warning_label.hide()
            return

        self.asset_name_label.setText(item.path.name)
        metadata = item.metadata

        if metadata is None:
            self._set_values(
                source=str(item.path),
                format_value="-",
                resolution="-",
                mode="-",
                alpha="-",
                frames="-",
                size="-",
                output=str(item.output_path) if item.output_path is not None else "Рядом с исходным файлом.",
            )
            self.warning_label.setText(item.message or "Не удалось прочитать метаданные.")
            self.warning_label.show()
            return

        self._set_values(
            source=str(item.path),
            format_value=metadata.format_name,
            resolution=metadata.resolution_text,
            mode=metadata.mode,
            alpha="Да" if metadata.has_alpha else "Нет",
            frames=str(metadata.frame_count),
            size=metadata.size_text,
            output=str(item.output_path) if item.output_path is not None else "Рядом с исходным файлом.",
        )

        warnings: list[str] = list(metadata.warnings)
        if item.status is QueueStatus.ERROR and item.message:
            warnings.append(item.message)

        if warnings:
            warning_lines = ["Preflight предупреждения:"] + [f"- {warning}" for warning in warnings]
            self.warning_label.setText("\n".join(warning_lines))
            self.warning_label.show()
            return

        self.warning_label.hide()

    def _set_values(
        self,
        *,
        source: str,
        format_value: str,
        resolution: str,
        mode: str,
        alpha: str,
        frames: str,
        size: str,
        output: str,
    ) -> None:
        self.source_field.set_value(source)
        self.format_field.set_value(format_value)
        self.resolution_field.set_value(resolution)
        self.mode_field.set_value(mode)
        self.alpha_field.set_value(alpha)
        self.frames_field.set_value(frames)
        self.size_field.set_value(size)
        self.output_field.set_value(output)

    def _sync_map_type_combo(self, item: QueueItem | None) -> None:
        self.map_type_combo.blockSignals(True)
        self.map_type_combo.clear()

        if item is None or item.metadata is None:
            self.map_type_combo.addItem("Авто", AUTO_MAP_TYPE_DATA)
            self.map_type_combo.setEnabled(False)
            self.map_type_combo.blockSignals(False)
            return

        detected_map_type = item.metadata.map_type
        self.map_type_combo.addItem(f"Авто: {_map_type_label(detected_map_type)}", AUTO_MAP_TYPE_DATA)
        self.map_type_combo.addItem(_map_type_label(TextureMapType.UNKNOWN), TextureMapType.UNKNOWN)
        for map_type in TextureMapType:
            if map_type is TextureMapType.UNKNOWN:
                continue
            self.map_type_combo.addItem(_map_type_label(map_type), map_type)

        current_value = item.map_type_override if item.map_type_override is not None else AUTO_MAP_TYPE_DATA
        for index in range(self.map_type_combo.count()):
            if self.map_type_combo.itemData(index) == current_value:
                self.map_type_combo.setCurrentIndex(index)
                break

        self.map_type_combo.setEnabled(True)
        self.map_type_combo.blockSignals(False)

    def _emit_map_type_override(self) -> None:
        if self._current_item is None or self._current_item.metadata is None:
            return

        selected_data = self.map_type_combo.currentData()
        if selected_data == AUTO_MAP_TYPE_DATA:
            self.map_type_override_changed.emit(None)
            return
        self.map_type_override_changed.emit(selected_data)


class LogPanel(QWidget):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._build_ui()

    def _build_ui(self) -> None:
        self.setObjectName("SectionPanel")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(12)

        title_label = QLabel("Журнал пайплайна")
        title_label.setObjectName("PanelTitle")
        layout.addWidget(title_label)

        subtitle_label = QLabel("Служебные сообщения, ошибки и итоги batch-конвертации.")
        subtitle_label.setObjectName("PanelSubtitle")
        subtitle_label.setWordWrap(True)
        layout.addWidget(subtitle_label)

        self.log_edit = QPlainTextEdit()
        self.log_edit.setReadOnly(True)
        self.log_edit.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.log_edit.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        layout.addWidget(self.log_edit, 1)

    def append_line(self, line: str) -> None:
        self.log_edit.appendPlainText(line)
        scrollbar = self.log_edit.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def clear(self) -> None:
        self.log_edit.clear()


class MainWindow(QMainWindow):
    convert_requested = pyqtSignal()
    queue_paths_received = pyqtSignal(list)

    def __init__(self):
        super().__init__()
        self._is_running = False
        self._queue_items: list[QueueItem] = []
        self.setWindowTitle("Конвертер изображений в PNG")
        self.resize(1280, 820)
        self.setMinimumSize(980, 680)
        self.setAcceptDrops(True)
        self._build_ui()
        self.statusBar().showMessage("Готово")

    def _build_ui(self) -> None:
        root = QWidget()
        root_layout = QHBoxLayout(root)
        root_layout.setContentsMargins(18, 18, 18, 14)
        root_layout.setSpacing(16)

        self.settings_panel = SettingsPanel()
        self.settings_panel.convert_requested.connect(self.convert_requested.emit)
        self.settings_panel.output_path_changed.connect(self._update_queue_output_paths)

        settings_scroll = QScrollArea()
        settings_scroll.setWidgetResizable(True)
        settings_scroll.setFrameShape(QFrame.Shape.NoFrame)
        settings_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        settings_scroll.setWidget(self.settings_panel)

        sidebar_card = QFrame()
        sidebar_card.setObjectName("SidebarPanel")
        sidebar_layout = QVBoxLayout(sidebar_card)
        sidebar_layout.setContentsMargins(0, 0, 0, 0)
        sidebar_layout.addWidget(settings_scroll)

        self.queue_panel = QueuePanel()
        self.queue_panel.paths_selected.connect(self.queue_paths_received.emit)
        self.queue_panel.paths_dropped.connect(self.queue_paths_received.emit)
        self.queue_panel.remove_requested.connect(self.remove_selected_queue_items)
        self.queue_panel.clear_requested.connect(self.clear_queue_items)
        self.queue_panel.table.itemSelectionChanged.connect(self._sync_status_bar_with_selection)
        self.queue_panel.table.itemSelectionChanged.connect(self._sync_workspace_selection)

        self.preview_panel = PreviewPanel()
        self.metadata_panel = MetadataPanel()
        self.metadata_panel.map_type_override_changed.connect(self._apply_selected_map_type_override)
        self.log_panel = LogPanel()

        self.detail_splitter = QSplitter(Qt.Orientation.Vertical)
        self.detail_splitter.addWidget(self.queue_panel)
        self.detail_splitter.addWidget(self.log_panel)
        self.detail_splitter.setChildrenCollapsible(False)
        self.detail_splitter.setStretchFactor(0, 3)
        self.detail_splitter.setStretchFactor(1, 2)

        self.inspector_splitter = QSplitter(Qt.Orientation.Vertical)
        self.inspector_splitter.addWidget(self.preview_panel)
        self.inspector_splitter.addWidget(self.metadata_panel)
        self.inspector_splitter.setChildrenCollapsible(False)
        self.inspector_splitter.setStretchFactor(0, 4)
        self.inspector_splitter.setStretchFactor(1, 3)

        self.workspace_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.workspace_splitter.addWidget(self.detail_splitter)
        self.workspace_splitter.addWidget(self.inspector_splitter)
        self.workspace_splitter.setChildrenCollapsible(False)
        self.workspace_splitter.setStretchFactor(0, 3)
        self.workspace_splitter.setStretchFactor(1, 2)

        workspace_card = QFrame()
        workspace_card.setObjectName("WorkspaceCard")
        workspace_layout = QVBoxLayout(workspace_card)
        workspace_layout.setContentsMargins(18, 18, 18, 18)
        workspace_layout.setSpacing(14)

        workspace_title = QLabel("Texture Desk")
        workspace_title.setObjectName("PanelTitle")
        workspace_layout.addWidget(workspace_title)

        workspace_subtitle = QLabel(
            "Середина отвечает за intake и лог, правая колонка за square-preview и техинспекцию."
        )
        workspace_subtitle.setObjectName("PanelSubtitle")
        workspace_subtitle.setWordWrap(True)
        workspace_layout.addWidget(workspace_subtitle)

        self.workspace_summary_label = QLabel(
            "Пока без ассетов. Добавьте папку или набор текстур, чтобы открыть рабочую сцену."
        )
        self.workspace_summary_label.setObjectName("SummaryText")
        self.workspace_summary_label.setWordWrap(True)
        workspace_layout.addWidget(self.workspace_summary_label)
        workspace_layout.addWidget(self.workspace_splitter, 1)

        self.main_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.main_splitter.addWidget(sidebar_card)
        self.main_splitter.addWidget(workspace_card)
        self.main_splitter.setChildrenCollapsible(False)
        self.main_splitter.setStretchFactor(0, 0)
        self.main_splitter.setStretchFactor(1, 1)
        root_layout.addWidget(self.main_splitter, 1)

        self.setCentralWidget(root)
        self._apply_default_splitter_sizes()

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if _extract_local_paths(event):
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event) -> None:
        if _extract_local_paths(event):
            event.acceptProposedAction()
            return
        super().dragMoveEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:
        paths = _extract_local_paths(event)
        if paths:
            self.queue_paths_received.emit(paths)
            event.acceptProposedAction()
            return
        super().dropEvent(event)

    def build_request(self) -> BatchRequest:
        request = self.settings_panel.build_request()
        queue_sources = tuple(
            item.source for item in self._queue_items if item.status is not QueueStatus.ERROR
        )
        return BatchRequest(
            input_path=request.input_path,
            output_root=request.output_root,
            options=request.options,
            sources=queue_sources,
        )

    def queue_recursive_enabled(self) -> bool:
        return self.settings_panel.recursive_checkbox.isChecked()

    def add_queue_items(self, items: list[QueueItem]) -> None:
        existing_index = {self._queue_key(item.path): index for index, item in enumerate(self._queue_items)}

        for item in items:
            item.output_path = self._build_output_path(item.source)
            key = self._queue_key(item.path)
            if key in existing_index:
                self._queue_items[existing_index[key]] = item
            else:
                self._queue_items.append(item)

        self._queue_items.sort(key=lambda item: str(item.path).lower())
        self._render_queue()
        self._sync_status_bar_with_selection()
        self._sync_workspace_selection()

    def remove_selected_queue_items(self) -> None:
        selected_rows = sorted(
            {index.row() for index in self.queue_panel.table.selectionModel().selectedRows()},
            reverse=True,
        )
        if not selected_rows:
            return

        for row in selected_rows:
            self._queue_items.pop(row)

        self._render_queue()
        self._sync_workspace_selection()
        self.set_status(f"Удалено элементов: {len(selected_rows)}")

    def clear_queue_items(self) -> None:
        self._queue_items.clear()
        self._render_queue()
        self.set_status("Очередь очищена")

    def reset_queue_statuses_for_run(self) -> None:
        for item in self._queue_items:
            if item.status is not QueueStatus.ERROR:
                item.status = QueueStatus.PENDING
                item.message = ""
        self._render_queue()
        self._sync_workspace_selection()

    def set_queue_item_running(self, path: Path) -> None:
        item = self._find_queue_item(path)
        if item is None:
            return
        item.status = QueueStatus.RUNNING
        item.message = ""
        self._render_queue()
        self._sync_workspace_selection()

    def set_queue_item_result(
        self,
        path: Path,
        *,
        status: QueueStatus,
        message: str,
        destination: Path | None = None,
    ) -> None:
        item = self._find_queue_item(path)
        if item is None:
            return
        item.status = status
        item.message = message
        if destination is not None:
            item.output_path = destination
        self._render_queue()
        self._sync_workspace_selection()

    def set_running(self, running: bool) -> None:
        self._is_running = running
        self.settings_panel.set_controls_enabled(not running)
        self.queue_panel.set_controls_enabled(not running)

    def append_log(self, line: str) -> None:
        self.log_panel.append_line(line)

    def clear_log(self) -> None:
        self.log_panel.clear()

    def set_status(self, text: str) -> None:
        self.statusBar().showMessage(text)

    def apply_app_settings(self, settings: AppSettings) -> None:
        self.resize(settings.window_width, settings.window_height)
        self.settings_panel.apply_app_settings(settings)
        if len(settings.splitter_sizes) == 2:
            self.main_splitter.setSizes(list(settings.splitter_sizes))
        if len(settings.workspace_splitter_sizes) == 2:
            self.workspace_splitter.setSizes(list(settings.workspace_splitter_sizes))
        if len(settings.detail_splitter_sizes) == 2:
            self.detail_splitter.setSizes(list(settings.detail_splitter_sizes))
        if len(settings.inspector_splitter_sizes) == 2:
            self.inspector_splitter.setSizes(list(settings.inspector_splitter_sizes))
        self._update_queue_output_paths()

    def build_app_settings(self) -> AppSettings:
        request = self.settings_panel.build_request()
        return AppSettings(
            input_path=str(request.input_path or ""),
            output_path=str(request.output_root or ""),
            options=request.options,
            window_width=self.width(),
            window_height=self.height(),
            splitter_sizes=tuple(self.main_splitter.sizes()[:2]),
            workspace_splitter_sizes=tuple(self.workspace_splitter.sizes()[:2]),
            detail_splitter_sizes=tuple(self.detail_splitter.sizes()[:2]),
            inspector_splitter_sizes=tuple(self.inspector_splitter.sizes()[:2]),
        )

    def confirm_delete_sources(self) -> bool:
        button = QMessageBox.question(
            self,
            "Подтверждение удаления",
            "Исходные файлы будут удаляться после успешной конвертации. Продолжить?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return button is QMessageBox.StandardButton.Yes

    def show_error(self, title: str, message: str) -> None:
        QMessageBox.critical(self, title, message)

    def show_info(self, title: str, message: str) -> None:
        QMessageBox.information(self, title, message)

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._is_running:
            QMessageBox.warning(
                self,
                "Конвертация выполняется",
                "Дождитесь завершения конвертации перед закрытием окна.",
            )
            event.ignore()
            return
        super().closeEvent(event)

    def _render_queue(self) -> None:
        table = self.queue_panel.table
        selected_key = self._selected_queue_key()
        table.setRowCount(len(self._queue_items))

        for row, item in enumerate(self._queue_items):
            metadata = item.metadata
            output_text = str(item.output_path) if item.output_path is not None else "-"
            status_text = _queue_status_display(item)
            status_tooltip = item.message or status_text
            if metadata and metadata.warnings and item.status in (QueueStatus.READY, QueueStatus.PENDING):
                status_tooltip = metadata.warning_summary

            row_items = [
                self._make_table_item(item.path.name, tooltip=str(item.path)),
                self._make_table_item(self._asset_type_text(item), tooltip=self._map_type_tooltip(item)),
                self._make_table_item(metadata.size_text if metadata else "-", tooltip=self._metadata_tooltip(item)),
                self._make_table_item(metadata.resolution_text if metadata else "-", tooltip=self._metadata_tooltip(item)),
                self._make_table_item(status_text, tooltip=status_tooltip),
                self._make_table_item(output_text, tooltip=output_text),
            ]
            row_items[0].setData(Qt.ItemDataRole.UserRole, self._queue_key(item.path))

            for column, table_item in enumerate(row_items):
                table.setItem(row, column, table_item)

            self._apply_row_style(row, item)

        if not self._queue_items:
            self.queue_panel.summary_label.setText("Очередь пуста")
            self.workspace_summary_label.setText(
                "Пока без ассетов. Добавьте папку или набор текстур, чтобы открыть рабочую сцену."
            )
            self.preview_panel.set_queue_item(None)
            self.metadata_panel.set_queue_item(None)
            self.set_status("Очередь пуста")
            return

        warning_count = sum(1 for item in self._queue_items if item.metadata and item.metadata.warnings)
        error_count = sum(1 for item in self._queue_items if item.status is QueueStatus.ERROR)
        summary_text = f"Всего: {len(self._queue_items)} | Предупреждений: {warning_count} | Ошибок: {error_count}"
        self.queue_panel.summary_label.setText(summary_text)
        self.workspace_summary_label.setText(
            f"{len(self._queue_items)} ассетов в desk | {warning_count} preflight warnings | {error_count} errors"
        )
        self._restore_queue_selection(selected_key)

    def _apply_row_style(self, row: int, item: QueueItem) -> None:
        table = self.queue_panel.table
        background, foreground = _status_colors(item)
        for column in range(table.columnCount()):
            table_item = table.item(row, column)
            if table_item is not None:
                table_item.setBackground(background)
                table_item.setForeground(foreground)

    def _make_table_item(self, text: str, *, tooltip: str = "") -> QTableWidgetItem:
        table_item = QTableWidgetItem(text)
        table_item.setToolTip(tooltip)
        table_item.setFlags(Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled)
        return table_item

    def _asset_type_text(self, item: QueueItem) -> str:
        return _map_type_label(item.effective_map_type)

    def _map_type_tooltip(self, item: QueueItem) -> str:
        metadata = item.metadata
        lines = [f"Тип карты: {_map_type_label(item.effective_map_type)}"]
        if item.map_type_override is not None:
            lines.append("Источник: ручное переопределение")
        elif metadata is not None:
            lines.append("Источник: автоопределение по имени файла")
        if metadata is not None:
            lines.append(f"Формат: {metadata.format_name}")
        return "\n".join(lines)

    def _metadata_tooltip(self, item: QueueItem) -> str:
        metadata = item.metadata
        if metadata is None:
            return ""

        lines = [
            f"Тип карты: {_map_type_label(item.effective_map_type)}",
            f"Формат: {metadata.format_name}",
            f"Разрешение: {metadata.resolution_text}",
            f"Режим: {metadata.mode}",
            f"Альфа: {'да' if metadata.has_alpha else 'нет'}",
            f"Кадров/страниц: {metadata.frame_count}",
            f"Размер файла: {metadata.size_text}",
        ]
        if metadata.warnings:
            lines.append("Предупреждения:")
            lines.extend(f"- {warning}" for warning in metadata.warnings)
        return "\n".join(lines)

    def _find_queue_item(self, path: Path) -> QueueItem | None:
        key = self._queue_key(path)
        for item in self._queue_items:
            if self._queue_key(item.path) == key:
                return item
        return None

    def _queue_key(self, path: Path) -> str:
        try:
            return str(path.resolve()).lower()
        except OSError:
            return str(path).lower()

    def _build_output_path(self, source: BatchSource) -> Path:
        output_root_text = self.settings_panel.output_edit.text().strip()
        output_root = Path(output_root_text) if output_root_text else None
        return BatchConversionService.build_destination_for_source(source, output_root)

    def _update_queue_output_paths(self, *_args: object) -> None:
        for item in self._queue_items:
            item.output_path = self._build_output_path(item.source)
        self._render_queue()
        self._sync_workspace_selection()

    def _sync_status_bar_with_selection(self) -> None:
        item = self._selected_queue_item()
        if item is None:
            if not self._queue_items:
                self.set_status("Очередь пуста")
            return
        if item.status in (QueueStatus.READY, QueueStatus.PENDING) and item.metadata and item.metadata.warnings:
            message = item.metadata.warning_summary
        else:
            message = item.message or _queue_status_display(item)
        self.set_status(f"{item.path.name}: {message}")

    def _sync_workspace_selection(self) -> None:
        item = self._selected_queue_item()
        self.preview_panel.set_queue_item(item)
        self.metadata_panel.set_queue_item(item)

    def _apply_selected_map_type_override(self, map_type_override: object) -> None:
        item = self._selected_queue_item()
        if item is None:
            return

        if map_type_override is None:
            item.map_type_override = None
        elif isinstance(map_type_override, TextureMapType):
            item.map_type_override = map_type_override
        else:
            return

        self._render_queue()
        self._sync_workspace_selection()
        self._sync_status_bar_with_selection()

    def _selected_queue_item(self) -> QueueItem | None:
        selection_model = self.queue_panel.table.selectionModel()
        if selection_model is None:
            return None

        selected_rows = selection_model.selectedRows()
        if not selected_rows:
            return None

        row = selected_rows[0].row()
        if row >= len(self._queue_items):
            return None
        return self._queue_items[row]

    def _selected_queue_key(self) -> str | None:
        item = self._selected_queue_item()
        if item is None:
            return None
        return self._queue_key(item.path)

    def _restore_queue_selection(self, key: str | None) -> None:
        if not self._queue_items:
            return

        target_row = 0
        if key is not None:
            for row, item in enumerate(self._queue_items):
                if self._queue_key(item.path) == key:
                    target_row = row
                    break

        self.queue_panel.table.selectRow(target_row)

    def _apply_default_splitter_sizes(self) -> None:
        self.main_splitter.setSizes([340, 940])
        self.workspace_splitter.setSizes([500, 440])
        self.detail_splitter.setSizes([420, 220])
        self.inspector_splitter.setSizes([500, 190])
