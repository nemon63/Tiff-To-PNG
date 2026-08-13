from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

from PIL import Image
from PyQt6.QtCore import QObject, QThread, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QImage, QPixmap

from image_converter.services.image_loading import copy_first_frame_preserving_alpha


@dataclass(slots=True, frozen=True)
class TextureVisualData:
    size: tuple[int, int]
    pixmap: QPixmap


class _TextureVisualWorker(QObject):
    ready = pyqtSignal(object, object, object, object)
    failed = pyqtSignal(object)

    @pyqtSlot(object, object, int)
    def render(self, path: Path, key: object, thumbnail_size: int) -> None:
        try:
            with Image.open(path) as image:
                size = image.size
                thumbnail = copy_first_frame_preserving_alpha(image)
                thumbnail.thumbnail((thumbnail_size, thumbnail_size))
                rgba = thumbnail.convert("RGBA")
                data = rgba.tobytes("raw", "RGBA")
                qimage = QImage(
                    data,
                    rgba.width,
                    rgba.height,
                    rgba.width * 4,
                    QImage.Format.Format_RGBA8888,
                ).copy()
            self.ready.emit(path, key, size, qimage)
        except Exception:
            self.failed.emit(key)


class TextureVisualCache(QObject):
    visual_ready = pyqtSignal(object)
    _request = pyqtSignal(object, object, int)

    def __init__(
        self,
        parent: QObject | None = None,
        max_entries: int = 256,
        thumbnail_size: int = 72,
        max_pending: int = 256,
    ):
        super().__init__(parent)
        self.max_entries = max_entries
        self.thumbnail_size = thumbnail_size
        self.max_pending = max(1, max_pending)
        self._entries: OrderedDict[tuple[str, int, int], TextureVisualData] = OrderedDict()
        self._pending: OrderedDict[object, Path] = OrderedDict()
        self._active_key: object | None = None
        self._thread: QThread | None = None
        self._worker: _TextureVisualWorker | None = None
        self._shutting_down = False

    @staticmethod
    def path_key(path: Path) -> str:
        return str(path.resolve(strict=False)).casefold()

    @classmethod
    def cache_key(cls, path: Path) -> tuple[str, int, int] | None:
        try:
            stat = path.stat()
        except OSError:
            return None
        return cls.path_key(path), stat.st_size, stat.st_mtime_ns

    def get(self, path: Path) -> TextureVisualData | None:
        key = self.cache_key(path)
        if key is None:
            return None
        cached = self._entries.get(key)
        if cached is not None:
            self._entries.move_to_end(key)
            return cached
        if not self._shutting_down and key != self._active_key and key not in self._pending:
            while len(self._pending) >= self.max_pending:
                self._pending.popitem(last=False)
            self._pending[key] = path
            self._ensure_thread()
            self._dispatch_next()
        return None

    def is_pending(self, path: Path) -> bool:
        key = self.cache_key(path)
        return key is not None and (key == self._active_key or key in self._pending)

    def _ensure_thread(self) -> None:
        if self._thread is not None:
            return
        self._thread = QThread(self)
        self._worker = _TextureVisualWorker()
        self._worker.moveToThread(self._thread)
        self._request.connect(self._worker.render)
        self._worker.ready.connect(self._on_ready)
        self._worker.failed.connect(self._on_failed)
        self._thread.finished.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._on_thread_finished)
        self._thread.start()

    def _dispatch_next(self) -> None:
        if self._active_key is not None or self._shutting_down or not self._pending:
            return
        key, path = self._pending.popitem(last=False)
        self._active_key = key
        self._request.emit(path, key, self.thumbnail_size)

    def _on_ready(
        self,
        path: Path,
        key: object,
        size: tuple[int, int],
        qimage: QImage,
    ) -> None:
        self._active_key = None
        if key == self.cache_key(path) and not self._shutting_down:
            self._entries[key] = TextureVisualData(
                size=size,
                pixmap=QPixmap.fromImage(qimage),
            )
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)
            self.visual_ready.emit(path)
        self._dispatch_or_stop_thread()

    def _on_failed(self, key: object) -> None:
        if key == self._active_key:
            self._active_key = None
        self._dispatch_or_stop_thread()

    def _dispatch_or_stop_thread(self) -> None:
        self._dispatch_next()

    def _on_thread_finished(self) -> None:
        self._thread = None
        self._worker = None
        self._active_key = None

    def invalidate(self, path: Path) -> None:
        normalized = self.path_key(path)
        for key in tuple(self._entries):
            if key[0] == normalized:
                self._entries.pop(key)
        for key in tuple(self._pending):
            if isinstance(key, tuple) and key[0] == normalized:
                self._pending.pop(key)

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.isRunning()

    def shutdown(self, *, wait_ms: int = 0) -> bool:
        self._shutting_down = True
        self._pending.clear()
        if self._thread is None:
            return True
        self._thread.quit()
        if wait_ms > 0:
            self._thread.wait(wait_ms)
        return not self._thread.isRunning()
