from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor, QCloseEvent, QDragEnterEvent, QDropEvent
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QFileDialog,
    QFormLayout,
    QFrame,
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
    QueueItem,
    QueueStatus,
    ResizeMode,
)
from image_converter.services.conversion import BatchConversionService

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
        root_layout.setContentsMargins(12, 12, 12, 12)
        root_layout.setSpacing(12)

        root_layout.addWidget(self._build_paths_group())
        root_layout.addWidget(self._build_basic_group())
        root_layout.addWidget(self._build_png_group())
        root_layout.addWidget(self._build_resize_group())
        root_layout.addStretch(1)

        controls_row = QHBoxLayout()
        self.convert_button = QPushButton("Конвертировать")
        self.convert_button.setDefault(True)
        self.convert_button.clicked.connect(self.convert_requested.emit)
        self.convert_button.setToolTip("Запустить пакетную конвертацию в PNG.")
        controls_row.addWidget(self.convert_button)
        controls_row.addStretch(1)
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
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        title_row = QHBoxLayout()
        title_row.addWidget(QLabel("Очередь задач"))
        title_row.addStretch(1)

        self.add_files_button = QPushButton("Добавить файлы")
        self.add_files_button.clicked.connect(self._pick_files)
        title_row.addWidget(self.add_files_button)

        self.add_folder_button = QPushButton("Добавить папку")
        self.add_folder_button.clicked.connect(self._pick_folder)
        title_row.addWidget(self.add_folder_button)

        self.remove_selected_button = QPushButton("Удалить выбранные")
        self.remove_selected_button.clicked.connect(self.remove_requested.emit)
        title_row.addWidget(self.remove_selected_button)

        self.clear_button = QPushButton("Очистить")
        self.clear_button.clicked.connect(self.clear_requested.emit)
        title_row.addWidget(self.clear_button)

        layout.addLayout(title_row)

        self.drop_hint = QLabel(
            "Перетащите файлы или папки сюда. Можно смешивать отдельные текстуры и каталоги."
        )
        self.drop_hint.setWordWrap(True)
        self.drop_hint.setStyleSheet("color: #b7d8ff; background: #1e2c3b; padding: 8px; border-radius: 4px;")
        layout.addWidget(self.drop_hint)

        self.table = QueueTableWidget()
        self.table.setColumnCount(len(QUEUE_HEADERS))
        self.table.setHorizontalHeaderLabels(QUEUE_HEADERS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
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


class LogPanel(QWidget):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        layout.addWidget(QLabel("Журнал"))
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
        self.settings_panel = SettingsPanel()
        self.settings_panel.convert_requested.connect(self.convert_requested.emit)
        self.settings_panel.output_path_changed.connect(self._update_queue_output_paths)

        settings_scroll = QScrollArea()
        settings_scroll.setWidgetResizable(True)
        settings_scroll.setFrameShape(QFrame.Shape.NoFrame)
        settings_scroll.setWidget(self.settings_panel)

        self.queue_panel = QueuePanel()
        self.queue_panel.paths_selected.connect(self.queue_paths_received.emit)
        self.queue_panel.paths_dropped.connect(self.queue_paths_received.emit)
        self.queue_panel.remove_requested.connect(self.remove_selected_queue_items)
        self.queue_panel.clear_requested.connect(self.clear_queue_items)
        self.queue_panel.table.itemSelectionChanged.connect(self._sync_status_bar_with_selection)

        self.log_panel = LogPanel()

        right_splitter = QSplitter(Qt.Orientation.Vertical)
        right_splitter.addWidget(self.queue_panel)
        right_splitter.addWidget(self.log_panel)
        right_splitter.setChildrenCollapsible(False)
        right_splitter.setStretchFactor(0, 3)
        right_splitter.setStretchFactor(1, 2)
        right_splitter.setSizes([420, 260])

        self.main_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.main_splitter.addWidget(settings_scroll)
        self.main_splitter.addWidget(right_splitter)
        self.main_splitter.setChildrenCollapsible(False)
        self.main_splitter.setStretchFactor(0, 0)
        self.main_splitter.setStretchFactor(1, 1)
        self.main_splitter.setSizes([420, 860])

        self.setCentralWidget(self.main_splitter)

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

    def set_queue_item_running(self, path: Path) -> None:
        item = self._find_queue_item(path)
        if item is None:
            return
        item.status = QueueStatus.RUNNING
        item.message = ""
        self._render_queue()

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
        table.setRowCount(len(self._queue_items))

        for row, item in enumerate(self._queue_items):
            metadata = item.metadata
            output_text = str(item.output_path) if item.output_path is not None else "-"

            row_items = [
                self._make_table_item(item.path.name, tooltip=str(item.path)),
                self._make_table_item(self._asset_type_text(item), tooltip=self._metadata_tooltip(metadata)),
                self._make_table_item(metadata.size_text if metadata else "-", tooltip=self._metadata_tooltip(metadata)),
                self._make_table_item(metadata.resolution_text if metadata else "-", tooltip=self._metadata_tooltip(metadata)),
                self._make_table_item(_status_text(item.status), tooltip=item.message or _status_text(item.status)),
                self._make_table_item(output_text, tooltip=output_text),
            ]
            row_items[0].setData(Qt.ItemDataRole.UserRole, self._queue_key(item.path))

            for column, table_item in enumerate(row_items):
                table.setItem(row, column, table_item)

            self._apply_row_style(row, item)

        if not self._queue_items:
            self.set_status("Очередь пуста")

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

    def _sync_status_bar_with_selection(self) -> None:
        selected_rows = self.queue_panel.table.selectionModel().selectedRows()
        if not selected_rows:
            return

        row = selected_rows[0].row()
        if row >= len(self._queue_items):
            return

        item = self._queue_items[row]
        message = item.message or _status_text(item.status)
        self.set_status(f"{item.path.name}: {message}")
