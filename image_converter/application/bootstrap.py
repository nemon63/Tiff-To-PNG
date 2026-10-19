from __future__ import annotations

import sys
from pathlib import Path

from PyQt6.QtCore import QStandardPaths
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QApplication

from image_converter.application.controller import ConversionController
from image_converter.services.conversion import BatchConversionService
from image_converter.services.presets import PresetRepository
from image_converter.services.settings import AppSettingsRepository
from image_converter.services.user_data import migrate_legacy_user_data
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


def _user_data_root() -> Path:
    location = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppLocalDataLocation)
    if not location:
        raise RuntimeError("Could not determine the user application data directory.")
    return Path(location)


def run_gui() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("TexturePipelineWorkbench")
    app.setStyleSheet(APP_STYLESHEET)

    runtime_root = _runtime_root()
    resource_root = _resource_root()
    user_data_root = _user_data_root()
    migration_errors = migrate_legacy_user_data(runtime_root, user_data_root)
    icon_path = resource_root / "ico" / "favicon.ico"
    settings_path = user_data_root / "app_settings.json"
    settings_repository = AppSettingsRepository(settings_path)
    preset_repository = PresetRepository(user_data_root / "conversion_presets.json")
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
    def save_settings() -> None:
        try:
            settings_repository.save(window.build_app_settings())
        except OSError as exc:
            window.show_error("Settings save failed", f"{settings_path}\n{exc}")

    app.aboutToQuit.connect(save_settings)
    window.show()
    if migration_errors:
        window.show_error("Settings migration failed", "\n".join(migration_errors))
    return app.exec()
