from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from image_converter.domain.models import AppSettings
from image_converter.services.options_codec import (
    deserialize_conversion_options,
    serialize_conversion_options,
)

LAYOUT_VERSION = 3


class AppSettingsRepository:
    def __init__(self, path: Path):
        self._path = path

    def load(self) -> AppSettings:
        if not self._path.exists():
            return AppSettings()

        try:
            raw_data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return AppSettings()

        return self._deserialize(raw_data)

    def save(self, settings: AppSettings) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps(self._serialize(settings), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            return

    def _serialize(self, settings: AppSettings) -> dict[str, Any]:
        return {
            "input_path": settings.input_path,
            "output_path": settings.output_path,
            "options": serialize_conversion_options(settings.options),
            "window": {
                "layout_version": LAYOUT_VERSION,
                "width": settings.window_width,
                "height": settings.window_height,
                "splitter_sizes": list(settings.splitter_sizes),
                "workspace_splitter_sizes": list(settings.workspace_splitter_sizes),
                "detail_splitter_sizes": list(settings.detail_splitter_sizes),
                "inspector_splitter_sizes": list(settings.inspector_splitter_sizes),
            },
        }

    def _deserialize(self, data: dict[str, Any]) -> AppSettings:
        options_data = data.get("options", {})
        window_data = data.get("window", {})
        use_saved_layout = self._coerce_int(window_data.get("layout_version"), 0) == LAYOUT_VERSION

        return AppSettings(
            input_path=str(data.get("input_path", "")),
            output_path=str(data.get("output_path", "")),
            options=deserialize_conversion_options(options_data if isinstance(options_data, dict) else {}),
            window_width=self._coerce_int(window_data.get("width"), 1280),
            window_height=self._coerce_int(window_data.get("height"), 820),
            splitter_sizes=self._coerce_pair(
                window_data.get("splitter_sizes") if use_saved_layout else None,
                (340, 940),
            ),
            workspace_splitter_sizes=self._coerce_pair(
                window_data.get("workspace_splitter_sizes") if use_saved_layout else None,
                (500, 440),
            ),
            detail_splitter_sizes=self._coerce_pair(
                window_data.get("detail_splitter_sizes") if use_saved_layout else None,
                (420, 220),
            ),
            inspector_splitter_sizes=self._coerce_pair(
                window_data.get("inspector_splitter_sizes") if use_saved_layout else None,
                (500, 190),
            ),
        )

    @staticmethod
    def _coerce_int(value: Any, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @classmethod
    def _coerce_pair(cls, value: Any, default: tuple[int, int]) -> tuple[int, int]:
        try:
            result = tuple(cls._coerce_int(item, default[index]) for index, item in enumerate(value[:2]))
        except (TypeError, ValueError, IndexError):
            return default

        if len(result) != 2:
            return default
        return result
