from __future__ import annotations

from PyQt6.QtCore import QObject, pyqtSignal


class ApplicationJobCoordinator(QObject):
    """Coordinates jobs that must never write project outputs concurrently."""

    state_changed = pyqtSignal()

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._exclusive_owner: object | None = None

    @property
    def exclusive_job_running(self) -> bool:
        return self._exclusive_owner is not None

    def try_acquire_exclusive(self, owner: object) -> bool:
        if self._exclusive_owner is not None and self._exclusive_owner is not owner:
            return False
        if self._exclusive_owner is owner:
            return True
        self._exclusive_owner = owner
        self.state_changed.emit()
        return True

    def release_exclusive(self, owner: object) -> None:
        if self._exclusive_owner is not owner:
            return
        self._exclusive_owner = None
        self.state_changed.emit()
