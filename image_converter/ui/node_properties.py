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
    OutputAlphaInputMode,
    OutputMode,
    OutputProfile,
    PbrNormalConvention,
    PbrWorkflow,
    TextureDataRole,
    TextureNodeColorSpace,
    node_has_enable_flag,
    node_has_resettable_parameters,
)
from image_converter.ui.node_editor_constants import (
    GRAPH_EXPORT_FILE_FILTER,
    INTERACTIVE_PREVIEW_INTERVAL_MS,
    NUMERIC_PREVIEW_DEBOUNCE_MS,
    PREVIEW_MODE_DRAFT,
    PREVIEW_MODE_FULL,
)
class NodePropertiesPanel(QWidget):
    node_changed = pyqtSignal(object, object, object, bool, object)
    transient_preview_requested = pyqtSignal(object, object, object)
    preview_refresh_requested = pyqtSignal(object, object)
    interactive_edit_started = pyqtSignal()
    interactive_edit_finished = pyqtSignal()
    output_profile_apply_requested = pyqtSignal(object)
    output_inputs_clear_requested = pyqtSignal(object)
    node_reset_requested = pyqtSignal(object)
    output_export_requested = pyqtSignal(object)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._node: GraphNode | None = None
        self._suppress = False
        self._slider_drag_active = False
        self._interactive_change_pending = False
        self._numeric_preview_timer = QTimer(self)
        self._numeric_preview_timer.setSingleShot(True)
        self._numeric_preview_timer.setInterval(NUMERIC_PREVIEW_DEBOUNCE_MS)
        self._numeric_preview_timer.timeout.connect(self._flush_numeric_preview)
        self._interactive_preview_timer = QTimer(self)
        self._interactive_preview_timer.setSingleShot(True)
        self._interactive_preview_timer.setInterval(INTERACTIVE_PREVIEW_INTERVAL_MS)
        self._interactive_preview_timer.timeout.connect(self._flush_interactive_preview)
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

    def _make_double_spin(
        self,
        minimum: float,
        maximum: float,
        *,
        step: float = 0.1,
        decimals: int = 2,
        suffix: str = "",
    ) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(minimum, maximum)
        spin.setSingleStep(step)
        spin.setDecimals(decimals)
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

    def _build_image_filter_combo(self, *, default: str = "bilinear") -> QComboBox:
        combo = QComboBox()
        for label, value in (
            ("Nearest", "nearest"),
            ("Bilinear", "bilinear"),
            ("Bicubic", "bicubic"),
            ("Lanczos", "lanczos"),
        ):
            combo.addItem(label, value)
        self._set_combo_value(combo, default)
        combo.currentIndexChanged.connect(self._apply_changes)
        return combo

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

        self.node_help_label = QLabel()
        self.node_help_label.setObjectName("SummaryText")
        self.node_help_label.setWordWrap(True)
        self.form.addRow("Purpose", self.node_help_label)

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

        self.mix_factor_slider, self.mix_factor_spin = self._make_slider_spin_pair(
            0,
            100,
            initial=50,
            suffix="%",
        )
        self.mix_factor_host = self._byte_row_widget(
            self.mix_factor_slider,
            self.mix_factor_spin,
        )
        self.form.addRow("Factor", self.mix_factor_host)

        self.normal_flip_red_checkbox = QCheckBox("Invert X")
        self.normal_flip_red_checkbox.toggled.connect(self._apply_changes)
        self.form.addRow("Flip Red", self.normal_flip_red_checkbox)

        self.normal_flip_green_checkbox = QCheckBox("DirectX ↔ OpenGL")
        self.normal_flip_green_checkbox.toggled.connect(self._apply_changes)
        self.form.addRow("Flip Green", self.normal_flip_green_checkbox)

        self.normal_reconstruct_blue_checkbox = QCheckBox("Rebuild Z from R + G")
        self.normal_reconstruct_blue_checkbox.toggled.connect(self._apply_changes)
        self.form.addRow("Blue", self.normal_reconstruct_blue_checkbox)

        self.normal_normalize_checkbox = QCheckBox("Unit-length vectors")
        self.normal_normalize_checkbox.toggled.connect(self._apply_changes)
        self.form.addRow("Normalize", self.normal_normalize_checkbox)

        self.normal_strength_slider, self.normal_strength_spin = self._make_slider_spin_pair(
            0,
            400,
            initial=100,
            suffix="%",
        )
        self.normal_strength_host = self._byte_row_widget(
            self.normal_strength_slider,
            self.normal_strength_spin,
        )
        self.form.addRow("Strength", self.normal_strength_host)

        self.height_strength_slider, self.height_strength_spin = self._make_slider_spin_pair(
            0,
            1000,
            initial=100,
            suffix="%",
        )
        self.height_strength_host = self._byte_row_widget(
            self.height_strength_slider,
            self.height_strength_spin,
        )
        self.form.addRow("Strength", self.height_strength_host)
        self.height_radius_spin = self._make_double_spin(
            0.0,
            32.0,
            step=0.25,
            suffix=" px",
        )
        self.form.addRow("Radius", self.height_radius_spin)
        self.height_convention_combo = QComboBox()
        self.height_convention_combo.addItem("OpenGL (Y+ / Unity)", "opengl")
        self.height_convention_combo.addItem("DirectX (Y− / Unreal)", "directx")
        self.height_convention_combo.currentIndexChanged.connect(self._apply_changes)
        self.form.addRow("Convention", self.height_convention_combo)

        self.normal_blend_strength_slider, self.normal_blend_strength_spin = (
            self._make_slider_spin_pair(0, 400, initial=100, suffix="%")
        )
        self.normal_blend_strength_host = self._byte_row_widget(
            self.normal_blend_strength_slider,
            self.normal_blend_strength_spin,
        )
        self.form.addRow("Detail Strength", self.normal_blend_strength_host)

        self.color_exposure_spin = self._make_double_spin(
            -10.0,
            10.0,
            step=0.1,
            suffix=" stops",
        )
        self.form.addRow("Exposure", self.color_exposure_spin)
        self.color_brightness_slider, self.color_brightness_spin = self._make_slider_spin_pair(
            -100, 100, suffix="%"
        )
        self.color_brightness_host = self._byte_row_widget(
            self.color_brightness_slider,
            self.color_brightness_spin,
        )
        self.form.addRow("Brightness", self.color_brightness_host)
        self.color_contrast_slider, self.color_contrast_spin = self._make_slider_spin_pair(
            0, 400, initial=100, suffix="%"
        )
        self.color_contrast_host = self._byte_row_widget(
            self.color_contrast_slider,
            self.color_contrast_spin,
        )
        self.form.addRow("Contrast", self.color_contrast_host)
        self.color_saturation_slider, self.color_saturation_spin = self._make_slider_spin_pair(
            0, 400, initial=100, suffix="%"
        )
        self.color_saturation_host = self._byte_row_widget(
            self.color_saturation_slider,
            self.color_saturation_spin,
        )
        self.form.addRow("Saturation", self.color_saturation_host)
        self.color_hue_slider, self.color_hue_spin = self._make_slider_spin_pair(
            -180, 180, suffix="°"
        )
        self.color_hue_host = self._byte_row_widget(
            self.color_hue_slider,
            self.color_hue_spin,
        )
        self.form.addRow("Hue", self.color_hue_host)
        self.color_gamma_spin = self._make_double_spin(0.05, 8.0, step=0.05)
        self.color_gamma_spin.setValue(1.0)
        self.form.addRow("Gamma", self.color_gamma_spin)

        self.transform_flip_horizontal_checkbox = QCheckBox("Horizontal")
        self.transform_flip_horizontal_checkbox.toggled.connect(self._apply_changes)
        self.form.addRow("Flip", self.transform_flip_horizontal_checkbox)
        self.transform_flip_vertical_checkbox = QCheckBox("Vertical")
        self.transform_flip_vertical_checkbox.toggled.connect(self._apply_changes)
        self.form.addRow("", self.transform_flip_vertical_checkbox)
        self.transform_rotation_combo = QComboBox()
        for angle in (0, 90, 180, 270):
            self.transform_rotation_combo.addItem(f"{angle}° clockwise", angle)
        self.transform_rotation_combo.currentIndexChanged.connect(self._apply_changes)
        self.form.addRow("Rotate", self.transform_rotation_combo)
        self.transform_offset_x_spin = self._make_int_spin(-16384, 16384, suffix=" px")
        self.transform_offset_y_spin = self._make_int_spin(-16384, 16384, suffix=" px")
        self.transform_offset_host = self._resolution_widget(
            self.transform_offset_x_spin,
            self.transform_offset_y_spin,
        )
        self.form.addRow("Offset X / Y", self.transform_offset_host)
        self.transform_scale_slider, self.transform_scale_spin = self._make_slider_spin_pair(
            1, 1000, initial=100, suffix="%"
        )
        self.transform_scale_host = self._byte_row_widget(
            self.transform_scale_slider,
            self.transform_scale_spin,
        )
        self.form.addRow("Scale", self.transform_scale_host)
        self.transform_address_combo = QComboBox()
        for label, value in (("Clamp", "clamp"), ("Repeat", "repeat"), ("Mirror", "mirror")):
            self.transform_address_combo.addItem(label, value)
        self.transform_address_combo.currentIndexChanged.connect(self._apply_changes)
        self.form.addRow("Address", self.transform_address_combo)
        self.transform_filter_combo = self._build_image_filter_combo()
        self.form.addRow("Filter", self.transform_filter_combo)

        self.resize_size_mode_combo = QComboBox()
        for label, value in (
            ("Exact", "exact"),
            ("POT Up", "pot_up"),
            ("POT Down", "pot_down"),
            ("POT Nearest", "pot_nearest"),
        ):
            self.resize_size_mode_combo.addItem(label, value)
        self.resize_size_mode_combo.currentIndexChanged.connect(self._on_resize_size_mode_changed)
        self.form.addRow("Output Size", self.resize_size_mode_combo)
        self.resize_width_spin = self._make_int_spin(1, 16384, suffix=" px")
        self.resize_height_spin = self._make_int_spin(1, 16384, suffix=" px")
        self.resize_resolution_host = self._resolution_widget(
            self.resize_width_spin,
            self.resize_height_spin,
        )
        self.form.addRow("Width × Height", self.resize_resolution_host)
        self.resize_mode_combo = QComboBox()
        for label, value in (
            ("Stretch", "stretch"),
            ("Fit + Transparent", "fit"),
            ("Fill + Crop", "fill"),
            ("Crop / Canvas", "crop"),
            ("Pad (no upscale)", "pad"),
        ):
            self.resize_mode_combo.addItem(label, value)
        self.resize_mode_combo.currentIndexChanged.connect(self._apply_changes)
        self.form.addRow("Layout", self.resize_mode_combo)
        self.resize_filter_combo = self._build_image_filter_combo(default="lanczos")
        self.form.addRow("Filter", self.resize_filter_combo)

        self.image_resolution_source_combo = QComboBox()
        for label, value in (
            ("Input A", "a"),
            ("Input B", "b"),
            ("Mask", "mask"),
            ("Custom", "custom"),
        ):
            self.image_resolution_source_combo.addItem(label, value)
        self.image_resolution_source_combo.currentIndexChanged.connect(
            self._on_resolution_source_changed
        )
        self.form.addRow("Resolution", self.image_resolution_source_combo)

        self.output_resolution_source_combo = QComboBox()
        for label, value in (
            ("Auto", "auto"),
            ("Image", "image"),
            ("R", "r"),
            ("G", "g"),
            ("B", "b"),
            ("A", "a"),
            ("Custom", "custom"),
        ):
            self.output_resolution_source_combo.addItem(label, value)
        self.output_resolution_source_combo.currentIndexChanged.connect(
            self._on_resolution_source_changed
        )
        self.form.addRow("Resolution", self.output_resolution_source_combo)

        self.resolution_width_spin = self._make_int_spin(1, 16384, suffix=" px")
        self.resolution_height_spin = self._make_int_spin(1, 16384, suffix=" px")
        self.custom_resolution_host = self._resolution_widget(
            self.resolution_width_spin,
            self.resolution_height_spin,
        )
        self.form.addRow("Custom Size", self.custom_resolution_host)

        self.mask_filter_combo = QComboBox()
        for label, value in (
            ("Nearest (hard masks)", "nearest"),
            ("Bilinear (soft masks)", "bilinear"),
            ("Lanczos (smooth resize)", "lanczos"),
        ):
            self.mask_filter_combo.addItem(label, value)
        self.mask_filter_combo.currentIndexChanged.connect(self._apply_changes)
        self.form.addRow("Mask Filter", self.mask_filter_combo)

        self.mask_mode_combo = QComboBox()
        for label, value in (
            ("Replace Alpha", "replace_alpha"),
            ("Multiply Alpha", "multiply_alpha"),
            ("Multiply RGB", "multiply_rgb"),
            ("Multiply RGBA", "multiply_rgba"),
        ):
            self.mask_mode_combo.addItem(label, value)
        self.mask_mode_combo.currentIndexChanged.connect(self._apply_changes)
        self.form.addRow("Apply To", self.mask_mode_combo)

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

        self.pbr_workflow_combo = QComboBox()
        for label, value in (
            ("Traditional / Separate Maps", PbrWorkflow.TRADITIONAL.value),
            ("Unity URP", PbrWorkflow.UNITY_URP.value),
            ("Unity HDRP", PbrWorkflow.UNITY_HDRP.value),
            ("Unreal ORM", PbrWorkflow.UNREAL_ORM.value),
            ("Unreal MRA", PbrWorkflow.UNREAL_MRA.value),
            ("Unreal RMA", PbrWorkflow.UNREAL_RMA.value),
        ):
            self.pbr_workflow_combo.addItem(label, value)
        self.pbr_workflow_combo.currentIndexChanged.connect(self._apply_changes)
        self.form.addRow("Workflow", self.pbr_workflow_combo)

        self.pbr_normal_convention_combo = QComboBox()
        self.pbr_normal_convention_combo.addItem(
            "From Workflow", PbrNormalConvention.WORKFLOW.value
        )
        self.pbr_normal_convention_combo.addItem(
            "OpenGL Y+", PbrNormalConvention.OPENGL.value
        )
        self.pbr_normal_convention_combo.addItem(
            "DirectX Y−", PbrNormalConvention.DIRECTX.value
        )
        self.pbr_normal_convention_combo.currentIndexChanged.connect(
            self._apply_changes
        )
        self.form.addRow("Normal Input", self.pbr_normal_convention_combo)

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

        self.output_alpha_input_mode_combo = QComboBox()
        self.output_alpha_input_mode_combo.addItem(
            "Multiply Image Alpha",
            OutputAlphaInputMode.MULTIPLY.value,
        )
        self.output_alpha_input_mode_combo.addItem(
            "Replace Image Alpha",
            OutputAlphaInputMode.REPLACE.value,
        )
        self.output_alpha_input_mode_combo.setToolTip(
            "Controls how a connected A input combines with alpha already present in Image."
        )
        self.output_alpha_input_mode_combo.currentIndexChanged.connect(self._apply_changes)
        self.form.addRow("Alpha Input", self.output_alpha_input_mode_combo)

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
        was_interactive_edit = self._slider_drag_active
        self._node = node
        self._suppress = True
        try:
            self._numeric_preview_timer.stop()
            self._interactive_preview_timer.stop()
            self._slider_drag_active = False
            self._interactive_change_pending = False
            self.empty_label.setVisible(node is None)
            self.form_host.setVisible(node is not None)
            self.reset_parameters_button.setVisible(
                node is not None and node_has_resettable_parameters(node.node_type)
            )
            if node is None:
                return
            self.title_edit.setText(node.title)
            self.node_help_label.setText(self._node_help_text(node.node_type))
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
            self.mix_factor_spin.setValue(self._coerce_int(node.properties.get("factor"), 50))
            self.normal_flip_red_checkbox.setChecked(
                bool(node.properties.get("flip_red", False))
            )
            self.normal_flip_green_checkbox.setChecked(
                bool(node.properties.get("flip_green", False))
            )
            self.normal_reconstruct_blue_checkbox.setChecked(
                bool(node.properties.get("reconstruct_blue", False))
            )
            self.normal_normalize_checkbox.setChecked(
                bool(node.properties.get("normalize", False))
            )
            self.normal_strength_spin.setValue(
                self._coerce_int(node.properties.get("strength"), 100)
            )
            self.height_strength_spin.setValue(
                self._coerce_int(node.properties.get("strength"), 100)
            )
            self.height_radius_spin.setValue(
                self._coerce_float(node.properties.get("radius"), 1.0)
            )
            self._set_combo_value(
                self.height_convention_combo,
                str(node.properties.get("convention", "opengl")),
            )
            self.normal_blend_strength_spin.setValue(
                self._coerce_int(node.properties.get("detail_strength"), 100)
            )
            self.color_exposure_spin.setValue(
                self._coerce_float(node.properties.get("exposure"), 0.0)
            )
            self.color_brightness_spin.setValue(
                self._coerce_int(node.properties.get("brightness"), 0)
            )
            self.color_contrast_spin.setValue(
                self._coerce_int(node.properties.get("contrast"), 100)
            )
            self.color_saturation_spin.setValue(
                self._coerce_int(node.properties.get("saturation"), 100)
            )
            self.color_hue_spin.setValue(
                self._coerce_int(node.properties.get("hue"), 0)
            )
            self.color_gamma_spin.setValue(
                self._coerce_float(node.properties.get("gamma"), 1.0)
            )
            self.transform_flip_horizontal_checkbox.setChecked(
                bool(node.properties.get("flip_horizontal", False))
            )
            self.transform_flip_vertical_checkbox.setChecked(
                bool(node.properties.get("flip_vertical", False))
            )
            self._set_combo_value(
                self.transform_rotation_combo,
                self._coerce_int(node.properties.get("rotation"), 0),
            )
            self.transform_offset_x_spin.setValue(
                self._coerce_int(node.properties.get("offset_x"), 0)
            )
            self.transform_offset_y_spin.setValue(
                self._coerce_int(node.properties.get("offset_y"), 0)
            )
            self.transform_scale_spin.setValue(
                self._coerce_int(node.properties.get("scale"), 100)
            )
            self._set_combo_value(
                self.transform_address_combo,
                str(node.properties.get("address_mode", "repeat")),
            )
            self._set_combo_value(
                self.transform_filter_combo,
                str(node.properties.get("filter", "bilinear")),
                fallback_index=1,
            )
            self._set_combo_value(
                self.resize_size_mode_combo,
                str(node.properties.get("size_mode", "exact")),
            )
            self.resize_width_spin.setValue(
                self._coerce_int(node.properties.get("width"), 2048)
            )
            self.resize_height_spin.setValue(
                self._coerce_int(node.properties.get("height"), 2048)
            )
            self._set_combo_value(
                self.resize_mode_combo,
                str(node.properties.get("resize_mode", "stretch")),
            )
            self._set_combo_value(
                self.resize_filter_combo,
                str(node.properties.get("filter", "lanczos")),
                fallback_index=3,
            )
            self._set_combo_value(
                self.image_resolution_source_combo,
                str(node.properties.get("resolution_source", "a")),
            )
            self._set_combo_value(
                self.output_resolution_source_combo,
                str(node.properties.get("resolution_source", "auto")),
            )
            self.resolution_width_spin.setValue(
                self._coerce_int(node.properties.get("resolution_width"), 1024)
            )
            self.resolution_height_spin.setValue(
                self._coerce_int(node.properties.get("resolution_height"), 1024)
            )
            self._set_combo_value(
                self.mask_filter_combo,
                str(node.properties.get("mask_filter", "bilinear")),
                fallback_index=1,
            )
            self._set_combo_value(
                self.mask_mode_combo,
                str(node.properties.get("mask_mode", "replace_alpha")),
            )
            self.filename_edit.setText(str(node.properties.get("filename", "packed.png")))
            self.output_path_edit.setText(str(node.properties.get("output_path", "")))
            self._update_output_format_hint()
            self._set_combo_value(
                self.output_profile_combo,
                str(node.properties.get("profile", OutputProfile.GENERIC_RGBA.value)),
            )
            self._set_combo_value(
                self.pbr_workflow_combo,
                str(node.properties.get("workflow", PbrWorkflow.TRADITIONAL.value)),
            )
            self._set_combo_value(
                self.pbr_normal_convention_combo,
                str(
                    node.properties.get(
                        "normal_convention",
                        PbrNormalConvention.WORKFLOW.value,
                    )
                ),
            )
            self.enabled_checkbox.setChecked(bool(node.properties.get("enabled", True)))
            mode_value = str(node.properties.get("mode", OutputMode.RGBA.value))
            self._set_combo_value(self.mode_combo, mode_value, fallback_index=1)
            self._set_combo_value(
                self.output_alpha_input_mode_combo,
                str(
                    node.properties.get(
                        "alpha_input_mode",
                        OutputAlphaInputMode.MULTIPLY.value,
                    )
                ),
            )
            self._sync_visibility(node.node_type)
        finally:
            self._suppress = False
        if was_interactive_edit:
            self.interactive_edit_finished.emit()

    def _sync_visibility(self, node_type: NodeType) -> None:
        self._set_row_visible(self.node_help_label, bool(self._node_help_text(node_type)))
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
        blend_node = node_type in (NodeType.BLEND_CHANNEL, NodeType.BLEND_IMAGE)
        self._set_row_visible(self.blend_mode_combo, blend_node)
        self._set_row_visible(self.blend_opacity_host, blend_node)
        self._set_row_visible(self.mix_factor_host, node_type is NodeType.MIX_IMAGE)
        normal_map_node = node_type is NodeType.NORMAL_MAP
        self._set_row_visible(self.normal_flip_red_checkbox, normal_map_node)
        self._set_row_visible(self.normal_flip_green_checkbox, normal_map_node)
        self._set_row_visible(self.normal_reconstruct_blue_checkbox, normal_map_node)
        self._set_row_visible(self.normal_normalize_checkbox, normal_map_node)
        self._set_row_visible(self.normal_strength_host, normal_map_node)
        height_node = node_type is NodeType.HEIGHT_TO_NORMAL
        self._set_row_visible(self.height_strength_host, height_node)
        self._set_row_visible(self.height_radius_spin, height_node)
        self._set_row_visible(self.height_convention_combo, height_node)
        self._set_row_visible(
            self.normal_blend_strength_host,
            node_type is NodeType.NORMAL_BLEND,
        )
        color_adjust_node = node_type is NodeType.COLOR_ADJUST
        self._set_row_visible(self.color_exposure_spin, color_adjust_node)
        self._set_row_visible(self.color_brightness_host, color_adjust_node)
        self._set_row_visible(self.color_contrast_host, color_adjust_node)
        self._set_row_visible(self.color_saturation_host, color_adjust_node)
        self._set_row_visible(self.color_hue_host, color_adjust_node)
        self._set_row_visible(self.color_gamma_spin, color_adjust_node)
        transform_node = node_type is NodeType.TRANSFORM_2D
        self._set_row_visible(self.transform_flip_horizontal_checkbox, transform_node)
        self._set_row_visible(self.transform_flip_vertical_checkbox, transform_node)
        self._set_row_visible(self.transform_rotation_combo, transform_node)
        self._set_row_visible(self.transform_offset_host, transform_node)
        self._set_row_visible(self.transform_scale_host, transform_node)
        self._set_row_visible(self.transform_address_combo, transform_node)
        self._set_row_visible(self.transform_filter_combo, transform_node)
        resize_node = node_type is NodeType.RESIZE_CANVAS
        self._set_row_visible(self.resize_size_mode_combo, resize_node)
        self._set_row_visible(
            self.resize_resolution_host,
            resize_node and self.resize_size_mode_combo.currentData() == "exact",
        )
        self._set_row_visible(self.resize_mode_combo, resize_node)
        self._set_row_visible(self.resize_filter_combo, resize_node)
        image_resolution_node = node_type in (NodeType.MIX_IMAGE, NodeType.BLEND_IMAGE)
        self._set_row_visible(self.image_resolution_source_combo, image_resolution_node)
        self._set_row_visible(
            self.output_resolution_source_combo,
            node_type is NodeType.OUTPUT_RGBA,
        )
        self._set_row_visible(
            self.mask_filter_combo,
            node_type
            in (
                NodeType.MIX_IMAGE,
                NodeType.BLEND_IMAGE,
                NodeType.NORMAL_BLEND,
                NodeType.SET_ALPHA,
            ),
        )
        self._set_row_visible(self.mask_mode_combo, node_type is NodeType.SET_ALPHA)
        self._set_row_visible(self.filename_edit, node_type is NodeType.OUTPUT_RGBA)
        self._set_row_visible(self.output_path_host, node_type is NodeType.OUTPUT_RGBA)
        self._set_row_visible(self.pbr_workflow_combo, node_type is NodeType.PBR_SHADER)
        self._set_row_visible(
            self.pbr_normal_convention_combo,
            node_type is NodeType.PBR_SHADER,
        )
        self._set_row_visible(self.output_profile_combo, node_type is NodeType.OUTPUT_RGBA)
        self._set_row_visible(self.profile_actions_host, node_type is NodeType.OUTPUT_RGBA)
        self._set_row_visible(self.profile_summary_label, node_type is NodeType.OUTPUT_RGBA)
        self._set_row_visible(self.mode_combo, node_type is NodeType.OUTPUT_RGBA)
        self._set_row_visible(
            self.output_alpha_input_mode_combo,
            node_type is NodeType.OUTPUT_RGBA,
        )
        self._set_row_visible(self.enabled_checkbox, node_type is NodeType.OUTPUT_RGBA)
        self._set_row_visible(self.export_output_button, node_type is NodeType.OUTPUT_RGBA)
        self._sync_resolution_visibility(node_type)

    @staticmethod
    def _node_help_text(node_type: NodeType) -> str:
        return {
            NodeType.MIX_IMAGE: (
                "Mixes RGBA inputs A and B. Mask 0 uses A; Mask 255 uses B. "
                "Factor is used when Mask is not connected."
            ),
            NodeType.BLEND_IMAGE: (
                "Blends two required RGBA inputs A and B using Blend Mode and Opacity. "
                "An optional Mask limits the effect. For one Color/Image plus a channel mask, "
                "use Apply Mask instead."
            ),
            NodeType.NORMAL_MAP: (
                "Converts tangent-space Normal Maps: flip X/Y, rebuild Blue, "
                "normalize vectors and adjust strength. Flip Green converts DirectX ↔ OpenGL."
            ),
            NodeType.HEIGHT_TO_NORMAL: (
                "Creates a normalized tangent-space normal map from Height. "
                "Choose OpenGL for Unity or DirectX for Unreal."
            ),
            NodeType.NORMAL_BLEND: (
                "Combines Base and Detail normal maps with Reoriented Normal Mapping (RNM). "
                "An optional Mask limits the detail contribution."
            ),
            NodeType.COLOR_ADJUST: (
                "Adjusts exposure, brightness, contrast, saturation, hue and gamma while "
                "preserving alpha."
            ),
            NodeType.TRANSFORM_2D: (
                "Flips, rotates, offsets and scales an image with Clamp, Repeat or Mirror "
                "addressing."
            ),
            NodeType.RESIZE_CANVAS: (
                "Resizes to an exact or power-of-two output using Stretch, Fit, Fill, Crop "
                "or Pad layout."
            ),
            NodeType.SPLIT_RGBA: "Splits one RGBA image into separate R, G, B and A channels.",
            NodeType.COMBINE_RGBA: (
                "Builds one RGBA image from separate R, G, B and optional A channels."
            ),
            NodeType.SET_ALPHA: (
                "Applies a channel mask to an RGBA image. Replace Alpha creates a regular "
                "transparent cutout; Multiply RGB reproduces per-channel color masking."
            ),
            NodeType.OUTPUT_RGBA: (
                "Image supplies the RGBA base. R, G and B replace individual base channels. "
                "A multiplies Image alpha by default, so existing transparency is preserved."
            ),
            NodeType.PBR_SHADER: (
                "Builds an interactive GPU material preview from separate or packed maps. "
                "Workflow controls packed channels and Normal Map orientation."
            ),
        }.get(node_type, "")

    def _on_resolution_source_changed(self, *_args: object) -> None:
        if self._node is not None:
            self._sync_resolution_visibility(self._node.node_type)
        self._apply_changes()

    def _on_resize_size_mode_changed(self, *_args: object) -> None:
        if self._node is not None:
            self._sync_visibility(self._node.node_type)
        self._apply_changes()

    def _sync_resolution_visibility(self, node_type: NodeType) -> None:
        custom_selected = False
        if node_type in (NodeType.MIX_IMAGE, NodeType.BLEND_IMAGE):
            custom_selected = self.image_resolution_source_combo.currentData() == "custom"
        elif node_type is NodeType.OUTPUT_RGBA:
            custom_selected = self.output_resolution_source_combo.currentData() == "custom"
        self._set_row_visible(self.custom_resolution_host, custom_selected)

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
        dialog = QColorDialog(initial, self)
        dialog.setWindowTitle("Choose Color")
        dialog.setOption(QColorDialog.ColorDialogOption.ShowAlphaChannel, True)
        dialog.currentColorChanged.connect(self._preview_selected_color)

        if dialog.exec():
            color = dialog.currentColor()
            if color.isValid():
                self._set_color_controls(color)
                self._apply_changes()
            return

        self._set_color_controls(initial)
        self._emit_transient_color_preview(initial, PREVIEW_MODE_FULL)

    def _preview_selected_color(self, color: QColor) -> None:
        if not color.isValid():
            return
        self._set_color_controls(color)
        self._emit_transient_color_preview(color, PREVIEW_MODE_DRAFT)

    def _set_color_controls(self, color: QColor) -> None:
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

    def _emit_transient_color_preview(self, color: QColor, preview_mode: str) -> None:
        if self._node is None or self._node.node_type is not NodeType.COLOR:
            return
        properties = dict(self._node.properties)
        properties.update(
            {
                "red": color.red(),
                "green": color.green(),
                "blue": color.blue(),
                "alpha": color.alpha(),
            }
        )
        self.transient_preview_requested.emit(self._node, properties, preview_mode)

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
            self._queue_interactive_preview()
            return
        self._numeric_preview_timer.start()

    def _flush_numeric_preview(self) -> None:
        self._emit_node_change(PREVIEW_MODE_FULL)

    def _on_slider_drag_started(self) -> None:
        if self._suppress:
            return
        self._slider_drag_active = True
        self._interactive_change_pending = False
        self._numeric_preview_timer.stop()
        self.interactive_edit_started.emit()

    def _on_slider_drag_finished(self) -> None:
        was_dragging = self._slider_drag_active
        self._slider_drag_active = False
        self._interactive_preview_timer.stop()
        self._interactive_change_pending = False
        if self._suppress or self._node is None or not was_dragging:
            return
        changed = self._emit_node_change(PREVIEW_MODE_FULL)
        self.interactive_edit_finished.emit()
        if not changed:
            self.preview_refresh_requested.emit(self._node, PREVIEW_MODE_FULL)

    def _queue_interactive_preview(self) -> None:
        self._interactive_change_pending = True
        if self._interactive_preview_timer.isActive():
            return
        self._flush_interactive_preview()
        self._interactive_preview_timer.start()

    def _flush_interactive_preview(self) -> None:
        if (
            self._suppress
            or self._node is None
            or not self._slider_drag_active
            or not self._interactive_change_pending
        ):
            return
        self._interactive_change_pending = False
        self._emit_node_change(PREVIEW_MODE_DRAFT)

    def _emit_node_change(self, preview_mode: str) -> bool:
        if self._suppress or self._node is None:
            return False
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
        elif self._node.node_type in (NodeType.BLEND_CHANNEL, NodeType.BLEND_IMAGE):
            next_properties["mode"] = str(self.blend_mode_combo.currentData() or "multiply")
            next_properties["opacity"] = self.blend_opacity_spin.value()
            if self._node.node_type is NodeType.BLEND_IMAGE:
                next_properties["resolution_source"] = str(
                    self.image_resolution_source_combo.currentData() or "a"
                )
                next_properties["resolution_width"] = self.resolution_width_spin.value()
                next_properties["resolution_height"] = self.resolution_height_spin.value()
                next_properties["mask_filter"] = str(
                    self.mask_filter_combo.currentData() or "bilinear"
                )
        elif self._node.node_type is NodeType.MIX_IMAGE:
            next_properties["factor"] = self.mix_factor_spin.value()
            next_properties["resolution_source"] = str(
                self.image_resolution_source_combo.currentData() or "a"
            )
            next_properties["resolution_width"] = self.resolution_width_spin.value()
            next_properties["resolution_height"] = self.resolution_height_spin.value()
            next_properties["mask_filter"] = str(
                self.mask_filter_combo.currentData() or "bilinear"
            )
        elif self._node.node_type is NodeType.NORMAL_MAP:
            next_properties["flip_red"] = self.normal_flip_red_checkbox.isChecked()
            next_properties["flip_green"] = self.normal_flip_green_checkbox.isChecked()
            next_properties["reconstruct_blue"] = (
                self.normal_reconstruct_blue_checkbox.isChecked()
            )
            next_properties["normalize"] = self.normal_normalize_checkbox.isChecked()
            next_properties["strength"] = self.normal_strength_spin.value()
        elif self._node.node_type is NodeType.HEIGHT_TO_NORMAL:
            next_properties["strength"] = self.height_strength_spin.value()
            next_properties["radius"] = self.height_radius_spin.value()
            next_properties["convention"] = str(
                self.height_convention_combo.currentData() or "opengl"
            )
        elif self._node.node_type is NodeType.NORMAL_BLEND:
            next_properties["detail_strength"] = self.normal_blend_strength_spin.value()
            next_properties["mask_filter"] = str(
                self.mask_filter_combo.currentData() or "bilinear"
            )
        elif self._node.node_type is NodeType.COLOR_ADJUST:
            next_properties["exposure"] = self.color_exposure_spin.value()
            next_properties["brightness"] = self.color_brightness_spin.value()
            next_properties["contrast"] = self.color_contrast_spin.value()
            next_properties["saturation"] = self.color_saturation_spin.value()
            next_properties["hue"] = self.color_hue_spin.value()
            next_properties["gamma"] = self.color_gamma_spin.value()
        elif self._node.node_type is NodeType.TRANSFORM_2D:
            next_properties["flip_horizontal"] = (
                self.transform_flip_horizontal_checkbox.isChecked()
            )
            next_properties["flip_vertical"] = (
                self.transform_flip_vertical_checkbox.isChecked()
            )
            next_properties["rotation"] = int(
                self.transform_rotation_combo.currentData() or 0
            )
            next_properties["offset_x"] = self.transform_offset_x_spin.value()
            next_properties["offset_y"] = self.transform_offset_y_spin.value()
            next_properties["scale"] = self.transform_scale_spin.value()
            next_properties["address_mode"] = str(
                self.transform_address_combo.currentData() or "repeat"
            )
            next_properties["filter"] = str(
                self.transform_filter_combo.currentData() or "bilinear"
            )
        elif self._node.node_type is NodeType.RESIZE_CANVAS:
            next_properties["size_mode"] = str(
                self.resize_size_mode_combo.currentData() or "exact"
            )
            next_properties["width"] = self.resize_width_spin.value()
            next_properties["height"] = self.resize_height_spin.value()
            next_properties["resize_mode"] = str(
                self.resize_mode_combo.currentData() or "stretch"
            )
            next_properties["filter"] = str(
                self.resize_filter_combo.currentData() or "lanczos"
            )
        elif self._node.node_type is NodeType.SET_ALPHA:
            next_properties["mask_mode"] = str(
                self.mask_mode_combo.currentData() or "replace_alpha"
            )
            next_properties["mask_filter"] = str(
                self.mask_filter_combo.currentData() or "bilinear"
            )
        elif self._node.node_type is NodeType.PBR_SHADER:
            next_properties["workflow"] = str(
                self.pbr_workflow_combo.currentData()
                or PbrWorkflow.TRADITIONAL.value
            )
            next_properties["normal_convention"] = str(
                self.pbr_normal_convention_combo.currentData()
                or PbrNormalConvention.WORKFLOW.value
            )
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
            next_properties["alpha_input_mode"] = str(
                self.output_alpha_input_mode_combo.currentData()
                or OutputAlphaInputMode.MULTIPLY.value
            )
            next_properties["enabled"] = self.enabled_checkbox.isChecked()
            next_properties["resolution_source"] = str(
                self.output_resolution_source_combo.currentData() or "auto"
            )
            next_properties["resolution_width"] = self.resolution_width_spin.value()
            next_properties["resolution_height"] = self.resolution_height_spin.value()
        if next_title == self._node.title and next_properties == self._node.properties:
            return False
        needs_rebuild = previous_title != next_title
        if self._node.node_type is NodeType.TEXTURE_INPUT:
            needs_rebuild = needs_rebuild or previous_path != str(next_properties.get("path", ""))
        self.node_changed.emit(self._node, next_title, next_properties, needs_rebuild, preview_mode)
        return True

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
    def _set_combo_value(combo: QComboBox, value: object, *, fallback_index: int = 0) -> None:
        combo.setCurrentIndex(fallback_index)
        for index in range(combo.count()):
            if combo.itemData(index) == value:
                combo.setCurrentIndex(index)
                return
