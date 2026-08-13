from __future__ import annotations

from collections import OrderedDict
from pathlib import Path
from typing import TYPE_CHECKING

from PIL import Image
from PyQt6.QtCore import QObject, QThread, pyqtSignal, pyqtSlot

from image_converter.services.image_loading import copy_first_frame_preserving_alpha

if TYPE_CHECKING:
    from image_converter.ui.main_window import MainWindow


class ThumbnailWorker(QObject):
    ready = pyqtSignal(object, object, object)
    failed = pyqtSignal(object)

    @pyqtSlot(object, object)
    def render(self, path: Path, cache_key: object) -> None:
        try:
            with Image.open(path) as source:
                image = copy_first_frame_preserving_alpha(source).convert("RGBA")
                image.thumbnail((64, 64), getattr(Image, "Resampling", Image).LANCZOS)
            self.ready.emit(path, cache_key, image)
        except Exception:
            self.failed.emit(cache_key)


class ThumbnailController(QObject):
    _request = pyqtSignal(object, object)

    def __init__(
        self,
        window: MainWindow,
        *,
        max_bytes: int = 64 * 1024 * 1024,
        max_pending: int = 128,
    ):
        super().__init__(window)
        self._window = window
        self._max_bytes = max_bytes
        self._current_bytes = 0
        self._cache: OrderedDict[object, tuple[Image.Image, int]] = OrderedDict()
        self._in_flight: set[object] = set()
        self._pending: OrderedDict[object, Path] = OrderedDict()
        self._active_key: object | None = None
        self._max_pending = max(1, max_pending)
        self._shutting_down = False
        self._thread: QThread | None = None
        self._worker: ThumbnailWorker | None = None
        window.destroyed.connect(self.shutdown)

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.isRunning()

    @staticmethod
    def cache_key(path: Path) -> tuple[str, int, int] | None:
        try:
            stat = path.stat()
        except OSError:
            return None
        return (
            str(path.resolve(strict=False)).casefold(),
            stat.st_size,
            stat.st_mtime_ns,
        )

    def request(self, path: Path) -> Image.Image | None:
        key = self.cache_key(path)
        if key is None:
            return None
        cached = self._cache.get(key)
        if cached is not None:
            self._cache.move_to_end(key)
            return cached[0]
        if key not in self._in_flight:
            self._ensure_thread()
            self._in_flight.add(key)
            while len(self._pending) >= self._max_pending:
                old_key, _old_path = self._pending.popitem(last=False)
                self._in_flight.discard(old_key)
            self._pending[key] = path
            self._dispatch_next()
        return None

    def invalidate(self, path: Path) -> None:
        prefix = str(path.resolve(strict=False)).casefold()
        for key, (_image, size) in tuple(self._cache.items()):
            if isinstance(key, tuple) and key[0] == prefix:
                self._cache.pop(key)
                self._current_bytes -= size

    def _ensure_thread(self) -> None:
        if self._thread is not None:
            return
        self._thread = QThread(self)
        self._worker = ThumbnailWorker()
        self._worker.moveToThread(self._thread)
        self._thread.finished.connect(self._worker.deleteLater)
        self._request.connect(self._worker.render)
        self._worker.ready.connect(self._on_ready)
        self._worker.failed.connect(self._on_failed)
        self._thread.finished.connect(self._on_thread_finished)
        self._thread.start()

    def _dispatch_next(self) -> None:
        if self._active_key is not None or self._shutting_down or not self._pending:
            return
        key, path = self._pending.popitem(last=False)
        self._active_key = key
        self._request.emit(path, key)

    def _on_ready(self, path: Path, key: object, image: Image.Image) -> None:
        self._in_flight.discard(key)
        self._active_key = None
        if key != self.cache_key(path) or self._shutting_down:
            self._dispatch_next()
            return
        size = image.width * image.height * max(1, len(image.getbands()))
        while self._cache and self._current_bytes + size > self._max_bytes:
            _old_key, (_old_image, old_size) = self._cache.popitem(last=False)
            self._current_bytes -= old_size
        if size <= self._max_bytes:
            self._cache[key] = (image, size)
            self._current_bytes += size
        self._window.on_asset_thumbnail_ready(path, image)
        self._dispatch_next()

    def _on_failed(self, key: object) -> None:
        self._in_flight.discard(key)
        if key == self._active_key:
            self._active_key = None
        self._dispatch_next()

    def _on_thread_finished(self) -> None:
        self._thread = None
        self._worker = None
        self._active_key = None

    @pyqtSlot()
    def shutdown(self, *_args: object, wait_ms: int = 0) -> bool:
        self._shutting_down = True
        self._pending.clear()
        self._in_flight = {self._active_key} if self._active_key is not None else set()
        if self._thread is None:
            return True
        self._thread.quit()
        if wait_ms > 0:
            self._thread.wait(wait_ms)
        return not self._thread.isRunning()
