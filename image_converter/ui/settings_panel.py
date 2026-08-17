from __future__ import annotations

from html import escape
from pathlib import Path

from PyQt6.QtCore import QSize, Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QSizePolicy,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from image_converter.domain.constants import FILE_DIALOG_FILTER
from image_converter.domain.models import (
    AppSettings,
    BatchRequest,
    ChannelPackLayout,
    ChannelPackingMode,
    ChannelPackingOptions,
    ConversionOptions,
    ConversionPreset,
    NamingRules,
    ResizeMode,
    TextureMapType,
)
from image_converter.services.naming import build_output_filename
from image_converter.services.packing import channel_pack_mapping_text
from image_converter.ui.common import CURRENT_PRESET_DATA


class TextureSetSummaryLabel(QLabel):
    """Readable rich-text view that keeps the plain summary available to callers."""

    def __init__(self, text: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        self._plain_summary = ""
        self._rendered_html = ""
        self.setWordWrap(True)
        self.setTextFormat(Qt.TextFormat.RichText)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.set_summary_text(text)

    def set_summary_text(self, text: str) -> None:
        self._plain_summary = text
        self._rendered_html = self._build_html(text)
        super().setText(self._rendered_html)

    def text(self) -> str:
        return self._plain_summary

    @property
    def rendered_html(self) -> str:
        return self._rendered_html

    @staticmethod
    def _build_html(text: str) -> str:
        lines = [line.rstrip() for line in text.splitlines() if line.strip()]
        if not lines:
            return ""

        parts = [
            '<div style="font-size:12px; color:#d7e2ef;">',
            (
                '<div style="font-size:13px; font-weight:600; color:#f2f6fb; '
                'margin-bottom:4px;">'
                f"{escape(lines[0])}</div>"
            ),
        ]
        output_rows: list[str] = []

        def flush_rows() -> None:
            if not output_rows:
                return
            parts.append(
                '<table width="100%" cellspacing="2" cellpadding="2" '
                'style="margin-top:4px; margin-bottom:4px;">'
            )
            parts.extend(output_rows)
            parts.append("</table>")
            output_rows.clear()

        for line in lines[1:]:
            if "найден источник" in line or "выход, предусмотренный пайплайном" in line:
                flush_rows()
                parts.append(
                    '<div style="margin:3px 0 7px 0; font-size:11px; color:#8fa3b8;">'
                    f"{escape(line.strip())}</div>"
                )
                continue
            if line[0] in {"✓", "○", "●"}:
                status = line[0]
                body = line[1:].strip()
                title, filename, description = TextureSetSummaryLabel._split_output_line(body)
                status_color = {
                    "✓": "#72d694",
                    "○": "#f0b95b",
                    "●": "#68aef2",
                }[status]
                detail_parts: list[str] = []
                if filename:
                    detail_parts.append(
                        f'<span style="color:#8fc7ff;">{escape(filename)}</span>'
                    )
                if description:
                    detail_parts.append(
                        f'<span style="color:#9eafc1;">{escape(description)}</span>'
                    )
                details = " &nbsp;·&nbsp; ".join(detail_parts)
                detail_html = f'<br/><span style="font-size:10px;">{details}</span>' if details else ""
                output_rows.append(
                    '<tr bgcolor="#202832">'
                    f'<td width="16" valign="top"><span style="font-size:12px; color:{status_color};">'
                    f"{status}</span></td>"
                    '<td valign="top">'
                    f'<span style="font-size:11px; font-weight:600; color:#edf4fb;">'
                    f"{escape(title)}</span>{detail_html}</td></tr>"
                )
                continue

            flush_rows()
            stripped = line.strip()
            if stripped.startswith("Каналы:"):
                parts.append(
                    '<div style="margin:1px 0 7px 25px; color:#9eb7d1;">'
                    f"{escape(stripped)}</div>"
                )
            elif stripped.startswith("Не будут созданы"):
                parts.append(
                    '<div style="margin-top:7px; padding:5px; color:#f0bd69; '
                    'background-color:#332b20;">'
                    f"{escape(stripped)}</div>"
                )
            elif stripped.startswith(("- ", "Дополнительно", "Наборы:")):
                parts.append(
                    '<div style="margin-top:5px; color:#b7c5d4;">'
                    f"{escape(stripped.removeprefix('- '))}</div>"
                )
            else:
                parts.append(
                    '<div style="margin-top:4px; color:#899caf;">'
                    f"{escape(stripped)}</div>"
                )

        flush_rows()
        parts.append("</div>")
        return "".join(parts)

    @staticmethod
    def _split_output_line(body: str) -> tuple[str, str, str]:
        title = body
        filename = ""
        description = ""
        if " → " in body:
            title, remainder = body.split(" → ", 1)
            if " — " in remainder:
                filename, description = remainder.split(" — ", 1)
            else:
                filename = remainder
        return title.strip(), filename.strip(), description.strip()


class SettingsPanel(QWidget):
    convert_requested = pyqtSignal()
    output_path_changed = pyqtSignal(str)
    preset_apply_requested = pyqtSignal(str)
    preset_save_requested = pyqtSignal()
    preset_delete_requested = pyqtSignal(str)
    options_changed = pyqtSignal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._interactive_widgets: list[QWidget] = []
        self._presets_by_id: dict[str, ConversionPreset] = {}
        self._suppress_option_signal = False
        self._packing_preflight_text = "Подходящих наборов для packing пока нет."
        self._packing_mode_text = ""
        self._build_ui()
        self._connect_option_change_signals()
        self._update_resize_state()
        self._update_png8_state()
        self._refresh_naming_ui()
        self._refresh_packing_ui()
        self.set_available_presets([])

    def _build_ui(self) -> None:
        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(18, 18, 18, 18)
        root_layout.setSpacing(16)

        title_label = QLabel("Параметры экспорта")
        title_label.setObjectName("PanelTitle")
        root_layout.addWidget(title_label)

        subtitle_label = QLabel("Сценарий обработки, папка результата и правила экспорта.")
        subtitle_label.setObjectName("PanelSubtitle")
        subtitle_label.setWordWrap(True)
        root_layout.addWidget(subtitle_label)

        self.settings_tabs = QTabWidget()
        self.settings_tabs.tabBar().setExpanding(True)
        self.settings_tabs.tabBar().setUsesScrollButtons(False)
        scenario_index = self.settings_tabs.addTab(
            self._build_tab(
                self._build_presets_group(),
                self._build_output_bundle_group(),
                self._build_paths_group(),
            ),
            "Сценарий",
        )
        naming_index = self.settings_tabs.addTab(
            self._build_tab(
                self._build_naming_group(),
                self._build_packing_group(),
            ),
            "Каналы",
        )
        format_index = self.settings_tabs.addTab(
            self._build_tab(
                self._build_basic_group(),
                self._build_png_group(),
                self._build_resize_group(),
            ),
            "Формат",
        )
        self.settings_tabs.setTabToolTip(scenario_index, "Готовый сценарий и папка результата.")
        self.settings_tabs.setTabToolTip(naming_index, "Правила именования файлов и упаковка каналов.")
        self.settings_tabs.setTabToolTip(format_index, "Формат PNG, сжатие, перезапись и размер текстур.")
        root_layout.addWidget(self.settings_tabs, 1)

        controls_row = QHBoxLayout()
        self.convert_button = QPushButton("Запустить обработку")
        self.convert_button.setObjectName("PrimaryButton")
        self.convert_button.setMinimumHeight(42)
        self.convert_button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.convert_button.setDefault(True)
        self.convert_button.clicked.connect(self.convert_requested.emit)
        self.convert_button.setToolTip("Запустить пакетную обработку по выбранному сценарию.")
        controls_row.addWidget(self.convert_button)
        root_layout.addLayout(controls_row)
        self._register_interactive(self.convert_button)

    def _build_tab(self, *groups: QGroupBox) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(12)
        for group in groups:
            layout.addWidget(group)
        layout.addStretch(1)
        return tab

    def _build_presets_group(self) -> QGroupBox:
        group = QGroupBox("Готовые сценарии")
        layout = QVBoxLayout(group)
        layout.setSpacing(10)

        self.preset_combo = QComboBox()
        self.preset_combo.currentIndexChanged.connect(self._on_preset_selection_changed)
        layout.addWidget(self.preset_combo)

        actions_row = QHBoxLayout()
        actions_row.setSpacing(8)

        self.save_preset_button = QPushButton("Сохранить как...")
        self.save_preset_button.setObjectName("GhostButton")
        self.save_preset_button.clicked.connect(self.preset_save_requested.emit)
        actions_row.addWidget(self.save_preset_button, 1)

        self.delete_preset_button = QPushButton("Удалить")
        self.delete_preset_button.setObjectName("DangerButton")
        self.delete_preset_button.clicked.connect(self._emit_delete_selected_preset)
        actions_row.addWidget(self.delete_preset_button)

        layout.addLayout(actions_row)

        self._register_interactive(
            self.preset_combo,
            self.save_preset_button,
            self.delete_preset_button,
        )
        return group

    def _build_output_bundle_group(self) -> QGroupBox:
        group = QGroupBox("Итоговый Texture Set")
        layout = QVBoxLayout(group)
        layout.setSpacing(10)

        self.output_bundle_summary_label = TextureSetSummaryLabel(
            "Выберите сценарий и добавьте файлы в очередь, чтобы увидеть ожидаемый состав экспорта."
        )
        self.output_bundle_summary_label.setObjectName("SummaryText")
        layout.addWidget(self.output_bundle_summary_label)
        return group

    def _build_paths_group(self) -> QGroupBox:
        group = QGroupBox("Папка результата")
        layout = QVBoxLayout(group)
        layout.setSpacing(10)

        info_label = QLabel(
            "Вход всегда берется из очереди справа.\n"
            "Здесь указывается только общая папка, куда будут сохранены PNG и packed texture."
        )
        info_label.setObjectName("SummaryText")
        info_label.setWordWrap(True)
        layout.addWidget(info_label)

        self.output_edit = QLineEdit()
        self.output_edit.setPlaceholderText("Общая папка результата")
        self.output_edit.textChanged.connect(self.output_path_changed.emit)
        self.output_edit.setToolTip(
            "Базовая папка результата. Если оставить пустой, PNG и packed texture будут сохранены рядом с исходниками."
        )

        output_row = QHBoxLayout()
        output_row.addWidget(self.output_edit)
        self.output_button = QPushButton("Выбрать")
        self.output_button.clicked.connect(self._pick_output_folder)
        output_row.addWidget(self.output_button)

        output_wrapper = QWidget()
        output_wrapper.setLayout(output_row)
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        form.setFormAlignment(Qt.AlignmentFlag.AlignTop)
        form.addRow("Папка:", output_wrapper)
        layout.addLayout(form)

        self._register_interactive(
            self.output_edit,
            self.output_button,
        )
        return group

    def _build_naming_group(self) -> QGroupBox:
        group = QGroupBox("Имена файлов")
        layout = QVBoxLayout(group)
        layout.setSpacing(10)

        self.naming_lowercase_checkbox = QCheckBox("Приводить имена к lowercase")
        layout.addWidget(self.naming_lowercase_checkbox)

        self.naming_replace_spaces_checkbox = QCheckBox("Заменять пробелы на _")
        layout.addWidget(self.naming_replace_spaces_checkbox)

        self.naming_normalize_suffix_checkbox = QCheckBox(
            "Нормализовать suffix карты по map type"
        )
        layout.addWidget(self.naming_normalize_suffix_checkbox)

        self.naming_summary_label = QLabel()
        self.naming_summary_label.setObjectName("SummaryText")
        self.naming_summary_label.setWordWrap(True)
        layout.addWidget(self.naming_summary_label)

        self.naming_lowercase_checkbox.toggled.connect(self._refresh_naming_ui)
        self.naming_replace_spaces_checkbox.toggled.connect(self._refresh_naming_ui)
        self.naming_normalize_suffix_checkbox.toggled.connect(self._refresh_naming_ui)

        self._register_interactive(
            self.naming_lowercase_checkbox,
            self.naming_replace_spaces_checkbox,
            self.naming_normalize_suffix_checkbox,
        )
        return group

    def _build_packing_group(self) -> QGroupBox:
        group = QGroupBox("Упаковка каналов")
        layout = QVBoxLayout(group)
        layout.setSpacing(10)

        self.pack_enable_checkbox = QCheckBox("Собрать packed texture из набора карт")
        self.pack_enable_checkbox.toggled.connect(self._refresh_packing_ui)
        layout.addWidget(self.pack_enable_checkbox)

        self.unpack_packed_checkbox = QCheckBox(
            "Распаковывать найденные packed textures в отдельные карты"
        )
        self.unpack_packed_checkbox.toggled.connect(self._refresh_packing_ui)
        layout.addWidget(self.unpack_packed_checkbox)

        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("Режим:"))
        self.pack_mode_combo = QComboBox()
        for packing_mode in ChannelPackingMode:
            self.pack_mode_combo.addItem(packing_mode.label, packing_mode)
        self.pack_mode_combo.currentIndexChanged.connect(self._refresh_packing_ui)
        mode_row.addWidget(self.pack_mode_combo, 1)
        layout.addLayout(mode_row)

        layout_row = QHBoxLayout()
        layout_row.addWidget(QLabel("Схема:"))
        self.pack_layout_combo = QComboBox()
        for pack_layout in ChannelPackLayout:
            self.pack_layout_combo.addItem(pack_layout.label, pack_layout)
        self.pack_layout_combo.currentIndexChanged.connect(self._refresh_packing_ui)
        layout_row.addWidget(self.pack_layout_combo, 1)
        layout.addLayout(layout_row)

        self.packing_mode_label = QLabel()
        self.packing_mode_label.setObjectName("SummaryText")
        self.packing_mode_label.setWordWrap(True)
        layout.addWidget(self.packing_mode_label)

        self.packing_mapping_label = QLabel()
        self.packing_mapping_label.setObjectName("SummaryText")
        self.packing_mapping_label.setWordWrap(True)
        layout.addWidget(self.packing_mapping_label)

        self.packing_queue_label = QLabel()
        self.packing_queue_label.setObjectName("SummaryText")
        self.packing_queue_label.setWordWrap(True)
        layout.addWidget(self.packing_queue_label)

        self._register_interactive(
            self.pack_enable_checkbox,
            self.unpack_packed_checkbox,
            self.pack_mode_combo,
            self.pack_layout_combo,
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
        self.resize_percent_radio = QRadioButton("Масштаб в процентах")
        self.resize_percent_radio.setToolTip("Может как уменьшать, так и увеличивать изображение.")
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
        self.resize_max_side_radio = QRadioButton("Уменьшать, если длинная сторона больше")
        self.resize_max_side_radio.setToolTip(
            "Только уменьшение. Если исходная текстура меньше указанного лимита, размер не изменится."
        )
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

    def _connect_option_change_signals(self) -> None:
        toggles = (
            self.recursive_checkbox,
            self.force_rgba_checkbox,
            self.overwrite_checkbox,
            self.delete_source_checkbox,
            self.optimize_checkbox,
            self.png8_checkbox,
            self.dither_checkbox,
            self.resize_none_radio,
            self.resize_percent_radio,
            self.resize_max_side_radio,
            self.naming_lowercase_checkbox,
            self.naming_replace_spaces_checkbox,
            self.naming_normalize_suffix_checkbox,
            self.pack_enable_checkbox,
            self.unpack_packed_checkbox,
        )
        for widget in toggles:
            widget.toggled.connect(self._notify_options_changed)

        for widget in (
            self.compress_spin,
            self.png8_colors_spin,
            self.resize_percent_spin,
            self.max_side_spin,
        ):
            widget.valueChanged.connect(self._notify_options_changed)

        self.pack_layout_combo.currentIndexChanged.connect(self._notify_options_changed)
        self.pack_mode_combo.currentIndexChanged.connect(self._notify_options_changed)

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

    def build_naming_rules(self) -> NamingRules:
        return NamingRules(
            lowercase=self.naming_lowercase_checkbox.isChecked(),
            replace_spaces=self.naming_replace_spaces_checkbox.isChecked(),
            normalize_map_suffix=self.naming_normalize_suffix_checkbox.isChecked(),
        )

    def build_channel_packing_options(self) -> ChannelPackingOptions:
        layout_data = self.pack_layout_combo.currentData()
        if not isinstance(layout_data, ChannelPackLayout):
            layout_data = ChannelPackLayout.ORM
        mode_data = self.pack_mode_combo.currentData()
        if not isinstance(mode_data, ChannelPackingMode):
            mode_data = ChannelPackingMode.AFTER_CONVERSION
        return ChannelPackingOptions(
            enabled=self.pack_enable_checkbox.isChecked(),
            layout=layout_data,
            mode=mode_data,
        )

    def build_conversion_options(self) -> ConversionOptions:
        return ConversionOptions(
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
            unpack_packed=self.unpack_packed_checkbox.isChecked(),
            naming=self.build_naming_rules(),
            packing=self.build_channel_packing_options(),
        )

    def build_request(self) -> BatchRequest:
        output_text = self.output_edit.text().strip()
        return BatchRequest(
            input_path=None,
            output_root=Path(output_text) if output_text else None,
            options=self.build_conversion_options(),
        )

    def apply_conversion_options(self, options: ConversionOptions) -> None:
        self._suppress_option_signal = True
        try:
            self.recursive_checkbox.setChecked(options.recursive)
            self.force_rgba_checkbox.setChecked(options.force_rgba)
            self.overwrite_checkbox.setChecked(options.overwrite)
            self.delete_source_checkbox.setChecked(options.delete_source)
            self.optimize_checkbox.setChecked(options.optimize)
            self.compress_spin.setValue(options.compress_level)
            self.png8_checkbox.setChecked(options.png8)
            self.png8_colors_spin.setValue(options.png8_colors)
            self.dither_checkbox.setChecked(options.dither)
            self.naming_lowercase_checkbox.setChecked(options.naming.lowercase)
            self.naming_replace_spaces_checkbox.setChecked(options.naming.replace_spaces)
            self.naming_normalize_suffix_checkbox.setChecked(options.naming.normalize_map_suffix)
            self.pack_enable_checkbox.setChecked(options.packing.enabled)
            self.unpack_packed_checkbox.setChecked(options.unpack_packed)
            for index in range(self.pack_layout_combo.count()):
                if self.pack_layout_combo.itemData(index) == options.packing.layout:
                    self.pack_layout_combo.setCurrentIndex(index)
                    break
            for index in range(self.pack_mode_combo.count()):
                if self.pack_mode_combo.itemData(index) == options.packing.mode:
                    self.pack_mode_combo.setCurrentIndex(index)
                    break
            self.resize_percent_spin.setValue(options.resize_percent)
            self.max_side_spin.setValue(options.max_side)

            if options.resize_mode is ResizeMode.PERCENT:
                self.resize_percent_radio.setChecked(True)
            elif options.resize_mode is ResizeMode.MAX_SIDE:
                self.resize_max_side_radio.setChecked(True)
            else:
                self.resize_none_radio.setChecked(True)
        finally:
            self._suppress_option_signal = False

        self._update_resize_state()
        self._update_png8_state()
        self._refresh_naming_ui()
        self._refresh_packing_ui()
        self._notify_options_changed()

    def apply_app_settings(self, settings: AppSettings) -> None:
        self.output_edit.setText(settings.output_path)
        self.apply_conversion_options(settings.options)

    def set_controls_enabled(self, enabled: bool) -> None:
        for widget in self._interactive_widgets:
            widget.setEnabled(enabled)

        if enabled:
            self._update_resize_state()
            self._update_png8_state()
            self._refresh_naming_ui()
            self._refresh_packing_ui()
            self._refresh_preset_ui()

    def set_available_presets(self, presets: list[ConversionPreset]) -> None:
        self._presets_by_id = {preset.preset_id: preset for preset in presets}
        self.preset_combo.blockSignals(True)
        self.preset_combo.clear()
        self.preset_combo.addItem("Ручной режим", CURRENT_PRESET_DATA)
        self.preset_combo.setItemData(
            0,
            self._preset_tooltip(None),
            Qt.ItemDataRole.ToolTipRole,
        )

        for preset in presets:
            source_label = "Системный" if preset.is_system else "Пользовательский"
            self.preset_combo.addItem(f"{preset.name} [{source_label}]", preset.preset_id)
            self.preset_combo.setItemData(
                self.preset_combo.count() - 1,
                self._preset_tooltip(preset),
                Qt.ItemDataRole.ToolTipRole,
            )

        self.preset_combo.blockSignals(False)
        self.set_selected_preset_id(None)
        self._refresh_preset_ui()

    def set_selected_preset_id(self, preset_id: str | None) -> None:
        target_data = preset_id or CURRENT_PRESET_DATA
        self.preset_combo.blockSignals(True)
        for index in range(self.preset_combo.count()):
            if self.preset_combo.itemData(index) == target_data:
                self.preset_combo.setCurrentIndex(index)
                break
        else:
            self.preset_combo.setCurrentIndex(0)
        self.preset_combo.blockSignals(False)
        self._refresh_preset_ui()

    def selected_preset_id(self) -> str | None:
        current_data = self.preset_combo.currentData()
        if current_data in (None, CURRENT_PRESET_DATA):
            return None
        return str(current_data)

    def _emit_apply_selected_preset(self) -> None:
        preset_id = self.selected_preset_id()
        if preset_id is not None:
            self.preset_apply_requested.emit(preset_id)

    def _emit_delete_selected_preset(self) -> None:
        preset_id = self.selected_preset_id()
        if preset_id is not None:
            self.preset_delete_requested.emit(preset_id)

    def _on_preset_selection_changed(self, *_args: object) -> None:
        self._refresh_preset_ui()
        preset_id = self.selected_preset_id()
        if preset_id is not None and not self._suppress_option_signal:
            self.preset_apply_requested.emit(preset_id)

    def _refresh_preset_ui(self, *_args: object) -> None:
        preset_id = self.selected_preset_id()
        preset = self._presets_by_id.get(preset_id or "")

        self.delete_preset_button.setEnabled(preset is not None and not preset.is_system)
        self.preset_combo.setToolTip(self._preset_tooltip(preset))

    def _notify_options_changed(self, *_args: object) -> None:
        if self._suppress_option_signal:
            return
        self.options_changed.emit()

    def _refresh_naming_ui(self, *_args: object) -> None:
        naming_rules = self.build_naming_rules()
        if not naming_rules.is_enabled:
            self.naming_summary_label.setText(
                "Имена выходных файлов повторяют исходники."
            )
            return

        example_name = build_output_filename(
            Path("Wood Floor Albedo.tga"),
            naming_rules,
            TextureMapType.BASECOLOR,
        )
        self.naming_summary_label.setText(
            f"Пример: Wood Floor Albedo.tga -> {example_name}"
        )

    def _refresh_packing_ui(self, *_args: object) -> None:
        packing_options = self.build_channel_packing_options()
        self.pack_layout_combo.setEnabled(self.pack_enable_checkbox.isChecked())
        self.pack_mode_combo.setEnabled(self.pack_enable_checkbox.isChecked())
        self.packing_mode_label.setText(self._packing_mode_description(packing_options))

        if not packing_options.enabled:
            if self.unpack_packed_checkbox.isChecked():
                self.packing_mapping_label.setText(
                    "Вход: ORM, RMA, MRA, Unity URP Metallic/Smoothness или Unity HDRP Mask Map. "
                    "Выход: отдельные AO, Roughness, Metallic и HDRP Detail Mask."
                )
                self.packing_queue_label.setText(
                    "Traditional / Non-Packed: распознанные ORM/RMA/MRA и Unity packed textures "
                    "будут разложены на AO, Roughness, Metallic и HDRP Detail Mask. "
                    "Остальные карты сохранятся отдельно."
                )
            else:
                self.packing_mapping_label.setText("Packed-преобразование не используется.")
                self.packing_queue_label.setText(
                    "Упаковка каналов выключена. Для каждого исходника будет сохранен отдельный PNG."
                )
            return

        self.packing_mapping_label.setText(channel_pack_mapping_text(packing_options.layout))
        self.packing_queue_label.setText(self._packing_preflight_text)

    def set_packing_preflight_summary(self, text: str) -> None:
        self._packing_preflight_text = text
        self._refresh_packing_ui()

    def set_output_bundle_summary(self, text: str) -> None:
        self.output_bundle_summary_label.set_summary_text(text)

    def minimumSizeHint(self) -> QSize:
        return QSize(260, 480)

    def sizeHint(self) -> QSize:
        return QSize(360, 900)

    def _packing_mode_description(self, packing_options: ChannelPackingOptions) -> str:
        if not packing_options.enabled:
            if self.unpack_packed_checkbox.isChecked():
                return "Traditional / Non-Packed: packed-входы распаковываются в отдельные PBR-карты."
            return "Режим упаковки каналов выключен."
        if packing_options.mode is ChannelPackingMode.PACK_WITH_REMAINDER:
            return "Packed + нужные карты: приложение сохранит итоговую packed texture и отдельно выгрузит только те карты, которые не входят в packed-схему."
        if packing_options.mode is ChannelPackingMode.PACK_ONLY:
            return "Только Packed: приложение соберет только итоговый packed texture и не будет сохранять отдельные PNG по каждому исходнику."
        return "Сначала PNG, потом Packed: приложение сохранит отдельные PNG по каждому исходнику и затем соберет итоговую packed texture."

    def _preset_tooltip(self, preset: ConversionPreset | None) -> str:
        if preset is None:
            return "Ручной режим\nИспользуются текущие параметры вкладок ниже."

        options = preset.options
        lines = [
            preset.name,
            (
                f"Формат: {self._format_summary(options)} · "
                f"Размер: {self._resize_summary(options)}"
            ),
        ]
        packing = options.packing
        if packing.enabled:
            mapping = channel_pack_mapping_text(packing.layout)
            if packing.mode is ChannelPackingMode.PACK_ONLY:
                output = "только packed texture"
            elif packing.mode is ChannelPackingMode.PACK_WITH_REMAINDER:
                output = "packed texture + карты вне packed-схемы"
            else:
                output = "packed texture + все отдельные карты"
            lines.extend((mapping, f"Выход: {output}"))
        elif options.unpack_packed:
            lines.append(
                "Выход: все карты отдельными PNG; packed-карты будут распакованы"
            )
        else:
            lines.append("Выход: отдельные PNG")
        return "\n".join(lines)

    def _format_summary(self, options: ConversionOptions) -> str:
        if options.png8:
            dither_text = ", dithering" if options.dither else ""
            return f"PNG-8 ({options.png8_colors} цветов{dither_text})"
        if options.force_rgba:
            return "PNG (RGBA)"
        return "PNG"

    def _resize_summary(self, options: ConversionOptions) -> str:
        if options.resize_mode is ResizeMode.PERCENT:
            return f"{options.resize_percent}%"
        if options.resize_mode is ResizeMode.MAX_SIDE:
            return f"до {options.max_side} px"
        return "исходный"
