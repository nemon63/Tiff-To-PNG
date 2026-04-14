from __future__ import annotations

import json
import re
from pathlib import Path

from image_converter.domain.models import (
    ChannelPackLayout,
    ChannelPackingOptions,
    ConversionOptions,
    ConversionPreset,
    NamingRules,
    PresetScope,
    ResizeMode,
)
from image_converter.services.options_codec import (
    deserialize_conversion_options,
    serialize_conversion_options,
)

SYSTEM_PRESETS: tuple[ConversionPreset, ...] = (
    ConversionPreset(
        preset_id="system:web",
        name="Web",
        scope=PresetScope.SYSTEM,
        description="Легкий web-экспорт: PNG-8, dithering и ограничение до 2048 px.",
        options=ConversionOptions(
            optimize=True,
            compress_level=9,
            resize_mode=ResizeMode.MAX_SIDE,
            max_side=2048,
            png8=True,
            png8_colors=256,
            dither=True,
        ),
    ),
    ConversionPreset(
        preset_id="system:game",
        name="Game",
        scope=PresetScope.SYSTEM,
        description="Полноцветный игровой экспорт с безопасными настройками и лимитом 4096 px.",
        options=ConversionOptions(
            optimize=True,
            compress_level=6,
            resize_mode=ResizeMode.MAX_SIDE,
            max_side=4096,
            png8=False,
        ),
    ),
    ConversionPreset(
        preset_id="system:unity-urp",
        name="Unity URP",
        scope=PresetScope.SYSTEM,
        description="Собирает metallic/smoothness карту для Unity URP. Альфа берет Smoothness или считает 1-Roughness.",
        options=ConversionOptions(
            optimize=True,
            compress_level=6,
            resize_mode=ResizeMode.MAX_SIDE,
            max_side=4096,
            png8=False,
            naming=NamingRules(normalize_map_suffix=True),
            packing=ChannelPackingOptions(
                enabled=True,
                layout=ChannelPackLayout.UNITY_URP,
            ),
        ),
    ),
    ConversionPreset(
        preset_id="system:unity-hdrp",
        name="Unity HDRP",
        scope=PresetScope.SYSTEM,
        description="Собирает HDRP Mask Map. B-канал заполняется белым как detail mask по умолчанию, альфа берет Smoothness или 1-Roughness.",
        options=ConversionOptions(
            optimize=True,
            compress_level=6,
            resize_mode=ResizeMode.MAX_SIDE,
            max_side=4096,
            png8=False,
            naming=NamingRules(normalize_map_suffix=True),
            packing=ChannelPackingOptions(
                enabled=True,
                layout=ChannelPackLayout.UNITY_HDRP,
            ),
        ),
    ),
    ConversionPreset(
        preset_id="system:ui",
        name="UI",
        scope=PresetScope.SYSTEM,
        description="RGBA-режим для интерфейсных текстур с высоким сжатием без потери альфы.",
        options=ConversionOptions(
            force_rgba=True,
            optimize=True,
            compress_level=9,
            png8=False,
        ),
    ),
    ConversionPreset(
        preset_id="system:preview",
        name="Preview",
        scope=PresetScope.SYSTEM,
        description="Быстрый предпросмотр: мягкое сжатие и даунскейл до 1024 px.",
        options=ConversionOptions(
            optimize=False,
            compress_level=4,
            resize_mode=ResizeMode.MAX_SIDE,
            max_side=1024,
            png8=False,
        ),
    ),
    ConversionPreset(
        preset_id="system:lossless",
        name="Lossless",
        scope=PresetScope.SYSTEM,
        description="Максимально бережный экспорт без PNG-8 и без изменения разрешения.",
        options=ConversionOptions(
            optimize=True,
            compress_level=9,
            resize_mode=ResizeMode.NONE,
            png8=False,
        ),
    ),
)

class PresetRepository:
    def __init__(self, path: Path):
        self._path = path

    def load_presets(self) -> tuple[ConversionPreset, ...]:
        user_presets = sorted(self._load_user_presets(), key=lambda preset: preset.name.lower())
        return SYSTEM_PRESETS + tuple(user_presets)

    def save_preset(self, name: str, options: ConversionOptions) -> ConversionPreset:
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("Preset name cannot be empty.")

        user_presets = list(self._load_user_presets())
        existing = next(
            (preset for preset in user_presets if preset.name.casefold() == clean_name.casefold()),
            None,
        )

        if existing is None:
            preset_id = self._build_unique_preset_id(clean_name, {preset.preset_id for preset in user_presets})
            preset = ConversionPreset(
                preset_id=preset_id,
                name=clean_name,
                options=options,
                scope=PresetScope.USER,
                description="Пользовательский preset",
            )
            user_presets.append(preset)
        else:
            preset = ConversionPreset(
                preset_id=existing.preset_id,
                name=clean_name,
                options=options,
                scope=PresetScope.USER,
                description=existing.description or "Пользовательский preset",
            )
            user_presets = [
                preset if candidate.preset_id == existing.preset_id else candidate
                for candidate in user_presets
            ]

        self._write_user_presets(user_presets)
        return preset

    def delete_preset(self, preset_id: str) -> bool:
        user_presets = list(self._load_user_presets())
        filtered = [preset for preset in user_presets if preset.preset_id != preset_id]
        if len(filtered) == len(user_presets):
            return False
        self._write_user_presets(filtered)
        return True

    def _load_user_presets(self) -> tuple[ConversionPreset, ...]:
        if not self._path.exists():
            return ()

        try:
            raw_data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return ()

        raw_presets = raw_data.get("presets", [])
        presets: list[ConversionPreset] = []
        for raw_preset in raw_presets:
            if not isinstance(raw_preset, dict):
                continue

            name = str(raw_preset.get("name", "")).strip()
            preset_id = str(raw_preset.get("id", "")).strip()
            options_data = raw_preset.get("options", {})

            if not name or not preset_id or not isinstance(options_data, dict):
                continue

            presets.append(
                ConversionPreset(
                    preset_id=preset_id,
                    name=name,
                    options=deserialize_conversion_options(options_data),
                    scope=PresetScope.USER,
                    description=str(raw_preset.get("description", "Пользовательский preset")),
                )
            )

        return tuple(presets)

    def _write_user_presets(self, presets: list[ConversionPreset]) -> None:
        payload = {
            "presets": [
                {
                    "id": preset.preset_id,
                    "name": preset.name,
                    "description": preset.description,
                    "options": serialize_conversion_options(preset.options),
                }
                for preset in sorted(presets, key=lambda item: item.name.lower())
            ]
        }

        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _build_unique_preset_id(self, name: str, existing_ids: set[str]) -> str:
        slug = re.sub(r"[^\w]+", "-", name.lower(), flags=re.UNICODE).strip("-_")
        slug = slug or "preset"
        candidate = f"user:{slug}"
        suffix = 2
        while candidate in existing_ids:
            candidate = f"user:{slug}-{suffix}"
            suffix += 1
        return candidate
