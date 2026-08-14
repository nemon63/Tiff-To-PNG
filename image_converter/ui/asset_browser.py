from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QItemSelectionModel, QMimeData, QSize, Qt, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QDrag, QIcon, QPixmap
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QGridLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from image_converter.domain.models import QueueItem
from image_converter.ui.common import _map_type_label, _qimage_from_pil


class AssetTableWidget(QTableWidget):
    asset_dropped = pyqtSignal(str)
    remove_requested = pyqtSignal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setDragEnabled(True)
        self.setAcceptDrops(False)

    def startDrag(self, supported_actions: Qt.DropAction) -> None:
        del supported_actions
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

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Delete:
            self.remove_requested.emit()
            event.accept()
            return
        super().keyPressEvent(event)


class GraphAssetsPanel(QFrame):
    files_requested = pyqtSignal()
    folder_requested = pyqtSignal()
    reload_requested = pyqtSignal()
    remove_requested = pyqtSignal()
    preview_requested = pyqtSignal()
    thumbnail_requested = pyqtSignal(object)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._assets: list[QueueItem] = []
        self._rows: list[QueueItem] = []
        self._build_ui()

    @property
    def rows(self) -> tuple[QueueItem, ...]:
        return tuple(self._rows)

    def _build_ui(self) -> None:
        self.setObjectName("SidebarPanel")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        title = QLabel("Assets")
        title.setObjectName("PanelTitle")
        layout.addWidget(title)

        buttons = QGridLayout()
        buttons.setHorizontalSpacing(8)
        buttons.setVerticalSpacing(8)
        self.add_files_button = QPushButton("+ Files")
        self.add_files_button.clicked.connect(
            lambda *_args: self.files_requested.emit()
        )
        buttons.addWidget(self.add_files_button, 0, 0)
        self.add_folder_button = QPushButton("+ Folder")
        self.add_folder_button.clicked.connect(
            lambda *_args: self.folder_requested.emit()
        )
        buttons.addWidget(self.add_folder_button, 0, 1)
        self.reload_button = QPushButton("Reload")
        self.reload_button.clicked.connect(
            lambda *_args: self.reload_requested.emit()
        )
        buttons.addWidget(self.reload_button, 1, 0)
        self.remove_button = QPushButton("Remove")
        self.remove_button.setObjectName("DangerButton")
        self.remove_button.clicked.connect(
            lambda *_args: self.remove_requested.emit()
        )
        buttons.addWidget(self.remove_button, 1, 1)
        layout.addLayout(buttons)

        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Filter assets")
        self.filter_edit.textChanged.connect(self.render)
        layout.addWidget(self.filter_edit)

        self.table = AssetTableWidget()
        self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels(("", "Name", "Type", "Res"))
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setIconSize(QSize(42, 42))
        self.table.itemDoubleClicked.connect(
            lambda *_args: self.preview_requested.emit()
        )
        self.table.remove_requested.connect(
            lambda *_args: self.remove_requested.emit()
        )
        self.table.verticalScrollBar().valueChanged.connect(self.request_visible_thumbnails)
        layout.addWidget(self.table, 1)

        hint = QLabel("Drag an asset into Graph. Double-click opens Preview.")
        hint.setObjectName("SummaryText")
        hint.setWordWrap(True)
        layout.addWidget(hint)

    def set_assets(self, assets: list[QueueItem] | tuple[QueueItem, ...]) -> None:
        self._assets = list(assets)
        self.render()

    def render(self, *_args: object) -> None:
        selected_keys = {self._path_key(item.path) for item in self.selected_items()}
        filter_text = self.filter_edit.text().strip().casefold()
        self._rows = [
            item
            for item in self._assets
            if not filter_text
            or filter_text in item.path.name.casefold()
            or filter_text in _map_type_label(item.effective_map_type).casefold()
        ]
        self.table.setRowCount(len(self._rows))
        for row, item in enumerate(self._rows):
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
                table_item.setData(Qt.ItemDataRole.UserRole, str(item.path))
                self.table.setItem(row, column, table_item)
        self._restore_selection(selected_keys)
        QTimer.singleShot(0, self.request_visible_thumbnails)

    def selected_items(self) -> list[QueueItem]:
        selection_model = self.table.selectionModel()
        if selection_model is None:
            return []
        selected_rows = sorted({index.row() for index in selection_model.selectedRows()})
        return [self._rows[row] for row in selected_rows if row < len(self._rows)]

    def request_visible_thumbnails(self, *_args: object) -> None:
        if not self._rows:
            return
        first_row = self.table.rowAt(0)
        if first_row < 0:
            first_row = 0
        last_row = self.table.rowAt(max(0, self.table.viewport().height() - 1))
        if last_row < 0:
            last_row = min(len(self._rows) - 1, first_row + 50)
        first_row = max(0, first_row - 8)
        last_row = min(len(self._rows) - 1, last_row + 8)
        for row in range(first_row, last_row + 1):
            table_item = self.table.item(row, 0)
            if table_item is None or not table_item.icon().isNull():
                continue
            self.thumbnail_requested.emit(self._rows[row])

    def set_thumbnail(self, path: Path, image: object) -> None:
        if not hasattr(image, "tobytes"):
            return
        key = self._path_key(path)
        icon = QIcon(QPixmap.fromImage(_qimage_from_pil(image)))
        for row, item in enumerate(self._rows):
            if self._path_key(item.path) != key:
                continue
            table_item = self.table.item(row, 0)
            if table_item is not None:
                table_item.setIcon(icon)

    def set_controls_enabled(self, enabled: bool) -> None:
        for widget in (
            self.add_files_button,
            self.add_folder_button,
            self.reload_button,
            self.remove_button,
            self.filter_edit,
            self.table,
        ):
            widget.setEnabled(enabled)

    def _restore_selection(self, keys: set[str]) -> None:
        if not keys:
            return
        selection_model = self.table.selectionModel()
        if selection_model is None:
            return
        first_row: int | None = None
        selection_model.clearSelection()
        for row, item in enumerate(self._rows):
            if self._path_key(item.path) not in keys:
                continue
            model_index = self.table.model().index(row, 0)
            selection_model.select(
                model_index,
                QItemSelectionModel.SelectionFlag.Select
                | QItemSelectionModel.SelectionFlag.Rows,
            )
            if first_row is None:
                first_row = row
        if first_row is not None:
            self.table.setCurrentCell(first_row, 0)

    @staticmethod
    def _path_key(path: Path) -> str:
        try:
            return str(path.resolve(strict=False)).casefold()
        except OSError:
            return str(path).casefold()
