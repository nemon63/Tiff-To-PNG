from __future__ import annotations

import sys
from pathlib import Path

from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QApplication

from image_converter.application.controller import ConversionController
from image_converter.services.conversion import BatchConversionService
from image_converter.services.presets import PresetRepository
from image_converter.services.settings import AppSettingsRepository
from image_converter.ui.main_window import MainWindow
from image_converter.ui.theme import APP_STYLESHEET


def _runtime_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def _resource_root() -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parents[2]


def run_gui() -> int:
    app = QApplication(sys.argv)
    app.setStyleSheet(APP_STYLESHEET)

    runtime_root = _runtime_root()
    resource_root = _resource_root()
    icon_path = resource_root / "ico" / "favicon.ico"
    settings_repository = AppSettingsRepository(runtime_root / "app_settings.json")
    preset_repository = PresetRepository(runtime_root / "conversion_presets.json")
    if icon_path.exists():
        icon = QIcon(str(icon_path))
        app.setWindowIcon(icon)
    else:
        icon = QIcon()

    window = MainWindow()
    window.set_preset_repository(preset_repository)
    if not icon.isNull():
        window.setWindowIcon(icon)
    window.apply_app_settings(settings_repository.load())

    service = BatchConversionService()
    _controller = ConversionController(window, service)
    app.aboutToQuit.connect(lambda: settings_repository.save(window.build_app_settings()))
    window.show()
    return app.exec()
