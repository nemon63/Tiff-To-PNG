from __future__ import annotations

from collections.abc import Callable, Iterable
from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path
from threading import Event

from PyQt6.QtCore import (
    QFileSystemWatcher,
    QObject,
    QPointF,
    QSize,
    QStandardPaths,
    Qt,
    QThread,
    QTimer,
    pyqtSignal,
)
from PyQt6.QtGui import (
    QKeySequence,
    QShortcut,
    QUndoStack,
)
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from image_converter.domain.models import (
    ChannelPackLayout,
    ConversionOptions,
    ConversionStatus,
    QueueItem,
    TextureMapType,
)
from image_converter.domain.node_graph import (
    GraphConnection,
    GraphNode,
    NodeGraphProject,
    NodeType,
    OutputMode,
    OutputProfile,
    SocketDirection,
    SocketType,
    create_graph_node,
    incoming_connection,
    make_connection_id,
    make_node_id,
    node_bypass_socket_pair,
    node_has_enable_flag,
    node_has_resettable_parameters,
    node_type_label,
    reset_node_parameters,
    socket_definitions,
)
from image_converter.ui.graph_commands import (
    AddNodesCommand,
    DeleteItemsCommand,
    InsertNodeInConnectionCommand,
    MoveAndRewireNodeCommand,
    MoveNodesCommand,
    RemoveConnectionsCommand,
    ReplaceInputConnectionCommand,
    SetDisplayFlagCommand,
    SetNodeStateCommand,
    clone_graph_connection,
    clone_graph_node,
)
from image_converter.ui.graph_canvas import (
    ConnectionItem,
    FLAG_GAP,
    FLAG_SIZE,
    FLAG_TOP,
    GraphNodeItem,
    GraphScene,
    GraphView,
    NODE_WIDTH,
    PORT_RADIUS,
    PortItem,
    ROW_HEIGHT,
    TEXTURE_BODY_HEIGHT,
    TEXTURE_META_X,
    TEXTURE_NODE_WIDTH,
    TEXTURE_PORT_CENTER_Y,
    TEXTURE_PORT_LABEL_X_PAD,
    TEXTURE_PORT_SPACING,
    TEXTURE_THUMBNAIL_SIZE,
    TEXTURE_THUMBNAIL_X,
    TEXTURE_THUMBNAIL_Y,
    TITLE_HEIGHT,
)
from image_converter.ui.node_editor_constants import (
    DRAFT_PREVIEW_MAX_SIDE,
    GRAPH_EXPORT_FILE_FILTER,
    NUMERIC_PREVIEW_DEBOUNCE_MS,
    PREVIEW_MODE_DRAFT,
    PREVIEW_MODE_FULL,
)
from image_converter.ui.node_properties import NodePropertiesPanel
from image_converter.services.node_graph_executor import (
    GraphExecutionCancelled,
    GraphExecutionError,
    GraphExportSummary,
    NodeGraphExecutor,
    NodeGraphPreviewCache,
)
from image_converter.services.node_graph_project import (
    GRAPH_PROJECT_EXTENSION,
    GRAPH_PROJECT_FILE_FILTER,
    GRAPH_PROJECT_FILENAME,
    NodeGraphProjectRepository,
)
from image_converter.services.map_types import detect_texture_map_type
from image_converter.services.packing import PACK_LAYOUTS, PackSourceCandidate
from image_converter.services.pbr_preview import PbrMaterialData
from image_converter.application.path_search import (
    MissingTextureSearchController,
    MissingTextureSearchRequest,
)

GRAPH_LAYOUT_X_SPACING = 300
GRAPH_LAYOUT_Y_SPACING = 36
GRAPH_LAYOUT_GRID = 20

OUTPUT_PROFILE_FILENAMES = {
    OutputProfile.GENERIC_RGBA: "packed_rgba.png",
    OutputProfile.UNITY_URP: "unity_urp_metallicsmoothness.png",
    OutputProfile.UNITY_HDRP: "unity_hdrp_maskmap.png",
    OutputProfile.UNREAL_ORM: "unreal_orm.png",
    OutputProfile.METAHUMAN_REPACK: "metahuman_repack.png",
}
OUTPUT_PROFILE_MODES = {
    OutputProfile.GENERIC_RGBA: OutputMode.RGBA,
    OutputProfile.UNITY_URP: OutputMode.RGBA,
    OutputProfile.UNITY_HDRP: OutputMode.RGBA,
    OutputProfile.UNREAL_ORM: OutputMode.RGB,
    OutputProfile.METAHUMAN_REPACK: OutputMode.RGBA,
}
OUTPUT_PROFILE_PACK_LAYOUTS = {
    OutputProfile.UNITY_URP: ChannelPackLayout.UNITY_URP,
    OutputProfile.UNITY_HDRP: ChannelPackLayout.UNITY_HDRP,
    OutputProfile.UNREAL_ORM: ChannelPackLayout.ORM,
}
OUTPUT_PROFILE_LABELS = {
    OutputProfile.GENERIC_RGBA: "Generic RGBA",
    OutputProfile.UNITY_URP: "Unity URP Metallic/Smoothness",
    OutputProfile.UNITY_HDRP: "Unity HDRP Mask Map",
    OutputProfile.UNREAL_ORM: "Unreal ORM",
    OutputProfile.METAHUMAN_REPACK: "MetaHuman Repack",
}
OUTPUT_PROFILE_SUMMARY_NOTES = {
    OutputProfile.UNITY_URP: "Uses Metallic/Smoothness layout. AO stays separate in URP.",
    OutputProfile.UNITY_HDRP: "Uses HDRP Mask Map layout.",
    OutputProfile.UNREAL_ORM: "Uses ORM packing: AO / Roughness / Metallic.",
}
OUTPUT_PROFILE_DESCRIPTIONS = {
    OutputProfile.GENERIC_RGBA: (
        "Target pack: Standard RGBA texture.",
        "Uses BaseColor first, then Emissive, then the first compatible texture.",
        "Opacity uses a dedicated Opacity map when available, otherwise the source alpha channel.",
    ),
    OutputProfile.UNITY_URP: (
        "Target pack: Unity URP Metallic/Smoothness.",
        "R = Metallic, G = 0, B = 0, A = Smoothness or Invert(Roughness).",
        "AO stays separate in the URP workflow.",
    ),
    OutputProfile.UNITY_HDRP: (
        "Target pack: Unity HDRP Mask Map.",
        "R = Metallic, G = AO, B = Detail Mask, A = Smoothness or Invert(Roughness).",
        "Detail Mask falls back to white when there is no dedicated source.",
    ),
    OutputProfile.UNREAL_ORM: (
        "Target pack: Unreal ORM.",
        "R = AO, G = Roughness, B = Metallic.",
        "If an HDRP Mask Map is detected, AO/Metallic are reused and Smoothness is inverted into Roughness.",
    ),
    OutputProfile.METAHUMAN_REPACK: (
        "Target pack: MetaHuman Repack.",
        "R = AO, G = Roughness, B = Metallic, A = Opacity.",
    ),
}
OUTPUT_PROFILE_AUTO_TITLE = {
    OutputProfile.GENERIC_RGBA: "Packed RGBA",
    OutputProfile.UNITY_URP: "URP MetallicSmoothness",
    OutputProfile.UNITY_HDRP: "HDRP Mask Map",
    OutputProfile.UNREAL_ORM: "Unreal ORM",
    OutputProfile.METAHUMAN_REPACK: "MetaHuman Repack",
}
DEFAULT_PROFILE_FILL = {
    TextureMapType.AO: 255,
    TextureMapType.ROUGHNESS: 255,
    TextureMapType.SMOOTHNESS: 0,
    TextureMapType.METALLIC: 0,
    TextureMapType.OPACITY: 255,
}
METAHUMAN_REPACK_RULES = (
    ("r", (PackSourceCandidate(TextureMapType.AO),), 255),
    ("g", (PackSourceCandidate(TextureMapType.ROUGHNESS),), 255),
    ("b", (PackSourceCandidate(TextureMapType.METALLIC),), 0),
    ("a", (PackSourceCandidate(TextureMapType.OPACITY),), 255),
)
PACKED_DETAIL_MASK = "detail_mask"
PACKED_SOURCE_CHANNELS = {
    "unity_hdrp_mask": {
        TextureMapType.METALLIC: ("r", False),
        TextureMapType.AO: ("g", False),
        PACKED_DETAIL_MASK: ("b", False),
        TextureMapType.SMOOTHNESS: ("a", False),
        TextureMapType.ROUGHNESS: ("a", True),
    },
    "unity_urp_mask": {
        TextureMapType.METALLIC: ("r", False),
        TextureMapType.SMOOTHNESS: ("a", False),
        TextureMapType.ROUGHNESS: ("a", True),
    },
    "unreal_orm": {
        TextureMapType.AO: ("r", False),
        TextureMapType.ROUGHNESS: ("g", False),
        TextureMapType.SMOOTHNESS: ("g", True),
        TextureMapType.METALLIC: ("b", False),
    },
    "unreal_mra": {
        TextureMapType.METALLIC: ("r", False),
        TextureMapType.ROUGHNESS: ("g", False),
        TextureMapType.SMOOTHNESS: ("g", True),
        TextureMapType.AO: ("b", False),
    },
    "unreal_rma": {
        TextureMapType.ROUGHNESS: ("r", False),
        TextureMapType.SMOOTHNESS: ("r", True),
        TextureMapType.METALLIC: ("g", False),
        TextureMapType.AO: ("b", False),
    },
}
@dataclass(slots=True)
class OutputProfilePlan:
    profile: OutputProfile
    mode: OutputMode
    output_title: str
    properties: dict
    utility_nodes: list[GraphNode]
    utility_connections: list[GraphConnection]
    output_connections: list[GraphConnection]
    target_label: str
    target_filename: str
    description_lines: tuple[str, ...]
    summary_lines: tuple[str, ...]


class GraphPreviewWorker(QObject):
    finished = pyqtSignal(int, object, str, str, str)
    failed = pyqtSignal(int, str, str, str)
    canceled = pyqtSignal(int)
    completed = pyqtSignal()

    def __init__(
        self,
        generation: int,
        graph_project: NodeGraphProject,
        node: GraphNode,
        *,
        mode_label: str = "",
        max_side: int = 1024,
        preview_cache: NodeGraphPreviewCache | None = None,
    ):
        super().__init__()
        self._generation = generation
        self._project = graph_project
        self._node = node
        self._mode_label = mode_label
        self._max_side = max_side
        self._preview_cache = preview_cache
        self._cancel_event = Event()

    def cancel(self) -> None:
        self._cancel_event.set()

    def run(self) -> None:
        try:
            executor = NodeGraphExecutor(
                cancel_requested=self._cancel_event.is_set,
            )
            preview_cache = self._preview_cache or NodeGraphPreviewCache(max_side=self._max_side)
            if self._node.node_type is NodeType.PBR_SHADER:
                result = executor.render_pbr_material_node(
                    self._project.graph,
                    self._node,
                    preview_cache=preview_cache,
                    max_side=self._max_side,
                )
                meta = f"{result.workflow_label} · {result.normal_status}"
            else:
                result, meta = executor.render_display_node(
                    self._project.graph,
                    self._node,
                    preview_cache=preview_cache,
                    max_side=self._max_side,
                )
            if self._mode_label and not isinstance(result, PbrMaterialData):
                filename = str(self._node.properties.get("filename", "")).strip()
                if self._node.properties.get("output_path"):
                    filename = Path(str(self._node.properties.get("output_path"))).name
                suffix = f" · {filename}" if filename else ""
                meta = (
                    f"Output preview · {result.width}x{result.height} · "
                    f"{self._mode_label}{suffix}"
                )
            if self._cancel_event.is_set():
                raise GraphExecutionCancelled("Preview render canceled.")
            self.finished.emit(
                self._generation,
                result,
                self._node.title,
                meta,
                self._node.node_id,
            )
        except GraphExecutionCancelled:
            self.canceled.emit(self._generation)
        except (GraphExecutionError, OSError, ValueError) as exc:
            self.failed.emit(
                self._generation,
                self._node.title,
                str(exc),
                self._node.node_id,
            )
        finally:
            self.completed.emit()


class GraphWorkspace(QWidget):
    export_requested = pyqtSignal()
    output_export_requested = pyqtSignal(object)
    preview_image_requested = pyqtSignal(object, str, str, str)
    pbr_material_requested = pyqtSignal(object, str, str, str)
    preview_failed = pyqtSignal(str, str, str)
    status_message = pyqtSignal(str)
    watched_paths_changed = pyqtSignal(tuple)
    assets_changed = pyqtSignal(tuple)
    template_changed = pyqtSignal()
    recent_projects_changed = pyqtSignal(tuple)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.project = NodeGraphProject()
        self.project_path: Path | None = None
        self.project_dir: Path | None = None
        self._assets: list[QueueItem] = []
        self._clipboard_nodes: list[GraphNode] = []
        self._clipboard_connections: list[GraphConnection] = []
        self._last_preview_node_id: str | None = None
        self._last_preview_mode_label = ""
        self._preview_generation = 0
        self._preview_threads: list[QThread] = []
        self._preview_workers: list[GraphPreviewWorker] = []
        self._preview_active_worker: GraphPreviewWorker | None = None
        self._preview_inflight_generation = 0
        self._pending_preview_request: tuple[
            str,
            str,
            str,
            dict[str, dict] | None,
        ] | None = None
        self._preview_dirty_node_ids: set[str] = set()
        self._preview_full_reset_pending = False
        self._last_preview_quality_request = PREVIEW_MODE_FULL
        self._property_edit_in_progress = False
        self._interactive_property_edit = False
        self._recent_project_dirs: list[Path] = []
        self._shortcuts = []
        self._repository = NodeGraphProjectRepository()
        self._executor = NodeGraphExecutor()
        self._missing_texture_search = MissingTextureSearchController(self)
        self._missing_texture_search.finished.connect(
            self._on_missing_texture_search_finished
        )
        self._missing_texture_search.failed.connect(
            lambda _request, message: self.status_message.emit(
                f"Missing texture search failed: {message}"
            )
        )
        self._preview_cache = NodeGraphPreviewCache(
            max_side=1024,
            interactive_preview=True,
        )
        self._autosave_timer = QTimer(self)
        self._autosave_timer.setInterval(30000)
        self._autosave_timer.setSingleShot(True)
        self._autosave_timer.timeout.connect(self.autosave_project)
        self._auto_watch_enabled = False
        self._asset_watch_timer = QTimer(self)
        self._asset_watch_timer.setInterval(350)
        self._asset_watch_timer.setSingleShot(True)
        self._asset_watch_timer.timeout.connect(self._emit_pending_asset_changes)
        self._asset_watcher = QFileSystemWatcher(self)
        self._asset_watcher.directoryChanged.connect(self._on_watched_directory_changed)
        self._asset_watcher.fileChanged.connect(self._on_watched_file_changed)
        self._watched_texture_keys: dict[str, str] = {}
        self._watched_file_keys: dict[str, str] = {}
        self._watched_directory_paths: dict[str, str] = {}
        self._watched_directory_keys: dict[str, set[str]] = {}
        self._pending_asset_change_keys: set[str] = set()
        self.undo_stack = QUndoStack(self)
        self.undo_stack.cleanChanged.connect(self._on_undo_stack_changed)
        self.undo_stack.indexChanged.connect(self._on_undo_stack_changed)
        self._scene = GraphScene(self.project, self)
        self._scene.connection_delete_requested.connect(self._on_connection_delete_requested)
        self._scene.connection_insert_node_requested.connect(self._on_connection_insert_node_requested)
        self._scene.connection_requested.connect(self._on_connection_requested)
        self._scene.connections_delete_requested.connect(self._on_delete_items_requested)
        self._scene.graph_changed.connect(self._on_graph_changed)
        self._scene.node_display_flag_clicked.connect(self._on_display_flag_clicked)
        self._scene.node_double_clicked.connect(self._on_node_double_clicked)
        self._scene.node_enable_flag_clicked.connect(self._on_enable_flag_clicked)
        self._scene.node_moved.connect(self._on_node_moved)
        self._scene.nodes_moved.connect(self._on_nodes_moved)
        self._scene.node_shake_disconnect_requested.connect(
            self._on_node_shake_disconnect_requested
        )
        self._scene.node_wire_insert_requested.connect(
            self._on_node_wire_insert_requested
        )
        self._scene.node_render_flag_clicked.connect(self._on_render_flag_clicked)
        self._scene.node_reset_clicked.connect(self._on_node_reset_clicked)
        self._scene.node_properties_selection_changed.connect(self._on_properties_node_selected)
        self._scene.node_selection_changed.connect(self._on_node_selected)
        self._scene.status_message.connect(self.status_message.emit)
        self._scene.wire_node_requested.connect(self._on_wire_node_requested)
        self._build_ui()
        self._scene.rebuild()
        self._refresh_validation()
        self.undo_stack.setClean()
        self._update_project_label()

    def _build_ui(self) -> None:
        self.setObjectName("SectionPanel")
        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(10, 10, 10, 10)
        root_layout.setSpacing(8)

        toolbar = QVBoxLayout()
        toolbar.setSpacing(6)
        project_row = QHBoxLayout()
        project_row.setSpacing(6)
        self.project_label = QLabel("Untitled Graph")
        self.project_label.setObjectName("PanelTitle")
        project_row.addWidget(self.project_label)

        menu_hint = QLabel("Right-click graph to add nodes")
        menu_hint.setObjectName("SummaryText")
        menu_hint.setToolTip(
            "Shake a connected node to detach it. "
            "Drop a compatible node on a highlighted wire to insert it."
        )
        project_row.addWidget(menu_hint)
        project_row.addStretch(1)

        self.export_button = QPushButton("Export Graph")
        self.export_button.setObjectName("PrimaryButton")
        self.export_button.clicked.connect(self.export_requested.emit)
        project_row.addWidget(self.export_button)
        toolbar.addLayout(project_row)

        tools_row = QHBoxLayout()
        tools_row.setSpacing(6)
        self.delete_button = QPushButton("Delete")
        self.delete_button.setObjectName("DangerButton")
        self.delete_button.clicked.connect(self._delete_selection)
        tools_row.addWidget(self.delete_button)
        self.fit_button = QPushButton("Fit")
        self.fit_button.clicked.connect(lambda: self.view.fit_graph())
        tools_row.addWidget(self.fit_button)
        self.layout_button = QPushButton("Layout")
        self.layout_button.clicked.connect(self.auto_layout_nodes)
        tools_row.addWidget(self.layout_button)
        tools_row.addWidget(QLabel("Preview"))
        self.preview_quality_combo = QComboBox()
        self.preview_quality_combo.setToolTip("Preview max side")
        for label, value in (("512", 512), ("1024", 1024), ("2048", 2048)):
            self.preview_quality_combo.addItem(label, value)
        self.preview_quality_combo.setCurrentIndex(1)
        self.preview_quality_combo.currentIndexChanged.connect(self._set_preview_quality)
        tools_row.addWidget(self.preview_quality_combo)
        self.remap_button = QPushButton("Find Missing Textures")
        self.remap_button.clicked.connect(self.remap_missing_texture_paths)
        tools_row.addWidget(self.remap_button)
        tools_row.addStretch(1)
        toolbar.addLayout(tools_row)
        root_layout.addLayout(toolbar)

        self.view = GraphView(self._scene)
        self.view.connection_delete_requested.connect(self._on_connection_delete_requested)
        self.view.connection_insert_node_requested.connect(self._on_connection_insert_node_requested)
        self.view.layout_requested.connect(self.auto_layout_nodes)
        self.view.texture_dropped.connect(self.add_texture_node_for_path)
        self.view.node_add_requested.connect(self.add_node_of_type)
        self.properties_panel = NodePropertiesPanel()
        self.properties_panel.node_changed.connect(self._on_node_properties_changed)
        self.properties_panel.transient_preview_requested.connect(
            self._on_transient_preview_requested
        )
        self.properties_panel.preview_refresh_requested.connect(self._on_preview_refresh_requested)
        self.properties_panel.interactive_edit_started.connect(
            self._on_interactive_property_edit_started
        )
        self.properties_panel.interactive_edit_finished.connect(
            self._on_interactive_property_edit_finished
        )
        self.properties_panel.output_profile_apply_requested.connect(self._apply_output_profile)
        self.properties_panel.output_inputs_clear_requested.connect(self._clear_output_inputs)
        self.properties_panel.output_export_requested.connect(self.output_export_requested.emit)
        self.properties_panel.node_reset_requested.connect(self._on_node_reset_clicked)
        self._install_shortcuts()

        self.result_table = QTableWidget()
        self.result_table.setColumnCount(4)
        self.result_table.setHorizontalHeaderLabels(("Output", "Status", "Destination", "Message"))
        self.result_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.result_table.verticalHeader().setVisible(False)
        self.result_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.result_table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.result_table.horizontalHeader().setStretchLastSection(True)
        self.result_table.setMinimumHeight(120)
        self.result_table.setMinimumWidth(0)

        self.validation_table = QTableWidget()
        self.validation_table.setColumnCount(3)
        self.validation_table.setHorizontalHeaderLabels(("Severity", "Node", "Message"))
        self.validation_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.validation_table.verticalHeader().setVisible(False)
        self.validation_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.validation_table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.validation_table.horizontalHeader().setStretchLastSection(True)
        self.validation_table.setMinimumHeight(120)
        self.validation_table.itemSelectionChanged.connect(self._select_validation_issue_node)

        root_layout.addWidget(self.view, 1)

    def build_properties_widget(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("SidebarPanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        tabs = QTabWidget()
        tabs.addTab(self.properties_panel, "Properties")

        validation_tab = QWidget()
        validation_layout = QVBoxLayout(validation_tab)
        validation_layout.setContentsMargins(8, 8, 8, 8)
        validation_layout.setSpacing(8)
        validation_layout.addWidget(QLabel("Graph Validation"))
        validation_layout.addWidget(self.validation_table, 1)
        tabs.addTab(validation_tab, "Validation")

        export_tab = QWidget()
        export_layout = QVBoxLayout(export_tab)
        export_layout.setContentsMargins(8, 8, 8, 8)
        export_layout.setSpacing(8)
        export_layout.addWidget(QLabel("Graph Export Queue"))
        export_layout.addWidget(self.result_table, 1)
        tabs.addTab(export_tab, "Export")

        layout.addWidget(tabs, 1)
        return panel

    def set_graph_editing_enabled(self, enabled: bool) -> None:
        self._scene.set_editing_enabled(enabled)
        self.properties_panel.setEnabled(enabled)
        for control in (
            self.delete_button,
            self.layout_button,
            self.remap_button,
        ):
            control.setEnabled(enabled)
        for shortcut in self._shortcuts:
            shortcut.setEnabled(enabled)

    def _install_shortcuts(self) -> None:
        shortcuts = (
            ("Ctrl+Z", self.undo_stack.undo),
            ("Ctrl+Y", self.undo_stack.redo),
            ("Ctrl+C", self._copy_selection),
            ("Ctrl+V", self._paste_clipboard),
            ("Ctrl+D", self._duplicate_selection),
            ("Delete", self._delete_selection),
        )
        for sequence, slot in shortcuts:
            shortcut = QShortcut(QKeySequence(sequence), self.view)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.activated.connect(slot)
            self._shortcuts.append(shortcut)

    def set_assets(self, items: list[QueueItem]) -> None:
        self._assets = [item for item in items if item.metadata is not None]

    def assets(self) -> tuple[QueueItem, ...]:
        return tuple(self._assets)

    def clear_assets(self) -> None:
        self._assets = []

    def refresh_asset_paths(self, paths: Iterable[Path]) -> None:
        paths = tuple(paths)
        for path in paths:
            self._scene.texture_visual_cache.invalidate(Path(path))
        changed_keys = {
            self._path_key(path)
            for path in paths
            if str(path).strip()
        }
        if not changed_keys:
            return

        selected_ids = self._scene.selected_node_ids()
        if not selected_ids and self.properties_panel._node is not None:
            selected_ids = [self.properties_panel._node.node_id]

        changed_node_ids = [
            node.node_id
            for node in self.project.graph.nodes
            if node.node_type is NodeType.TEXTURE_INPUT
            and self._path_key(Path(str(node.properties.get("path", "")))) in changed_keys
        ]
        if not changed_node_ids:
            return

        self._preview_generation += 1
        for node_id in changed_node_ids:
            self._preview_cache.invalidate_node_and_downstream(self.project.graph, node_id)
            if self._preview_inflight_generation:
                self._preview_dirty_node_ids.add(node_id)

        for node_id in changed_node_ids:
            item = self._scene.node_items.get(node_id)
            if item is not None:
                item.refresh_content()

        if selected_ids:
            self._scene.select_node_ids(selected_ids)
        self._refresh_validation()
        self.properties_panel.set_node(self._scene.selected_node())
        self._refresh_properties_profile_summary()
        if not self._refresh_last_preview_request():
            self._preview_active_display_node()

    def set_auto_watch_enabled(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if self._auto_watch_enabled == enabled:
            if enabled:
                self._rebuild_asset_watchers()
            return
        self._auto_watch_enabled = enabled
        if enabled:
            self._rebuild_asset_watchers()
            self.status_message.emit("Graph Auto Watch enabled.")
        else:
            self._clear_asset_watchers()
            self.status_message.emit("Graph Auto Watch disabled.")

    def auto_watch_enabled(self) -> bool:
        return self._auto_watch_enabled

    def watched_texture_paths(self) -> tuple[Path, ...]:
        return tuple(
            Path(path)
            for path in sorted(self._watched_texture_keys.values())
        )

    def export_destinations(self, output_root: Path) -> list[Path]:
        return self._executor.resolve_output_paths(self.project.graph, output_root)

    def export_destination(self, output_node: GraphNode, output_root: Path) -> Path:
        return self._executor.resolve_output_path(output_node, output_root)

    def apply_recent_projects(self, paths: Iterable[str]) -> None:
        self._recent_project_dirs = []
        for raw_path in paths:
            path = self._repository.resolve_project_path(Path(raw_path))
            if path.exists() and path not in self._recent_project_dirs:
                self._recent_project_dirs.append(path)
        self.recent_projects_changed.emit(self.recent_project_paths())

    def recent_project_paths(self) -> tuple[str, ...]:
        return tuple(str(path) for path in self._recent_project_dirs[:8])

    def new_project(self) -> None:
        self.project = NodeGraphProject()
        self.project_path = None
        self.project_dir = None
        self._last_preview_node_id = None
        self._last_preview_mode_label = ""
        self._preview_generation += 1
        self._preview_inflight_generation = 0
        self._pending_preview_request = None
        self._last_preview_quality_request = PREVIEW_MODE_FULL
        self._preview_cache.clear()
        self.undo_stack.clear()
        self._scene.project = self.project
        self._scene.rebuild()
        self.undo_stack.setClean()
        self._update_project_label()
        self.result_table.setRowCount(0)
        self._refresh_validation()
        self._rebuild_asset_watchers()
        self.status_message.emit("New graph project.")

    def load_project_dialog(self) -> bool:
        path, _selected_filter = QFileDialog.getOpenFileName(
            self,
            "Load Graph Project",
            str(self._project_dialog_directory()),
            GRAPH_PROJECT_FILE_FILTER,
        )
        if not path:
            return False
        return self.load_project(Path(path))

    def save_project_dialog(self) -> bool:
        if self.project_path is None:
            return self.save_project_as_dialog()
        return self.save_project(self.project_path)

    def save_project_as_dialog(self) -> bool:
        suggested_name = f"{self.project.name or 'Untitled Graph'}{GRAPH_PROJECT_EXTENSION}"
        suggested_path = self.project_path or (
            self._project_dialog_directory() / suggested_name
        )
        path, _selected_filter = QFileDialog.getSaveFileName(
            self,
            "Save Graph Project",
            str(suggested_path),
            GRAPH_PROJECT_FILE_FILTER,
        )
        if not path:
            return False
        project_path = Path(path)
        if not project_path.suffix:
            project_path = project_path.with_suffix(GRAPH_PROJECT_EXTENSION)
        return self.save_project(project_path)

    def save_project(self, location: Path) -> bool:
        project_path = self._repository.resolve_project_path(location)
        if not project_path.suffix:
            project_path = project_path.with_suffix(GRAPH_PROJECT_EXTENSION)
        previous_name = self.project.name
        self.project.name = self._project_name_from_path(project_path)
        try:
            project_path = self._repository.save(self.project, project_path)
        except Exception as exc:
            self.project.name = previous_name
            self.status_message.emit(f"Graph save failed: {exc}")
            return False
        self.project_path = project_path
        self.project_dir = project_path.parent
        self._remember_recent_project(project_path)
        self.undo_stack.setClean()
        self._update_project_label()
        self.status_message.emit(f"Saved graph file: {project_path}")
        return True

    def autosave_project(self) -> None:
        if not self.has_unsaved_changes() or self.project_path is None:
            return
        autosave_path = self.project_path.with_suffix(".autosave.texturegraph")
        try:
            self._repository.save(self.project, autosave_path)
        except Exception as exc:
            self.status_message.emit(f"Graph autosave failed: {exc}")
            return
        self.status_message.emit(f"Autosaved graph file: {autosave_path}")

    def load_project(self, location: Path) -> bool:
        project_path = self._repository.resolve_project_path(location)
        try:
            self.project = self._repository.load(project_path)
        except Exception as exc:
            self.status_message.emit(f"Graph load failed: {exc}")
            return False
        self.project_path = project_path
        self.project_dir = project_path.parent
        self._remember_recent_project(project_path)
        self._last_preview_node_id = None
        self._last_preview_mode_label = ""
        self._preview_generation += 1
        self._preview_inflight_generation = 0
        self._pending_preview_request = None
        self._last_preview_quality_request = PREVIEW_MODE_FULL
        self._preview_cache.clear()
        self.undo_stack.clear()
        self._scene.project = self.project
        self._scene.rebuild()
        self.undo_stack.setClean()
        self._update_project_label()
        self._refresh_validation()
        self._rebuild_asset_watchers()
        self.status_message.emit(f"Loaded graph: {self.project.name}")
        return True

    @staticmethod
    def _project_name_from_path(project_path: Path) -> str:
        if project_path.name == GRAPH_PROJECT_FILENAME:
            return project_path.parent.name or "Graph"
        return project_path.stem or "Graph"

    def _project_dialog_directory(self) -> Path:
        if self.project_dir is not None:
            return self.project_dir
        documents = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.DocumentsLocation
        )
        return Path(documents) if documents else Path.cwd()

    def remap_missing_texture_paths(self) -> None:
        missing_nodes = [
            node
            for node in self.project.graph.nodes
            if node.node_type is NodeType.TEXTURE_INPUT
            and (
                not str(node.properties.get("path", "")).strip()
                or not Path(str(node.properties.get("path", ""))).exists()
            )
        ]
        if not missing_nodes:
            self.status_message.emit("All texture paths are valid.")
            return
        root = QFileDialog.getExistingDirectory(self, "Select folder to search for missing textures")
        if not root:
            return
        root_path = Path(root)
        request = MissingTextureSearchRequest(
            root=root_path,
            names=tuple(
                Path(str(node.properties.get("path", ""))).name
                for node in missing_nodes
            ),
        )
        if self._missing_texture_search.submit(request):
            self.status_message.emit(
                f"Searching for {len(missing_nodes)} missing texture(s)..."
            )
        else:
            self.status_message.emit("Missing texture search is already running.")

    def _on_missing_texture_search_finished(
        self,
        request: MissingTextureSearchRequest,
        indexed_paths: object,
    ) -> None:
        if not isinstance(indexed_paths, dict):
            return
        missing_nodes = [
            node
            for node in self.project.graph.nodes
            if node.node_type is NodeType.TEXTURE_INPUT
            and (
                not str(node.properties.get("path", "")).strip()
                or not Path(str(node.properties.get("path", ""))).exists()
            )
        ]
        updated = 0
        for node in missing_nodes:
            old_path = Path(str(node.properties.get("path", "")))
            replacement = indexed_paths.get(old_path.name.casefold())
            if replacement is None:
                continue
            properties = dict(node.properties)
            properties["path"] = str(replacement)
            self._push_graph_command(
                SetNodeStateCommand(
                    self.project.graph,
                    self._on_graph_command_changed,
                    node,
                    title=node.title,
                    properties=properties,
                    text="Remap texture path",
                    needs_rebuild=True,
                )
            )
            updated += 1
        self.status_message.emit(f"Remapped missing textures: {updated}/{len(missing_nodes)}")

    def _set_preview_quality(self) -> None:
        max_side = int(self.preview_quality_combo.currentData() or 1024)
        self._preview_cache.max_side = max_side
        self._preview_cache.clear()
        self._preview_active_display_node()

    def _schedule_autosave(self) -> None:
        if self.project_path is not None and self.has_unsaved_changes():
            self._autosave_timer.start()

    def _remember_recent_project(self, bundle_dir: Path) -> None:
        try:
            resolved = bundle_dir.resolve()
        except OSError:
            resolved = bundle_dir
        self._recent_project_dirs = [
            path for path in self._recent_project_dirs if path != resolved
        ]
        self._recent_project_dirs.insert(0, resolved)
        self._recent_project_dirs = self._recent_project_dirs[:8]
        self.recent_projects_changed.emit(self.recent_project_paths())

    def add_texture_node_from_selected_asset(self) -> None:
        if not self._assets:
            self.status_message.emit("Add assets to Queue first, then create Texture nodes.")
            return
        self.add_texture_node_for_path(str(self._assets[0].path), None)

    def add_texture_node_for_path(self, path: str, scene_position: object | None = None) -> None:
        name = Path(str(path)).stem[:28]
        if isinstance(scene_position, QPointF):
            position = (scene_position.x(), scene_position.y())
        else:
            position = self._next_node_position()
        node = create_graph_node(
            NodeType.TEXTURE_INPUT,
            title=name,
            position=position,
            properties={"path": str(path)},
        )
        self._push_graph_command(
            AddNodesCommand(self.project.graph, self._on_graph_command_changed, [node], text="Add texture"),
            select_node_ids=[node.node_id],
        )

    def clone_project_with_texture_mapping(
        self,
        mapping: dict[TextureMapType, Path],
        *,
        output_prefix: str = "",
    ) -> NodeGraphProject:
        project = replace(self.project)
        graph = replace(self.project.graph)
        graph.nodes = [clone_graph_node(node) for node in self.project.graph.nodes]
        graph.connections = [clone_graph_connection(connection) for connection in self.project.graph.connections]
        project.graph = graph

        nodes_by_map_type: dict[TextureMapType, GraphNode] = {}
        for node in graph.nodes:
            if node.node_type is not NodeType.TEXTURE_INPUT:
                continue
            path = Path(str(node.properties.get("path", "")))
            map_type = detect_texture_map_type(path)
            if map_type is TextureMapType.UNKNOWN:
                continue
            nodes_by_map_type.setdefault(map_type, node)

        for map_type, replacement_path in mapping.items():
            node = nodes_by_map_type.get(map_type)
            if node is None:
                continue
            node.properties["path"] = str(replacement_path)

        if output_prefix:
            safe_prefix = output_prefix.replace("\\", "_").replace("/", "_").strip()
            for node in graph.nodes:
                if node.node_type is not NodeType.OUTPUT_RGBA:
                    continue
                raw_output_path = str(node.properties.get("output_path", "")).strip()
                raw_filename = str(node.properties.get("filename", "")).strip()
                if raw_output_path:
                    output_path = Path(raw_output_path)
                    node.properties["output_path"] = str(output_path.with_name(f"{safe_prefix}_{output_path.name}"))
                elif raw_filename:
                    node.properties["filename"] = f"{safe_prefix}_{raw_filename}"
                else:
                    node.properties["filename"] = f"{safe_prefix}_{node.title}.png"
        return project

    def add_node_of_type(
        self,
        node_type: object,
        scene_position: object | None = None,
        context: object | None = None,
    ) -> None:
        try:
            resolved_type = node_type if isinstance(node_type, NodeType) else NodeType(str(node_type))
        except ValueError:
            self.status_message.emit(f"Unknown node type: {node_type}")
            return

        position = self._node_position_for(scene_position)
        count = sum(1 for node in self.project.graph.nodes if node.node_type is resolved_type)
        properties: dict[str, object] = {}
        title = f"{node_type_label(resolved_type)} {count + 1}"

        if resolved_type is NodeType.OUTPUT_RGBA:
            title = f"Output {count + 1}"
            properties["filename"] = f"graph_output_{count + 1}.png"
        elif resolved_type is NodeType.TEXTURE_INPUT:
            title = f"Texture {count + 1}"
        elif resolved_type is NodeType.VIEW:
            title = f"View {count + 1}"

        node = create_graph_node(
            resolved_type,
            title=title,
            position=position,
            properties=properties,
        )
        connections = self._connections_for_new_node_context(node, context)
        self._push_graph_command(
            AddNodesCommand(
                self.project.graph,
                self._on_graph_command_changed,
                [node],
                connections,
                text=f"Add {node_type_label(resolved_type)}",
            ),
            select_node_ids=[node.node_id],
        )

    def add_constant_node(self) -> None:
        self.add_node_of_type(NodeType.CONSTANT_CHANNEL)

    def add_invert_node(self) -> None:
        self.add_node_of_type(NodeType.INVERT_CHANNEL)

    def add_view_node(self) -> None:
        self.add_node_of_type(NodeType.VIEW)

    def add_output_node(self) -> None:
        self.add_node_of_type(NodeType.OUTPUT_RGBA)

    def add_pbr_shader_node(self) -> None:
        self.add_node_of_type(NodeType.PBR_SHADER)

    def auto_layout_nodes(self) -> None:
        nodes = list(self.project.graph.nodes)
        if not nodes:
            self.status_message.emit("Graph is empty.")
            return
        before_positions = {node.node_id: node.position for node in nodes}
        after_positions = self._auto_layout_positions(nodes)
        if after_positions == before_positions:
            self.status_message.emit("Graph layout already looks clean.")
            return
        selected_ids = self._scene.selected_node_ids()
        self._push_graph_command(
            MoveNodesCommand(
                self.project.graph,
                self._on_graph_command_changed,
                before_positions,
                after_positions,
            ),
            select_node_ids=selected_ids,
        )
        self.view.fit_graph()
        self.status_message.emit(f"Auto layout: arranged {len(nodes)} node(s).")

    def _auto_layout_positions(self, nodes: list[GraphNode]) -> dict[str, tuple[float, float]]:
        node_ids = {node.node_id for node in nodes}
        depth_by_id = self._layout_depth_by_node(nodes)
        columns: dict[int, list[GraphNode]] = {}
        for node in nodes:
            columns.setdefault(depth_by_id.get(node.node_id, 0), []).append(node)

        depths = sorted(columns)
        center = self.view.mapToScene(self.view.viewport().rect().center())
        total_width = max(0, (len(depths) - 1) * GRAPH_LAYOUT_X_SPACING)
        left = center.x() - total_width / 2
        positions: dict[str, tuple[float, float]] = {}

        for column_index, depth in enumerate(depths):
            column_nodes = sorted(
                columns[depth],
                key=lambda item: (self._layout_type_rank(item.node_type), item.position[1], item.title),
            )
            heights = [self._estimated_node_height(node) for node in column_nodes]
            total_height = sum(heights) + max(0, len(heights) - 1) * GRAPH_LAYOUT_Y_SPACING
            x = self._snap_to_layout_grid(left + column_index * GRAPH_LAYOUT_X_SPACING)
            y = self._snap_to_layout_grid(center.y() - total_height / 2)
            for node, height in zip(column_nodes, heights, strict=True):
                if node.node_id in node_ids:
                    positions[node.node_id] = (x, y)
                y = self._snap_to_layout_grid(y + height + GRAPH_LAYOUT_Y_SPACING)
        return positions

    def _layout_depth_by_node(self, nodes: list[GraphNode]) -> dict[str, int]:
        node_by_id = {node.node_id: node for node in nodes}
        depth_by_id = {
            node.node_id: self._layout_type_rank(node.node_type)
            for node in nodes
        }
        incoming_count = {node.node_id: 0 for node in nodes}
        outgoing: dict[str, list[str]] = {node.node_id: [] for node in nodes}
        for connection in self.project.graph.connections:
            if connection.source_node_id not in node_by_id or connection.target_node_id not in node_by_id:
                continue
            outgoing[connection.source_node_id].append(connection.target_node_id)
            incoming_count[connection.target_node_id] += 1

        queue = sorted(
            (node_id for node_id, count in incoming_count.items() if count == 0),
            key=lambda node_id: (
                depth_by_id.get(node_id, 0),
                node_by_id[node_id].position[0],
                node_by_id[node_id].position[1],
            ),
        )
        while queue:
            node_id = queue.pop(0)
            for target_id in outgoing[node_id]:
                depth_by_id[target_id] = max(depth_by_id[target_id], depth_by_id[node_id] + 1)
                incoming_count[target_id] -= 1
                if incoming_count[target_id] == 0:
                    queue.append(target_id)
                    queue.sort(
                        key=lambda item: (
                            depth_by_id.get(item, 0),
                            node_by_id[item].position[0],
                            node_by_id[item].position[1],
                        )
                    )
        return depth_by_id

    @staticmethod
    def _layout_type_rank(node_type: NodeType) -> int:
        if node_type in (NodeType.TEXTURE_INPUT, NodeType.COLOR, NodeType.CONSTANT_CHANNEL):
            return 0
        if node_type in (
            NodeType.INVERT_CHANNEL,
            NodeType.LEVELS_CHANNEL,
            NodeType.REMAP_CHANNEL,
            NodeType.CLAMP_CHANNEL,
            NodeType.THRESHOLD_CHANNEL,
            NodeType.BLUR_CHANNEL,
            NodeType.DILATE_CHANNEL,
            NodeType.ERODE_CHANNEL,
            NodeType.BLEND_CHANNEL,
            NodeType.LUMINANCE,
            NodeType.MIX_IMAGE,
            NodeType.BLEND_IMAGE,
            NodeType.NORMAL_MAP,
            NodeType.HEIGHT_TO_NORMAL,
            NodeType.NORMAL_BLEND,
            NodeType.COLOR_ADJUST,
            NodeType.TRANSFORM_2D,
            NodeType.RESIZE_CANVAS,
            NodeType.SPLIT_RGBA,
            NodeType.COMBINE_RGBA,
            NodeType.SET_ALPHA,
        ):
            return 1
        return 2

    @staticmethod
    def _estimated_node_height(node: GraphNode) -> float:
        sockets = socket_definitions(node.node_type)
        input_count = sum(1 for socket in sockets if socket.direction is SocketDirection.INPUT)
        output_count = sum(1 for socket in sockets if socket.direction is SocketDirection.OUTPUT)
        row_count = max(input_count, output_count, 2)
        body_height = row_count * ROW_HEIGHT + 16
        if node.node_type is NodeType.TEXTURE_INPUT:
            body_height = TEXTURE_BODY_HEIGHT
        return float(TITLE_HEIGHT + body_height)

    @staticmethod
    def _snap_to_layout_grid(value: float) -> float:
        return float(round(value / GRAPH_LAYOUT_GRID) * GRAPH_LAYOUT_GRID)

    def export_graph(
        self,
        output_root: Path,
        options: ConversionOptions,
        logger,
    ) -> GraphExportSummary:
        self._refresh_validation()
        warnings = self._executor.validate(self.project)
        if warnings:
            for warning in warnings:
                logger(f"GRAPH VALIDATION -> {warning}")
        summary = self._executor.export_enabled_outputs(
            self.project,
            output_root,
            options,
            logger=logger,
        )
        self.set_export_summary(summary)
        return summary

    def export_output(
        self,
        output_node: GraphNode,
        output_root: Path,
        options: ConversionOptions,
        logger,
    ) -> GraphExportSummary:
        summary = self._executor.export_output(
            self.project,
            output_node,
            output_root,
            options,
            logger=logger,
        )
        self.set_export_summary(summary)
        return summary

    def set_export_summary(self, summary: GraphExportSummary) -> None:
        self.result_table.setRowCount(len(summary.results))
        for row, result in enumerate(summary.results):
            values = (
                result.output_name,
                self._status_text(result.status),
                str(result.destination),
                result.message,
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                self.result_table.setItem(row, column, item)

    def _refresh_validation(self) -> None:
        issues = self._executor.validate_issues(self.project)
        self._scene.set_validation_issues(issues)
        self.validation_table.blockSignals(True)
        try:
            self.validation_table.setRowCount(len(issues))
            for row, issue in enumerate(issues):
                node_title = self._node_title(issue.node_id)
                values = (
                    issue.severity.value.upper(),
                    node_title,
                    issue.message,
                )
                for column, value in enumerate(values):
                    item = QTableWidgetItem(value)
                    item.setData(Qt.ItemDataRole.UserRole, issue.node_id)
                    item.setToolTip(issue.message)
                    self.validation_table.setItem(row, column, item)
        finally:
            self.validation_table.blockSignals(False)

    def _select_validation_issue_node(self) -> None:
        selected_rows = self.validation_table.selectionModel().selectedRows()
        if not selected_rows:
            return
        row = selected_rows[0].row()
        item = self.validation_table.item(row, 0)
        if item is None:
            return
        node_id = str(item.data(Qt.ItemDataRole.UserRole) or "")
        if node_id:
            self._scene.select_node_ids([node_id])

    def _node_title(self, node_id: str) -> str:
        for node in self.project.graph.nodes:
            if node.node_id == node_id:
                return node.title
        return "-"

    def _next_node_position(self) -> tuple[float, float]:
        view_center = self.view.mapToScene(self.view.viewport().rect().center())
        offset = 28 * len(self.project.graph.nodes)
        return (view_center.x() + offset, view_center.y() + offset)

    def _node_position_for(self, scene_position: object | None) -> tuple[float, float]:
        if isinstance(scene_position, QPointF):
            return (scene_position.x(), scene_position.y())
        return self._next_node_position()

    def _push_graph_command(self, command, *, select_node_ids: Iterable[str] = ()) -> None:
        selected_ids = list(select_node_ids)
        self.undo_stack.push(command)
        if selected_ids:
            self._scene.select_node_ids(selected_ids)
        self._update_project_label()

    def _on_graph_command_changed(
        self,
        needs_rebuild: bool | str = True,
        *,
        preview_dirty_node_ids: Iterable[str] = (),
    ) -> None:
        selected_ids = self._scene.selected_node_ids()
        if not selected_ids and self.properties_panel._node is not None:
            selected_ids = [self.properties_panel._node.node_id]

        position_sync_only = needs_rebuild == "positions"
        if position_sync_only:
            self._scene.sync_node_positions()
            self._update_project_label()
            self._schedule_autosave()
            self.template_changed.emit()
            return
        dirty_node_ids = tuple(dict.fromkeys(preview_dirty_node_ids))
        if needs_rebuild:
            if dirty_node_ids:
                for node_id in dirty_node_ids:
                    self._preview_cache.invalidate_node_and_downstream(
                        self.project.graph,
                        node_id,
                    )
                if self._preview_inflight_generation:
                    self._preview_dirty_node_ids.update(dirty_node_ids)
            else:
                self._preview_cache.clear()
                if self._preview_inflight_generation:
                    self._preview_full_reset_pending = True
        else:
            selected_node = self._scene.selected_node() or self.properties_panel._node
            if selected_node is not None:
                self._invalidate_preview_from_node(selected_node)
            else:
                self._preview_cache.clear()
        interactive_edit = self._interactive_property_edit and not needs_rebuild
        if needs_rebuild:
            self._scene.rebuild()
            self._scene.select_node_ids(selected_ids)
        else:
            self._scene.sync_node_positions()
            if not interactive_edit:
                self._refresh_node_flags()
                for node_id in selected_ids:
                    item = self._scene.node_items.get(node_id)
                    if item is not None:
                        item.refresh_content()

        if not interactive_edit:
            self._refresh_validation()
        if not self._property_edit_in_progress:
            self.properties_panel.set_node(self._scene.selected_node())
        if not interactive_edit:
            self._refresh_properties_profile_summary()
        if not self._property_edit_in_progress:
            self._preview_active_display_node()
        self._update_project_label()
        if not interactive_edit:
            self._schedule_autosave()
        if needs_rebuild:
            self._rebuild_asset_watchers()
        if not interactive_edit:
            self.template_changed.emit()

    def _delete_selection(self) -> None:
        self._scene.delete_selected()

    def _on_delete_items_requested(self, payload: object) -> None:
        try:
            node_ids, connection_ids = payload
        except (TypeError, ValueError):
            return
        if not node_ids and not connection_ids:
            return
        connection_id_set = set(connection_ids)
        connection_target_ids = {
            connection.target_node_id
            for connection in self.project.graph.connections
            if connection.connection_id in connection_id_set
        }
        replacement_connections = self._dissolve_replacement_connections(
            node_ids,
            connection_ids,
        )
        on_changed = self._on_graph_command_changed
        if not node_ids and connection_target_ids:
            on_changed = self._targeted_graph_change_callback(connection_target_ids)
        self._push_graph_command(
            DeleteItemsCommand(
                self.project.graph,
                on_changed,
                node_ids=node_ids,
                connection_ids=connection_ids,
                replacement_connections=replacement_connections,
            )
        )
        if replacement_connections:
            suffix = "connection" if len(replacement_connections) == 1 else "connections"
            self.status_message.emit(
                f"Node dissolved; restored {len(replacement_connections)} {suffix}."
            )

    def _dissolve_replacement_connections(
        self,
        node_ids: Iterable[str],
        connection_ids: Iterable[str],
    ) -> list[GraphConnection]:
        resolved_node_ids = tuple(node_ids)
        if len(resolved_node_ids) != 1 or tuple(connection_ids):
            return []

        node_id = resolved_node_ids[0]
        node = next(
            (candidate for candidate in self.project.graph.nodes if candidate.node_id == node_id),
            None,
        )
        if node is None:
            return []
        bypass_pair = node_bypass_socket_pair(node.node_type)
        if bypass_pair is None:
            return []
        input_socket_id, output_socket_id = bypass_pair
        incoming = next(
            (
                connection
                for connection in self.project.graph.connections
                if connection.target_node_id == node_id
                and connection.target_socket_id == input_socket_id
            ),
            None,
        )
        if incoming is None:
            return []

        source_node = next(
            (
                candidate
                for candidate in self.project.graph.nodes
                if candidate.node_id == incoming.source_node_id
            ),
            None,
        )
        if source_node is None:
            return []
        source_socket = next(
            (
                socket
                for socket in socket_definitions(source_node.node_type)
                if socket.socket_id == incoming.source_socket_id
                and socket.direction is SocketDirection.OUTPUT
            ),
            None,
        )
        if source_socket is None:
            return []

        incident_connection_ids = {
            connection.connection_id
            for connection in self.project.graph.connections
            if connection.source_node_id == node_id or connection.target_node_id == node_id
        }
        occupied_targets = {
            (connection.target_node_id, connection.target_socket_id)
            for connection in self.project.graph.connections
            if connection.connection_id not in incident_connection_ids
        }
        replacements: list[GraphConnection] = []
        replacement_targets: set[tuple[str, str]] = set()
        for outgoing in self.project.graph.connections:
            if (
                outgoing.source_node_id != node_id
                or outgoing.source_socket_id != output_socket_id
                or outgoing.target_node_id == incoming.source_node_id
            ):
                continue
            target_key = (outgoing.target_node_id, outgoing.target_socket_id)
            if target_key in occupied_targets or target_key in replacement_targets:
                continue
            target_node = next(
                (
                    candidate
                    for candidate in self.project.graph.nodes
                    if candidate.node_id == outgoing.target_node_id
                ),
                None,
            )
            if target_node is None:
                continue
            target_socket = next(
                (
                    socket
                    for socket in socket_definitions(target_node.node_type)
                    if socket.socket_id == outgoing.target_socket_id
                    and socket.direction is SocketDirection.INPUT
                ),
                None,
            )
            if target_socket is None or target_socket.socket_type is not source_socket.socket_type:
                continue
            replacements.append(
                GraphConnection(
                    connection_id=make_connection_id(),
                    source_node_id=incoming.source_node_id,
                    source_socket_id=incoming.source_socket_id,
                    target_node_id=outgoing.target_node_id,
                    target_socket_id=outgoing.target_socket_id,
                )
            )
            replacement_targets.add(target_key)
        return replacements

    def _on_connection_requested(
        self,
        connection: GraphConnection,
        rewire_connection: object | None = None,
    ) -> None:
        remove_connections = list(
            [rewire_connection]
            if isinstance(rewire_connection, GraphConnection)
            else []
        )
        cleared_overrides = 0
        target_node = next(
            (
                node
                for node in self.project.graph.nodes
                if node.node_id == connection.target_node_id
            ),
            None,
        )
        if (
            target_node is not None
            and target_node.node_type is NodeType.OUTPUT_RGBA
            and connection.target_socket_id == "image"
        ):
            channel_connections = [
                candidate
                for candidate in self.project.graph.connections
                if candidate.target_node_id == target_node.node_id
                and candidate.target_socket_id in {"r", "g", "b", "a"}
            ]
            remove_connections.extend(channel_connections)
            cleared_overrides = len(channel_connections)
        self._push_graph_command(
            ReplaceInputConnectionCommand(
                self.project.graph,
                self._targeted_graph_change_callback(
                    (connection.target_node_id,)
                ),
                connection,
                remove_connections=remove_connections,
            ),
            select_node_ids=[connection.target_node_id],
        )
        if cleared_overrides:
            self.status_message.emit(
                f"Image connected; cleared {cleared_overrides} previous channel override(s)."
            )
        else:
            self.status_message.emit("Connection created.")

    def _on_connection_delete_requested(self, connection: GraphConnection) -> None:
        self._push_graph_command(
            RemoveConnectionsCommand(
                self.project.graph,
                self._targeted_graph_change_callback(
                    (connection.target_node_id,)
                ),
                [connection],
                text="Delete connection",
            )
        )

    def _targeted_graph_change_callback(
        self,
        node_ids: Iterable[str],
    ) -> Callable[[bool], None]:
        dirty_node_ids = tuple(dict.fromkeys(node_ids))
        return lambda needs_rebuild: self._on_graph_command_changed(
            needs_rebuild,
            preview_dirty_node_ids=dirty_node_ids,
        )

    def _on_connection_insert_node_requested(
        self,
        connection: GraphConnection,
        node_type: object,
        scene_position: object,
    ) -> None:
        try:
            resolved_type = node_type if isinstance(node_type, NodeType) else NodeType(str(node_type))
        except ValueError:
            return
        input_socket_id = self._first_channel_input_socket_id(resolved_type)
        output_socket_id = self._first_channel_output_socket_id(resolved_type)
        if input_socket_id is None or output_socket_id is None:
            self.status_message.emit(f"{node_type_label(resolved_type)} cannot be inserted in a wire.")
            return

        position = self._node_position_for(scene_position)
        count = sum(1 for node in self.project.graph.nodes if node.node_type is resolved_type)
        node = create_graph_node(
            resolved_type,
            title=f"{node_type_label(resolved_type)} {count + 1}",
            position=position,
        )
        input_connection = GraphConnection(
            connection_id=make_connection_id(),
            source_node_id=connection.source_node_id,
            source_socket_id=connection.source_socket_id,
            target_node_id=node.node_id,
            target_socket_id=input_socket_id,
        )
        output_connection = GraphConnection(
            connection_id=make_connection_id(),
            source_node_id=node.node_id,
            source_socket_id=output_socket_id,
            target_node_id=connection.target_node_id,
            target_socket_id=connection.target_socket_id,
        )
        self._push_graph_command(
            InsertNodeInConnectionCommand(
                self.project.graph,
                self._on_graph_command_changed,
                connection,
                node,
                input_connection,
                output_connection,
            ),
            select_node_ids=[node.node_id],
        )

    def _on_wire_node_requested(self, port: PortItem, scene_position: object) -> None:
        if isinstance(scene_position, QPointF):
            self.view.open_node_menu(scene_position, wire_port=port)

    def _on_node_shake_disconnect_requested(
        self,
        node: GraphNode,
        before_positions: object,
        after_positions: object,
    ) -> None:
        if not isinstance(before_positions, dict) or not isinstance(after_positions, dict):
            return
        connections = [
            connection
            for connection in self.project.graph.connections
            if node.node_id
            in (connection.source_node_id, connection.target_node_id)
        ]
        if not connections:
            self._on_nodes_moved(before_positions, after_positions)
            return
        bypass_connections = self._shake_bypass_connections(node, connections)
        self._push_graph_command(
            MoveAndRewireNodeCommand(
                self.project.graph,
                self._on_graph_command_changed,
                before_positions,
                after_positions,
                remove_connections=connections,
                add_connections=bypass_connections,
                text=(
                    "Shake bypass node"
                    if bypass_connections
                    else "Shake disconnect node"
                ),
            ),
            select_node_ids=list(after_positions),
        )
        if bypass_connections:
            self.status_message.emit(
                f"{node.title}: bypassed and restored "
                f"{len(bypass_connections)} wire(s)."
            )
        else:
            self.status_message.emit(
                f"{node.title}: disconnected {len(connections)} wire(s)."
            )

    def _shake_bypass_connections(
        self,
        node: GraphNode,
        removed_connections: Iterable[GraphConnection],
    ) -> list[GraphConnection]:
        bypass_pair = node_bypass_socket_pair(node.node_type)
        if bypass_pair is None:
            return []
        input_socket_id, output_socket_id = bypass_pair
        removed = list(removed_connections)
        incoming = next(
            (
                connection
                for connection in removed
                if connection.target_node_id == node.node_id
                and connection.target_socket_id == input_socket_id
            ),
            None,
        )
        if incoming is None:
            return []
        outgoing = [
            connection
            for connection in removed
            if connection.source_node_id == node.node_id
            and connection.source_socket_id == output_socket_id
        ]
        if not outgoing:
            return []

        source_socket = self._connection_source_socket(incoming)
        if source_socket is None:
            return []
        removed_ids = {connection.connection_id for connection in removed}
        existing_routes = {
            (
                connection.source_node_id,
                connection.source_socket_id,
                connection.target_node_id,
                connection.target_socket_id,
            )
            for connection in self.project.graph.connections
            if connection.connection_id not in removed_ids
        }
        bypass_connections: list[GraphConnection] = []
        for outgoing_connection in outgoing:
            target_socket = self._connection_target_socket(outgoing_connection)
            if (
                target_socket is None
                or source_socket.socket_type is not target_socket.socket_type
            ):
                continue
            route = (
                incoming.source_node_id,
                incoming.source_socket_id,
                outgoing_connection.target_node_id,
                outgoing_connection.target_socket_id,
            )
            if route in existing_routes:
                continue
            bypass_connections.append(
                GraphConnection(
                    make_connection_id(),
                    route[0],
                    route[1],
                    route[2],
                    route[3],
                )
            )
            existing_routes.add(route)
        return bypass_connections

    def _on_node_wire_insert_requested(
        self,
        node: GraphNode,
        connection: GraphConnection,
        before_positions: object,
        after_positions: object,
    ) -> None:
        if not isinstance(before_positions, dict) or not isinstance(after_positions, dict):
            return
        current_connection = next(
            (
                candidate
                for candidate in self.project.graph.connections
                if candidate.connection_id == connection.connection_id
            ),
            None,
        )
        bypass_pair = node_bypass_socket_pair(node.node_type)
        if current_connection is None or bypass_pair is None:
            self._on_nodes_moved(before_positions, after_positions)
            return
        input_socket_id, output_socket_id = bypass_pair
        socket_by_id = {
            socket.socket_id: socket for socket in socket_definitions(node.node_type)
        }
        input_socket = socket_by_id.get(input_socket_id)
        output_socket = socket_by_id.get(output_socket_id)
        source_socket = self._connection_source_socket(current_connection)
        target_socket = self._connection_target_socket(current_connection)
        if (
            input_socket is None
            or output_socket is None
            or source_socket is None
            or target_socket is None
            or input_socket.socket_type is not source_socket.socket_type
            or output_socket.socket_type is not target_socket.socket_type
        ):
            self._on_nodes_moved(before_positions, after_positions)
            self.status_message.emit(
                f"{node.title}: socket types do not match this wire."
            )
            return

        replaced_input_connections = [
            candidate
            for candidate in self.project.graph.connections
            if candidate.target_node_id == node.node_id
            and candidate.target_socket_id == input_socket_id
        ]
        removed = list(
            {
                item.connection_id: item
                for item in (current_connection, *replaced_input_connections)
            }.values()
        )
        added = [
            GraphConnection(
                make_connection_id(),
                current_connection.source_node_id,
                current_connection.source_socket_id,
                node.node_id,
                input_socket_id,
            ),
            GraphConnection(
                make_connection_id(),
                node.node_id,
                output_socket_id,
                current_connection.target_node_id,
                current_connection.target_socket_id,
            ),
        ]
        proposed_connections = [
            candidate
            for candidate in self.project.graph.connections
            if candidate.connection_id
            not in {removed_connection.connection_id for removed_connection in removed}
        ]
        proposed_connections.extend(added)
        if self._connections_contain_cycle(proposed_connections):
            self._on_nodes_moved(before_positions, after_positions)
            self.status_message.emit(
                f"{node.title}: insertion canceled because it would create a cycle."
            )
            return

        self._push_graph_command(
            MoveAndRewireNodeCommand(
                self.project.graph,
                self._on_graph_command_changed,
                before_positions,
                after_positions,
                remove_connections=removed,
                add_connections=added,
                text="Insert existing node in wire",
            ),
            select_node_ids=list(after_positions),
        )
        self.status_message.emit(f"{node.title}: inserted into wire.")

    def _connection_source_socket(self, connection: GraphConnection):
        node = next(
            (
                candidate
                for candidate in self.project.graph.nodes
                if candidate.node_id == connection.source_node_id
            ),
            None,
        )
        if node is None:
            return None
        return next(
            (
                socket
                for socket in socket_definitions(node.node_type)
                if socket.socket_id == connection.source_socket_id
            ),
            None,
        )

    def _connection_target_socket(self, connection: GraphConnection):
        node = next(
            (
                candidate
                for candidate in self.project.graph.nodes
                if candidate.node_id == connection.target_node_id
            ),
            None,
        )
        if node is None:
            return None
        return next(
            (
                socket
                for socket in socket_definitions(node.node_type)
                if socket.socket_id == connection.target_socket_id
            ),
            None,
        )

    @staticmethod
    def _connections_contain_cycle(connections: Iterable[GraphConnection]) -> bool:
        outgoing: dict[str, set[str]] = {}
        node_ids: set[str] = set()
        for connection in connections:
            outgoing.setdefault(connection.source_node_id, set()).add(
                connection.target_node_id
            )
            node_ids.add(connection.source_node_id)
            node_ids.add(connection.target_node_id)
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node_id: str) -> bool:
            if node_id in visiting:
                return True
            if node_id in visited:
                return False
            visiting.add(node_id)
            for target_id in outgoing.get(node_id, ()):
                if visit(target_id):
                    return True
            visiting.remove(node_id)
            visited.add(node_id)
            return False

        return any(visit(node_id) for node_id in node_ids if node_id not in visited)

    def _on_node_moved(
        self,
        node: GraphNode,
        previous_position: object,
        next_position: object,
    ) -> None:
        if not isinstance(previous_position, tuple) or not isinstance(next_position, tuple):
            return
        if previous_position == next_position:
            return
        self._push_graph_command(
            MoveNodesCommand(
                self.project.graph,
                self._on_graph_command_changed,
                {node.node_id: previous_position},
                {node.node_id: next_position},
            ),
            select_node_ids=[node.node_id],
        )

    def _on_nodes_moved(self, previous_positions: object, next_positions: object) -> None:
        if not isinstance(previous_positions, dict) or not isinstance(next_positions, dict):
            return
        if not previous_positions or not next_positions:
            return
        self._push_graph_command(
            MoveNodesCommand(
                self.project.graph,
                self._on_graph_command_changed,
                previous_positions,
                next_positions,
            ),
            select_node_ids=list(next_positions.keys()),
        )

    def _on_node_properties_changed(
        self,
        node: GraphNode,
        title: object,
        properties: object,
        needs_rebuild: bool,
        preview_mode: object,
    ) -> None:
        if not isinstance(title, str) or not isinstance(properties, dict):
            return
        self._property_edit_in_progress = True
        try:
            self._push_graph_command(
                SetNodeStateCommand(
                    self.project.graph,
                    self._on_graph_command_changed,
                    node,
                    title=title,
                    properties=properties,
                    text="Edit node properties",
                    needs_rebuild=needs_rebuild,
                )
            )
        finally:
            self._property_edit_in_progress = False
        self._on_preview_refresh_requested(
            node,
            preview_mode if isinstance(preview_mode, str) else PREVIEW_MODE_FULL,
        )

    def _on_interactive_property_edit_started(self) -> None:
        self._interactive_property_edit = True
        self._autosave_timer.stop()

    def _on_interactive_property_edit_finished(self) -> None:
        if not self._interactive_property_edit:
            return
        self._interactive_property_edit = False
        self._refresh_node_flags()
        for node_id in self._scene.selected_node_ids():
            item = self._scene.node_items.get(node_id)
            if item is not None:
                item.refresh_content()
        self._refresh_validation()
        self._refresh_properties_profile_summary()
        self._schedule_autosave()
        self.template_changed.emit()

    def _apply_output_profile(self, node: GraphNode | None) -> None:
        if node is None or node.node_type is not NodeType.OUTPUT_RGBA:
            return
        plan = self._build_output_profile_plan(node)

        self.undo_stack.beginMacro("Auto connect output profile")
        try:
            self.undo_stack.push(
                SetNodeStateCommand(
                    self.project.graph,
                    self._on_graph_command_changed,
                    node,
                    title=plan.output_title,
                    properties=plan.properties,
                    text="Set output profile",
                    needs_rebuild=False,
                )
            )
            if plan.utility_nodes:
                self.undo_stack.push(
                    AddNodesCommand(
                        self.project.graph,
                        self._on_graph_command_changed,
                        plan.utility_nodes,
                        plan.utility_connections,
                        text="Add profile nodes",
                    )
                )
            for connection in plan.output_connections:
                self.undo_stack.push(
                    ReplaceInputConnectionCommand(
                        self.project.graph,
                        self._on_graph_command_changed,
                        connection,
                    )
                )
        finally:
            self.undo_stack.endMacro()

        self._scene.select_node_ids([node.node_id])
        self._refresh_properties_profile_summary(node)
        self._schedule_autosave()
        self.status_message.emit(f"{node.title}: {self._profile_status_text(plan)}")

    def _clear_output_inputs(self, node: GraphNode | None) -> None:
        if node is None or node.node_type is not NodeType.OUTPUT_RGBA:
            return
        input_socket_ids = {
            socket.socket_id
            for socket in socket_definitions(NodeType.OUTPUT_RGBA)
            if socket.direction is SocketDirection.INPUT
        }
        connections = [
            connection
            for connection in self.project.graph.connections
            if connection.target_node_id == node.node_id
            and connection.target_socket_id in input_socket_ids
        ]
        if not connections:
            self.status_message.emit(f"{node.title}: output inputs are already clear.")
            return
        self._push_graph_command(
            RemoveConnectionsCommand(
                self.project.graph,
                self._on_graph_command_changed,
                connections,
                text="Clear output inputs",
            ),
            select_node_ids=[node.node_id],
        )
        self.status_message.emit(f"{node.title}: cleared {len(connections)} input wire(s).")

    def _build_output_profile_plan(self, node: GraphNode) -> OutputProfilePlan:
        profile = self._output_profile(node)
        mode = OUTPUT_PROFILE_MODES.get(profile, OutputMode.RGBA)
        target_label = OUTPUT_PROFILE_LABELS.get(profile, profile.value)
        target_filename = OUTPUT_PROFILE_FILENAMES.get(profile, "packed_rgba.png")
        current_filename = str(node.properties.get("filename", "")).strip()
        current_output_path = str(node.properties.get("output_path", "")).strip()
        properties = dict(node.properties)
        properties["profile"] = profile.value
        properties["mode"] = mode.value

        if self._should_autorename_output_title(node):
            properties["title_auto_generated"] = True
            node_title = self._auto_output_title(profile)
        else:
            properties["title_auto_generated"] = bool(node.properties.get("title_auto_generated", False))
            node_title = node.title

        auto_filename = self._auto_output_filename(profile, node)
        if self._should_autorename_output_filename(node):
            properties["filename"] = auto_filename
            if current_output_path:
                output_path = Path(current_output_path)
                properties["output_path"] = str(output_path.with_name(auto_filename))
        target_filename = str(properties.get("filename", target_filename))

        utility_nodes: list[GraphNode] = []
        utility_connections: list[GraphConnection] = []
        output_connections: list[GraphConnection] = []
        texture_nodes_by_map_type = self._texture_nodes_by_map_type()
        packed_texture_nodes = self._packed_texture_nodes()
        output_x, output_y = node.position

        if profile is OutputProfile.GENERIC_RGBA:
            generic_connections, generic_utility_nodes = self._generic_rgba_profile_connections(
                node,
                texture_nodes_by_map_type,
            )
            output_connections.extend(generic_connections)
            utility_nodes.extend(generic_utility_nodes)
        else:
            for index, (socket_id, candidates, fill_value) in enumerate(self._profile_rules(profile)):
                if mode is OutputMode.RGB and socket_id == "a":
                    continue
                source_node, source_socket_id, invert = self._resolve_separate_profile_source(
                    candidates,
                    texture_nodes_by_map_type,
                )
                if source_node is None:
                    source_node, source_socket_id, invert = self._resolve_packed_profile_source(
                        profile,
                        socket_id,
                        candidates,
                        packed_texture_nodes,
                    )
                resolved_fill = self._profile_fill_value(
                    profile,
                    socket_id,
                    candidates,
                    fill_value,
                )
                if source_node is not None:
                    if invert:
                        invert_node = create_graph_node(
                            NodeType.INVERT_CHANNEL,
                            title=f"Invert {socket_id.upper()}",
                            position=(output_x - 240.0, output_y + 58.0 * index),
                        )
                        utility_nodes.append(invert_node)
                        utility_connections.append(
                            GraphConnection(
                                connection_id=make_connection_id(),
                                source_node_id=source_node.node_id,
                                source_socket_id=source_socket_id,
                                target_node_id=invert_node.node_id,
                                target_socket_id="in",
                            )
                        )
                        source_node = invert_node
                        source_socket_id = "out"
                else:
                    constant_node = create_graph_node(
                        NodeType.CONSTANT_CHANNEL,
                        title=f"{socket_id.upper()} {resolved_fill}",
                        position=(output_x - 240.0, output_y + 58.0 * index),
                        properties={"value": resolved_fill},
                    )
                    utility_nodes.append(constant_node)
                    source_node = constant_node
                    source_socket_id = "out"

                output_connections.append(
                    GraphConnection(
                        connection_id=make_connection_id(),
                        source_node_id=source_node.node_id,
                        source_socket_id=source_socket_id,
                        target_node_id=node.node_id,
                        target_socket_id=socket_id,
                    )
                )

        return OutputProfilePlan(
            profile=profile,
            mode=mode,
            output_title=node_title,
            properties=properties,
            target_label=target_label,
            target_filename=target_filename,
            description_lines=self._profile_description_lines(profile),
            utility_nodes=utility_nodes,
            utility_connections=utility_connections,
            output_connections=output_connections,
            summary_lines=self._profile_plan_summary_lines(
                utility_nodes,
                utility_connections,
                output_connections,
            ),
        )

    def _refresh_properties_profile_summary(self, node: GraphNode | None = None) -> None:
        selected_node = node or self._scene.selected_node()
        if selected_node is None or selected_node.node_type is not NodeType.OUTPUT_RGBA:
            self.properties_panel.set_profile_summary("")
            return
        self.properties_panel.set_profile_summary(self._profile_summary_text(selected_node))

    def _profile_summary_text(self, node: GraphNode) -> str:
        plan = self._build_output_profile_plan(node)
        lines = [f"{plan.target_label} -> {plan.mode.value.upper()} / {plan.target_filename}"]
        lines.extend(plan.description_lines)
        if any(connection.target_node_id == node.node_id for connection in self.project.graph.connections):
            lines.append("Existing input wires will be replaced.")
        if plan.summary_lines:
            lines.extend(plan.summary_lines)
        else:
            lines.append("No compatible output channel rules.")
        return "\n".join(lines)

    def _profile_status_text(self, plan: OutputProfilePlan) -> str:
        mappings = "; ".join(plan.summary_lines)
        if len(mappings) > 180:
            mappings = f"{mappings[:177]}..."
        return f"{plan.target_label}: {mappings}" if mappings else f"{plan.target_label}: no channel mappings."

    def _profile_plan_summary_lines(
        self,
        utility_nodes: list[GraphNode],
        utility_connections: list[GraphConnection],
        output_connections: list[GraphConnection],
    ) -> tuple[str, ...]:
        node_by_id = {node.node_id: node for node in self.project.graph.nodes}
        node_by_id.update({node.node_id: node for node in utility_nodes})
        utility_input_by_target = {
            (connection.target_node_id, connection.target_socket_id): connection
            for connection in utility_connections
        }
        channel_order = {"r": 0, "g": 1, "b": 2, "a": 3}
        lines: list[str] = []
        for connection in sorted(
            output_connections,
            key=lambda item: channel_order.get(item.target_socket_id, 99),
        ):
            source_text = self._profile_source_text(
                connection.source_node_id,
                connection.source_socket_id,
                node_by_id,
                utility_input_by_target,
            )
            lines.append(f"{connection.target_socket_id.upper()} <- {source_text}")
        return tuple(lines)

    @staticmethod
    def _profile_description_lines(profile: OutputProfile) -> tuple[str, ...]:
        return OUTPUT_PROFILE_DESCRIPTIONS.get(profile, ())

    def _auto_output_title(self, profile: OutputProfile) -> str:
        return OUTPUT_PROFILE_AUTO_TITLE.get(profile, "Packed Output")

    def _auto_output_filename(self, profile: OutputProfile, node: GraphNode) -> str:
        current_name = str(node.properties.get("filename", "")).strip()
        current_path = str(node.properties.get("output_path", "")).strip()
        extension = ".png"
        source_name = current_name or current_path
        if source_name:
            candidate_suffix = Path(source_name).suffix.lower()
            if candidate_suffix:
                extension = candidate_suffix
        base_name = Path(OUTPUT_PROFILE_FILENAMES.get(profile, "packed_rgba.png")).stem
        return f"{base_name}{extension}"

    def _should_autorename_output_title(self, node: GraphNode) -> bool:
        if bool(node.properties.get("title_auto_generated", False)):
            return True
        title = node.title.strip()
        if not title:
            return True
        if title.lower().startswith("output "):
            return True
        return title == "Output"

    def _should_autorename_output_filename(self, node: GraphNode) -> bool:
        filename = str(node.properties.get("filename", "")).strip()
        if not filename:
            return True
        default_names = {value.casefold() for value in OUTPUT_PROFILE_FILENAMES.values()}
        if filename.casefold() in default_names:
            return True
        if filename.casefold().startswith("graph_output_"):
            return True
        if filename.casefold().startswith("output_"):
            return True
        if filename.casefold() in {"packed.png", "packed_rgba.png"}:
            return True
        return False

    def _profile_source_text(
        self,
        node_id: str,
        socket_id: str,
        node_by_id: dict[str, GraphNode],
        utility_input_by_target: dict[tuple[str, str], GraphConnection],
    ) -> str:
        node = node_by_id.get(node_id)
        socket_label = socket_id.upper() if socket_id in {"r", "g", "b", "a"} else socket_id
        if node is None:
            return f"{node_id}.{socket_label}"
        if node.node_type is NodeType.CONSTANT_CHANNEL:
            return f"Constant {self._coerce_channel_value(node.properties.get('value'), 255)}"
        if node.node_type is NodeType.INVERT_CHANNEL:
            input_connection = utility_input_by_target.get((node.node_id, "in"))
            if input_connection is None:
                return "Invert"
            inner_text = self._profile_source_text(
                input_connection.source_node_id,
                input_connection.source_socket_id,
                node_by_id,
                utility_input_by_target,
            )
            return f"Invert({inner_text})"
        if node.node_type is NodeType.TEXTURE_INPUT:
            path_name = Path(str(node.properties.get("path", ""))).name
            source_name = path_name or node.title
            return f"{source_name}.{socket_label}"
        return f"{node.title}.{socket_label}"

    @staticmethod
    def _coerce_channel_value(value: object, default: int) -> int:
        try:
            return max(0, min(255, int(value)))
        except (TypeError, ValueError):
            return default

    def _profile_rules(
        self,
        profile: OutputProfile,
    ) -> tuple[tuple[str, tuple[PackSourceCandidate, ...], int | None], ...]:
        if profile is OutputProfile.GENERIC_RGBA:
            return ()
        if profile is OutputProfile.METAHUMAN_REPACK:
            return METAHUMAN_REPACK_RULES
        layout = OUTPUT_PROFILE_PACK_LAYOUTS.get(profile)
        if layout is None:
            return ()
        return tuple(
            (
                rule.channel.lower(),
                rule.candidates,
                rule.fill_value,
            )
            for rule in PACK_LAYOUTS[layout]
        )

    def _generic_rgba_profile_connections(
        self,
        output_node: GraphNode,
        texture_nodes_by_map_type: dict[TextureMapType, GraphNode],
    ) -> tuple[list[GraphConnection], list[GraphNode]]:
        color_node = (
            texture_nodes_by_map_type.get(TextureMapType.BASECOLOR)
            or texture_nodes_by_map_type.get(TextureMapType.EMISSIVE)
            or self._first_generic_texture_node()
        )
        opacity_node = texture_nodes_by_map_type.get(TextureMapType.OPACITY)
        utility_nodes: list[GraphNode] = []

        if color_node is None:
            output_x, output_y = output_node.position
            constants = [
                create_graph_node(
                    NodeType.CONSTANT_CHANNEL,
                    title=f"{socket.upper()} {value}",
                    position=(output_x - 240.0, output_y + 58.0 * index),
                    properties={"value": value},
                )
                for index, (socket, value) in enumerate((("r", 0), ("g", 0), ("b", 0), ("a", 255)))
            ]
            utility_nodes.extend(constants)
            return (
                [
                    GraphConnection(
                        connection_id=make_connection_id(),
                        source_node_id=constant.node_id,
                        source_socket_id="out",
                        target_node_id=output_node.node_id,
                        target_socket_id=socket_id,
                    )
                    for constant, socket_id in zip(constants, ("r", "g", "b", "a"), strict=True)
                ],
                utility_nodes,
            )

        alpha_source_node = opacity_node or color_node
        alpha_socket_id = "r" if opacity_node is not None else "a"
        source_specs = (
            ("r", color_node, "r"),
            ("g", color_node, "g"),
            ("b", color_node, "b"),
            ("a", alpha_source_node, alpha_socket_id),
        )
        return (
            [
                GraphConnection(
                    connection_id=make_connection_id(),
                    source_node_id=source_node.node_id,
                    source_socket_id=source_socket_id,
                    target_node_id=output_node.node_id,
                    target_socket_id=target_socket_id,
                )
                for target_socket_id, source_node, source_socket_id in source_specs
            ],
            utility_nodes,
        )

    def _first_generic_texture_node(self) -> GraphNode | None:
        for node in self.project.graph.nodes:
            if node.node_type is not NodeType.TEXTURE_INPUT:
                continue
            path = Path(str(node.properties.get("path", "")))
            map_type = detect_texture_map_type(path)
            if map_type in (TextureMapType.NORMAL, TextureMapType.HEIGHT):
                continue
            return node
        return next(
            (
                node
                for node in self.project.graph.nodes
                if node.node_type is NodeType.TEXTURE_INPUT
            ),
            None,
        )

    def _resolve_separate_profile_source(
        self,
        candidates: tuple[PackSourceCandidate, ...],
        texture_nodes_by_map_type: dict[TextureMapType, GraphNode],
    ) -> tuple[GraphNode | None, str, bool]:
        for candidate in candidates:
            source_node = texture_nodes_by_map_type.get(candidate.map_type)
            if source_node is not None:
                return source_node, "r", candidate.invert
        return None, "", False

    def _resolve_packed_profile_source(
        self,
        profile: OutputProfile,
        socket_id: str,
        candidates: tuple[PackSourceCandidate, ...],
        packed_texture_nodes: list[tuple[str, GraphNode]],
    ) -> tuple[GraphNode | None, str, bool]:
        semantic = self._profile_socket_semantic(profile, socket_id, candidates)
        if semantic is None:
            return None, "", False
        for pack_kind, texture_node in packed_texture_nodes:
            mapping = PACKED_SOURCE_CHANNELS.get(pack_kind, {})
            source = mapping.get(semantic)
            if source is None:
                continue
            source_socket_id, invert = source
            return texture_node, source_socket_id, invert
        return None, "", False

    def _profile_fill_value(
        self,
        profile: OutputProfile,
        socket_id: str,
        candidates: tuple[PackSourceCandidate, ...],
        fill_value: int | None,
    ) -> int:
        if fill_value is not None:
            return fill_value
        if candidates:
            return DEFAULT_PROFILE_FILL.get(candidates[0].map_type, 255)
        if profile is OutputProfile.UNITY_URP and socket_id in {"g", "b"}:
            return 0
        return 255

    @staticmethod
    def _profile_socket_semantic(
        profile: OutputProfile,
        socket_id: str,
        candidates: tuple[PackSourceCandidate, ...],
    ) -> TextureMapType | str | None:
        if candidates:
            return candidates[0].map_type
        if profile is OutputProfile.UNITY_HDRP and socket_id == "b":
            return PACKED_DETAIL_MASK
        return None

    def _texture_nodes_by_map_type(self) -> dict[TextureMapType, GraphNode]:
        asset_map_types = {
            self._path_key(item.path): item.effective_map_type
            for item in self._assets
        }
        nodes_by_map_type: dict[TextureMapType, GraphNode] = {}
        for node in self.project.graph.nodes:
            if node.node_type is not NodeType.TEXTURE_INPUT:
                continue
            path = Path(str(node.properties.get("path", "")))
            map_type = asset_map_types.get(self._path_key(path))
            if map_type is None:
                map_type = detect_texture_map_type(path)
            if map_type is TextureMapType.UNKNOWN:
                continue
            nodes_by_map_type.setdefault(map_type, node)
        return nodes_by_map_type

    def _packed_texture_nodes(self) -> list[tuple[str, GraphNode]]:
        packed_nodes: list[tuple[str, GraphNode]] = []
        for node in self.project.graph.nodes:
            if node.node_type is not NodeType.TEXTURE_INPUT:
                continue
            pack_kind = self._detect_packed_texture_kind(
                Path(str(node.properties.get("path", ""))).stem
            )
            if pack_kind:
                packed_nodes.append((pack_kind, node))
        return packed_nodes

    @staticmethod
    def _detect_packed_texture_kind(stem: str) -> str:
        normalized = stem.lower().replace("-", "_").replace(" ", "_")
        collapsed = normalized.replace("_", "")
        if "maskmap" in collapsed or "hdrpmask" in collapsed:
            return "unity_hdrp_mask"
        if "orm" in collapsed or "occlusionroughnessmetallic" in collapsed:
            return "unreal_orm"
        if "mra" in collapsed or "metallicroughnessao" in collapsed:
            return "unreal_mra"
        if "rma" in collapsed or "roughnessmetallicao" in collapsed:
            return "unreal_rma"
        if "metallicsmoothness" in collapsed or "urpmask" in collapsed:
            return "unity_urp_mask"
        return ""

    @staticmethod
    def _path_key(path: Path) -> str:
        try:
            return str(path.resolve()).lower()
        except OSError:
            return str(path).lower()

    def _rebuild_asset_watchers(self) -> None:
        pending_change_keys = set(self._pending_asset_change_keys)
        texture_paths: dict[str, str] = {}
        directory_keys: dict[str, set[str]] = {}
        directory_paths: dict[str, str] = {}
        file_keys: dict[str, str] = {}

        if self._auto_watch_enabled:
            for node in self.project.graph.nodes:
                if node.node_type is not NodeType.TEXTURE_INPUT:
                    continue
                raw_path = str(node.properties.get("path", "")).strip()
                if not raw_path:
                    continue
                texture_path = Path(raw_path)
                path_key = self._path_key(texture_path)
                texture_paths[path_key] = str(texture_path)
                file_keys[path_key] = str(texture_path)
                directory = texture_path.parent if str(texture_path.parent) else texture_path
                directory_key = self._path_key(directory)
                directory_paths[directory_key] = str(directory)
                directory_keys.setdefault(directory_key, set()).add(path_key)

        self._replace_watched_paths(self._asset_watcher.files(), file_keys.values(), is_file=True)
        self._replace_watched_paths(
            self._asset_watcher.directories(),
            directory_paths.values(),
            is_file=False,
        )
        self._watched_texture_keys = texture_paths
        self._watched_file_keys = file_keys
        self._watched_directory_paths = directory_paths
        self._watched_directory_keys = directory_keys
        self._pending_asset_change_keys = {
            path_key
            for path_key in pending_change_keys
            if path_key in self._watched_texture_keys
        }
        self.watched_paths_changed.emit(self.watched_texture_paths())

    def _clear_asset_watchers(self) -> None:
        self._asset_watch_timer.stop()
        self._pending_asset_change_keys.clear()
        existing_files = self._asset_watcher.files()
        existing_directories = self._asset_watcher.directories()
        if existing_files:
            self._asset_watcher.removePaths(existing_files)
        if existing_directories:
            self._asset_watcher.removePaths(existing_directories)
        self._watched_texture_keys.clear()
        self._watched_file_keys.clear()
        self._watched_directory_paths.clear()
        self._watched_directory_keys.clear()
        self.watched_paths_changed.emit(())

    def _replace_watched_paths(
        self,
        existing_paths: list[str],
        target_paths: Iterable[str],
        *,
        is_file: bool,
    ) -> None:
        target_list = list(dict.fromkeys(path for path in target_paths if str(path).strip()))
        existing_keys = {self._path_key(Path(path)): path for path in existing_paths if str(path).strip()}
        target_keys = {self._path_key(Path(path)): path for path in target_list}
        remove_paths = [
            existing_keys[key]
            for key in existing_keys.keys() - target_keys.keys()
        ]
        add_paths = [
            path
            for key, path in target_keys.items()
            if key not in existing_keys
        ]
        if remove_paths:
            self._asset_watcher.removePaths(remove_paths)
        if add_paths:
            filtered_paths = [
                path
                for path in add_paths
                if (Path(path).is_file() if is_file else Path(path).is_dir())
            ]
            if filtered_paths:
                self._asset_watcher.addPaths(filtered_paths)

    def _on_watched_directory_changed(self, directory: str) -> None:
        directory_key = self._path_key(Path(directory))
        changed_keys = self._watched_directory_keys.get(directory_key, set())
        if not changed_keys:
            return
        self._pending_asset_change_keys.update(changed_keys)
        self._asset_watch_timer.start()
        if self._auto_watch_enabled:
            self._rebuild_asset_watchers()

    def _on_watched_file_changed(self, path: str) -> None:
        path_key = self._path_key(Path(path))
        if path_key in self._watched_file_keys:
            self._pending_asset_change_keys.add(path_key)
            self._asset_watch_timer.start()
        if self._auto_watch_enabled:
            self._rebuild_asset_watchers()

    def _emit_pending_asset_changes(self) -> None:
        if not self._pending_asset_change_keys:
            return
        changed_paths = tuple(
            Path(self._watched_texture_keys[path_key])
            for path_key in sorted(self._pending_asset_change_keys)
            if path_key in self._watched_texture_keys
        )
        self._pending_asset_change_keys.clear()
        if changed_paths:
            self.assets_changed.emit(changed_paths)

    def _connections_for_new_node_context(
        self,
        node: GraphNode,
        context: object | None,
    ) -> list[GraphConnection]:
        if not isinstance(context, PortItem):
            return []
        if context.direction is SocketDirection.OUTPUT:
            target_socket_id = self._first_socket_id(
                node.node_type,
                SocketDirection.INPUT,
                context.socket_type,
            )
            if target_socket_id is None:
                return []
            return [
                GraphConnection(
                    connection_id=make_connection_id(),
                    source_node_id=context.node_item.node.node_id,
                    source_socket_id=context.socket_id,
                    target_node_id=node.node_id,
                    target_socket_id=target_socket_id,
                )
            ]

        source_socket_id = self._first_socket_id(
            node.node_type,
            SocketDirection.OUTPUT,
            context.socket_type,
        )
        if source_socket_id is None:
            return []
        return [
            GraphConnection(
                connection_id=make_connection_id(),
                source_node_id=node.node_id,
                source_socket_id=source_socket_id,
                target_node_id=context.node_item.node.node_id,
                target_socket_id=context.socket_id,
            )
        ]

    def _copy_selection(self) -> None:
        node_ids = set(self._scene.selected_node_ids())
        if not node_ids:
            self.status_message.emit("No graph nodes selected.")
            return
        self._clipboard_nodes = [
            clone_graph_node(node)
            for node in self.project.graph.nodes
            if node.node_id in node_ids
        ]
        self._clipboard_connections = [
            clone_graph_connection(connection)
            for connection in self.project.graph.connections
            if connection.source_node_id in node_ids and connection.target_node_id in node_ids
        ]
        self.status_message.emit(f"Copied nodes: {len(self._clipboard_nodes)}")

    def _paste_clipboard(self) -> None:
        if not self._clipboard_nodes:
            self.status_message.emit("Clipboard is empty.")
            return
        id_map = {node.node_id: make_node_id() for node in self._clipboard_nodes}
        pasted_nodes: list[GraphNode] = []
        for node in self._clipboard_nodes:
            pasted = clone_graph_node(node, node_id=id_map[node.node_id])
            pasted.position = (pasted.position[0] + 36.0, pasted.position[1] + 36.0)
            pasted_nodes.append(pasted)
        pasted_connections = [
            clone_graph_connection(
                connection,
                connection_id=make_connection_id(),
                source_node_id=id_map[connection.source_node_id],
                target_node_id=id_map[connection.target_node_id],
            )
            for connection in self._clipboard_connections
            if connection.source_node_id in id_map and connection.target_node_id in id_map
        ]
        self._push_graph_command(
            AddNodesCommand(
                self.project.graph,
                self._on_graph_command_changed,
                pasted_nodes,
                pasted_connections,
                text="Paste nodes",
            ),
            select_node_ids=[node.node_id for node in pasted_nodes],
        )
        self._clipboard_nodes = [clone_graph_node(node) for node in pasted_nodes]
        self._clipboard_connections = [
            clone_graph_connection(connection)
            for connection in pasted_connections
        ]

    def _duplicate_selection(self) -> None:
        self._copy_selection()
        self._paste_clipboard()

    def has_unsaved_changes(self) -> bool:
        try:
            return not self.undo_stack.isClean()
        except RuntimeError:
            return False

    def _on_undo_stack_changed(self, *_args: object) -> None:
        self._update_project_label()

    def _update_project_label(self) -> None:
        try:
            suffix = "*" if self.has_unsaved_changes() else ""
            self.project_label.setText(f"{self.project.name}{suffix}")
        except RuntimeError:
            return

    @staticmethod
    def _first_channel_input_socket_id(node_type: NodeType) -> str | None:
        return GraphWorkspace._first_socket_id(
            node_type,
            SocketDirection.INPUT,
            SocketType.CHANNEL,
        )

    @staticmethod
    def _first_channel_output_socket_id(node_type: NodeType) -> str | None:
        return GraphWorkspace._first_socket_id(
            node_type,
            SocketDirection.OUTPUT,
            SocketType.CHANNEL,
        )

    @staticmethod
    def _first_socket_id(
        node_type: NodeType,
        direction: SocketDirection,
        socket_type: SocketType,
    ) -> str | None:
        return next(
            (
                socket.socket_id
                for socket in socket_definitions(node_type)
                if socket.direction is direction and socket.socket_type is socket_type
            ),
            None,
        )

    def _on_graph_changed(self) -> None:
        self._on_graph_command_changed(True)

    def _on_properties_node_selected(self, node: GraphNode | None) -> None:
        self.properties_panel.set_node(node)
        self._refresh_properties_profile_summary(node)

    def _on_node_selected(self, node: GraphNode | None) -> None:
        if self._active_display_node() is None:
            self._preview_view_node(node)

    def _on_node_double_clicked(self, node: GraphNode | None) -> None:
        if node is None:
            return
        if node.node_type is NodeType.OUTPUT_RGBA:
            self._preview_output_node(node)
            return
        if node.node_type is NodeType.PBR_SHADER:
            self._preview_display_node(node)
            return
        if node.node_type is NodeType.VIEW:
            self._preview_view_node(node)

    def _on_display_flag_clicked(self, node: GraphNode | None) -> None:
        if node is None:
            return
        self._push_graph_command(
            SetDisplayFlagCommand(
                self.project.graph,
                self._on_graph_command_changed,
                node.node_id,
            ),
            select_node_ids=[node.node_id],
        )

    def _on_render_flag_clicked(self, node: GraphNode | None) -> None:
        if node is None or node.node_type is not NodeType.OUTPUT_RGBA:
            return
        properties = dict(node.properties)
        properties["enabled"] = not bool(properties.get("enabled", True))
        self._push_graph_command(
            SetNodeStateCommand(
                self.project.graph,
                self._on_graph_command_changed,
                node,
                title=node.title,
                properties=properties,
                text="Toggle render flag",
                needs_rebuild=False,
            ),
            select_node_ids=[node.node_id],
        )
        state = "on" if properties.get("enabled", True) else "off"
        self.status_message.emit(f"{node.title}: render flag {state}.")

    def _on_enable_flag_clicked(self, node: GraphNode | None) -> None:
        if node is None or not node_has_enable_flag(node.node_type):
            return
        properties = dict(node.properties)
        properties["enabled"] = not bool(properties.get("enabled", True))
        self._push_graph_command(
            SetNodeStateCommand(
                self.project.graph,
                self._on_graph_command_changed,
                node,
                title=node.title,
                properties=properties,
                text="Toggle node enabled",
                needs_rebuild=False,
            ),
            select_node_ids=[node.node_id],
        )
        state = "enabled" if properties.get("enabled", True) else "bypassed"
        self.status_message.emit(f"{node.title}: {state}.")

    def _on_node_reset_clicked(self, node: GraphNode | None) -> None:
        if node is None or not node_has_resettable_parameters(node.node_type):
            return
        shadow = clone_graph_node(node)
        reset_node_parameters(shadow)
        self._push_graph_command(
            SetNodeStateCommand(
                self.project.graph,
                self._on_graph_command_changed,
                node,
                title=node.title,
                properties=shadow.properties,
                text="Reset node parameters",
                needs_rebuild=False,
            ),
            select_node_ids=[node.node_id],
        )
        self.status_message.emit(f"{node.title}: parameters reset.")

    def _refresh_node_flags(self) -> None:
        for item in self._scene.node_items.values():
            item.refresh_flags()

    def _invalidate_preview_from_node(self, node: GraphNode) -> None:
        self._preview_cache.invalidate_node_and_downstream(self.project.graph, node.node_id)
        if self._preview_inflight_generation:
            self._preview_dirty_node_ids.add(node.node_id)

    def _active_display_node(self) -> GraphNode | None:
        return next(
            (
                node
                for node in self.project.graph.nodes
                if bool(node.properties.get("display", False))
            ),
            None,
        )

    def _preview_active_display_node(self) -> None:
        display_node = self._active_display_node()
        if display_node is not None:
            self._preview_display_node(display_node)

    def _preview_display_node(self, node: GraphNode) -> None:
        self._request_preview_node(node, preview_mode=PREVIEW_MODE_FULL)

    def _preview_view_node(self, node: GraphNode | None) -> None:
        if node is None or node.node_type is not NodeType.VIEW:
            return
        if incoming_connection(
            self.project.graph,
            target_node_id=node.node_id,
            target_socket_id="in",
        ) is None:
            return
        self._request_preview_node(node, preview_mode=PREVIEW_MODE_FULL)

    def _preview_output_node(self, node: GraphNode) -> None:
        mode = self._output_mode_label(node)
        self._request_preview_node(node, mode_label=mode, preview_mode=PREVIEW_MODE_FULL)

    def _request_preview_node(
        self,
        node: GraphNode,
        *,
        mode_label: str = "",
        preview_mode: str = PREVIEW_MODE_FULL,
        property_overrides: dict[str, dict] | None = None,
    ) -> None:
        self._last_preview_node_id = node.node_id
        self._last_preview_mode_label = mode_label
        self._last_preview_quality_request = preview_mode
        self._preview_generation += 1
        generation = self._preview_generation
        if self._preview_inflight_generation:
            if self._preview_active_worker is not None:
                self._preview_active_worker.cancel()
            self._pending_preview_request = (
                node.node_id,
                mode_label,
                preview_mode,
                deepcopy(property_overrides) if property_overrides else None,
            )
            return
        self._start_preview_request(
            node,
            mode_label,
            generation,
            preview_mode,
            property_overrides,
        )

    def _start_preview_request(
        self,
        node: GraphNode,
        mode_label: str,
        generation: int,
        preview_mode: str,
        property_overrides: dict[str, dict] | None = None,
    ) -> None:
        self._preview_inflight_generation = generation
        snapshot = deepcopy(self.project)
        if property_overrides:
            for graph_node in snapshot.graph.nodes:
                properties = property_overrides.get(graph_node.node_id)
                if properties is not None:
                    graph_node.properties = deepcopy(properties)
        node_snapshot = next(
            (
                graph_node
                for graph_node in snapshot.graph.nodes
                if graph_node.node_id == node.node_id
            ),
            None,
        )
        if node_snapshot is None:
            self._preview_inflight_generation = 0
            self._preview_active_worker = None
            return
        max_side = self._preview_max_side_for_mode(preview_mode)
        thread = QThread(self)
        worker = GraphPreviewWorker(
            generation,
            snapshot,
            node_snapshot,
            mode_label=mode_label,
            max_side=max_side,
            preview_cache=self._preview_cache,
        )
        worker.moveToThread(thread)
        self._preview_active_worker = worker
        self._preview_threads.append(thread)
        self._preview_workers.append(worker)
        thread.started.connect(worker.run)
        worker.finished.connect(self._on_preview_worker_finished)
        worker.failed.connect(self._on_preview_worker_failed)
        worker.canceled.connect(self._on_preview_worker_canceled)
        worker.completed.connect(thread.quit)
        worker.completed.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(lambda current=thread: self._forget_preview_thread(current))
        thread.finished.connect(lambda current=worker: self._forget_preview_worker(current))
        thread.start()
        self.status_message.emit(f"{node.title}: rendering preview...")

    def _refresh_last_preview_request(self) -> bool:
        if not self._last_preview_node_id:
            return False
        node = next(
            (
                graph_node
                for graph_node in self.project.graph.nodes
                if graph_node.node_id == self._last_preview_node_id
            ),
            None,
        )
        if node is None:
            self._last_preview_node_id = None
            self._last_preview_mode_label = ""
            self._last_preview_quality_request = PREVIEW_MODE_FULL
            return False
        self._request_preview_node(
            node,
            mode_label=self._last_preview_mode_label,
            preview_mode=self._last_preview_quality_request,
        )
        return True

    def _on_preview_worker_finished(
        self,
        generation: int,
        image: object,
        title: str,
        meta: str,
        node_id: str,
    ) -> None:
        self._preview_inflight_generation = 0
        self._preview_active_worker = None
        self._reapply_pending_preview_invalidation()
        pending = self._pending_preview_request
        self._pending_preview_request = None
        if generation != self._preview_generation:
            if pending is not None:
                self._restart_pending_preview(pending)
            return
        if isinstance(image, PbrMaterialData):
            self.pbr_material_requested.emit(image, title, meta, node_id)
        else:
            self.preview_image_requested.emit(image, title, meta, node_id)
        if pending is not None:
            self._restart_pending_preview(pending)

    def _on_preview_worker_failed(
        self,
        generation: int,
        title: str,
        message: str,
        node_id: str,
    ) -> None:
        self._preview_inflight_generation = 0
        self._preview_active_worker = None
        self._reapply_pending_preview_invalidation()
        pending = self._pending_preview_request
        self._pending_preview_request = None
        if generation != self._preview_generation:
            if pending is not None:
                self._restart_pending_preview(pending)
            return
        self.preview_failed.emit(title, message, node_id)
        self.status_message.emit(f"{title}: preview failed: {message}")
        if pending is not None:
            self._restart_pending_preview(pending)

    def _on_preview_worker_canceled(self, generation: int) -> None:
        self._preview_inflight_generation = 0
        self._preview_active_worker = None
        self._reapply_pending_preview_invalidation()
        pending = self._pending_preview_request
        self._pending_preview_request = None
        if pending is not None:
            self._restart_pending_preview(pending)

    def _restart_pending_preview(
        self,
        pending: tuple[str, str, str, dict[str, dict] | None],
    ) -> None:
        node_id, mode_label, preview_mode, property_overrides = pending
        node = next(
            (
                graph_node
                for graph_node in self.project.graph.nodes
                if graph_node.node_id == node_id
            ),
            None,
        )
        if node is None:
            return
        self._start_preview_request(
            node,
            mode_label,
            self._preview_generation,
            preview_mode,
            property_overrides,
        )

    def _reapply_pending_preview_invalidation(self) -> None:
        if self._preview_full_reset_pending:
            self._preview_cache.clear()
        else:
            for node_id in self._preview_dirty_node_ids:
                self._preview_cache.invalidate_node_and_downstream(
                    self.project.graph,
                    node_id,
                )
        self._preview_dirty_node_ids.clear()
        self._preview_full_reset_pending = False

    def _on_preview_refresh_requested(self, node: GraphNode, preview_mode: object) -> None:
        if not isinstance(preview_mode, str):
            preview_mode = PREVIEW_MODE_FULL
        preview_target = self._active_display_node() or node
        mode_label = self._last_preview_mode_label if preview_target.node_id == self._last_preview_node_id else ""
        self._request_preview_node(preview_target, mode_label=mode_label, preview_mode=preview_mode)

    def _on_transient_preview_requested(
        self,
        node: GraphNode,
        properties: object,
        preview_mode: object,
    ) -> None:
        if not isinstance(properties, dict):
            return
        if not isinstance(preview_mode, str):
            preview_mode = PREVIEW_MODE_DRAFT
        item = self._scene.node_items.get(node.node_id)
        if item is not None:
            item.set_color_preview(properties)
        self._invalidate_preview_from_node(node)
        preview_target = self._active_display_node() or node
        mode_label = (
            self._last_preview_mode_label
            if preview_target.node_id == self._last_preview_node_id
            else ""
        )
        self._request_preview_node(
            preview_target,
            mode_label=mode_label,
            preview_mode=preview_mode,
            property_overrides={node.node_id: properties},
        )

    def _preview_max_side_for_mode(self, preview_mode: str) -> int:
        if preview_mode == PREVIEW_MODE_DRAFT:
            return min(self._preview_cache.max_side, DRAFT_PREVIEW_MAX_SIDE)
        return self._preview_cache.max_side

    def _forget_preview_thread(self, thread: QThread) -> None:
        if thread in self._preview_threads:
            self._preview_threads.remove(thread)

    def _forget_preview_worker(self, worker: GraphPreviewWorker) -> None:
        if worker in self._preview_workers:
            self._preview_workers.remove(worker)

    @staticmethod
    def _output_mode_label(node: GraphNode) -> str:
        try:
            return OutputMode(str(node.properties.get("mode", OutputMode.RGBA.value))).value.upper()
        except ValueError:
            return OutputMode.RGBA.value.upper()

    @staticmethod
    def _output_profile(node: GraphNode) -> OutputProfile:
        try:
            return OutputProfile(str(node.properties.get("profile", OutputProfile.GENERIC_RGBA.value)))
        except ValueError:
            return OutputProfile.GENERIC_RGBA

    @staticmethod
    def _status_text(status: ConversionStatus) -> str:
        if status is ConversionStatus.SUCCESS:
            return "Success"
        if status is ConversionStatus.SKIPPED:
            return "Skipped"
        return "Error"

    def minimumSizeHint(self) -> QSize:
        return QSize(360, 260)

    def sizeHint(self) -> QSize:
        return QSize(980, 620)

    def background_job_running(self) -> bool:
        return (
            any(thread.isRunning() for thread in self._preview_threads)
            or self._scene.texture_visual_cache.is_running
            or self._missing_texture_search.is_running
        )

    def shutdown_background_jobs(self, *, wait_ms: int = 0) -> bool:
        self._preview_generation += 1
        self._pending_preview_request = None
        if self._preview_active_worker is not None:
            self._preview_active_worker.cancel()
        for thread in list(self._preview_threads):
            thread.quit()
        self._scene.texture_visual_cache.shutdown(wait_ms=wait_ms)
        self._missing_texture_search.shutdown(wait_ms=wait_ms)
        return not self.background_job_running()

    def closeEvent(self, event) -> None:
        self.shutdown_background_jobs(wait_ms=100)
        super().closeEvent(event)
