from __future__ import annotations

from PyQt6.QtCore import QObject, pyqtSignal

from image_converter.domain.models import BatchRequest
from image_converter.services.conversion import BatchConversionService


class BatchConversionWorker(QObject):
    log_message = pyqtSignal(str)
    finished = pyqtSignal(object)
    failed = pyqtSignal(str)
    completed = pyqtSignal()

    def __init__(self, service: BatchConversionService, request: BatchRequest):
        super().__init__()
        self._service = service
        self._request = request

    def run(self) -> None:
        try:
            summary = self._service.run(self._request, logger=self.log_message.emit)
            self.finished.emit(summary)
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            self.completed.emit()
