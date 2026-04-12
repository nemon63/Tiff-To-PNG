from __future__ import annotations

import sys
from pathlib import Path

from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QApplication

from image_converter.application.controller import ConversionController
from image_converter.services.conversion import BatchConversionService
from image_converter.services.settings import AppSettingsRepository
from image_converter.ui.main_window import MainWindow


def run_gui() -> int:
    app = QApplication(sys.argv)

    project_root = Path(__file__).resolve().parents[2]
    icon_path = project_root / "ico" / "favicon.ico"
    settings_repository = AppSettingsRepository(project_root / "app_settings.json")
    if icon_path.exists():
        icon = QIcon(str(icon_path))
        app.setWindowIcon(icon)
    else:
        icon = QIcon()

    window = MainWindow()
    if not icon.isNull():
        window.setWindowIcon(icon)
    window.apply_app_settings(settings_repository.load())

    service = BatchConversionService()
    _controller = ConversionController(window, service)
    app.aboutToQuit.connect(lambda: settings_repository.save(window.build_app_settings()))
    window.show()
    return app.exec()
