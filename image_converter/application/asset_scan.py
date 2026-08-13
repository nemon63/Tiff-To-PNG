from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from threading import Event
from typing import TYPE_CHECKING

from PyQt6.QtCore import QObject, QThread, QTimer, pyqtSignal

from image_converter.services.asset_queue import AssetScanner
from image_converter.domain.models import AssetMetadata

if TYPE_CHECKING:
    from image_converter.ui.main_window import MainWindow


@dataclass(slots=True, frozen=True)
class AssetScanRequest:
    target: str
    paths: tuple[Path, ...]
    recursive: bool

    @property
    def coalesce_key(self) -> tuple[str, tuple[str, ...]]:
        normalized = tuple(
            sorted(str(path.resolve(strict=False)).casefold() for path in self.paths)
        )
        return self.target, normalized


class AssetScanWorker(QObject):
    items_found = pyqtSignal(object, object)
    finished = pyqtSignal(object, object)
    failed = pyqtSignal(object, str)
    completed = pyqtSignal()

    def __init__(self, request: AssetScanRequest):
        super().__init__()
        self._request = request
        self._cancelled = Event()

    def cancel(self) -> None:
        self._cancelled.set()

    def run(self) -> None:
        try:
            scanner = AssetScanner()
            batch: list[object] = []

            def collect_item(item: object) -> None:
                if self._cancelled.is_set():
                    return
                batch.append(item)
                if len(batch) >= 64:
                    self.items_found.emit(self._request, tuple(batch))
                    batch.clear()

            result = scanner.scan_paths(
                self._request.paths,
                recursive=self._request.recursive,
                item_callback=collect_item,
                cancelled=self._cancelled.is_set,
            )
            if batch and not self._cancelled.is_set():
                self.items_found.emit(self._request, tuple(batch))
            if self._cancelled.is_set():
                return
            self.finished.emit(self._request, result)
        except Exception as exc:
            self.failed.emit(self._request, str(exc))
        finally:
            self.completed.emit()


class AssetScanController(QObject):
    def __init__(self, window: MainWindow):
        super().__init__(window)
        self._window = window
        self._thread: QThread | None = None
        self._worker: AssetScanWorker | None = None
        self._active_key: tuple[str, tuple[str, ...]] | None = None
        self._pending: dict[str, AssetScanRequest] = {}
        window.destroyed.connect(self.shutdown)

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.isRunning()

    def submit(self, request: AssetScanRequest) -> None:
        if self._thread is not None:
            if request.coalesce_key != self._active_key:
                self._pending[request.target] = request
            return
        self._thread = QThread(self)
        self._worker = AssetScanWorker(request)
        self._active_key = request.coalesce_key
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.items_found.connect(self._window.on_asset_scan_items)
        self._worker.finished.connect(self._window.on_asset_scan_finished)
        self._worker.failed.connect(self._window.on_asset_scan_failed)
        self._worker.completed.connect(self._thread.quit)
        self._worker.completed.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.finished.connect(self._reset)
        self._thread.start()

    def _reset(self) -> None:
        self._thread = None
        self._worker = None
        self._active_key = None
        if self._pending:
            target = next(iter(self._pending))
            request = self._pending.pop(target)
            QTimer.singleShot(0, lambda: self.submit(request))

    def shutdown(self, *_args: object, wait_ms: int = 0) -> bool:
        self._pending.clear()
        if self._thread is None:
            return True
        if self._worker is not None:
            self._worker.cancel()
        self._thread.quit()
        if wait_ms > 0:
            self._thread.wait(wait_ms)
        return not self._thread.isRunning()


@dataclass(slots=True, frozen=True)
class AlphaAnalysisRequest:
    path: Path
    metadata: AssetMetadata
    revision: tuple[int, int] | None = None

    def with_current_revision(self) -> AlphaAnalysisRequest:
        if self.revision is not None:
            return self
        return replace(self, revision=file_revision(self.path))


def file_revision(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_size, stat.st_mtime_ns


class AlphaAnalysisWorker(QObject):
    finished = pyqtSignal(object, object)
    failed = pyqtSignal(object, str)
    completed = pyqtSignal()

    def __init__(self, request: AlphaAnalysisRequest):
        super().__init__()
        self._request = request
        self._cancelled = Event()

    def cancel(self) -> None:
        self._cancelled.set()

    def run(self) -> None:
        try:
            if self._cancelled.is_set():
                return
            metadata = AssetScanner().analyze_alpha(self._request.path, self._request.metadata)
            if self._cancelled.is_set():
                return
            self.finished.emit(self._request, metadata)
        except Exception as exc:
            self.failed.emit(self._request, str(exc))
        finally:
            self.completed.emit()


class AlphaAnalysisController(QObject):
    def __init__(self, window: MainWindow):
        super().__init__(window)
        self._window = window
        self._thread: QThread | None = None
        self._worker: AlphaAnalysisWorker | None = None
        self._active_key: tuple[str, tuple[int, int] | None] | None = None
        self._pending: dict[str, AlphaAnalysisRequest] = {}
        window.destroyed.connect(self.shutdown)

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.isRunning()

    def submit(self, request: AlphaAnalysisRequest) -> None:
        if request.metadata.alpha_fully_opaque is not None:
            return
        request = request.with_current_revision()
        path_key = str(request.path.resolve(strict=False)).casefold()
        if self._thread is not None:
            request_key = (path_key, request.revision)
            if request_key != self._active_key:
                self._pending[path_key] = request
            return
        self._thread = QThread(self)
        self._worker = AlphaAnalysisWorker(request)
        self._active_key = (path_key, request.revision)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.finished.connect(self._window.on_alpha_analysis_finished)
        self._worker.failed.connect(self._window.on_alpha_analysis_failed)
        self._worker.completed.connect(self._thread.quit)
        self._worker.completed.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.finished.connect(self._reset)
        self._thread.start()

    def _reset(self) -> None:
        self._thread = None
        self._worker = None
        self._active_key = None
        if self._pending:
            _key, request = self._pending.popitem()
            QTimer.singleShot(0, lambda: self.submit(request))

    def shutdown(self, *_args: object, wait_ms: int = 0) -> bool:
        self._pending.clear()
        if self._thread is None:
            return True
        if self._worker is not None:
            self._worker.cancel()
        self._thread.quit()
        if wait_ms > 0:
            self._thread.wait(wait_ms)
        return not self._thread.isRunning()
