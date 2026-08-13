from __future__ import annotations

from pathlib import Path
from threading import Event, RLock

from PIL import Image
from PyQt6.QtCore import QCoreApplication, QObject, QThread, pyqtSignal, pyqtSlot

from image_converter.domain.models import PreviewChannel
from image_converter.services.preview import TexturePreviewService


class _TexturePreviewWorker(QObject):
    ready = pyqtSignal(int, object)
    failed = pyqtSignal(int, str)

    def __init__(self):
        super().__init__()
        self._lock = RLock()
        self._latest: tuple[int, Path, PreviewChannel, int | None] | None = None
        self._scheduled = False
        self._cancelled = Event()

    def cancel(self) -> None:
        self._cancelled.set()
        with self._lock:
            self._latest = None

    def submit(
        self,
        generation: int,
        path: Path,
        channel: PreviewChannel,
        max_size: int | None,
    ) -> bool:
        if self._cancelled.is_set():
            return False
        with self._lock:
            self._latest = (generation, path, channel, max_size)
            if self._scheduled:
                return False
            self._scheduled = True
            return True

    @pyqtSlot()
    def run_latest(self) -> None:
        while True:
            if self._cancelled.is_set():
                with self._lock:
                    self._latest = None
                    self._scheduled = False
                return
            with self._lock:
                request = self._latest
                self._latest = None
            if request is None:
                with self._lock:
                    if self._latest is None:
                        self._scheduled = False
                        return
                continue
            generation, path, channel, max_size = request
            try:
                image = TexturePreviewService().render(path, channel, max_size=max_size)
                if not self._cancelled.is_set():
                    self.ready.emit(generation, image)
            except Exception as exc:
                self.failed.emit(generation, str(exc))


class TexturePreviewController(QObject):
    ready = pyqtSignal(int, object)
    failed = pyqtSignal(int, str)
    _wake = pyqtSignal()

    def __init__(self, parent: QObject):
        super().__init__(parent)
        self._generation = 0
        self._thread: QThread | None = None
        self._worker: _TexturePreviewWorker | None = None
        self._shutting_down = False
        parent.destroyed.connect(self.shutdown)
        app = QCoreApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.shutdown)

    def request(self, path: Path, channel: PreviewChannel, *, max_size: int | None) -> int:
        if self._shutting_down:
            return self._generation
        self._ensure_thread()
        self._generation += 1
        generation = self._generation
        if self._worker is not None and self._worker.submit(generation, path, channel, max_size):
            self._wake.emit()
        return generation

    def _ensure_thread(self) -> None:
        if self._thread is not None:
            return
        self._thread = QThread(self)
        self._worker = _TexturePreviewWorker()
        self._worker.moveToThread(self._thread)
        self._thread.finished.connect(self._worker.deleteLater)
        self._wake.connect(self._worker.run_latest)
        self._worker.ready.connect(self.ready)
        self._worker.failed.connect(self.failed)
        self._thread.finished.connect(self._on_thread_finished)
        self._thread.start()

    def invalidate(self) -> None:
        self._generation += 1

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.isRunning()

    def _on_thread_finished(self) -> None:
        self._thread = None
        self._worker = None

    @pyqtSlot()
    def shutdown(self, *_args: object, wait_ms: int = 0) -> bool:
        self._shutting_down = True
        self.invalidate()
        if self._thread is None:
            return True
        if self._worker is not None:
            self._worker.cancel()
        self._thread.quit()
        if wait_ms > 0:
            self._thread.wait(wait_ms)
        return not self._thread.isRunning()
