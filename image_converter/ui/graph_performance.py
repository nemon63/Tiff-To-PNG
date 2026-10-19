from __future__ import annotations

from PyQt6.QtCore import QTimer, Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractItemView, QHeaderView, QLabel, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from image_converter.services.graph_profiling import GraphEvaluationProfile


class GraphPerformancePanel(QWidget):
    node_requested = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.profile: GraphEvaluationProfile | None = None
        self._profile_dirty = False
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(100)
        self._refresh_timer.timeout.connect(self._refresh_if_visible)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        self.summary = QLabel()
        self.summary.setTextFormat(Qt.TextFormat.PlainText)
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        self.status = QLabel()
        self.status.setObjectName("SummaryText")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(
            ["Node", "Output", "Self ms", "Branch ms", "Cache H/M", "Size", "Calls"]
        )
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setWordWrap(False)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        # Keep node names readable in a narrow dock; scroll the metrics instead
        # of squeezing the first column to the remaining few pixels.
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        self.table.setColumnWidth(0, 170)
        self.table.cellDoubleClicked.connect(self._select_node)
        layout.addWidget(self.table, 1)
        explanation = QLabel(
            "Self: this node only. Branch: includes upstream work; do not add branch times.\n"
            "Cache H/M: image-cache hits/misses. Double-click a row to select its node.\n"
            "CPU wall time includes loading and copies. PBR upload/draw: enable Timings in PBR Preview."
        )
        explanation.setObjectName("SummaryText")
        explanation.setWordWrap(True)
        layout.addWidget(explanation)
        self.clear_profile()

    def clear_profile(self) -> None:
        self._refresh_timer.stop()
        self._profile_dirty = False
        self.profile = None
        self.table.setRowCount(0)
        self.summary.setText("No completed graph preview yet.")
        self.status.clear()

    def mark_pending(self, message: str = "Rendering; rows below belong to the previous preview.") -> None:
        self.status.setText(message)

    def set_profile(self, profile: GraphEvaluationProfile) -> None:
        self.profile = profile
        self._profile_dirty = True
        self.status.clear()
        # Do not rebuild a hidden table on every slider update. While visible,
        # coalesce reports to at most ten table refreshes per second.
        if self.isVisible() and not self._refresh_timer.isActive():
            self._refresh_timer.start()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._refresh_if_visible()

    def _refresh_if_visible(self) -> None:
        if self.isVisible() and self._profile_dirty:
            self._render_profile()

    def _render_profile(self) -> None:
        profile = self.profile
        if profile is None:
            return
        self._profile_dirty = False
        self.summary.setText(
            f"Last preview: {profile.total_ms:.2f} ms CPU · limit {profile.max_side} px\n"
            "Sorted by self time (slowest first)."
        )
        self.table.setRowCount(len(profile.timings))
        for row, timing in enumerate(profile.timings):
            size = " × ".join(str(value) for value in timing.resolution) if timing.resolution else "—"
            cache = (
                f"{timing.cache_hits}/{timing.cache_misses}"
                if timing.cache_hits or timing.cache_misses else "—"
            )
            values = (
                timing.title, timing.socket_id, f"{timing.own_ms:.2f}",
                f"{timing.branch_ms:.2f}", cache, size, str(timing.calls),
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, timing.node_id)
                item.setToolTip(
                    f"{timing.title} · {timing.socket_id}\n"
                    f"{timing.calls} evaluation(s), {timing.cache_hits} cache hit(s), "
                    f"{timing.cache_misses} cache miss(es)\n"
                    "preview = final assembly; socket rows = requested evaluations."
                )
                self.table.setItem(row, column, item)

    def _select_node(self, row: int, _column: int) -> None:
        item = self.table.item(row, 0)
        if item is not None:
            self.node_requested.emit(str(item.data(Qt.ItemDataRole.UserRole)))
