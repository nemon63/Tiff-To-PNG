from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QByteArray, QMimeData, Qt, QUrl, pyqtSignal
from PyQt6.QtGui import QAction, QCloseEvent, QDrag, QDragEnterEvent, QDropEvent
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QDockWidget,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from image_converter.domain.models import (
    AppSettings,
    BatchRequest,
    BatchSource,
    ConversionPreset,
    QueueItem,
    QueueStatus,
    TextureMapType,
)
from image_converter.services.colorspace import (
    item_preflight_warnings,
    item_warning_count,
    item_warning_summary,
    recommended_colorspace_for_map_type,
)
from image_converter.services.conversion import BatchConversionService
from image_converter.services.packing import build_channel_pack_jobs, summarize_channel_pack_jobs
from image_converter.services.presets import PresetRepository
from image_converter.ui.common import (
    _colorspace_label,
    _extract_local_paths,
    _map_type_label,
    _queue_status_display,
    _status_colors,
)
from image_converter.ui.inspector import MetadataPanel
from image_converter.ui.log_panel import LogPanel
from image_converter.ui.node_editor import GraphWorkspace
from image_converter.ui.preview import DetachedPreviewWindow, PreviewPanel
from image_converter.ui.queue_panel import QueuePanel
from image_converter.ui.settings_panel import SettingsPanel


class AssetTableWidget(QTableWidget):
    asset_dropped = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setDragEnabled(True)
        self.setAcceptDrops(False)

    def startDrag(self, supported_actions: Qt.DropAction) -> None:
        item = self.currentItem()
        if item is None:
            return
        path = item.data(Qt.ItemDataRole.UserRole)
        if not path:
            return
        mime_data = QMimeData()
        mime_data.setData("application/x-texture-path", str(path).encode("utf-8"))
        mime_data.setText(str(path))
        mime_data.setUrls([QUrl.fromLocalFile(str(path))])
        drag = QDrag(self)
        drag.setMimeData(mime_data)
        drag.exec(Qt.DropAction.CopyAction)


class MainWindow(QMainWindow):
    convert_requested = pyqtSignal()
    queue_paths_received = pyqtSignal(list)

    def __init__(self):
        super().__init__()
        self._is_running = False
        self._queue_items: list[QueueItem] = []
        self._preset_repository: PresetRepository | None = None
        self._presets_by_id: dict[str, ConversionPreset] = {}
        self.setWindowTitle("Texture Pipeline Workbench")
        self.resize(1280, 820)
        self.setMinimumSize(720, 480)
        self.setAcceptDrops(True)
        self._build_ui()
        self.statusBar().showMessage("Готово")

    def _build_ui(self) -> None:
        self.setDockNestingEnabled(True)
        self.view_menu = self.menuBar().addMenu("Вид")

        self.settings_panel = SettingsPanel()
        self.settings_panel.convert_requested.connect(self.convert_requested.emit)
        self.settings_panel.output_path_changed.connect(self._sync_top_output_path)
        self.settings_panel.output_path_changed.connect(self._update_queue_output_paths)
        self.settings_panel.preset_apply_requested.connect(self._apply_preset)
        self.settings_panel.preset_save_requested.connect(self._save_current_preset)
        self.settings_panel.preset_delete_requested.connect(self._delete_preset)
        self.settings_panel.options_changed.connect(self._sync_preset_selection_with_current_options)
        self.settings_panel.options_changed.connect(self._update_queue_output_paths)
        self.settings_panel.options_changed.connect(self._sync_metadata_conversion_options)

        settings_scroll = QScrollArea()
        settings_scroll.setWidgetResizable(True)
        settings_scroll.setFrameShape(QFrame.Shape.NoFrame)
        settings_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        settings_scroll.setWidget(self.settings_panel)

        self.queue_panel = QueuePanel()
        self.queue_panel.paths_selected.connect(self.queue_paths_received.emit)
        self.queue_panel.paths_dropped.connect(self.queue_paths_received.emit)
        self.queue_panel.remove_requested.connect(self.remove_selected_queue_items)
        self.queue_panel.clear_requested.connect(self.clear_queue_items)
        self.queue_panel.table.itemSelectionChanged.connect(self._sync_status_bar_with_selection)
        self.queue_panel.table.itemSelectionChanged.connect(self._sync_workspace_selection)
        self.queue_panel.table.itemDoubleClicked.connect(self._open_selected_preview_window)

        self.preview_window = DetachedPreviewWindow()
        self.preview_panel = PreviewPanel(allow_detach=True)
        self.metadata_panel = MetadataPanel()
        self.metadata_panel.set_conversion_options(self.settings_panel.build_conversion_options())
        self.metadata_panel.map_type_override_changed.connect(self._apply_selected_map_type_override)
        self.graph_workspace = GraphWorkspace()
        self.graph_workspace.export_requested.connect(self._export_graph)
        self.graph_workspace.preview_image_requested.connect(self._show_graph_preview)
        self.graph_workspace.status_message.connect(self.set_status)
        self.graph_workspace.status_message.connect(self.append_log)
        self.log_panel = LogPanel()

        central = QWidget()
        central_layout = QVBoxLayout(central)
        central_layout.setContentsMargins(10, 10, 10, 8)
        central_layout.setSpacing(8)
        central_layout.addWidget(self._build_top_toolbar())
        central_layout.addWidget(self.graph_workspace, 1)
        self.setCentralWidget(central)

        self.assets_panel = self._build_assets_panel()
        self.assets_dock = self._create_dock("Assets", self.assets_panel, "AssetsDock")
        self.node_properties_dock = self._create_dock(
            "Node Properties",
            self.graph_workspace.build_properties_widget(),
            "NodePropertiesDock",
        )
        self.inspector_dock = self._create_dock("Inspector", self.metadata_panel, "InspectorDock")
        self.export_dock = self._create_dock("Export Settings", settings_scroll, "ExportDock")
        self.preview_dock = self._create_dock("Preview", self.preview_panel, "PreviewDock")
        self.queue_dock = self._create_dock("Queue", self.queue_panel, "QueueDock")
        self.log_dock = self._create_dock("Log", self.log_panel, "LogDock")

        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.assets_dock)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.node_properties_dock)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.inspector_dock)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.export_dock)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.preview_dock)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.queue_dock)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.log_dock)
        self.tabifyDockWidget(self.node_properties_dock, self.inspector_dock)
        self.tabifyDockWidget(self.inspector_dock, self.export_dock)
        self.tabifyDockWidget(self.export_dock, self.preview_dock)
        self.tabifyDockWidget(self.queue_dock, self.log_dock)
        self.node_properties_dock.raise_()

        self._register_view_docks()
        self.inspector_dock.hide()
        self.export_dock.hide()
        self.preview_dock.hide()
        self.queue_dock.hide()
        self.log_dock.hide()
        self.resizeDocks(
            [self.assets_dock, self.node_properties_dock],
            [280, 340],
            Qt.Orientation.Horizontal,
        )
        self._sync_active_workspace_label()

    def _create_dock(self, title: str, widget: QWidget, object_name: str) -> QDockWidget:
        dock = QDockWidget(title, self)
        dock.setObjectName(object_name)
        dock.setWidget(widget)
        dock.setAllowedAreas(
            Qt.DockWidgetArea.LeftDockWidgetArea
            | Qt.DockWidgetArea.RightDockWidgetArea
            | Qt.DockWidgetArea.BottomDockWidgetArea
            | Qt.DockWidgetArea.TopDockWidgetArea
        )
        dock.setFeatures(
            QDockWidget.DockWidgetFeature.DockWidgetClosable
            | QDockWidget.DockWidgetFeature.DockWidgetMovable
            | QDockWidget.DockWidgetFeature.DockWidgetFloatable
        )
        return dock

    def _register_view_docks(self) -> None:
        for dock in (
            self.assets_dock,
            self.node_properties_dock,
            self.inspector_dock,
            self.export_dock,
            self.preview_dock,
            self.queue_dock,
            self.log_dock,
        ):
            self.view_menu.addAction(dock.toggleViewAction())
        self.view_menu.addSeparator()
        show_log_action = QAction("Показать лог", self)
        show_log_action.triggered.connect(lambda: self.log_dock.show())
        self.view_menu.addAction(show_log_action)

    def _build_top_toolbar(self) -> QFrame:
        toolbar = QFrame()
        toolbar.setObjectName("TopToolbar")
        layout = QHBoxLayout(toolbar)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(8)

        title = QLabel("Texture Pipeline Workbench")
        title.setObjectName("AppTitle")
        layout.addWidget(title)

        self.active_workspace_label = QLabel("Queue")
        self.active_workspace_label.setObjectName("StatusPill")
        layout.addWidget(self.active_workspace_label)

        layout.addStretch(1)
        layout.addWidget(QLabel("Output"))
        self.top_output_edit = QLineEdit()
        self.top_output_edit.setPlaceholderText("Output folder")
        self.top_output_edit.setMinimumWidth(360)
        self.top_output_edit.textEdited.connect(self._apply_top_output_path)
        layout.addWidget(self.top_output_edit)

        self.run_batch_button = QPushButton("Run Batch")
        self.run_batch_button.setObjectName("PrimaryButton")
        self.run_batch_button.clicked.connect(self.convert_requested.emit)
        layout.addWidget(self.run_batch_button)

        self.export_graph_button = QPushButton("Export Graph")
        self.export_graph_button.clicked.connect(self._export_graph)
        layout.addWidget(self.export_graph_button)

        self.toolbar_status_label = QLabel("Ready")
        self.toolbar_status_label.setObjectName("StatusPill")
        layout.addWidget(self.toolbar_status_label)
        return toolbar

    def _build_assets_panel(self) -> QFrame:
        panel = QFrame()
        panel.setObjectName("SidebarPanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        title = QLabel("Assets")
        title.setObjectName("PanelTitle")
        layout.addWidget(title)

        buttons = QHBoxLayout()
        add_files = QPushButton("+ Files")
        add_files.clicked.connect(self.queue_panel._pick_files)
        buttons.addWidget(add_files)
        add_folder = QPushButton("+ Folder")
        add_folder.clicked.connect(self.queue_panel._pick_folder)
        buttons.addWidget(add_folder)
        layout.addLayout(buttons)

        self.asset_table = AssetTableWidget()
        self.asset_table.setColumnCount(3)
        self.asset_table.setHorizontalHeaderLabels(("Name", "Type", "Res"))
        self.asset_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.asset_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.asset_table.verticalHeader().setVisible(False)
        self.asset_table.horizontalHeader().setStretchLastSection(True)
        self.asset_table.itemDoubleClicked.connect(self._preview_selected_asset)
        layout.addWidget(self.asset_table, 1)

        hint = QLabel("Drag an asset into Graph. Double-click opens Preview.")
        hint.setObjectName("SummaryText")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        return panel

    def _apply_top_output_path(self, text: str) -> None:
        if self.settings_panel.output_edit.text() != text:
            self.settings_panel.output_edit.setText(text)

    def _sync_top_output_path(self, text: str) -> None:
        if self.top_output_edit.text() != text:
            self.top_output_edit.setText(text)

    def _sync_active_workspace_label(self, *_args: object) -> None:
        if hasattr(self, "active_workspace_label"):
            self.active_workspace_label.setText("Graph")

    def _render_asset_browser(self) -> None:
        table = self.asset_table
        table.setRowCount(len(self._queue_items))
        for row, item in enumerate(self._queue_items):
            metadata = item.metadata
            values = (
                item.path.name,
                _map_type_label(item.effective_map_type),
                metadata.resolution_text if metadata else "-",
            )
            for column, value in enumerate(values):
                table_item = QTableWidgetItem(value)
                table_item.setToolTip(str(item.path))
                table_item.setData(Qt.ItemDataRole.UserRole, self._queue_key(item.path))
                table.setItem(row, column, table_item)
        self.graph_workspace.set_assets(list(self._queue_items))

    def _add_selected_asset_to_graph(self, *_args: object) -> None:
        item = self._selected_asset_item()
        if item is None:
            return
        self.graph_workspace.add_texture_node_for_path(str(item.path), None)

    def _preview_selected_asset(self, *_args: object) -> None:
        item = self._selected_asset_item()
        if item is None:
            return
        self.preview_panel.set_queue_item(item)
        self.preview_dock.show()
        self.preview_dock.raise_()

    def _show_graph_preview(self, image, title: str, meta: str) -> None:
        self.preview_panel.set_graph_preview(image, title, meta)
        self.preview_dock.show()
        self.preview_dock.raise_()

    def _selected_asset_item(self) -> QueueItem | None:
        selected_rows = self.asset_table.selectionModel().selectedRows()
        if not selected_rows:
            return None
        row = selected_rows[0].row()
        if row >= len(self._queue_items):
            return None
        return self._queue_items[row]

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
            item.batch_source for item in self._queue_items if item.status is not QueueStatus.ERROR
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
            item.output_path = self._build_output_path(item.batch_source)
            key = self._queue_key(item.path)
            if key in existing_index:
                self._queue_items[existing_index[key]] = item
            else:
                self._queue_items.append(item)

        self._queue_items.sort(key=lambda item: str(item.path).lower())
        self._render_queue()
        self._render_asset_browser()
        self._refresh_packing_preflight()
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
        self._render_asset_browser()
        self._refresh_packing_preflight()
        self._sync_workspace_selection()
        self.set_status(f"Удалено элементов: {len(selected_rows)}")

    def clear_queue_items(self) -> None:
        self._queue_items.clear()
        self._render_queue()
        self._render_asset_browser()
        self._refresh_packing_preflight()
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
        self.run_batch_button.setEnabled(not running)
        self.export_graph_button.setEnabled(not running)

    def append_log(self, line: str) -> None:
        self.log_panel.append_line(line)

    def clear_log(self) -> None:
        self.log_panel.clear()

    def set_status(self, text: str) -> None:
        self.statusBar().showMessage(text)
        if hasattr(self, "toolbar_status_label"):
            self.toolbar_status_label.setText(text[:60])

    def apply_app_settings(self, settings: AppSettings) -> None:
        if settings.window_geometry:
            self.restoreGeometry(QByteArray.fromBase64(settings.window_geometry.encode("ascii")))
        else:
            self.resize(settings.window_width, settings.window_height)
        self.settings_panel.apply_app_settings(settings)
        self._sync_top_output_path(settings.output_path)
        if settings.window_state:
            self.restoreState(QByteArray.fromBase64(settings.window_state.encode("ascii")))
        self._update_queue_output_paths()
        self._sync_preset_selection_with_current_options()
        self._refresh_packing_preflight()

    def set_preset_repository(self, preset_repository: PresetRepository) -> None:
        self._preset_repository = preset_repository
        self._reload_presets()

    def build_app_settings(self) -> AppSettings:
        request = self.settings_panel.build_request()
        return AppSettings(
            input_path=str(request.input_path or ""),
            output_path=str(request.output_root or ""),
            options=request.options,
            window_width=self.width(),
            window_height=self.height(),
            splitter_sizes=(300, 980),
            workspace_splitter_sizes=(980, 340),
            detail_splitter_sizes=(640, 180),
            inspector_splitter_sizes=(1,),
            window_geometry=bytes(self.saveGeometry().toBase64()).decode("ascii"),
            window_state=bytes(self.saveState().toBase64()).decode("ascii"),
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
        self.preview_window.hide()
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
            dynamic_warnings = item_preflight_warnings(item)
            if dynamic_warnings and item.status in (QueueStatus.READY, QueueStatus.PENDING):
                status_tooltip = "; ".join(dynamic_warnings)

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
            if self.preview_window.isVisible():
                self.preview_window.set_queue_item(None)
            self.metadata_panel.set_queue_item(None)
            self.preview_panel.set_queue_item(None)
            self.set_status("Очередь пуста")
            return

        warning_count = sum(1 for item in self._queue_items if item_warning_count(item))
        error_count = sum(1 for item in self._queue_items if item.status is QueueStatus.ERROR)
        summary_text = f"Всего: {len(self._queue_items)} | Предупреждений: {warning_count} | Ошибок: {error_count}"
        self.queue_panel.summary_label.setText(summary_text)
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
        lines.append(
            f"Color Space: {_colorspace_label(recommended_colorspace_for_map_type(item.effective_map_type))}"
        )
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
            f"Color Space: {_colorspace_label(recommended_colorspace_for_map_type(item.effective_map_type))}",
            f"Формат: {metadata.format_name}",
            f"Разрешение: {metadata.resolution_text}",
            f"Режим: {metadata.mode}",
            f"Альфа: {'да' if metadata.has_alpha else 'нет'}",
            f"Кадров/страниц: {metadata.frame_count}",
            f"Размер файла: {metadata.size_text}",
        ]
        if item.output_path is not None:
            lines.append(f"Выходное имя: {item.output_path.name}")
        dynamic_warnings = item_preflight_warnings(item)
        if dynamic_warnings:
            lines.append("Предупреждения:")
            lines.extend(f"- {warning}" for warning in dynamic_warnings)
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
        return BatchConversionService.build_destination_for_source(
            source,
            output_root,
            self.settings_panel.build_conversion_options(),
        )

    def _update_queue_output_paths(self, *_args: object) -> None:
        for item in self._queue_items:
            item.output_path = self._build_output_path(item.batch_source)
        self._render_queue()
        self._render_asset_browser()
        self._refresh_packing_preflight()
        self._sync_workspace_selection()

    def _reload_presets(self) -> None:
        if self._preset_repository is None:
            self._presets_by_id = {}
            self.settings_panel.set_available_presets([])
            return

        presets = list(self._preset_repository.load_presets())
        self._presets_by_id = {preset.preset_id: preset for preset in presets}
        self.settings_panel.set_available_presets(presets)
        self._sync_preset_selection_with_current_options()

    def _sync_preset_selection_with_current_options(self, *_args: object) -> None:
        current_options = self.settings_panel.build_conversion_options()
        matched_preset_id = next(
            (
                preset.preset_id
                for preset in self._presets_by_id.values()
                if preset.options == current_options
            ),
            None,
        )
        self.settings_panel.set_selected_preset_id(matched_preset_id)

    def _sync_metadata_conversion_options(self, *_args: object) -> None:
        self.metadata_panel.set_conversion_options(self.settings_panel.build_conversion_options())

    def _refresh_packing_preflight(self, *_args: object) -> None:
        options = self.settings_panel.build_conversion_options()
        if not options.packing.enabled:
            self.settings_panel.set_packing_preflight_summary(
                "Packing выключен. Включите его, если нужно собрать ORM/RMA/MRA или Unity-packed карты прямо из набора текстур."
            )
            return

        sources = [item.batch_source for item in self._queue_items if item.status is not QueueStatus.ERROR]
        jobs = build_channel_pack_jobs(sources, None, options)
        self.settings_panel.set_packing_preflight_summary(summarize_channel_pack_jobs(jobs))

    def _apply_preset(self, preset_id: str) -> None:
        preset = self._presets_by_id.get(preset_id)
        if preset is None:
            return

        self.settings_panel.apply_conversion_options(preset.options)
        self.settings_panel.set_selected_preset_id(preset.preset_id)
        self.set_status(f"Применен preset: {preset.name}")

    def _save_current_preset(self) -> None:
        if self._preset_repository is None:
            return

        selected_preset = self._presets_by_id.get(self.settings_panel.selected_preset_id() or "")
        suggested_name = "My Preset"
        if selected_preset is not None:
            suggested_name = (
                f"{selected_preset.name} Copy" if selected_preset.is_system else selected_preset.name
            )

        name, accepted = QInputDialog.getText(
            self,
            "Сохранить preset",
            "Название preset:",
            text=suggested_name,
        )
        if not accepted:
            return

        clean_name = name.strip()
        if not clean_name:
            self.show_error("Preset не сохранен", "Название preset не может быть пустым.")
            return

        existing_user_preset = next(
            (
                preset
                for preset in self._presets_by_id.values()
                if not preset.is_system and preset.name.casefold() == clean_name.casefold()
            ),
            None,
        )
        if existing_user_preset is not None:
            button = QMessageBox.question(
                self,
                "Перезаписать preset",
                f"Preset '{existing_user_preset.name}' уже существует. Перезаписать его?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if button is not QMessageBox.StandardButton.Yes:
                return

        try:
            preset = self._preset_repository.save_preset(
                clean_name,
                self.settings_panel.build_conversion_options(),
            )
        except (OSError, ValueError) as exc:
            self.show_error("Preset не сохранен", str(exc))
            return

        self._reload_presets()
        self.settings_panel.set_selected_preset_id(preset.preset_id)
        self.set_status(f"Сохранен preset: {preset.name}")

    def _delete_preset(self, preset_id: str) -> None:
        if self._preset_repository is None:
            return

        preset = self._presets_by_id.get(preset_id)
        if preset is None or preset.is_system:
            return

        button = QMessageBox.question(
            self,
            "Удалить preset",
            f"Удалить пользовательский preset '{preset.name}'?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if button is not QMessageBox.StandardButton.Yes:
            return

        try:
            deleted = self._preset_repository.delete_preset(preset_id)
        except OSError as exc:
            self.show_error("Preset не удален", str(exc))
            return

        if not deleted:
            return

        self._reload_presets()
        self.set_status(f"Удален preset: {preset.name}")

    def _sync_status_bar_with_selection(self) -> None:
        item = self._selected_queue_item()
        if item is None:
            if not self._queue_items:
                self.set_status("Очередь пуста")
            return
        if item.status in (QueueStatus.READY, QueueStatus.PENDING) and item_warning_count(item):
            message = item_warning_summary(item)
        else:
            message = item.message or _queue_status_display(item)
        self.set_status(f"{item.path.name}: {message}")

    def _sync_workspace_selection(self) -> None:
        item = self._selected_queue_item()
        self.metadata_panel.set_queue_item(item)
        self.preview_panel.set_queue_item(item)
        if self.preview_window.isVisible():
            self.preview_window.set_queue_item(item)

    def _open_selected_preview_window(self, *_args: object) -> None:
        item = self._selected_queue_item()
        if item is None or item.status is QueueStatus.ERROR:
            return
        self.preview_panel.set_queue_item(item)
        self.preview_dock.show()
        self.preview_dock.raise_()

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

        item.output_path = self._build_output_path(item.batch_source)
        self._render_queue()
        self._refresh_packing_preflight()
        self._sync_workspace_selection()
        self._sync_status_bar_with_selection()

    def _export_graph(self) -> None:
        request = self.settings_panel.build_request()
        output_root = request.output_root
        if output_root is None:
            if self.graph_workspace.project_dir is not None:
                output_root = self.graph_workspace.project_dir / "exports"
            else:
                output_root = Path.cwd() / "graph_exports"

        self.append_log("---- Graph export ----")
        summary = self.graph_workspace.export_graph(
            output_root,
            request.options,
            self.append_log,
        )
        self.append_log(summary.as_text())
        self.set_status(summary.as_text())
        if summary.failed:
            self.show_error("Graph export", summary.as_text())
        elif summary.succeeded:
            self.show_info("Graph export", summary.as_text())

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
        return
