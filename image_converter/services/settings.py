from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from image_converter.domain.models import AppSettings, ConversionOptions, ResizeMode


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
            "options": {
                "recursive": settings.options.recursive,
                "force_rgba": settings.options.force_rgba,
                "overwrite": settings.options.overwrite,
                "delete_source": settings.options.delete_source,
                "optimize": settings.options.optimize,
                "compress_level": settings.options.compress_level,
                "resize_mode": settings.options.resize_mode.value,
                "resize_percent": settings.options.resize_percent,
                "max_side": settings.options.max_side,
                "png8": settings.options.png8,
                "png8_colors": settings.options.png8_colors,
                "dither": settings.options.dither,
            },
            "window": {
                "width": settings.window_width,
                "height": settings.window_height,
                "splitter_sizes": list(settings.splitter_sizes),
            },
        }

    def _deserialize(self, data: dict[str, Any]) -> AppSettings:
        options_data = data.get("options", {})
        window_data = data.get("window", {})
        splitter_sizes = window_data.get("splitter_sizes", [420, 740])

        try:
            resize_mode = ResizeMode(options_data.get("resize_mode", ResizeMode.NONE.value))
        except ValueError:
            resize_mode = ResizeMode.NONE

        try:
            splitter_tuple = tuple(int(size) for size in splitter_sizes[:2])
        except (TypeError, ValueError):
            splitter_tuple = (420, 740)

        if len(splitter_tuple) != 2:
            splitter_tuple = (420, 740)

        return AppSettings(
            input_path=str(data.get("input_path", "")),
            output_path=str(data.get("output_path", "")),
            options=ConversionOptions(
                recursive=bool(options_data.get("recursive", True)),
                force_rgba=bool(options_data.get("force_rgba", False)),
                overwrite=bool(options_data.get("overwrite", False)),
                delete_source=bool(options_data.get("delete_source", False)),
                optimize=bool(options_data.get("optimize", True)),
                compress_level=self._coerce_int(options_data.get("compress_level"), 6),
                resize_mode=resize_mode,
                resize_percent=self._coerce_int(options_data.get("resize_percent"), 100),
                max_side=self._coerce_int(options_data.get("max_side"), 2048),
                png8=bool(options_data.get("png8", False)),
                png8_colors=self._coerce_int(options_data.get("png8_colors"), 256),
                dither=bool(options_data.get("dither", True)),
            ),
            window_width=self._coerce_int(window_data.get("width"), 1180),
            window_height=self._coerce_int(window_data.get("height"), 780),
            splitter_sizes=splitter_tuple,
        )

    @staticmethod
    def _coerce_int(value: Any, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
