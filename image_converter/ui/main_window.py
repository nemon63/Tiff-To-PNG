from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

from PyQt6.QtCore import QByteArray, QItemSelectionModel, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QAction,
    QActionGroup,
    QCloseEvent,
    QColor,
    QDragEnterEvent,
    QDropEvent,
    QFont,
    QIcon,
    QKeySequence,
    QPixmap,
    QShortcut,
)
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QDockWidget,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

WORKSPACE_BATCH = "batch"
WORKSPACE_GRAPH = "graph"

from image_converter.domain.models import (
    AppSettings,
    BatchRequest,
    BatchSource,
    ChannelPackingMode,
    ConversionPreset,
    QueueItem,
    QueueStatus,
    TextureMapType,
)
from image_converter.domain.node_graph import GraphNode, NodeType
from image_converter.domain.errors import ValidationError
from image_converter.services.asset_queue import AssetScanner
from image_converter.services.colorspace import (
    item_preflight_warnings,
    item_warning_count,
    item_warning_summary,
    recommended_colorspace_for_map_type,
)
from image_converter.services.conversion import BatchConversionService
from image_converter.services.node_graph_executor import NodeGraphExecutor
from image_converter.application.graph_export import (
    GraphBatchExportRequest,
    GraphBatchExportResult,
    GraphBatchExportTask,
    GraphExportController,
    GraphExportRequest,
)
from image_converter.application.asset_scan import (
    AlphaAnalysisController,
    AlphaAnalysisRequest,
    AssetScanController,
    AssetScanRequest,
    file_revision,
)
from image_converter.application.thumbnail import ThumbnailController
from image_converter.application.job_coordinator import ApplicationJobCoordinator
from image_converter.services.packing import (
    build_channel_pack_jobs,
    packed_source_map_types,
    summarize_channel_pack_jobs,
)
from image_converter.services.presets import PresetRepository
from image_converter.services.validation import validate_request
from image_converter.domain.constants import FILE_DIALOG_FILTER
from image_converter.ui.common import (
    _colorspace_label,
    _extract_local_paths,
    _map_type_label,
    _queue_status_display,
    _status_colors,
    _qimage_from_pil,
)
from image_converter.ui.asset_browser import AssetTableWidget
from image_converter.ui.inspector import MetadataPanel
from image_converter.ui.log_panel import LogPanel
from image_converter.ui.node_editor import GraphWorkspace
from image_converter.ui.preview import DetachedPreviewWindow, PreviewPanel
from image_converter.ui.queue_panel import QueuePanel
from image_converter.ui.settings_panel import SettingsPanel


class MainWindow(QMainWindow):
    convert_requested = pyqtSignal()
    queue_paths_received = pyqtSignal(list)

    def __init__(self):
        super().__init__()
        self._is_running = False
        self._queue_items: list[QueueItem] = []
        self._path_key_cache: OrderedDict[str, str] = OrderedDict()
        self._queue_row_items: list[QueueItem | None] = []
        self._asset_rows: list[QueueItem] = []
        self._preset_repository: PresetRepository | None = None
        self._presets_by_id: dict[str, ConversionPreset] = {}
        self._workspace_mode = WORKSPACE_GRAPH
        self._graph_node_properties_visible = False
        self._graph_auto_watch_enabled = False
        self._graph_auto_export_enabled = False
        self._graph_auto_export_scheduled = False
        self._graph_auto_export_paths: tuple[Path, ...] = ()
        self._graph_export_in_progress = False
        self._graph_batch_export_in_progress = False
        self._graph_auto_watch_status = "Auto Watch Off"
        self._asset_scanner = AssetScanner()
        self.job_coordinator = ApplicationJobCoordinator(self)
        self._graph_export_controller = GraphExportController(self)
        self._asset_scan_controller = AssetScanController(self)
        self._alpha_analysis_controller = AlphaAnalysisController(self)
        self._thumbnail_controller = ThumbnailController(self)
        self._scan_progress_counts: dict[tuple[str, tuple[str, ...]], int] = {}
        self._pending_scan_items: dict[
            tuple[str, tuple[str, ...]], tuple[AssetScanRequest, list[QueueItem]]
        ] = {}
        self._scan_flush_timer = QTimer(self)
        self._scan_flush_timer.setSingleShot(True)
        self._scan_flush_timer.setInterval(50)
        self._scan_flush_timer.timeout.connect(self._flush_asset_scan_items)
        self.setWindowTitle("Texture Pipeline Workbench")
        self.resize(1280, 820)
        self.setMinimumSize(720, 480)
        self.setAcceptDrops(True)
        self._build_ui()
        self.queue_paths_received.connect(self._request_queue_scan)
        self.statusBar().showMessage("Готово")

    def _build_ui(self) -> None:
        self.setDockNestingEnabled(True)
        self.mode_menu = self.menuBar().addMenu("Режим")
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

        self.settings_scroll = QScrollArea()
        self.settings_scroll.setWidgetResizable(True)
        self.settings_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.settings_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.settings_scroll.setWidget(self.settings_panel)

        self.queue_panel = QueuePanel()
        self.queue_panel.paths_selected.connect(self.queue_paths_received.emit)
        self.queue_panel.paths_dropped.connect(self.queue_paths_received.emit)
        self.queue_panel.remove_requested.connect(self.remove_selected_queue_items)
        self.queue_panel.clear_requested.connect(self.clear_queue_items)
        self.queue_panel.open_selected_set_in_graph_requested.connect(self._open_selected_set_in_graph)
        self.queue_panel.open_selected_files_in_graph_requested.connect(self._open_selected_files_in_graph)
        self.queue_panel.apply_graph_to_queue_requested.connect(self._apply_current_graph_to_queue)
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
        self.graph_workspace.output_export_requested.connect(self._export_graph_output)
        self.graph_workspace.preview_image_requested.connect(self._show_graph_preview)
        self.graph_workspace.preview_failed.connect(self._show_graph_preview_error)
        self.graph_workspace.status_message.connect(self.set_status)
        self.graph_workspace.status_message.connect(self.append_log)
        self.graph_workspace.assets_changed.connect(self._on_graph_assets_changed)
        self.graph_workspace.template_changed.connect(self._refresh_graph_apply_preflight)
        self.graph_workspace.watched_paths_changed.connect(self._on_graph_watched_paths_changed)
        self.log_panel = LogPanel()
        self._graph_auto_export_timer = QTimer(self)
        self._graph_auto_export_timer.setInterval(250)
        self._graph_auto_export_timer.setSingleShot(True)
        self._graph_auto_export_timer.timeout.connect(self._run_graph_auto_export)

        central = QWidget()
        central_layout = QVBoxLayout(central)
        central_layout.setContentsMargins(10, 10, 10, 8)
        central_layout.setSpacing(8)
        self.top_toolbar = self._build_top_toolbar()
        central_layout.addWidget(self.top_toolbar)
        self.workspace_stack = QStackedWidget()
        self.batch_workspace = self._build_batch_workspace()
        self.workspace_stack.addWidget(self.graph_workspace)
        self.workspace_stack.addWidget(self.batch_workspace)
        central_layout.addWidget(self.workspace_stack, 1)
        self.setCentralWidget(central)

        self.assets_panel = self._build_assets_panel()
        self.assets_dock = self._create_dock("Assets", self.assets_panel, "AssetsDock")
        self.node_properties_dock = self._create_dock(
            "Node Properties",
            self.graph_workspace.build_properties_widget(),
            "NodePropertiesDock",
        )
        self.inspector_dock = self._create_dock("Inspector", self.metadata_panel, "InspectorDock")
        self.preview_dock = self._create_dock("Preview", self.preview_panel, "PreviewDock")
        self.log_dock = self._create_dock("Log", self.log_panel, "LogDock")
        self.assets_dock.setMinimumWidth(220)
        self.node_properties_dock.setMinimumWidth(260)
        self.inspector_dock.setMinimumWidth(260)
        self.preview_dock.setMinimumWidth(240)

        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.assets_dock)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.node_properties_dock)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.inspector_dock)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.preview_dock)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.log_dock)
        self.tabifyDockWidget(self.node_properties_dock, self.inspector_dock)
        self.tabifyDockWidget(self.inspector_dock, self.preview_dock)
        self.node_properties_dock.visibilityChanged.connect(self._on_node_properties_visibility_changed)
        self.node_properties_shortcut = QShortcut(QKeySequence("P"), self)
        self.node_properties_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self.node_properties_shortcut.activated.connect(self._show_node_properties_dock)

        self._register_mode_actions()
        self._register_view_docks()
        self.node_properties_dock.hide()
        self.inspector_dock.hide()
        self.preview_dock.hide()
        self.log_dock.hide()
        self.resizeDocks(
            [self.assets_dock, self.node_properties_dock],
            [280, 340],
            Qt.Orientation.Horizontal,
        )
        self._set_workspace_mode(WORKSPACE_GRAPH)

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

    def _build_batch_workspace(self) -> QFrame:
        panel = QFrame()
        panel.setObjectName("WorkspaceCard")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.settings_scroll.setMinimumWidth(340)
        self.settings_scroll.setMaximumWidth(460)
        splitter.addWidget(self.settings_scroll)
        splitter.addWidget(self.queue_panel)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([380, 820])
        layout.addWidget(splitter, 1)
        return panel

    def _register_view_docks(self) -> None:
        for dock in (
            self.assets_dock,
            self.node_properties_dock,
            self.inspector_dock,
            self.preview_dock,
            self.log_dock,
        ):
            self.view_menu.addAction(dock.toggleViewAction())
        self.view_menu.addSeparator()
        show_log_action = QAction("Показать лог", self)
        show_log_action.triggered.connect(lambda: self.log_dock.show())
        self.view_menu.addAction(show_log_action)
        reset_layout_action = QAction("Reset Layout", self)
        reset_layout_action.triggered.connect(self._reset_window_layout)
        self.view_menu.addAction(reset_layout_action)

    def _reset_window_layout(self) -> None:
        for dock in (
            self.assets_dock,
            self.node_properties_dock,
            self.inspector_dock,
            self.preview_dock,
            self.log_dock,
        ):
            dock.setFloating(False)

        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.assets_dock)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.node_properties_dock)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.inspector_dock)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.preview_dock)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.log_dock)
        self.tabifyDockWidget(self.node_properties_dock, self.inspector_dock)
        self.tabifyDockWidget(self.inspector_dock, self.preview_dock)
        self.log_dock.hide()
        self.resizeDocks(
            [self.assets_dock, self.node_properties_dock],
            [280, 340],
            Qt.Orientation.Horizontal,
        )
        self._set_workspace_mode(self._workspace_mode)

    def _register_mode_actions(self) -> None:
        self.mode_action_group = QActionGroup(self)
        self.mode_action_group.setExclusive(True)
        self.batch_mode_action = QAction("Batch Converter", self)
        self.batch_mode_action.setCheckable(True)
        self.graph_mode_action = QAction("Graph Workbench", self)
        self.graph_mode_action.setCheckable(True)
        self.batch_mode_action.triggered.connect(lambda: self._set_workspace_mode(WORKSPACE_BATCH))
        self.graph_mode_action.triggered.connect(lambda: self._set_workspace_mode(WORKSPACE_GRAPH))
        self.mode_action_group.addAction(self.batch_mode_action)
        self.mode_action_group.addAction(self.graph_mode_action)
        self.mode_menu.addAction(self.batch_mode_action)
        self.mode_menu.addAction(self.graph_mode_action)

    def _build_top_toolbar(self) -> QFrame:
        toolbar = QFrame()
        toolbar.setObjectName("TopToolbar")
        toolbar.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        layout = QVBoxLayout(toolbar)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(6)

        primary_row = QHBoxLayout()
        primary_row.setSpacing(8)

        title = QLabel("Texture Pipeline Workbench")
        title.setObjectName("AppTitle")
        primary_row.addWidget(title)

        self.batch_mode_button = QPushButton("Batch Converter")
        self.batch_mode_button.setObjectName("ModeButton")
        self.batch_mode_button.setCheckable(True)
        self.batch_mode_button.setToolTip("Переключиться в пакетную обработку.")
        self.batch_mode_button.clicked.connect(lambda: self._set_workspace_mode(WORKSPACE_BATCH))
        self._configure_mode_button(self.batch_mode_button, "Batch Converter")
        primary_row.addWidget(self.batch_mode_button)

        self.graph_mode_button = QPushButton("Graph Workbench")
        self.graph_mode_button.setObjectName("ModeButton")
        self.graph_mode_button.setCheckable(True)
        self.graph_mode_button.setToolTip("Переключиться в режим ручной сборки и правки графа.")
        self.graph_mode_button.clicked.connect(lambda: self._set_workspace_mode(WORKSPACE_GRAPH))
        self._configure_mode_button(self.graph_mode_button, "Graph Workbench")
        primary_row.addWidget(self.graph_mode_button)

        self.active_workspace_label = QLabel("Queue")
        self.active_workspace_label.setObjectName("StatusPill")
        self.active_workspace_label.setMinimumWidth(0)
        self.active_workspace_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        primary_row.addWidget(self.active_workspace_label)

        primary_row.addStretch(1)

        self.toolbar_status_label = QLabel("Готово")
        self.toolbar_status_label.setObjectName("StatusPill")
        self.toolbar_status_label.setMinimumWidth(0)
        self.toolbar_status_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        primary_row.addWidget(self.toolbar_status_label)
        layout.addLayout(primary_row)

        secondary_row = QHBoxLayout()
        secondary_row.setSpacing(8)
        self.top_output_label = QLabel("Export Folder")
        self.top_output_label.setToolTip(
            "Base folder for graph export. Relative Output File values resolve inside this folder."
        )
        secondary_row.addWidget(self.top_output_label)
        self.top_output_edit = QLineEdit()
        self.top_output_edit.setPlaceholderText("Base folder for graph exports")
        self.top_output_edit.setMinimumWidth(160)
        self.top_output_edit.setMaximumWidth(360)
        self.top_output_edit.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.top_output_edit.setToolTip(
            "Base folder for graph export. Relative Output File values resolve inside this folder."
        )
        self.top_output_edit.textEdited.connect(self._apply_top_output_path)
        secondary_row.addWidget(self.top_output_edit, 1)

        self.export_graph_button = QPushButton("Export Graph")
        self.export_graph_button.clicked.connect(self._export_graph)
        self.export_graph_button.hide()
        secondary_row.addWidget(self.export_graph_button)

        self.auto_watch_button = QPushButton("Auto Watch")
        self.auto_watch_button.setCheckable(True)
        self.auto_watch_button.clicked.connect(self._toggle_graph_auto_watch)
        secondary_row.addWidget(self.auto_watch_button)

        self.auto_export_button = QPushButton("Auto Rebuild")
        self.auto_export_button.setCheckable(True)
        self.auto_export_button.clicked.connect(self._toggle_graph_auto_export)
        secondary_row.addWidget(self.auto_export_button)

        self.graph_watch_status_label = QLabel(self._graph_auto_watch_status)
        self.graph_watch_status_label.setObjectName("StatusPill")
        self.graph_watch_status_label.setMinimumWidth(0)
        self.graph_watch_status_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        secondary_row.addWidget(self.graph_watch_status_label)
        layout.addLayout(secondary_row)
        return toolbar

    def _configure_mode_button(self, button: QPushButton, text: str) -> None:
        button.setMinimumHeight(30)
        button.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        button.setMinimumWidth(button.fontMetrics().horizontalAdvance(text) + 28)

    def _build_assets_panel(self) -> QFrame:
        panel = QFrame()
        panel.setObjectName("SidebarPanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        title = QLabel("Assets")
        title.setObjectName("PanelTitle")
        layout.addWidget(title)

        buttons = QGridLayout()
        buttons.setHorizontalSpacing(8)
        buttons.setVerticalSpacing(8)
        add_files = QPushButton("+ Files")
        add_files.clicked.connect(self._pick_graph_asset_files)
        buttons.addWidget(add_files, 0, 0)
        add_folder = QPushButton("+ Folder")
        add_folder.clicked.connect(self._pick_graph_asset_folder)
        buttons.addWidget(add_folder, 0, 1)
        reload_asset = QPushButton("Reload")
        reload_asset.clicked.connect(self._reload_selected_asset)
        buttons.addWidget(reload_asset, 1, 0)
        self.remove_asset_button = QPushButton("Remove")
        self.remove_asset_button.setObjectName("DangerButton")
        self.remove_asset_button.clicked.connect(self._remove_selected_assets)
        buttons.addWidget(self.remove_asset_button, 1, 1)
        layout.addLayout(buttons)

        self.asset_filter_edit = QLineEdit()
        self.asset_filter_edit.setPlaceholderText("Filter assets")
        self.asset_filter_edit.textChanged.connect(self._render_asset_browser)
        layout.addWidget(self.asset_filter_edit)

        self.asset_table = AssetTableWidget()
        self.asset_table.setColumnCount(4)
        self.asset_table.setHorizontalHeaderLabels(("", "Name", "Type", "Res"))
        self.asset_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.asset_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.asset_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.asset_table.verticalHeader().setVisible(False)
        self.asset_table.horizontalHeader().setStretchLastSection(True)
        self.asset_table.setIconSize(QSize(42, 42))
        self.asset_table.itemDoubleClicked.connect(self._preview_selected_asset)
        self.asset_table.remove_requested.connect(self._remove_selected_assets)
        self.asset_table.verticalScrollBar().valueChanged.connect(
            self._request_visible_asset_thumbnails
        )
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

    def _set_workspace_mode(self, mode: str) -> None:
        if mode not in {WORKSPACE_BATCH, WORKSPACE_GRAPH}:
            mode = WORKSPACE_GRAPH
        self._workspace_mode = mode
        is_batch = mode == WORKSPACE_BATCH

        if hasattr(self, "workspace_stack"):
            self.workspace_stack.setCurrentWidget(self.batch_workspace if is_batch else self.graph_workspace)
        if hasattr(self, "batch_mode_button"):
            self.batch_mode_button.setChecked(is_batch)
            self.graph_mode_button.setChecked(not is_batch)
        if hasattr(self, "batch_mode_action"):
            self.batch_mode_action.setChecked(is_batch)
            self.graph_mode_action.setChecked(not is_batch)
        if hasattr(self, "active_workspace_label"):
            self.active_workspace_label.setText("Batch" if is_batch else "Graph")

        if hasattr(self, "export_graph_button"):
            self.export_graph_button.setVisible(False)
            self.top_output_label.setVisible(not is_batch)
            self.top_output_edit.setVisible(not is_batch)
            self.auto_watch_button.setVisible(not is_batch)
            self.auto_export_button.setVisible(not is_batch)
            self.graph_watch_status_label.setVisible(not is_batch)
        if hasattr(self, "assets_dock"):
            if is_batch:
                self.assets_dock.hide()
                self._graph_node_properties_visible = not self.node_properties_dock.isHidden()
                self.node_properties_dock.hide()
                self.inspector_dock.show()
                self.inspector_dock.raise_()
            else:
                self.assets_dock.show()
                self.inspector_dock.hide()
                if self._graph_node_properties_visible:
                    self.node_properties_dock.show()
                    self.node_properties_dock.raise_()
                else:
                    self.node_properties_dock.hide()

        self.set_status("Режим Batch Converter" if is_batch else "Режим Graph Workbench")

    def _sync_active_workspace_label(self, *_args: object) -> None:
        self._set_workspace_mode(self._workspace_mode)

    def _render_asset_browser(self) -> None:
        table = self.asset_table
        selected_keys = set(self._selected_asset_keys())
        filter_text = self.asset_filter_edit.text().strip().casefold() if hasattr(self, "asset_filter_edit") else ""
        self._asset_rows = [
            item
            for item in self.graph_workspace.assets()
            if not filter_text
            or filter_text in item.path.name.casefold()
            or filter_text in _map_type_label(item.effective_map_type).casefold()
        ]
        table.setRowCount(len(self._asset_rows))
        for row, item in enumerate(self._asset_rows):
            metadata = item.metadata
            exists = item.path.exists()
            values = (
                "",
                item.path.name,
                _map_type_label(item.effective_map_type) if exists else "Missing",
                metadata.resolution_text if metadata else "-",
            )
            for column, value in enumerate(values):
                table_item = QTableWidgetItem(value)
                table_item.setToolTip(str(item.path))
                table_item.setData(Qt.ItemDataRole.UserRole, self._queue_key(item.path))
                table.setItem(row, column, table_item)
        self._restore_asset_selection(selected_keys)
        QTimer.singleShot(0, self._request_visible_asset_thumbnails)

    def _request_visible_asset_thumbnails(self, *_args: object) -> None:
        table = self.asset_table
        if not self._asset_rows:
            return
        first_row = table.rowAt(0)
        if first_row < 0:
            first_row = 0
        last_row = table.rowAt(max(0, table.viewport().height() - 1))
        if last_row < 0:
            last_row = min(len(self._asset_rows) - 1, first_row + 50)
        first_row = max(0, first_row - 8)
        last_row = min(len(self._asset_rows) - 1, last_row + 8)
        for row in range(first_row, last_row + 1):
            table_item = table.item(row, 0)
            if table_item is None or not table_item.icon().isNull():
                continue
            icon = self._asset_thumbnail_icon(self._asset_rows[row])
            if icon is not None:
                table_item.setIcon(icon)

    def _add_selected_asset_to_graph(self, *_args: object) -> None:
        item = self._selected_asset_item()
        if item is None:
            return
        self.graph_workspace.add_texture_node_for_path(str(item.path), None)

    def _preview_selected_asset(self, *_args: object) -> None:
        item = self._selected_asset_item()
        if item is None:
            return
        self._request_lazy_alpha_analysis(item)
        self.preview_panel.set_queue_item(item)
        self.preview_dock.show()
        self.preview_dock.raise_()

    def _pick_graph_asset_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "Выберите изображения для Graph", "", FILE_DIALOG_FILTER)
        if paths:
            self._add_graph_assets_from_paths(paths)

    def _pick_graph_asset_folder(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Выберите папку с текстурами для Graph")
        if path:
            self._add_graph_assets_from_paths([path])

    def _add_graph_assets_from_paths(self, raw_paths: list[str]) -> None:
        self._asset_scan_controller.submit(
            AssetScanRequest(
                target="graph",
                paths=tuple(Path(raw_path) for raw_path in raw_paths),
                recursive=self.queue_recursive_enabled(),
            )
        )

    def _request_queue_scan(self, raw_paths: list[str]) -> None:
        self._asset_scan_controller.submit(
            AssetScanRequest(
                target="queue",
                paths=tuple(Path(raw_path) for raw_path in raw_paths),
                recursive=self.queue_recursive_enabled(),
            )
        )

    def on_asset_scan_item(self, request: AssetScanRequest, item: QueueItem) -> None:
        self.on_asset_scan_items(request, (item,))

    def on_asset_scan_items(
        self,
        request: AssetScanRequest,
        items: object,
    ) -> None:
        batch = [item for item in items if isinstance(item, QueueItem)]
        if not batch:
            return
        key = request.coalesce_key
        self._scan_progress_counts[key] = self._scan_progress_counts.get(key, 0) + len(batch)
        pending = self._pending_scan_items.get(key)
        if pending is None:
            self._pending_scan_items[key] = (request, batch)
        else:
            pending[1].extend(batch)
        if not self._scan_flush_timer.isActive():
            self._scan_flush_timer.start()
        self.set_status(f"Сканирование: найдено {self._scan_progress_counts[key]}")

    def _flush_asset_scan_items(self) -> None:
        pending_items = tuple(self._pending_scan_items.values())
        self._pending_scan_items.clear()
        for request, items in pending_items:
            if request.target == "queue":
                self.add_queue_items(items)
            elif request.target == "graph":
                assets = list(self.graph_workspace.assets())
                index = {
                    self._queue_key(asset.path): offset
                    for offset, asset in enumerate(assets)
                }
                for item in items:
                    item_key = self._queue_key(item.path)
                    if item_key in index:
                        assets[index[item_key]] = item
                    else:
                        index[item_key] = len(assets)
                        assets.append(item)
                assets.sort(key=lambda asset: str(asset.path).lower())
                self.graph_workspace.set_assets(assets)
                self._render_asset_browser()
                self._refresh_graph_apply_preflight()
            elif request.target == "reload":
                for item in items:
                    self._apply_reloaded_asset(item)

    def on_asset_scan_finished(self, request: AssetScanRequest, result: object) -> None:
        self._flush_asset_scan_items()
        count = self._scan_progress_counts.pop(request.coalesce_key, 0)
        for message in getattr(result, "ignored_messages", ()):
            self.append_log(message)
        if request.target == "reload":
            self.graph_workspace.refresh_asset_paths(request.paths)
            self._render_queue()
            self._render_asset_browser()
            self._sync_workspace_selection()
            self._refresh_graph_apply_preflight()
        if count:
            label = "Graph Assets" if request.target == "graph" else "очередь"
            self.set_status(f"Добавлено/обновлено в {label}: {count}")
        else:
            self.set_status("Поддерживаемые файлы не найдены")

    def on_asset_scan_failed(self, request: AssetScanRequest, message: str) -> None:
        self._flush_asset_scan_items()
        self._scan_progress_counts.pop(request.coalesce_key, None)
        self.append_log(f"Scan error: {message}")
        self.set_status("Ошибка сканирования")

    def _apply_reloaded_asset(self, refreshed: QueueItem) -> None:
        queue_item = self._find_queue_item(refreshed.path)
        if queue_item is not None:
            refreshed.source = queue_item.source
            refreshed.output_path = self._build_output_path(refreshed.batch_source)
            queue_item.asset_kind = refreshed.asset_kind
            queue_item.metadata = refreshed.metadata
            queue_item.status = refreshed.status
            queue_item.message = refreshed.message
            queue_item.output_path = refreshed.output_path
        assets = list(self.graph_workspace.assets())
        for index, asset in enumerate(assets):
            if self._queue_key(asset.path) != self._queue_key(refreshed.path):
                continue
            if queue_item is not None:
                assets[index] = queue_item
            else:
                refreshed.source = asset.source
                assets[index] = refreshed
        self.graph_workspace.set_assets(assets)

    def _reload_selected_asset(self) -> None:
        items = self._selected_asset_items()
        if not items:
            return
        paths = [item.path for item in items]
        self._reload_assets_for_paths(tuple(paths), source_label="Reload")

    def _remove_selected_assets(self) -> None:
        selected_items = self._selected_asset_items()
        if not selected_items:
            return
        selected_keys = {self._queue_key(item.path) for item in selected_items}
        remaining = [
            item
            for item in self.graph_workspace.assets()
            if self._queue_key(item.path) not in selected_keys
        ]
        self.graph_workspace.set_assets(remaining)
        self._render_asset_browser()
        self._refresh_graph_apply_preflight()
        self.set_status(f"Удалено ассетов из Graph: {len(selected_items)}")

    def _asset_thumbnail_icon(self, item: QueueItem) -> QIcon | None:
        image = self._thumbnail_controller.request(item.path)
        if image is None:
            return None
        return QIcon(QPixmap.fromImage(_qimage_from_pil(image)))

    def on_asset_thumbnail_ready(self, path: Path, image: object) -> None:
        if not hasattr(image, "tobytes"):
            return
        key = self._queue_key(path)
        icon = QIcon(QPixmap.fromImage(_qimage_from_pil(image)))
        for row, item in enumerate(self._asset_rows):
            if self._queue_key(item.path) != key:
                continue
            table_item = self.asset_table.item(row, 0)
            if table_item is not None:
                table_item.setIcon(icon)

    def _show_graph_preview(self, image, title: str, meta: str, node_id: str) -> None:
        preserve_zoom = self.preview_panel.current_graph_preview_node_id() == node_id
        self.preview_panel.set_graph_preview(image, title, meta, node_id=node_id, preserve_zoom=preserve_zoom)
        self.preview_dock.show()
        self.preview_dock.raise_()

    def _show_graph_preview_error(self, title: str, message: str, node_id: str) -> None:
        self.preview_panel.set_graph_preview_error(title, message, node_id=node_id)
        self.preview_dock.show()
        self.preview_dock.raise_()

    def _show_node_properties_dock(self) -> None:
        if self._workspace_mode != WORKSPACE_GRAPH:
            return
        self._graph_node_properties_visible = True
        self.node_properties_dock.show()
        self.node_properties_dock.raise_()

    def _on_node_properties_visibility_changed(self, visible: bool) -> None:
        if self._workspace_mode == WORKSPACE_GRAPH:
            self._graph_node_properties_visible = visible

    def _selected_asset_items(self) -> list[QueueItem]:
        selection_model = self.asset_table.selectionModel()
        if selection_model is None:
            return []
        selected_rows = sorted({index.row() for index in selection_model.selectedRows()})
        return [
            self._asset_rows[row]
            for row in selected_rows
            if row < len(self._asset_rows)
        ]

    def _selected_asset_item(self) -> QueueItem | None:
        items = self._selected_asset_items()
        return items[0] if items else None

    def _selected_asset_keys(self) -> list[str]:
        return [self._queue_key(item.path) for item in self._selected_asset_items()]

    def _selected_queue_items(self) -> list[QueueItem]:
        selection_model = self.queue_panel.table.selectionModel()
        if selection_model is None:
            return []
        selected_rows = sorted({index.row() for index in selection_model.selectedRows()})
        items: list[QueueItem] = []
        seen: set[str] = set()
        for row in selected_rows:
            if row >= len(self._queue_row_items):
                continue
            item = self._queue_row_items[row]
            if item is None:
                continue
            key = self._queue_key(item.path)
            if key in seen:
                continue
            seen.add(key)
            items.append(item)
        return items

    def _restore_asset_selection(self, keys: set[str]) -> None:
        if not keys:
            return
        selection_model = self.asset_table.selectionModel()
        if selection_model is None:
            return
        first_row = None
        selection_model.clearSelection()
        for row, item in enumerate(self._asset_rows):
            if self._queue_key(item.path) not in keys:
                continue
            model_index = self.asset_table.model().index(row, 0)
            selection_model.select(
                model_index,
                QItemSelectionModel.SelectionFlag.Select
                | QItemSelectionModel.SelectionFlag.Rows,
            )
            if first_row is None:
                first_row = row
        if first_row is not None:
            self.asset_table.setCurrentCell(first_row, 0)

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
            self._route_dropped_paths(paths)
            event.acceptProposedAction()
            return
        super().dropEvent(event)

    def _route_dropped_paths(self, paths: list[str]) -> None:
        if self._workspace_mode == WORKSPACE_GRAPH:
            self._add_graph_assets_from_paths(paths)
            return
        self.queue_paths_received.emit(paths)

    def build_request(self) -> BatchRequest:
        request = self.settings_panel.build_request()
        queue_sources = tuple(
            item.batch_source for item in self._queue_items if item.status is not QueueStatus.ERROR
        )
        return BatchRequest(
            input_path=None,
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
        self._refresh_graph_apply_preflight()
        self._sync_status_bar_with_selection()
        self._sync_workspace_selection()

    def remove_selected_queue_items(self) -> None:
        selection_model = self.queue_panel.table.selectionModel()
        if selection_model is None:
            return
        selected_rows = {index.row() for index in selection_model.selectedRows()}
        selected_keys = set()
        for row in selected_rows:
            if row >= len(self._queue_row_items):
                continue
            item = self._queue_row_items[row]
            if item is None:
                continue
            selected_keys.add(self._queue_key(item.path))
        removed_count = self._remove_queue_items_by_keys(selected_keys)
        if removed_count:
            self.set_status(f"Удалено элементов: {removed_count}")

    def clear_queue_items(self) -> None:
        self._queue_items.clear()
        self._render_queue()
        self._render_asset_browser()
        self._refresh_packing_preflight()
        self._refresh_graph_apply_preflight()
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
        self.export_graph_button.setEnabled(not running)
        self.graph_workspace.export_button.setEnabled(not running)
        self.asset_table.setEnabled(not running)
        self.remove_asset_button.setEnabled(not running)
        if hasattr(self, "auto_watch_button"):
            self.auto_watch_button.setEnabled(not running)
        if hasattr(self, "auto_export_button"):
            self.auto_export_button.setEnabled(not running)

    def is_running(self) -> bool:
        return self._is_running

    def set_graph_job_running(self, running: bool) -> None:
        self._graph_export_in_progress = running
        self._graph_batch_export_in_progress = running
        self.set_running(running)
        self.graph_workspace.set_graph_editing_enabled(not running)

    def on_graph_job_finished(self, request: object, result: object) -> None:
        if isinstance(request, GraphExportRequest) and hasattr(result, "as_text"):
            summary_text = result.as_text()
            self.append_log(summary_text)
            self.set_status(summary_text)
            if request.show_dialogs:
                if result.failed:
                    self.show_error(request.label, summary_text)
                elif result.succeeded:
                    self.show_info(request.label, summary_text)
            else:
                self._update_graph_watch_status(summary_text)
            return
        if isinstance(request, GraphBatchExportRequest) and isinstance(result, GraphBatchExportResult):
            for paths, summary in result.groups:
                if summary.failed:
                    status = QueueStatus.ERROR
                elif summary.skipped and not summary.succeeded:
                    status = QueueStatus.SKIPPED
                else:
                    status = QueueStatus.DONE
                for path in paths:
                    item = self._find_queue_item(path)
                    if item is not None:
                        item.status = status
                        item.message = summary.as_text()
            self._render_queue()
            self._sync_workspace_selection()
            summary_text = result.as_text()
            self.append_log(summary_text)
            self.set_status(summary_text)
            if result.failed:
                self.show_error(request.label, summary_text)
            else:
                self.show_info(request.label, summary_text)

    def on_graph_job_failed(self, request: object, message: str) -> None:
        label = getattr(request, "label", "Graph export")
        self.append_log(f"ERROR: {message}")
        self.set_status(f"{label}: error")
        if isinstance(request, GraphExportRequest) and not request.show_dialogs:
            self._update_graph_watch_status(f"Auto Export error: {message}")
        else:
            self.show_error(label, message)

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
        self.graph_workspace.apply_recent_projects(settings.recent_graph_projects)
        self._sync_top_output_path(settings.output_path)
        self._set_graph_auto_export_enabled(settings.graph_auto_export)
        self._set_graph_auto_watch_enabled(settings.graph_auto_watch)
        if settings.window_state:
            self.restoreState(QByteArray.fromBase64(settings.window_state.encode("ascii")))
        self._set_workspace_mode(settings.workspace_mode)
        self._update_queue_output_paths()
        self._sync_preset_selection_with_current_options()
        self._refresh_packing_preflight()

    def set_preset_repository(self, preset_repository: PresetRepository) -> None:
        self._preset_repository = preset_repository
        self._reload_presets()

    def build_app_settings(self) -> AppSettings:
        request = self.settings_panel.build_request()
        return AppSettings(
            input_path="",
            output_path=str(request.output_root or ""),
            workspace_mode=self._workspace_mode,
            graph_auto_watch=self._graph_auto_watch_enabled,
            graph_auto_export=self._graph_auto_export_enabled,
            recent_graph_projects=self.graph_workspace.recent_project_paths(),
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
        if self.graph_workspace.has_unsaved_changes():
            button = QMessageBox.question(
                self,
                "Graph не сохранен",
                "В графе есть несохраненные изменения. Сохранить проект перед закрытием?",
                QMessageBox.StandardButton.Save
                | QMessageBox.StandardButton.Discard
                | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Save,
            )
            if button is QMessageBox.StandardButton.Cancel:
                event.ignore()
                return
            if button is QMessageBox.StandardButton.Save:
                self.graph_workspace.save_project_dialog()
                if self.graph_workspace.has_unsaved_changes():
                    event.ignore()
                    return
        background_jobs_stopped = all(
            (
                self._asset_scan_controller.shutdown(wait_ms=100),
                self._alpha_analysis_controller.shutdown(wait_ms=100),
                self._thumbnail_controller.shutdown(wait_ms=100),
                self.preview_panel.shutdown_background_jobs(wait_ms=100),
                self.preview_window.shutdown_background_jobs(wait_ms=100),
                self.graph_workspace.shutdown_background_jobs(wait_ms=100),
            )
        )
        if not background_jobs_stopped:
            self.set_status(
                "Завершение фоновых операций... Закройте окно после их завершения."
            )
            event.ignore()
            return
        self.preview_window.hide()
        super().closeEvent(event)

    def _render_queue(self) -> None:
        table = self.queue_panel.table
        selected_key = self._selected_queue_key()
        self.queue_panel.drop_hint.setVisible(not self._queue_items)
        table.clearContents()
        table.clearSpans()
        options = self.settings_panel.build_conversion_options()
        grouped_items = self._group_queue_items_by_folder()
        self._queue_row_items = []
        total_rows = sum(len(items) + 1 for _label, _tooltip, items in grouped_items)
        table.setRowCount(total_rows)

        row = 0
        for group_label, group_tooltip, items in grouped_items:
            self._queue_row_items.append(None)
            header_item = self._make_group_header_item(group_label, tooltip=group_tooltip)
            table.setItem(row, 0, header_item)
            table.setSpan(row, 0, 1, table.columnCount())
            table.setRowHeight(row, 24)
            row += 1

            for item in items:
                self._queue_row_items.append(item)
                metadata = item.metadata
                if options.packing.enabled and options.packing.mode is ChannelPackingMode.PACK_ONLY:
                    output_text = "В составе packed texture"
                    output_tooltip = (
                        str(item.output_path)
                        if item.output_path is not None
                        else "Файл будет создан только как часть packed texture."
                    )
                elif (
                    options.packing.enabled
                    and options.packing.mode is ChannelPackingMode.PACK_WITH_REMAINDER
                    and item.effective_map_type in packed_source_map_types(options.packing.layout)
                ):
                    output_text = "В составе packed texture"
                    output_tooltip = (
                        str(self._build_output_path(item.batch_source))
                        if item.output_path is not None
                        else "Файл будет включен в packed texture и отдельно не выгружается."
                    )
                else:
                    output_text = str(item.output_path) if item.output_path is not None else "-"
                    output_tooltip = output_text
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
                    self._make_table_item(output_text, tooltip=output_tooltip),
                ]
                row_items[0].setData(Qt.ItemDataRole.UserRole, self._queue_key(item.path))

                for column, table_item in enumerate(row_items):
                    table.setItem(row, column, table_item)

                self._apply_row_style(row, item)
                row += 1

        self._refresh_output_bundle_summary()

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
        summary_text = (
            f"Всего: {len(self._queue_items)} | Папок: {len(grouped_items)} | "
            f"Предупреждений: {warning_count} | Ошибок: {error_count}"
        )
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

    def _make_group_header_item(self, text: str, *, tooltip: str = "") -> QTableWidgetItem:
        table_item = QTableWidgetItem(text)
        table_item.setToolTip(tooltip)
        table_item.setFlags(Qt.ItemFlag.ItemIsEnabled)
        font = QFont(table_item.font())
        font.setBold(True)
        table_item.setFont(font)
        table_item.setBackground(QColor("#202730"))
        table_item.setForeground(QColor("#C7D7EB"))
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
        raw_path = str(path)
        cached = self._path_key_cache.get(raw_path)
        if cached is not None:
            self._path_key_cache.move_to_end(raw_path)
            return cached
        try:
            key = str(path.resolve(strict=False)).casefold()
        except OSError:
            key = raw_path.casefold()
        self._path_key_cache[raw_path] = key
        while len(self._path_key_cache) > 4096:
            self._path_key_cache.popitem(last=False)
        return key

    def _build_output_path(self, source: BatchSource) -> Path:
        options = self.settings_panel.build_conversion_options()
        output_root_text = self.settings_panel.output_edit.text().strip()
        output_root = Path(output_root_text) if output_root_text else None
        if options.packing.enabled and options.packing.mode in {
            ChannelPackingMode.PACK_ONLY,
            ChannelPackingMode.PACK_WITH_REMAINDER,
        }:
            pack_jobs = build_channel_pack_jobs([source], output_root, options)
            if (
                pack_jobs
                and options.packing.mode is ChannelPackingMode.PACK_ONLY
            ):
                return pack_jobs[0].output_path
            if (
                pack_jobs
                and options.packing.mode is ChannelPackingMode.PACK_WITH_REMAINDER
                and source.map_type in packed_source_map_types(options.packing.layout)
            ):
                return pack_jobs[0].output_path
        return BatchConversionService.build_destination_for_source(
            source,
            output_root,
            options,
        )

    def _update_queue_output_paths(self, *_args: object) -> None:
        for item in self._queue_items:
            item.output_path = self._build_output_path(item.batch_source)
        self._render_queue()
        self._render_asset_browser()
        self._refresh_packing_preflight()
        self._refresh_graph_apply_preflight()
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
                "Упаковка каналов выключена. Включите ее, если нужно собрать ORM/RMA/MRA или Unity-packed карты прямо из набора текстур."
            )
            return

        sources = [item.batch_source for item in self._queue_items if item.status is not QueueStatus.ERROR]
        jobs = build_channel_pack_jobs(sources, None, options)
        summary = summarize_channel_pack_jobs(jobs)
        if options.packing.mode is ChannelPackingMode.PACK_ONLY:
            summary = "Режим: только packed texture. Будет создан только итоговый packed texture.\n" + summary
        elif options.packing.mode is ChannelPackingMode.PACK_WITH_REMAINDER:
            summary = (
                "Режим: packed texture + остальные нужные карты. "
                "Карты, вошедшие в packed texture, отдельно не выгружаются.\n"
                + summary
            )
        else:
            summary = "Режим: сначала обычные PNG, затем packed texture.\n" + summary
        self.settings_panel.set_packing_preflight_summary(summary)

    def _refresh_graph_apply_preflight(self) -> None:
        template_map_types = self._graph_template_map_types()
        if not template_map_types:
            self.queue_panel.set_graph_apply_preflight_summary(
                "Graph template пока не задан. Откройте набор в Graph, чтобы увидеть какие наборы очереди подойдут."
            )
            return

        if not self._queue_items:
            self.queue_panel.set_graph_apply_preflight_summary(
                "Graph template готов, но очередь пуста. Добавьте файлы или папки для batch-обработки."
            )
            return

        preflight = self._graph_queue_preflight(template_map_types)
        lines = [
            f"Graph template: {self._map_type_list_text(template_map_types)}",
            f"Подойдут наборы: {preflight['compatible_count']}",
            f"Будут пропущены: {preflight['skipped_count']}",
        ]
        skipped_groups = preflight["skipped_groups"]
        if skipped_groups:
            lines.append("Чего не хватает:")
            preview_limit = 3
            for group_label, missing_map_types in skipped_groups[:preview_limit]:
                lines.append(f"- {group_label}: нет {self._map_type_list_text(missing_map_types)}")
            remainder = len(skipped_groups) - preview_limit
            if remainder > 0:
                lines.append(f"- ... и еще {remainder} набор(ов)")
        else:
            lines.append("Все найденные наборы содержат нужные карты.")
        self.queue_panel.set_graph_apply_preflight_summary("\n".join(lines))

    def _graph_template_map_types(self) -> set[TextureMapType]:
        graph_assets = {
            self._queue_key(item.path): item.effective_map_type
            for item in self.graph_workspace.assets()
        }
        map_types: set[TextureMapType] = set()
        for node in self.graph_workspace.project.graph.nodes:
            if node.node_type is not NodeType.TEXTURE_INPUT:
                continue
            raw_path = str(node.properties.get("path", "")).strip()
            if not raw_path:
                continue
            map_type = graph_assets.get(self._queue_key(Path(raw_path)), TextureMapType.UNKNOWN)
            if map_type is TextureMapType.UNKNOWN:
                continue
            map_types.add(map_type)
        return map_types

    def _graph_queue_preflight(
        self,
        template_map_types: set[TextureMapType],
    ) -> dict[str, object]:
        groups: OrderedDict[str, list[QueueItem]] = OrderedDict()
        for item in self._queue_items:
            groups.setdefault(self._queue_group_key(item), []).append(item)

        compatible_count = 0
        skipped_groups: list[tuple[str, list[TextureMapType]]] = []
        ordered_template = self._ordered_map_types(template_map_types)
        for items in groups.values():
            available_map_types = {
                item.effective_map_type
                for item in items
                if item.effective_map_type is not TextureMapType.UNKNOWN
            }
            missing_map_types = [
                map_type
                for map_type in ordered_template
                if map_type not in available_map_types
            ]
            if missing_map_types:
                skipped_groups.append(
                    (self._queue_group_label(items[0]).replace("Папка: ", ""), missing_map_types)
                )
                continue
            compatible_count += 1

        return {
            "compatible_count": compatible_count,
            "skipped_count": len(skipped_groups),
            "skipped_groups": skipped_groups,
        }

    def _refresh_output_bundle_summary(self) -> None:
        self.settings_panel.set_output_bundle_summary(
            self._build_output_bundle_summary()
        )

    def _build_output_bundle_summary(self) -> str:
        options = self.settings_panel.build_conversion_options()
        source_items = [
            item
            for item in self._queue_items
            if item.status is not QueueStatus.ERROR
        ]
        if not source_items:
            if not options.packing.enabled:
                return "Для выбранного сценария\n- Отдельно: все исходные карты\n- Packed texture: не используется"
            packed_types = packed_source_map_types(options.packing.layout)
            packed_labels = self._map_type_list_text(packed_types)
            if options.packing.mode is ChannelPackingMode.PACK_ONLY:
                return (
                    "Для выбранного сценария\n"
                    f"- Packed: {options.packing.layout.label}\n"
                    f"- Не будут выгружены отдельно: {packed_labels}"
                )
            if options.packing.mode is ChannelPackingMode.PACK_WITH_REMAINDER:
                separate_text = self._map_type_list_text(
                    self._generic_nonpacked_map_types(options.packing.layout)
                )
                return (
                    "Для выбранного сценария\n"
                    f"- Отдельно при наличии: {separate_text}\n"
                    f"- Packed: {options.packing.layout.label}\n"
                    f"- Не дублировать отдельно: {packed_labels}"
                )
            return (
                "Для выбранного сценария\n"
                "- Отдельно: все исходные карты\n"
                f"- Дополнительно packed: {options.packing.layout.label}"
            )

        available_map_types = {
            item.effective_map_type
            for item in source_items
            if item.effective_map_type is not TextureMapType.UNKNOWN
        }

        if not options.packing.enabled:
            return (
                "Для текущей очереди\n"
                "- Отдельно: все исходные карты\n"
                "- Packed texture: не используется"
            )

        packed_types = packed_source_map_types(options.packing.layout)
        packed_labels = self._map_type_list_text(packed_types)

        if options.packing.mode is ChannelPackingMode.PACK_ONLY:
            return (
                "Для текущей очереди\n"
                f"- Packed: {options.packing.layout.label}\n"
                f"- Не будут выгружены отдельно: {packed_labels}"
            )

        if options.packing.mode is ChannelPackingMode.PACK_WITH_REMAINDER:
            separate_source = (
                available_map_types
                if available_map_types
                else self._generic_nonpacked_map_types(options.packing.layout)
            )
            separate_types = [
                map_type
                for map_type in self._ordered_map_types(separate_source)
                if map_type not in packed_types
            ]
            separate_text = (
                self._map_type_list_text(separate_types)
                if separate_types
                else "нет"
            )
            prefix = "Отдельно" if available_map_types else "Отдельно при наличии"
            return (
                "Для текущей очереди\n"
                f"- {prefix}: {separate_text}\n"
                f"- Packed: {options.packing.layout.label}\n"
                f"- Не дублировать отдельно: {packed_labels}"
            )

        return (
            "Для текущей очереди\n"
            "- Отдельно: все исходные карты\n"
            f"- Дополнительно packed: {options.packing.layout.label}"
        )

    @staticmethod
    def _ordered_map_types(map_types: set[TextureMapType] | frozenset[TextureMapType]) -> list[TextureMapType]:
        display_order = (
            TextureMapType.BASECOLOR,
            TextureMapType.NORMAL,
            TextureMapType.EMISSIVE,
            TextureMapType.HEIGHT,
            TextureMapType.OPACITY,
            TextureMapType.AO,
            TextureMapType.ROUGHNESS,
            TextureMapType.SMOOTHNESS,
            TextureMapType.METALLIC,
        )
        present = set(map_types)
        ordered = [map_type for map_type in display_order if map_type in present]
        remainder = sorted(
            (map_type for map_type in present if map_type not in set(display_order)),
            key=lambda value: value.label,
        )
        return ordered + remainder

    def _map_type_list_text(
        self,
        map_types: set[TextureMapType] | frozenset[TextureMapType] | list[TextureMapType] | tuple[TextureMapType, ...],
    ) -> str:
        if not map_types:
            return "нет"
        if not isinstance(map_types, list):
            ordered = self._ordered_map_types(set(map_types))
        else:
            ordered = map_types
        return ", ".join(map_type.label for map_type in ordered)

    @staticmethod
    def _generic_nonpacked_map_types(layout: object) -> set[TextureMapType]:
        packed_types = packed_source_map_types(layout)
        return {
            TextureMapType.BASECOLOR,
            TextureMapType.NORMAL,
            TextureMapType.EMISSIVE,
            TextureMapType.HEIGHT,
            TextureMapType.OPACITY,
            TextureMapType.SMOOTHNESS,
        } - set(packed_types)

    def _apply_preset(self, preset_id: str) -> None:
        preset = self._presets_by_id.get(preset_id)
        if preset is None:
            return

        self.settings_panel.apply_conversion_options(preset.options)
        self.settings_panel.set_selected_preset_id(preset.preset_id)
        self.set_status(f"Применен сценарий: {preset.name}")

    def _save_current_preset(self) -> None:
        if self._preset_repository is None:
            return

        selected_preset = self._presets_by_id.get(self.settings_panel.selected_preset_id() or "")
        suggested_name = "My Workflow"
        if selected_preset is not None:
            suggested_name = (
                f"{selected_preset.name} Copy" if selected_preset.is_system else selected_preset.name
            )

        name, accepted = QInputDialog.getText(
            self,
            "Сохранить workflow preset",
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
                f"Workflow preset '{existing_user_preset.name}' уже существует. Перезаписать его?",
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
        self.set_status(f"Сохранен сценарий: {preset.name}")

    def _delete_preset(self, preset_id: str) -> None:
        if self._preset_repository is None:
            return

        preset = self._presets_by_id.get(preset_id)
        if preset is None or preset.is_system:
            return

        button = QMessageBox.question(
            self,
            "Удалить preset",
            f"Удалить пользовательский workflow preset '{preset.name}'?",
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
        self.set_status(f"Удален сценарий: {preset.name}")

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
        self._request_lazy_alpha_analysis(item)
        self.metadata_panel.set_queue_item(item)
        self.preview_panel.set_queue_item(item)
        if self.preview_window.isVisible():
            self.preview_window.set_queue_item(item)

    def _request_lazy_alpha_analysis(self, item: QueueItem | None) -> None:
        if item is None or item.metadata is None:
            return
        if item.metadata.alpha_fully_opaque is not None:
            return
        self._alpha_analysis_controller.submit(
            AlphaAnalysisRequest(item.path, item.metadata)
        )

    def on_alpha_analysis_finished(
        self,
        request: AlphaAnalysisRequest,
        metadata: object,
    ) -> None:
        if not hasattr(metadata, "alpha_fully_opaque"):
            return
        if request.revision != file_revision(request.path):
            return
        affected: list[QueueItem] = []
        for item in (*self._queue_items, *self.graph_workspace.assets()):
            if self._queue_key(item.path) != self._queue_key(request.path):
                continue
            item.metadata = metadata
            item.message = "; ".join(metadata.warnings)
            affected.append(item)
        if affected:
            self._render_queue()
            self._render_asset_browser()
            self._refresh_packing_preflight()
            self._refresh_graph_apply_preflight()
            selected = self._selected_queue_item()
            self.metadata_panel.set_queue_item(selected)

    def on_alpha_analysis_failed(self, request: AlphaAnalysisRequest, message: str) -> None:
        self.append_log(f"Alpha analysis failed for {request.path.name}: {message}")

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
        self._refresh_graph_apply_preflight()
        self._sync_workspace_selection()
        self._sync_status_bar_with_selection()

    def _export_graph(self) -> None:
        self._export_graph_with_feedback(show_dialogs=True)

    def _export_graph_output(self, output_node: GraphNode) -> None:
        self._export_graph_with_feedback(show_dialogs=True, output_node=output_node)

    def _export_graph_with_feedback(
        self,
        *,
        show_dialogs: bool,
        output_node: GraphNode | None = None,
    ) -> None:
        if self._is_running and not self._graph_export_controller.is_running:
            return
        if self._graph_export_controller.is_running and show_dialogs:
            return
        request = self.settings_panel.build_request()
        output_root = request.output_root
        if output_root is None:
            if self.graph_workspace.project_dir is not None:
                output_root = self.graph_workspace.project_dir / "exports"
            else:
                output_root = Path.cwd() / "graph_exports"
        export_options = request.options
        executor = NodeGraphExecutor()
        if output_node is None:
            plan = executor.plan_enabled_outputs(self.graph_workspace.project.graph, output_root)
        else:
            plan = executor.plan_outputs(
                self.graph_workspace.project.graph,
                (output_node,),
                output_root,
            )
        if not plan.is_valid:
            message = plan.collision_message()
            self.append_log(f"ERROR: {message}")
            self.set_status("Graph export blocked: output path collision.")
            if show_dialogs:
                self.show_error("Graph export", message)
            else:
                self._update_graph_watch_status("Auto Export blocked: path collision")
            return
        destinations = [item.destination for item in plan.items]
        existing_paths = [path for path in destinations if path.exists()]
        if show_dialogs and existing_paths and not request.options.overwrite:
            preview_lines = "\n".join(f"- {path.name}" for path in existing_paths[:5])
            if len(existing_paths) > 5:
                preview_lines += f"\n... и еще {len(existing_paths) - 5}"
            button = QMessageBox.question(
                self,
                "Перезаписать export-файлы",
                (
                    f"Найдено существующих файлов: {len(existing_paths)}.\n"
                    f"{preview_lines}\n\n"
                    "Перезаписать их и продолжить export?"
                ),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if button is not QMessageBox.StandardButton.Yes:
                self.set_status("Graph export canceled.")
                return
            export_options = replace(request.options, overwrite=True)
        elif not show_dialogs and not request.options.overwrite:
            export_options = replace(request.options, overwrite=True)

        export_label = "Graph export" if output_node is None else f"Output export: {output_node.title}"
        self.append_log(f"---- {export_label} ----")
        snapshot = deepcopy(self.graph_workspace.project)
        self._graph_export_controller.submit(
            GraphExportRequest(
                project=snapshot,
                output_root=Path(output_root),
                options=export_options,
                output_node_id=output_node.node_id if output_node is not None else None,
                label=export_label,
                show_dialogs=show_dialogs,
                auto_export=not show_dialogs,
            )
        )

    def _toggle_graph_auto_watch(self, checked: bool) -> None:
        self._set_graph_auto_watch_enabled(bool(checked))

    def _toggle_graph_auto_export(self, checked: bool) -> None:
        self._set_graph_auto_export_enabled(bool(checked))

    def _set_graph_auto_watch_enabled(self, enabled: bool) -> None:
        self._graph_auto_watch_enabled = bool(enabled)
        self.graph_workspace.set_auto_watch_enabled(self._graph_auto_watch_enabled)
        if hasattr(self, "auto_watch_button"):
            self.auto_watch_button.blockSignals(True)
            self.auto_watch_button.setChecked(self._graph_auto_watch_enabled)
            self.auto_watch_button.blockSignals(False)
        self._update_graph_watch_status()

    def _set_graph_auto_export_enabled(self, enabled: bool) -> None:
        self._graph_auto_export_enabled = bool(enabled)
        if hasattr(self, "auto_export_button"):
            self.auto_export_button.blockSignals(True)
            self.auto_export_button.setChecked(self._graph_auto_export_enabled)
            self.auto_export_button.blockSignals(False)
        self._update_graph_watch_status()

    def _on_graph_assets_changed(self, paths: tuple[Path, ...]) -> None:
        if not paths:
            return
        self.append_log(f"Auto Watch -> changed: {', '.join(path.name for path in paths[:4])}")
        self._reload_assets_for_paths(paths, source_label="Auto Watch")
        self._refresh_graph_apply_preflight()
        if self._graph_auto_export_enabled:
            self._graph_auto_export_paths = paths
            self._graph_auto_export_scheduled = True
            self._graph_auto_export_timer.start()
        else:
            self._update_graph_watch_status(f"Updated {len(paths)} texture(s)")

    def _reload_assets_for_paths(self, paths: tuple[Path, ...] | list[Path], *, source_label: str) -> None:
        normalized_paths = [Path(path) for path in paths if str(path).strip()]
        if not normalized_paths:
            return
        for path in normalized_paths:
            self._thumbnail_controller.invalidate(path)
        self.set_status(f"{source_label}: scanning {len(normalized_paths)} asset(s)...")
        self._asset_scan_controller.submit(
            AssetScanRequest(
                target="reload",
                paths=tuple(normalized_paths),
                recursive=False,
            )
        )

    def _scan_single_queue_item(self, path: Path, batch_source: BatchSource) -> QueueItem:
        return self._asset_scanner._build_queue_item(batch_source)

    def _open_selected_set_in_graph(self) -> None:
        seed = self._selected_queue_item()
        if seed is None:
            self.set_status("Выберите строку очереди, чтобы открыть набор в Graph.")
            return
        items = [
            item
            for item in self._queue_items
            if self._queue_group_key(item) == self._queue_group_key(seed)
        ]
        if not items:
            self.set_status("Для выбранной папки не найдено файлов.")
            return
        self.graph_workspace.set_assets(items)
        self._render_asset_browser()
        self._refresh_graph_apply_preflight()
        self._set_workspace_mode(WORKSPACE_GRAPH)
        self.set_status(f"Открыт набор в Graph: {len(items)} файл(ов)")

    def _open_selected_files_in_graph(self) -> None:
        items = self._selected_queue_items()
        if not items:
            self.set_status("Выберите файлы в очереди, чтобы открыть их в Graph.")
            return
        self.graph_workspace.set_assets(items)
        self._render_asset_browser()
        self._refresh_graph_apply_preflight()
        self._set_workspace_mode(WORKSPACE_GRAPH)
        self.set_status(f"Открыто файлов в Graph: {len(items)}")

    def _apply_current_graph_to_queue(self) -> None:
        if self._graph_batch_export_in_progress or self._is_running:
            return
        if not self._queue_items:
            self.show_error("Применить граф к очереди", "Очередь пуста.")
            return
        graph_assets = list(self.graph_workspace.assets())
        if not graph_assets:
            self.show_error(
                "Применить граф к очереди",
                "Сначала откройте набор или файлы в Graph, чтобы задать graph template.",
            )
            return
        if not self.graph_workspace.project.graph.nodes:
            self.show_error("Применить граф к очереди", "Текущий граф пуст.")
            return

        request = self.build_request()
        try:
            validate_request(request)
        except ValidationError as exc:
            self.show_error("Применить граф к очереди", str(exc))
            return

        template_map_types = self._graph_template_map_types()
        if not template_map_types:
            self.show_error(
                "Применить граф к очереди",
                "В texture input нодах графа не удалось определить типы карт. Откройте корректный набор в Graph.",
            )
            return

        grouped_items = self._group_queue_items_for_batch_graph(template_map_types)
        if not grouped_items:
            self.show_error(
                "Применить граф к очереди",
                "В очереди нет подходящих наборов для текущего graph template.",
            )
            return

        tasks: list[GraphBatchExportTask] = []
        for group_items in grouped_items.values():
            mapping = {
                item.effective_map_type: item.path
                for item in group_items
                if item.effective_map_type is not TextureMapType.UNKNOWN
            }
            output_prefix = self._graph_export_prefix_for_group(group_items)
            export_root = self._graph_export_root_for_group(group_items, request.output_root)
            tasks.append(
                GraphBatchExportTask(
                    output_root=Path(export_root),
                    source_paths=tuple(item.path for item in group_items),
                    label=output_prefix,
                    texture_mapping=tuple(mapping.items()),
                    output_prefix=output_prefix,
                )
            )

        self.clear_log()
        self.append_log("---- Применение графа к очереди ----")
        self.set_status("Применение graph template к очереди...")
        self.reset_queue_statuses_for_run()
        self.log_dock.show()
        self._graph_export_controller.submit(
            GraphBatchExportRequest(
                project=deepcopy(self.graph_workspace.project),
                tasks=tuple(tasks),
                options=request.options,
                label="Применить граф к очереди",
            )
        )

    def _group_queue_items_for_batch_graph(
        self,
        template_map_types: set[TextureMapType],
    ) -> OrderedDict[str, list[QueueItem]]:
        groups: OrderedDict[str, list[QueueItem]] = OrderedDict()
        for item in self._queue_items:
            groups.setdefault(self._queue_group_key(item), []).append(item)

        usable: OrderedDict[str, list[QueueItem]] = OrderedDict()
        for group_key, items in groups.items():
            map_index: dict[TextureMapType, QueueItem] = {}
            for item in items:
                map_type = item.effective_map_type
                if map_type is TextureMapType.UNKNOWN:
                    continue
                map_index.setdefault(map_type, item)
            if not template_map_types.issubset(map_index.keys()):
                continue
            usable[group_key] = [
                map_index[map_type]
                for map_type in sorted(template_map_types, key=lambda value: value.value)
            ]
        return usable

    def _graph_export_root_for_group(self, items: list[QueueItem], output_root: Path | None) -> Path:
        base_root = output_root or (Path.cwd() / "graph_batch_exports")
        seed = items[0]
        root = seed.root
        parent = seed.path.parent
        if root is not None:
            try:
                relative = parent.relative_to(root)
            except ValueError:
                relative = Path()
            return base_root / relative
        return base_root

    def _graph_export_prefix_for_group(self, items: list[QueueItem]) -> str:
        suffixes_by_map_type: dict[TextureMapType, tuple[str, ...]] = {
            TextureMapType.BASECOLOR: ("_basecolor", "_albedo", "_diffuse", "_diff", "_dif", "_color", "_col"),
            TextureMapType.NORMAL: ("_normal", "_normalmap", "_nrm", "_nml", "_nor"),
            TextureMapType.ROUGHNESS: ("_roughness", "_rough", "_rgh"),
            TextureMapType.SMOOTHNESS: ("_smoothness", "_smooth", "_gloss", "_gls"),
            TextureMapType.METALLIC: ("_metallic", "_metalness", "_metal", "_met", "_mtl"),
            TextureMapType.AO: ("_ambientocclusion", "_ambient_occlusion", "_occlusion", "_occ", "_ao"),
            TextureMapType.OPACITY: ("_opacity", "_alpha", "_mask", "_opc"),
            TextureMapType.EMISSIVE: ("_emissive", "_emission", "_emit", "_emi", "_ems"),
            TextureMapType.HEIGHT: ("_height", "_displacement", "_disp", "_bump", "_hgt"),
        }
        base_candidates: list[str] = []
        for item in items:
            stem = item.path.stem
            lowered = stem.lower()
            for suffix in suffixes_by_map_type.get(item.effective_map_type, ()):
                if lowered.endswith(suffix):
                    stem = stem[: -len(suffix)]
                    break
            stem = stem.strip("_- ")
            if stem:
                base_candidates.append(stem)
        if base_candidates:
            return min(base_candidates, key=len)
        return items[0].path.parent.name or items[0].path.stem

    def _run_graph_auto_export(self) -> None:
        if not self._graph_auto_export_enabled or not self._graph_auto_export_scheduled:
            return
        self._graph_auto_export_scheduled = False
        self._export_graph_with_feedback(show_dialogs=False)

    def _on_graph_watched_paths_changed(self, paths: tuple[Path, ...]) -> None:
        if not self._graph_auto_watch_enabled:
            self._update_graph_watch_status()
            return
        if not paths:
            self._update_graph_watch_status("Watching: no texture nodes")
            return
        folder_count = len({str(path.parent).lower() for path in paths})
        self._update_graph_watch_status(
            f"Watching {len(paths)} texture(s) in {folder_count} folder(s)"
        )

    def _update_graph_watch_status(self, text: str | None = None) -> None:
        if text is None:
            if not self._graph_auto_watch_enabled:
                text = "Auto Watch Off"
            elif self._graph_auto_export_enabled:
                text = "Auto Watch + Auto Rebuild"
            else:
                text = "Auto Watch On"
        self._graph_auto_watch_status = text
        if hasattr(self, "graph_watch_status_label"):
            self.graph_watch_status_label.setText(text[:64])

    def _selected_queue_item(self) -> QueueItem | None:
        items = self._selected_queue_items()
        return items[0] if items else None

    def _remove_queue_items_by_keys(self, keys: set[str]) -> int:
        if not keys:
            return 0
        before_count = len(self._queue_items)
        self._queue_items = [
            item
            for item in self._queue_items
            if self._queue_key(item.path) not in keys
        ]
        removed_count = before_count - len(self._queue_items)
        if not removed_count:
            return 0
        self._render_queue()
        self._render_asset_browser()
        self._refresh_packing_preflight()
        self._refresh_graph_apply_preflight()
        self._sync_workspace_selection()
        return removed_count

    def _selected_queue_key(self) -> str | None:
        item = self._selected_queue_item()
        if item is None:
            return None
        return self._queue_key(item.path)

    def _restore_queue_selection(self, key: str | None) -> None:
        if not self._queue_items:
            return

        target_row = next(
            (row for row, item in enumerate(self._queue_row_items) if item is not None),
            None,
        )
        if target_row is None:
            return
        if key is not None:
            for row, item in enumerate(self._queue_row_items):
                if item is not None and self._queue_key(item.path) == key:
                    target_row = row
                    break

        self.queue_panel.table.selectRow(target_row)

    def _group_queue_items_by_folder(self) -> list[tuple[str, str, list[QueueItem]]]:
        groups: list[tuple[str, str, list[QueueItem]]] = []
        current_key: str | None = None

        for item in self._queue_items:
            group_key = self._queue_group_key(item)
            group_label = self._queue_group_label(item)
            group_tooltip = str(item.path.parent)
            if current_key != group_key:
                groups.append((group_label, group_tooltip, [item]))
                current_key = group_key
                continue
            groups[-1][2].append(item)

        return groups

    def _queue_group_key(self, item: QueueItem) -> str:
        return self._queue_key(item.path.parent)

    def _queue_group_label(self, item: QueueItem) -> str:
        root = item.root
        parent = item.path.parent
        if root is not None:
            try:
                relative = parent.relative_to(root)
            except ValueError:
                relative = Path()
            if relative == Path():
                return f"Папка: {root.name}"
            return f"Папка: {root.name}/{relative.as_posix()}"
        return f"Папка: {parent.as_posix()}"

    def _apply_default_splitter_sizes(self) -> None:
        return
