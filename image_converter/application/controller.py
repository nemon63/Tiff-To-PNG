from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QObject, QThread

from image_converter.application.worker import BatchConversionWorker
from image_converter.domain.errors import ValidationError
from image_converter.domain.models import BatchSummary, ConversionStatus, QueueStatus
from image_converter.services.conversion import BatchConversionService
from image_converter.services.validation import validate_request
from image_converter.ui.main_window import MainWindow


class ConversionController(QObject):
    def __init__(self, window: MainWindow, service: BatchConversionService):
        super().__init__(window)
        self._window = window
        self._service = service
        self._thread: QThread | None = None
        self._worker: BatchConversionWorker | None = None

        self._window.convert_requested.connect(self.start_conversion)

    def add_paths_to_queue(self, raw_paths: list[str]) -> None:
        self._window._request_queue_scan(raw_paths)

    def start_conversion(self) -> None:
        if self._thread is not None or self._window.is_running():
            return

        request = self._window.build_request()
        try:
            validate_request(request)
        except ValidationError as exc:
            self._window.show_error("Ошибка", str(exc))
            return

        if request.options.delete_source and not self._window.confirm_delete_sources():
            return
        if not self._window.job_coordinator.try_acquire_exclusive(self):
            self._window.set_status("Другая операция экспорта или конвертации ещё выполняется.")
            return

        self._window.clear_log()
        self._window.append_log("---- Старт конвертации ----")
        self._window.set_status("Конвертация...")
        self._window.set_running(True)
        self._window.reset_queue_statuses_for_run()

        self._thread = QThread(self)
        self._worker = BatchConversionWorker(self._service, request)
        self._worker.moveToThread(self._thread)

        self._thread.started.connect(self._worker.run)
        self._worker.log_message.connect(self._window.append_log)
        self._worker.item_started.connect(self._on_item_started)
        self._worker.item_completed.connect(self._on_item_completed)
        self._worker.finished.connect(self._on_finished)
        self._worker.failed.connect(self._on_failed)
        self._worker.completed.connect(self._thread.quit)
        self._worker.completed.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.finished.connect(self._reset_worker_state)

        self._thread.start()

    def _on_finished(self, summary: BatchSummary) -> None:
        summary_text = summary.as_text()
        self._window.append_log(summary_text)
        self._window.set_status(summary_text)
        self._window.set_running(False)
        self._window.show_info("Готово", summary_text)

    def _on_failed(self, message: str) -> None:
        self._window.append_log(f"ОШИБКА: {message}")
        self._window.set_status("Ошибка")
        self._window.set_running(False)
        self._window.show_error("Ошибка", message)

    def _on_item_started(self, raw_path: str) -> None:
        self._window.set_queue_item_running(Path(raw_path))

    def _on_item_completed(self, result) -> None:
        status_mapping = {
            ConversionStatus.SUCCESS: QueueStatus.DONE,
            ConversionStatus.SKIPPED: QueueStatus.SKIPPED,
            ConversionStatus.FAILED: QueueStatus.ERROR,
        }
        queue_status = status_mapping.get(result.status, QueueStatus.ERROR)
        self._window.set_queue_item_result(
            result.source,
            status=queue_status,
            message=result.message,
            destination=result.destination,
        )

    def _reset_worker_state(self) -> None:
        self._window.job_coordinator.release_exclusive(self)
        self._thread = None
        self._worker = None
