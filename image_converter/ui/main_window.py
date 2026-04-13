from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import (
    QColor,
    QCloseEvent,
    QDragEnterEvent,
    QDropEvent,
    QImage,
    QPainter,
    QPixmap,
)
from PyQt6.QtWidgets import (
    QAbstractItemView,
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
    QVBoxLayout,
    QWidget,
)

from image_converter.domain.constants import FILE_DIALOG_FILTER
from image_converter.domain.models import (
    AppSettings,
    AssetMetadata,
    BatchRequest,
    BatchSource,
    ConversionOptions,
    PreviewChannel,
    QueueItem,
    QueueStatus,
    ResizeMode,
)
from image_converter.services.conversion import BatchConversionService
from image_converter.services.preview import TexturePreviewService

QUEUE_HEADERS = ("Имя", "Тип", "Размер", "Разрешение", "Статус", "Выходной путь")


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
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._source_pixmap: QPixmap | None = None
        self._placeholder_text = "Выберите текстуру из очереди, чтобы открыть preview stage."
        self.setMinimumHeight(320)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def set_preview_pixmap(self, pixmap: QPixmap) -> None:
        self._source_pixmap = pixmap
        self.update()

    def clear_preview(self, text: str = "Выберите текстуру из очереди, чтобы открыть preview stage.") -> None:
        self._source_pixmap = None
        self._placeholder_text = text
        self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        frame_rect = self.rect().adjusted(1, 1, -1, -1)
        painter.setPen(QColor("#31404D"))
        painter.setBrush(QColor("#1C232B"))
        painter.drawRoundedRect(frame_rect, 18, 18)

        inner_rect = frame_rect.adjusted(22, 22, -22, -22)
        if inner_rect.width() <= 0 or inner_rect.height() <= 0:
            return

        self._draw_checkerboard(painter, inner_rect)

        if self._source_pixmap is None:
            painter.setPen(QColor("#8F9CAA"))
            painter.drawText(
                inner_rect,
                Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
                self._placeholder_text,
            )
            return

        scaled = self._source_pixmap.scaled(
            inner_rect.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        x = inner_rect.x() + (inner_rect.width() - scaled.width()) // 2
        y = inner_rect.y() + (inner_rect.height() - scaled.height()) // 2
        painter.drawPixmap(x, y, scaled)

    def _draw_checkerboard(self, painter: QPainter, rect) -> None:
        tile_size = 18
        light_color = QColor("#34414C")
        dark_color = QColor("#29333D")

        for top in range(rect.top(), rect.bottom() + 1, tile_size):
            row_index = (top - rect.top()) // tile_size
            for left in range(rect.left(), rect.right() + 1, tile_size):
                column_index = (left - rect.left()) // tile_size
                color = light_color if (row_index + column_index) % 2 == 0 else dark_color
                painter.fillRect(left, top, tile_size, tile_size, color)


class MetricTile(QFrame):
    def __init__(self, label_text: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("MetricCard")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(2)

        label = QLabel(label_text)
        label.setObjectName("MetricLabel")
        layout.addWidget(label)

        self.value_label = QLabel("-")
        self.value_label.setObjectName("MetricValue")
        self.value_label.setWordWrap(True)
        layout.addWidget(self.value_label)

    def set_value(self, text: str) -> None:
        self.value_label.setText(text)


class PreviewPanel(QWidget):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._preview_service = TexturePreviewService()
        self._current_item: QueueItem | None = None
        self._build_ui()

    def _build_ui(self) -> None:
        self.setObjectName("SectionPanel")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(12)

        header_row = QHBoxLayout()
        title_stack = QVBoxLayout()

        title_label = QLabel("Preview Stage")
        title_label.setObjectName("PanelTitle")
        title_stack.addWidget(title_label)

        subtitle_label = QLabel("Просмотр по каналам и быстрый визуальный контроль текстуры.")
        subtitle_label.setObjectName("PanelSubtitle")
        subtitle_label.setWordWrap(True)
        title_stack.addWidget(subtitle_label)

        header_row.addLayout(title_stack, 1)

        channel_stack = QVBoxLayout()
        channel_label = QLabel("Активный канал")
        channel_label.setObjectName("SummaryText")
        channel_stack.addWidget(channel_label)

        self.channel_combo = QComboBox()
        self.channel_combo.currentIndexChanged.connect(self._refresh_preview)
        channel_stack.addWidget(self.channel_combo)
        header_row.addLayout(channel_stack)

        layout.addLayout(header_row)

        self.preview_canvas = PreviewCanvas()
        layout.addWidget(self.preview_canvas, 1)

        self.asset_label = QLabel("Ничего не выбрано")
        self.asset_label.setObjectName("AssetName")
        self.asset_label.setWordWrap(True)
        layout.addWidget(self.asset_label)

        self.asset_meta_label = QLabel(
            "Выберите строку в очереди, чтобы оценить форму, alpha и отдельные каналы."
        )
        self.asset_meta_label.setObjectName("SummaryText")
        self.asset_meta_label.setWordWrap(True)
        layout.addWidget(self.asset_meta_label)

    def set_queue_item(self, item: QueueItem | None) -> None:
        self._current_item = item
        self._rebuild_channels(item)
        self._refresh_preview()

    def _rebuild_channels(self, item: QueueItem | None) -> None:
        current_channel = self.channel_combo.currentData()
        self.channel_combo.blockSignals(True)
        self.channel_combo.clear()

        channels = self._available_channels(item)
        for channel in channels:
            self.channel_combo.addItem(_preview_channel_label(channel), channel)

        self.channel_combo.setEnabled(bool(channels))

        if current_channel in channels:
            self.channel_combo.setCurrentIndex(channels.index(current_channel))
        elif channels:
            self.channel_combo.setCurrentIndex(0)

        self.channel_combo.blockSignals(False)

    def _available_channels(self, item: QueueItem | None) -> list[PreviewChannel]:
        if item is None or item.metadata is None or item.status is QueueStatus.ERROR:
            return []

        channels = [
            PreviewChannel.COMPOSITE,
            PreviewChannel.RED,
            PreviewChannel.GREEN,
            PreviewChannel.BLUE,
            PreviewChannel.LUMA,
        ]
        if item.metadata.has_alpha:
            channels.append(PreviewChannel.ALPHA)
        return channels

    def _refresh_preview(self) -> None:
        item = self._current_item
        if item is None:
            self.preview_canvas.clear_preview()
            self._update_footer(item)
            return

        if item.status is QueueStatus.ERROR:
            self.preview_canvas.clear_preview("Предпросмотр недоступен для поврежденного или неподдерживаемого файла.")
            self._update_footer(item)
            return

        if self.channel_combo.count() == 0:
            self.preview_canvas.clear_preview("Нет доступных каналов для предпросмотра.")
            self._update_footer(item)
            return

        try:
            channel = self.channel_combo.currentData()
            if channel is None:
                channel = PreviewChannel.COMPOSITE
            preview_image = self._preview_service.render(item.path, channel)
            preview_pixmap = QPixmap.fromImage(_qimage_from_pil(preview_image))
            self.preview_canvas.set_preview_pixmap(preview_pixmap)
        except Exception as exc:
            self.preview_canvas.clear_preview(f"Ошибка предпросмотра: {exc}")

        self._update_footer(item)

    def _update_footer(self, item: QueueItem | None) -> None:
        if item is None:
            self.asset_label.setText("Ничего не выбрано")
            self.asset_meta_label.setText(
                "Выберите строку в очереди, чтобы оценить форму, alpha и отдельные каналы."
            )
            return

        self.asset_label.setText(item.path.name)
        metadata = item.metadata
        if metadata is None:
            self.asset_meta_label.setText(item.message or "Метаданные недоступны.")
            return

        channel_name = self.channel_combo.currentText() or "RGB"
        alpha_state = "alpha" if metadata.has_alpha else "opaque"
        self.asset_meta_label.setText(
            f"{metadata.format_name} · {metadata.resolution_text} · {metadata.mode} · канал: {channel_name} · {alpha_state}"
        )


class MetadataPanel(QWidget):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._build_ui()

    def _build_ui(self) -> None:
        self.setObjectName("SectionPanel")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(12)

        title_label = QLabel("Asset Inspector")
        title_label.setObjectName("PanelTitle")
        layout.addWidget(title_label)

        subtitle_label = QLabel("Технический срез выбранной текстуры и preflight-предупреждения.")
        subtitle_label.setObjectName("PanelSubtitle")
        subtitle_label.setWordWrap(True)
        layout.addWidget(subtitle_label)

        path_caption = QLabel("Исходный файл")
        path_caption.setObjectName("PathCaption")
        layout.addWidget(path_caption)

        self.path_label = QLabel("-")
        self.path_label.setObjectName("PathValue")
        self.path_label.setWordWrap(True)
        self.path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.path_label)

        metrics_grid = QGridLayout()
        metrics_grid.setHorizontalSpacing(10)
        metrics_grid.setVerticalSpacing(10)

        self.format_tile = MetricTile("Формат")
        self.resolution_tile = MetricTile("Разрешение")
        self.mode_tile = MetricTile("Режим")
        self.alpha_tile = MetricTile("Alpha")
        self.frames_tile = MetricTile("Кадры")
        self.size_tile = MetricTile("Размер")

        metric_tiles = (
            self.format_tile,
            self.resolution_tile,
            self.mode_tile,
            self.alpha_tile,
            self.frames_tile,
            self.size_tile,
        )
        for index, tile in enumerate(metric_tiles):
            metrics_grid.addWidget(tile, index // 2, index % 2)

        layout.addLayout(metrics_grid)

        output_caption = QLabel("Выходной PNG")
        output_caption.setObjectName("OutputCaption")
        layout.addWidget(output_caption)

        self.output_label = QLabel("Будет рассчитан после выбора выходной папки.")
        self.output_label.setObjectName("OutputValue")
        self.output_label.setWordWrap(True)
        self.output_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.output_label)

        self.warning_label = QLabel("")
        self.warning_label.setObjectName("WarningBanner")
        self.warning_label.setWordWrap(True)
        self.warning_label.hide()
        layout.addWidget(self.warning_label)
        layout.addStretch(1)

    def set_queue_item(self, item: QueueItem | None) -> None:
        if item is None:
            self.path_label.setText("-")
            self.output_label.setText("Будет рассчитан после выбора выходной папки.")
            self._set_metric_values("-", "-", "-", "-", "-", "-")
            self.warning_label.hide()
            return

        self.path_label.setText(str(item.path))
        self.output_label.setText(
            str(item.output_path) if item.output_path is not None else "Рядом с исходным файлом."
        )

        metadata = item.metadata
        if metadata is None:
            self._set_metric_values("-", "-", "-", "-", "-", "-")
            self.warning_label.setText(item.message or "Не удалось прочитать метаданные.")
            self.warning_label.show()
            return

        self._set_metric_values(
            metadata.format_name,
            metadata.resolution_text,
            metadata.mode,
            "Да" if metadata.has_alpha else "Нет",
            str(metadata.frame_count),
            metadata.size_text,
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

    def _set_metric_values(
        self,
        format_value: str,
        resolution_value: str,
        mode_value: str,
        alpha_value: str,
        frames_value: str,
        size_value: str,
    ) -> None:
        self.format_tile.set_value(format_value)
        self.resolution_tile.set_value(resolution_value)
        self.mode_tile.set_value(mode_value)
        self.alpha_tile.set_value(alpha_value)
        self.frames_tile.set_value(frames_value)
        self.size_tile.set_value(size_value)


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
        self.log_panel = LogPanel()

        self.inspector_splitter = QSplitter(Qt.Orientation.Vertical)
        self.inspector_splitter.addWidget(self.metadata_panel)
        self.inspector_splitter.addWidget(self.log_panel)
        self.inspector_splitter.setChildrenCollapsible(False)
        self.inspector_splitter.setStretchFactor(0, 3)
        self.inspector_splitter.setStretchFactor(1, 2)

        self.detail_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.detail_splitter.addWidget(self.preview_panel)
        self.detail_splitter.addWidget(self.inspector_splitter)
        self.detail_splitter.setChildrenCollapsible(False)
        self.detail_splitter.setStretchFactor(0, 3)
        self.detail_splitter.setStretchFactor(1, 2)

        self.workspace_splitter = QSplitter(Qt.Orientation.Vertical)
        self.workspace_splitter.addWidget(self.queue_panel)
        self.workspace_splitter.addWidget(self.detail_splitter)
        self.workspace_splitter.setChildrenCollapsible(False)
        self.workspace_splitter.setStretchFactor(0, 3)
        self.workspace_splitter.setStretchFactor(1, 4)

        workspace_card = QFrame()
        workspace_card.setObjectName("WorkspaceCard")
        workspace_layout = QVBoxLayout(workspace_card)
        workspace_layout.setContentsMargins(18, 18, 18, 18)
        workspace_layout.setSpacing(14)

        workspace_title = QLabel("Texture Desk")
        workspace_title.setObjectName("PanelTitle")
        workspace_layout.addWidget(workspace_title)

        workspace_subtitle = QLabel(
            "Очередь, preview-stage и preflight-анализ собраны в одном рабочем полотне."
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
                self._make_table_item(self._asset_type_text(item), tooltip=self._metadata_tooltip(metadata)),
                self._make_table_item(metadata.size_text if metadata else "-", tooltip=self._metadata_tooltip(metadata)),
                self._make_table_item(metadata.resolution_text if metadata else "-", tooltip=self._metadata_tooltip(metadata)),
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
        if item.metadata is not None and item.metadata.format_name:
            return item.metadata.format_name
        suffix = item.path.suffix.removeprefix(".").upper()
        return suffix or "IMAGE"

    def _metadata_tooltip(self, metadata: AssetMetadata | None) -> str:
        if metadata is None:
            return ""

        lines = [
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
        self.main_splitter.setSizes([360, 880])
        self.workspace_splitter.setSizes([240, 390])
        self.detail_splitter.setSizes([540, 300])
        self.inspector_splitter.setSizes([230, 150])
