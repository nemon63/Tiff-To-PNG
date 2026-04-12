from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QCloseEvent
from PyQt6.QtWidgets import (
    QCheckBox,
    QFrame,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
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
    QVBoxLayout,
    QWidget,
)

from image_converter.domain.constants import FILE_DIALOG_FILTER
from image_converter.domain.models import BatchRequest, ConversionOptions, ResizeMode


class SettingsPanel(QWidget):
    convert_requested = pyqtSignal()

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
        self.input_file_button.setToolTip("Выбрать один файл для конвертации.")
        input_row.addWidget(self.input_file_button)
        self.input_folder_button = QPushButton("Папка")
        self.input_folder_button.clicked.connect(self._pick_input_folder)
        self.input_folder_button.setToolTip("Выбрать папку для пакетной обработки.")
        input_row.addWidget(self.input_folder_button)

        input_wrapper = QWidget()
        input_wrapper.setLayout(input_row)
        layout.addRow("Вход:", input_wrapper)

        self.output_edit = QLineEdit()
        self.output_edit.setPlaceholderText("Папка назначения")
        self.output_edit.setToolTip(
            "Папка для PNG. Если оставить пустой, файлы будут сохранены рядом с исходниками."
        )

        output_row = QHBoxLayout()
        output_row.addWidget(self.output_edit)
        self.output_button = QPushButton("Выбрать")
        self.output_button.clicked.connect(self._pick_output_folder)
        self.output_button.setToolTip("Выбрать выходную папку.")
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
        self.recursive_checkbox.setToolTip(
            "Если выбран каталог, будут обработаны и вложенные папки."
        )
        layout.addWidget(self.recursive_checkbox)

        self.force_rgba_checkbox = QCheckBox("Принудительно сохранять как RGBA")
        self.force_rgba_checkbox.setToolTip("Полезно, если нужен гарантированный альфа-канал.")
        layout.addWidget(self.force_rgba_checkbox)

        self.overwrite_checkbox = QCheckBox("Перезаписывать существующие PNG")
        self.overwrite_checkbox.setToolTip(
            "Если выключено, уже существующие PNG будут пропущены."
        )
        layout.addWidget(self.overwrite_checkbox)

        self.delete_source_checkbox = QCheckBox("Удалять исходники после успешной конвертации")
        self.delete_source_checkbox.setToolTip(
            "Удаление выполняется только после успешного сохранения PNG."
        )
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
        self.optimize_checkbox.setToolTip("Дополнительная оптимизация PNG, обычно уменьшает размер.")
        layout.addWidget(self.optimize_checkbox)

        compress_row = QHBoxLayout()
        compress_row.addWidget(QLabel("Степень сжатия:"))
        self.compress_spin = QSpinBox()
        self.compress_spin.setRange(0, 9)
        self.compress_spin.setValue(6)
        self.compress_spin.setToolTip("0 = быстрее, 9 = меньше размер файла.")
        compress_row.addWidget(self.compress_spin)
        compress_row.addStretch(1)
        layout.addLayout(compress_row)

        palette_row = QHBoxLayout()
        self.png8_checkbox = QCheckBox("PNG-8")
        self.png8_checkbox.toggled.connect(self._update_png8_state)
        self.png8_checkbox.setToolTip("Сохранять PNG в палитровом режиме до 256 цветов.")
        palette_row.addWidget(self.png8_checkbox)
        palette_row.addWidget(QLabel("Цветов:"))
        self.png8_colors_spin = QSpinBox()
        self.png8_colors_spin.setRange(2, 256)
        self.png8_colors_spin.setValue(256)
        self.png8_colors_spin.setToolTip("Размер палитры PNG-8.")
        palette_row.addWidget(self.png8_colors_spin)
        self.dither_checkbox = QCheckBox("Dithering")
        self.dither_checkbox.setChecked(True)
        self.dither_checkbox.setToolTip("Смягчает переходы цветов при палитровом сохранении.")
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
        self.resize_none_radio.setToolTip("Оставить исходный размер изображения.")
        layout.addWidget(self.resize_none_radio)

        percent_row = QHBoxLayout()
        self.resize_percent_radio = QRadioButton("Процент от оригинала")
        self.resize_percent_radio.toggled.connect(self._update_resize_state)
        percent_row.addWidget(self.resize_percent_radio)
        self.resize_percent_spin = QSpinBox()
        self.resize_percent_spin.setRange(1, 1000)
        self.resize_percent_spin.setValue(100)
        self.resize_percent_spin.setSuffix(" %")
        self.resize_percent_spin.setToolTip("50 уменьшит изображение вдвое, 200 увеличит вдвое.")
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
        self.max_side_spin.setToolTip("Пропорции сохраняются автоматически.")
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

    def set_controls_enabled(self, enabled: bool) -> None:
        for widget in self._interactive_widgets:
            widget.setEnabled(enabled)

        if enabled:
            self._update_resize_state()
            self._update_png8_state()


class LogPanel(QWidget):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        title = QLabel("Журнал")
        layout.addWidget(title)

        self.log_edit = QPlainTextEdit()
        self.log_edit.setReadOnly(True)
        self.log_edit.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.log_edit.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.log_edit.setToolTip("Здесь отображается ход конвертации и возможные ошибки.")
        layout.addWidget(self.log_edit, 1)

    def append_line(self, line: str) -> None:
        self.log_edit.appendPlainText(line)
        scrollbar = self.log_edit.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def clear(self) -> None:
        self.log_edit.clear()


class MainWindow(QMainWindow):
    convert_requested = pyqtSignal()

    def __init__(self):
        super().__init__()
        self._is_running = False
        self.setWindowTitle("Конвертер изображений в PNG")
        self.resize(1180, 780)
        self.setMinimumSize(920, 640)
        self._build_ui()
        self.statusBar().showMessage("Готово")

    def _build_ui(self) -> None:
        self.settings_panel = SettingsPanel()
        self.settings_panel.convert_requested.connect(self.convert_requested.emit)

        settings_scroll = QScrollArea()
        settings_scroll.setWidgetResizable(True)
        settings_scroll.setFrameShape(QFrame.Shape.NoFrame)
        settings_scroll.setWidget(self.settings_panel)

        self.log_panel = LogPanel()

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(settings_scroll)
        splitter.addWidget(self.log_panel)
        splitter.setChildrenCollapsible(False)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([420, 740])

        self.setCentralWidget(splitter)

    def build_request(self) -> BatchRequest:
        return self.settings_panel.build_request()

    def set_running(self, running: bool) -> None:
        self._is_running = running
        self.settings_panel.set_controls_enabled(not running)

    def append_log(self, line: str) -> None:
        self.log_panel.append_line(line)

    def clear_log(self) -> None:
        self.log_panel.clear()

    def set_status(self, text: str) -> None:
        self.statusBar().showMessage(text)

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
