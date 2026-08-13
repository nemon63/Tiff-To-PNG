from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QTimer, Qt, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from image_converter.domain.node_graph import (
    GraphNode,
    NodeType,
    OutputMode,
    OutputProfile,
    TextureDataRole,
    TextureNodeColorSpace,
    node_has_enable_flag,
    node_has_resettable_parameters,
)
from image_converter.ui.node_editor_constants import (
    GRAPH_EXPORT_FILE_FILTER,
    NUMERIC_PREVIEW_DEBOUNCE_MS,
    PREVIEW_MODE_DRAFT,
    PREVIEW_MODE_FULL,
)
class NodePropertiesPanel(QWidget):
    node_changed = pyqtSignal(object, object, object, bool, object)
    preview_refresh_requested = pyqtSignal(object, object)
    output_profile_apply_requested = pyqtSignal(object)
    output_inputs_clear_requested = pyqtSignal(object)
    node_reset_requested = pyqtSignal(object)
    output_export_requested = pyqtSignal(object)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._node: GraphNode | None = None
        self._suppress = False
        self._slider_drag_active = False
        self._numeric_preview_timer = QTimer(self)
        self._numeric_preview_timer.setSingleShot(True)
        self._numeric_preview_timer.setInterval(NUMERIC_PREVIEW_DEBOUNCE_MS)
        self._numeric_preview_timer.timeout.connect(self._flush_numeric_preview)
        self._build_ui()
        self.set_node(None)

    def _make_byte_spin(self) -> QSpinBox:
        spin = QSpinBox()
        spin.setRange(0, 255)
        spin.setKeyboardTracking(False)
        spin.valueChanged.connect(self._on_numeric_value_changed)
        return spin

    def _make_int_spin(self, minimum: int, maximum: int, *, suffix: str = "") -> QSpinBox:
        spin = QSpinBox()
        spin.setRange(minimum, maximum)
        spin.setKeyboardTracking(False)
        if suffix:
            spin.setSuffix(suffix)
        spin.valueChanged.connect(self._on_numeric_value_changed)
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

        spin = self._make_int_spin(minimum, maximum, suffix=suffix)
        spin.setValue(initial)
        slider.valueChanged.connect(spin.setValue)
        spin.valueChanged.connect(slider.setValue)
        slider.sliderPressed.connect(self._on_slider_drag_started)
        slider.sliderReleased.connect(self._on_slider_drag_finished)
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

        self.color_button = QPushButton()
        self.color_button.setToolTip("Choose a solid RGBA color.")
        self.color_button.clicked.connect(self._choose_color)
        self.form.addRow("Color", self.color_button)
        self.color_red_spin = self._make_byte_spin()
        self.color_green_spin = self._make_byte_spin()
        self.color_blue_spin = self._make_byte_spin()
        self.color_alpha_spin = self._make_byte_spin()
        for spin in (
            self.color_red_spin,
            self.color_green_spin,
            self.color_blue_spin,
            self.color_alpha_spin,
        ):
            spin.valueChanged.connect(self._update_color_button)
        self.color_components_host = self._color_components_widget()
        self.form.addRow("RGBA", self.color_components_host)
        self.color_width_spin = self._make_int_spin(1, 16384, suffix=" px")
        self.color_height_spin = self._make_int_spin(1, 16384, suffix=" px")
        self.color_resolution_host = self._resolution_widget(
            self.color_width_spin,
            self.color_height_spin,
        )
        self.form.addRow("Resolution", self.color_resolution_host)

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
        self.level_gamma_spin.valueChanged.connect(self._on_numeric_value_changed)
        self.form.addRow("Gamma", self.level_gamma_spin)
        self.level_out_min_slider, self.level_out_min_spin = self._make_byte_slider_pair()
        self.level_out_min_host = self._byte_row_widget(self.level_out_min_slider, self.level_out_min_spin)
        self.form.addRow("Output Min", self.level_out_min_host)
        self.level_out_max_slider, self.level_out_max_spin = self._make_byte_slider_pair()
        self.level_out_max_host = self._byte_row_widget(self.level_out_max_slider, self.level_out_max_spin)
        self.form.addRow("Output Max", self.level_out_max_host)

        self.remap_in_min_slider, self.remap_in_min_spin = self._make_byte_slider_pair()
        self.remap_in_min_host = self._byte_row_widget(self.remap_in_min_slider, self.remap_in_min_spin)
        self.form.addRow("Input Min", self.remap_in_min_host)
        self.remap_in_max_slider, self.remap_in_max_spin = self._make_byte_slider_pair(initial=255)
        self.remap_in_max_host = self._byte_row_widget(self.remap_in_max_slider, self.remap_in_max_spin)
        self.form.addRow("Input Max", self.remap_in_max_host)
        self.remap_out_min_slider, self.remap_out_min_spin = self._make_byte_slider_pair()
        self.remap_out_min_host = self._byte_row_widget(self.remap_out_min_slider, self.remap_out_min_spin)
        self.form.addRow("Output Min", self.remap_out_min_host)
        self.remap_out_max_slider, self.remap_out_max_spin = self._make_byte_slider_pair(initial=255)
        self.remap_out_max_host = self._byte_row_widget(self.remap_out_max_slider, self.remap_out_max_spin)
        self.form.addRow("Output Max", self.remap_out_max_host)

        self.clamp_min_slider, self.clamp_min_spin = self._make_byte_slider_pair()
        self.clamp_min_host = self._byte_row_widget(self.clamp_min_slider, self.clamp_min_spin)
        self.form.addRow("Min", self.clamp_min_host)
        self.clamp_max_slider, self.clamp_max_spin = self._make_byte_slider_pair()
        self.clamp_max_host = self._byte_row_widget(self.clamp_max_slider, self.clamp_max_spin)
        self.form.addRow("Max", self.clamp_max_host)

        self.threshold_slider, self.threshold_spin = self._make_byte_slider_pair()
        self.threshold_host = self._byte_row_widget(self.threshold_slider, self.threshold_spin)
        self.form.addRow("Threshold", self.threshold_host)

        self.blur_radius_slider, self.blur_radius_spin = self._make_slider_spin_pair(
            0,
            64,
            initial=1,
            suffix=" px",
        )
        self.blur_radius_host = self._byte_row_widget(self.blur_radius_slider, self.blur_radius_spin)
        self.form.addRow("Radius", self.blur_radius_host)

        self.dilate_radius_slider, self.dilate_radius_spin = self._make_slider_spin_pair(
            0,
            64,
            initial=1,
            suffix=" px",
        )
        self.dilate_radius_host = self._byte_row_widget(self.dilate_radius_slider, self.dilate_radius_spin)
        self.form.addRow("Radius", self.dilate_radius_host)

        self.erode_radius_slider, self.erode_radius_spin = self._make_slider_spin_pair(
            0,
            64,
            initial=1,
            suffix=" px",
        )
        self.erode_radius_host = self._byte_row_widget(self.erode_radius_slider, self.erode_radius_spin)
        self.form.addRow("Radius", self.erode_radius_host)

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
            ("Unity URP Metallic/Smoothness", OutputProfile.UNITY_URP.value),
            ("Unity HDRP Mask Map", OutputProfile.UNITY_HDRP.value),
            ("Unreal ORM", OutputProfile.UNREAL_ORM.value),
            ("MetaHuman Repack", OutputProfile.METAHUMAN_REPACK.value),
        ):
            self.output_profile_combo.addItem(label, value)
        self.output_profile_combo.currentIndexChanged.connect(self._apply_changes)
        self.form.addRow("Target Pack", self.output_profile_combo)

        self.apply_profile_button = QPushButton("Build Auto-Connect Plan")
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

        self.export_output_button = QPushButton("Export This Output")
        self.export_output_button.setObjectName("PrimaryButton")
        self.export_output_button.setToolTip(
            "Export only this Output node. The bulk Export checkbox is ignored."
        )
        self.export_output_button.clicked.connect(self._export_output)
        self.form.addRow("", self.export_output_button)

        layout.addWidget(self.form_host)
        layout.addStretch(1)

    def set_node(self, node: GraphNode | None) -> None:
        self._node = node
        self._suppress = True
        try:
            self._numeric_preview_timer.stop()
            self._slider_drag_active = False
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
            self.color_red_spin.setValue(self._coerce_int(node.properties.get("red"), 255))
            self.color_green_spin.setValue(self._coerce_int(node.properties.get("green"), 255))
            self.color_blue_spin.setValue(self._coerce_int(node.properties.get("blue"), 255))
            self.color_alpha_spin.setValue(self._coerce_int(node.properties.get("alpha"), 255))
            self.color_width_spin.setValue(self._coerce_int(node.properties.get("width"), 1024))
            self.color_height_spin.setValue(self._coerce_int(node.properties.get("height"), 1024))
            self._update_color_button()
            self.level_black_spin.setValue(self._coerce_int(node.properties.get("black"), 0))
            self.level_white_spin.setValue(self._coerce_int(node.properties.get("white"), 255))
            self.level_gamma_spin.setValue(self._coerce_float(node.properties.get("gamma"), 1.0))
            self.level_out_min_spin.setValue(self._coerce_int(node.properties.get("out_min"), 0))
            self.level_out_max_spin.setValue(self._coerce_int(node.properties.get("out_max"), 255))
            self.remap_in_min_spin.setValue(self._coerce_int(node.properties.get("in_min"), 0))
            self.remap_in_max_spin.setValue(self._coerce_int(node.properties.get("in_max"), 255))
            self.remap_out_min_spin.setValue(self._coerce_int(node.properties.get("out_min"), 0))
            self.remap_out_max_spin.setValue(self._coerce_int(node.properties.get("out_max"), 255))
            self.clamp_min_spin.setValue(self._coerce_int(node.properties.get("min"), 0))
            self.clamp_max_spin.setValue(self._coerce_int(node.properties.get("max"), 255))
            self.threshold_spin.setValue(self._coerce_int(node.properties.get("threshold"), 128))
            self.blur_radius_spin.setValue(self._coerce_int(node.properties.get("radius"), 1))
            self.dilate_radius_spin.setValue(self._coerce_int(node.properties.get("radius"), 1))
            self.erode_radius_spin.setValue(self._coerce_int(node.properties.get("radius"), 1))
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
        self._set_row_visible(self.color_button, node_type is NodeType.COLOR)
        self._set_row_visible(self.color_components_host, node_type is NodeType.COLOR)
        self._set_row_visible(self.color_resolution_host, node_type is NodeType.COLOR)
        self._set_row_visible(self.value_host, node_type is NodeType.CONSTANT_CHANNEL)
        self._set_row_visible(self.level_black_host, node_type is NodeType.LEVELS_CHANNEL)
        self._set_row_visible(self.level_white_host, node_type is NodeType.LEVELS_CHANNEL)
        self._set_row_visible(self.level_gamma_spin, node_type is NodeType.LEVELS_CHANNEL)
        self._set_row_visible(self.level_out_min_host, node_type is NodeType.LEVELS_CHANNEL)
        self._set_row_visible(self.level_out_max_host, node_type is NodeType.LEVELS_CHANNEL)
        self._set_row_visible(self.remap_in_min_host, node_type is NodeType.REMAP_CHANNEL)
        self._set_row_visible(self.remap_in_max_host, node_type is NodeType.REMAP_CHANNEL)
        self._set_row_visible(self.remap_out_min_host, node_type is NodeType.REMAP_CHANNEL)
        self._set_row_visible(self.remap_out_max_host, node_type is NodeType.REMAP_CHANNEL)
        self._set_row_visible(self.clamp_min_host, node_type is NodeType.CLAMP_CHANNEL)
        self._set_row_visible(self.clamp_max_host, node_type is NodeType.CLAMP_CHANNEL)
        self._set_row_visible(self.threshold_host, node_type is NodeType.THRESHOLD_CHANNEL)
        self._set_row_visible(self.blur_radius_host, node_type is NodeType.BLUR_CHANNEL)
        self._set_row_visible(self.dilate_radius_host, node_type is NodeType.DILATE_CHANNEL)
        self._set_row_visible(self.erode_radius_host, node_type is NodeType.ERODE_CHANNEL)
        self._set_row_visible(self.blend_mode_combo, node_type is NodeType.BLEND_CHANNEL)
        self._set_row_visible(self.blend_opacity_host, node_type is NodeType.BLEND_CHANNEL)
        self._set_row_visible(self.filename_edit, node_type is NodeType.OUTPUT_RGBA)
        self._set_row_visible(self.output_path_host, node_type is NodeType.OUTPUT_RGBA)
        self._set_row_visible(self.output_profile_combo, node_type is NodeType.OUTPUT_RGBA)
        self._set_row_visible(self.profile_actions_host, node_type is NodeType.OUTPUT_RGBA)
        self._set_row_visible(self.profile_summary_label, node_type is NodeType.OUTPUT_RGBA)
        self._set_row_visible(self.mode_combo, node_type is NodeType.OUTPUT_RGBA)
        self._set_row_visible(self.enabled_checkbox, node_type is NodeType.OUTPUT_RGBA)
        self._set_row_visible(self.export_output_button, node_type is NodeType.OUTPUT_RGBA)

    def _export_output(self) -> None:
        if self._node is None or self._node.node_type is not NodeType.OUTPUT_RGBA:
            return
        self._apply_changes()
        self.output_export_requested.emit(self._node)

    def _set_row_visible(self, widget: QWidget, visible: bool) -> None:
        widget.setVisible(visible)
        label = self.form.labelForField(widget)
        if label is not None:
            label.setVisible(visible)

    def _choose_color(self) -> None:
        if self._node is None or self._node.node_type is not NodeType.COLOR:
            return
        initial = QColor(
            self.color_red_spin.value(),
            self.color_green_spin.value(),
            self.color_blue_spin.value(),
            self.color_alpha_spin.value(),
        )
        color = QColorDialog.getColor(
            initial,
            self,
            "Choose Color",
            QColorDialog.ColorDialogOption.ShowAlphaChannel,
        )
        if not color.isValid():
            return
        self._numeric_preview_timer.stop()
        self._suppress = True
        try:
            self.color_red_spin.setValue(color.red())
            self.color_green_spin.setValue(color.green())
            self.color_blue_spin.setValue(color.blue())
            self.color_alpha_spin.setValue(color.alpha())
        finally:
            self._suppress = False
        self._update_color_button()
        self._apply_changes()

    def _update_color_button(self, *_args: object) -> None:
        red = self.color_red_spin.value()
        green = self.color_green_spin.value()
        blue = self.color_blue_spin.value()
        alpha = self.color_alpha_spin.value()
        text_color = "#111111" if red * 299 + green * 587 + blue * 114 > 128000 else "#FFFFFF"
        self.color_button.setText(f"#{red:02X}{green:02X}{blue:02X}{alpha:02X}")
        self.color_button.setStyleSheet(
            f"background-color: rgba({red}, {green}, {blue}, {alpha}); color: {text_color};"
        )

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

    def _color_components_widget(self) -> QWidget:
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)
        for label, spin in (
            ("R", self.color_red_spin),
            ("G", self.color_green_spin),
            ("B", self.color_blue_spin),
            ("A", self.color_alpha_spin),
        ):
            row.addWidget(QLabel(label))
            row.addWidget(spin)
        host = QWidget()
        host.setLayout(row)
        return host

    @staticmethod
    def _resolution_widget(width_spin: QSpinBox, height_spin: QSpinBox) -> QWidget:
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        row.addWidget(width_spin)
        row.addWidget(QLabel("×"))
        row.addWidget(height_spin)
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
        self._emit_node_change(PREVIEW_MODE_FULL)

    def _on_numeric_value_changed(self, *_args: object) -> None:
        if self._suppress or self._node is None:
            return
        if self._slider_drag_active:
            self._numeric_preview_timer.stop()
            self._emit_node_change(PREVIEW_MODE_DRAFT)
            return
        self._numeric_preview_timer.start()

    def _flush_numeric_preview(self) -> None:
        self._emit_node_change(PREVIEW_MODE_FULL)

    def _on_slider_drag_started(self) -> None:
        if self._suppress:
            return
        self._slider_drag_active = True
        self._numeric_preview_timer.stop()

    def _on_slider_drag_finished(self) -> None:
        was_dragging = self._slider_drag_active
        self._slider_drag_active = False
        if self._suppress or self._node is None or not was_dragging:
            return
        self.preview_refresh_requested.emit(self._node, PREVIEW_MODE_FULL)

    def _emit_node_change(self, preview_mode: str) -> None:
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
        elif self._node.node_type is NodeType.COLOR:
            next_properties["red"] = self.color_red_spin.value()
            next_properties["green"] = self.color_green_spin.value()
            next_properties["blue"] = self.color_blue_spin.value()
            next_properties["alpha"] = self.color_alpha_spin.value()
            next_properties["width"] = self.color_width_spin.value()
            next_properties["height"] = self.color_height_spin.value()
        elif self._node.node_type is NodeType.CONSTANT_CHANNEL:
            next_properties["value"] = self.value_spin.value()
        elif self._node.node_type is NodeType.LEVELS_CHANNEL:
            next_properties["black"] = self.level_black_spin.value()
            next_properties["white"] = self.level_white_spin.value()
            next_properties["gamma"] = self.level_gamma_spin.value()
            next_properties["out_min"] = self.level_out_min_spin.value()
            next_properties["out_max"] = self.level_out_max_spin.value()
        elif self._node.node_type is NodeType.REMAP_CHANNEL:
            next_properties["in_min"] = self.remap_in_min_spin.value()
            next_properties["in_max"] = self.remap_in_max_spin.value()
            next_properties["out_min"] = self.remap_out_min_spin.value()
            next_properties["out_max"] = self.remap_out_max_spin.value()
        elif self._node.node_type is NodeType.CLAMP_CHANNEL:
            next_properties["min"] = self.clamp_min_spin.value()
            next_properties["max"] = self.clamp_max_spin.value()
        elif self._node.node_type is NodeType.THRESHOLD_CHANNEL:
            next_properties["threshold"] = self.threshold_spin.value()
        elif self._node.node_type is NodeType.BLUR_CHANNEL:
            next_properties["radius"] = self.blur_radius_spin.value()
        elif self._node.node_type is NodeType.DILATE_CHANNEL:
            next_properties["radius"] = self.dilate_radius_spin.value()
        elif self._node.node_type is NodeType.ERODE_CHANNEL:
            next_properties["radius"] = self.erode_radius_spin.value()
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
        self.node_changed.emit(self._node, next_title, next_properties, needs_rebuild, preview_mode)

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
