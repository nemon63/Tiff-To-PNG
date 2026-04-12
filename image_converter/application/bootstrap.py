from __future__ import annotations

import sys
from pathlib import Path

from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QApplication

from image_converter.application.controller import ConversionController
from image_converter.services.conversion import BatchConversionService
from image_converter.ui.main_window import MainWindow


def run_gui() -> int:
    app = QApplication(sys.argv)

    icon_path = Path(__file__).resolve().parents[2] / "ico" / "favicon.ico"
    if icon_path.exists():
        icon = QIcon(str(icon_path))
        app.setWindowIcon(icon)
    else:
        icon = QIcon()

    window = MainWindow()
    if not icon.isNull():
        window.setWindowIcon(icon)

    service = BatchConversionService()
    _controller = ConversionController(window, service)
    window.show()
    return app.exec()
