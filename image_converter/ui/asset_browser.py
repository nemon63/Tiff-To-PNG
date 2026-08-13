from __future__ import annotations

from PyQt6.QtCore import QMimeData, Qt, QUrl, pyqtSignal
from PyQt6.QtGui import QDrag
from PyQt6.QtWidgets import QTableWidget, QWidget


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
