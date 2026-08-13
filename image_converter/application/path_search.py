from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from threading import Event

from PyQt6.QtCore import QObject, QThread, pyqtSignal


@dataclass(slots=True, frozen=True)
class MissingTextureSearchRequest:
    root: Path
    names: tuple[str, ...]


class _MissingTextureSearchWorker(QObject):
    finished = pyqtSignal(object, object)
    failed = pyqtSignal(object, str)
    completed = pyqtSignal()

    def __init__(self, request: MissingTextureSearchRequest):
        super().__init__()
        self._request = request
        self._cancelled = Event()

    def cancel(self) -> None:
        self._cancelled.set()

    def run(self) -> None:
        try:
            wanted = {name.casefold() for name in self._request.names if name}
            matches: dict[str, Path] = {}
            for candidate in self._request.root.rglob("*"):
                if self._cancelled.is_set():
                    return
                if not candidate.is_file():
                    continue
                key = candidate.name.casefold()
                if key in wanted:
                    matches.setdefault(key, candidate)
                    if len(matches) == len(wanted):
                        break
            if not self._cancelled.is_set():
                self.finished.emit(self._request, matches)
        except Exception as exc:
            self.failed.emit(self._request, str(exc))
        finally:
            self.completed.emit()


class MissingTextureSearchController(QObject):
    finished = pyqtSignal(object, object)
    failed = pyqtSignal(object, str)

    def __init__(self, parent: QObject):
        super().__init__(parent)
        self._thread: QThread | None = None
        self._worker: _MissingTextureSearchWorker | None = None

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.isRunning()

    def submit(self, request: MissingTextureSearchRequest) -> bool:
        if self._thread is not None:
            return False
        self._thread = QThread(self)
        self._worker = _MissingTextureSearchWorker(request)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.finished.connect(self.finished)
        self._worker.failed.connect(self.failed)
        self._worker.completed.connect(self._thread.quit)
        self._worker.completed.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.finished.connect(self._reset)
        self._thread.start()
        return True

    def _reset(self) -> None:
        self._thread = None
        self._worker = None

    def shutdown(self, *, wait_ms: int = 0) -> bool:
        if self._thread is None:
            return True
        if self._worker is not None:
            self._worker.cancel()
        self._thread.quit()
        if wait_ms > 0:
            self._thread.wait(wait_ms)
        return not self._thread.isRunning()
