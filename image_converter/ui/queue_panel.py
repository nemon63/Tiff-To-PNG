from __future__ import annotations

from PyQt6.QtCore import QSize, pyqtSignal
from PyQt6.QtGui import QDragEnterEvent, QDropEvent
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QSizePolicy,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)

from image_converter.domain.constants import FILE_DIALOG_FILTER
from image_converter.ui.common import QUEUE_HEADERS, _extract_local_paths


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
        self.setObjectName("SectionPanel")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        title_label = QLabel("Очередь конвертации")
        title_label.setObjectName("PanelTitle")
        layout.addWidget(title_label)

        subtitle_label = QLabel("Drop textures or folders, then run the batch export.")
        subtitle_label.setObjectName("PanelSubtitle")
        subtitle_label.setWordWrap(True)
        layout.addWidget(subtitle_label)

        controls_row = QHBoxLayout()
        controls_row.setSpacing(6)
        self.add_files_button = QPushButton("+ Files")
        self.add_files_button.clicked.connect(self._pick_files)
        controls_row.addWidget(self.add_files_button)

        self.add_folder_button = QPushButton("+ Folder")
        self.add_folder_button.clicked.connect(self._pick_folder)
        controls_row.addWidget(self.add_folder_button)

        self.remove_selected_button = QPushButton("Remove")
        self.remove_selected_button.clicked.connect(self.remove_requested.emit)
        controls_row.addWidget(self.remove_selected_button)

        self.clear_button = QPushButton("Clear")
        self.clear_button.setObjectName("DangerButton")
        self.clear_button.clicked.connect(self.clear_requested.emit)
        controls_row.addWidget(self.clear_button)
        controls_row.addStretch(1)
        layout.addLayout(controls_row)

        self.drop_hint = QLabel("Drop files or folders here.")
        self.drop_hint.setObjectName("DropHint")
        self.drop_hint.setWordWrap(True)
        layout.addWidget(self.drop_hint)

        self.summary_label = QLabel("Очередь пуста")
        self.summary_label.setObjectName("SummaryText")
        layout.addWidget(self.summary_label)

        self.table = QueueTableWidget()
        self.table.setColumnCount(len(QUEUE_HEADERS))
        self.table.setHorizontalHeaderLabels(QUEUE_HEADERS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setMinimumSectionSize(48)
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.resizeSection(0, 150)
        header.resizeSection(1, 90)
        header.resizeSection(2, 80)
        header.resizeSection(3, 90)
        header.resizeSection(4, 150)
        header.resizeSection(5, 180)
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

    def minimumSizeHint(self) -> QSize:
        return QSize(240, 180)

    def sizeHint(self) -> QSize:
        return QSize(720, 360)
