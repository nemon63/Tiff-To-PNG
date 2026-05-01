from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from image_converter.domain.models import ConversionOptions, QueueItem, QueueStatus, TextureMapType
from image_converter.services.colorspace import item_preflight_warnings
from image_converter.services.inspection import build_output_estimate
from image_converter.services.naming import build_output_filename
from image_converter.ui.common import AUTO_MAP_TYPE_DATA, _map_type_label


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


class MetricTile(QFrame):
    def __init__(self, title: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("InspectorMetricTile")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(4)

        self.title_label = QLabel(title)
        self.title_label.setObjectName("MetricKey")
        layout.addWidget(self.title_label)

        self.value_label = QLabel("-")
        self.value_label.setObjectName("MetricValue")
        self.value_label.setWordWrap(False)
        self.value_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.value_label)

    def set_value(self, text: str) -> None:
        self.value_label.setText(text)
        self.value_label.setToolTip(text)



class MetadataPanel(QWidget):
    map_type_override_changed = pyqtSignal(object)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._current_item: QueueItem | None = None
        self._conversion_options = ConversionOptions()
        self._build_ui()

    def _build_ui(self) -> None:
        self.setObjectName("SectionPanel")
        self.setMinimumHeight(170)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(12)

        header_row = QHBoxLayout()
        title_label = QLabel("Asset Inspector")
        title_label.setObjectName("PanelTitle")
        header_row.addWidget(title_label)
        header_row.addStretch(1)
        layout.addLayout(header_row)

        self.inspector_hint_label = QLabel("Selected asset metadata and export estimate.")
        self.inspector_hint_label.setObjectName("InspectorHintText")
        self.inspector_hint_label.setWordWrap(True)
        layout.addWidget(self.inspector_hint_label)

        hero_card = QFrame()
        hero_card.setObjectName("InspectorCard")
        hero_layout = QVBoxLayout(hero_card)
        hero_layout.setContentsMargins(16, 14, 16, 14)
        hero_layout.setSpacing(10)

        self.asset_name_label = QLabel("Ничего не выбрано")
        self.asset_name_label.setObjectName("AssetName")
        self.asset_name_label.setWordWrap(True)
        hero_layout.addWidget(self.asset_name_label)

        self.asset_meta_label = QLabel("Select an asset.")
        self.asset_meta_label.setObjectName("PreviewMetaText")
        self.asset_meta_label.setWordWrap(True)
        hero_layout.addWidget(self.asset_meta_label)

        map_type_row = QHBoxLayout()
        map_type_row.setSpacing(8)
        map_type_label = QLabel("Map Type")
        map_type_label.setObjectName("SummaryText")
        map_type_row.addWidget(map_type_label)
        self.map_type_combo = QComboBox()
        self.map_type_combo.currentIndexChanged.connect(self._emit_map_type_override)
        map_type_row.addWidget(self.map_type_combo, 1)
        hero_layout.addLayout(map_type_row)
        layout.addWidget(hero_card)

        fields_grid = QGridLayout()
        fields_grid.setHorizontalSpacing(10)
        fields_grid.setVerticalSpacing(10)

        self.source_field = InspectorField("Исходный файл", wrap_value=True)
        self.output_field = InspectorField("Выходной PNG", wrap_value=True)
        self.name_preview_field = InspectorField("Имя после rules", wrap_value=True)
        self.expected_field = InspectorField("После настроек", wrap_value=True)

        fields_grid.addWidget(self.source_field, 0, 0, 1, 2)
        fields_grid.addWidget(self.output_field, 1, 0, 1, 2)
        fields_grid.addWidget(self.name_preview_field, 2, 0)
        fields_grid.addWidget(self.expected_field, 2, 1)
        fields_grid.setColumnStretch(0, 1)
        fields_grid.setColumnStretch(1, 1)
        layout.addLayout(fields_grid)

        metrics_grid = QGridLayout()
        metrics_grid.setHorizontalSpacing(10)
        metrics_grid.setVerticalSpacing(10)
        self.format_field = MetricTile("Формат")
        self.resolution_field = MetricTile("Разрешение")
        self.mode_field = MetricTile("Режим")
        self.colorspace_field = MetricTile("Color Space")
        self.alpha_field = MetricTile("Alpha")
        self.frames_field = MetricTile("Кадры")
        self.size_field = MetricTile("Размер")

        metrics_grid.addWidget(self.format_field, 0, 0)
        metrics_grid.addWidget(self.resolution_field, 0, 1)
        metrics_grid.addWidget(self.mode_field, 1, 0)
        metrics_grid.addWidget(self.colorspace_field, 1, 1)
        metrics_grid.addWidget(self.alpha_field, 2, 0)
        metrics_grid.addWidget(self.size_field, 2, 1)
        metrics_grid.addWidget(self.frames_field, 3, 0, 1, 2)
        metrics_grid.setColumnStretch(0, 1)
        metrics_grid.setColumnStretch(1, 1)
        layout.addLayout(metrics_grid)

        self.warning_label = QLabel("")
        self.warning_label.setObjectName("WarningBanner")
        self.warning_label.setWordWrap(True)
        self.warning_label.hide()
        layout.addWidget(self.warning_label)

        self.viewer_hint_label = QLabel("Viewer: double-click asset, Ctrl+wheel zoom, MMB pan, F fit.")
        self.viewer_hint_label.setObjectName("InspectorHintText")
        self.viewer_hint_label.setWordWrap(True)
        layout.addWidget(self.viewer_hint_label)
        layout.addStretch(1)

    def set_queue_item(self, item: QueueItem | None) -> None:
        self._current_item = item
        self._sync_map_type_combo(item)

        if item is None:
            self.asset_name_label.setText("Ничего не выбрано")
            self.asset_meta_label.setText("Select an asset.")
            self._set_values(
                source="-",
                format_value="-",
                resolution="-",
                mode="-",
                colorspace="-",
                alpha="-",
                frames="-",
                size="-",
                name_preview="-",
                output="Будет рассчитан после выбора выходной папки.",
                expected="-",
            )
            self.warning_label.hide()
            return

        self.asset_name_label.setText(item.path.name)
        metadata = item.metadata

        if metadata is None:
            self.asset_meta_label.setText(item.message or "Метаданные недоступны.")
            self._set_values(
                source=str(item.path),
                format_value="-",
                resolution="-",
                mode="-",
                colorspace="-",
                alpha="-",
                frames="-",
                size="-",
                name_preview="Недоступно без метаданных.",
                output=str(item.output_path) if item.output_path is not None else "Рядом с исходным файлом.",
                expected="Недоступно без метаданных.",
            )
            self.warning_label.setText(item.message or "Не удалось прочитать метаданные.")
            self.warning_label.show()
            return

        output_estimate = build_output_estimate(
            metadata,
            self._conversion_options,
            item.effective_map_type,
        )
        self.asset_meta_label.setText(
            f"{_map_type_label(item.effective_map_type)}  •  {metadata.resolution_text}  •  {metadata.mode}  •  {output_estimate.colorspace_text}"
        )
        output_name = build_output_filename(
            item.path,
            self._conversion_options.naming,
            item.effective_map_type,
        )
        self._set_values(
            source=str(item.path),
            format_value=metadata.format_name,
            resolution=metadata.resolution_text,
            mode=metadata.mode,
            colorspace=output_estimate.colorspace_text,
            alpha="Да" if metadata.has_alpha else "Нет",
            frames=str(metadata.frame_count),
            size=metadata.size_text,
            name_preview=output_name,
            output=str(item.output_path) if item.output_path is not None else "Рядом с исходным файлом.",
            expected=output_estimate.summary,
        )

        warnings: list[str] = list(item_preflight_warnings(item))
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
        colorspace: str,
        alpha: str,
        frames: str,
        size: str,
        name_preview: str,
        output: str,
        expected: str,
    ) -> None:
        self.source_field.set_value(source)
        self.name_preview_field.set_value(name_preview)
        self.format_field.set_value(format_value)
        self.resolution_field.set_value(resolution)
        self.mode_field.set_value(mode)
        self.colorspace_field.set_value(colorspace)
        self.alpha_field.set_value(alpha)
        self.frames_field.set_value(frames)
        self.size_field.set_value(size)
        self.output_field.set_value(output)
        self.expected_field.set_value(expected)

    def set_conversion_options(self, options: ConversionOptions) -> None:
        self._conversion_options = options
        self.set_queue_item(self._current_item)

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
