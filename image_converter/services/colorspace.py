from __future__ import annotations

import re
from pathlib import Path

from image_converter.domain.models import QueueItem, TextureColorSpace, TextureMapType
from image_converter.services.map_types import map_type_aliases
from image_converter.services.material_validation import filename_semantic_warning

COLOR_MAP_TYPES = {
    TextureMapType.BASECOLOR,
    TextureMapType.EMISSIVE,
}

TECHNICAL_MAP_TYPES = {
    TextureMapType.NORMAL,
    TextureMapType.ROUGHNESS,
    TextureMapType.SMOOTHNESS,
    TextureMapType.METALLIC,
    TextureMapType.AO,
    TextureMapType.OPACITY,
    TextureMapType.HEIGHT,
    TextureMapType.DETAIL_MASK,
}

COLOR_NAME_ALIASES = map_type_aliases(TextureMapType.BASECOLOR) + map_type_aliases(TextureMapType.EMISSIVE)
TECHNICAL_NAME_ALIASES = (
    map_type_aliases(TextureMapType.NORMAL)
    + map_type_aliases(TextureMapType.ROUGHNESS)
    + map_type_aliases(TextureMapType.SMOOTHNESS)
    + map_type_aliases(TextureMapType.METALLIC)
    + map_type_aliases(TextureMapType.AO)
    + map_type_aliases(TextureMapType.OPACITY)
    + map_type_aliases(TextureMapType.HEIGHT)
    + map_type_aliases(TextureMapType.DETAIL_MASK)
)


def recommended_colorspace_for_map_type(map_type: TextureMapType) -> TextureColorSpace:
    if map_type in COLOR_MAP_TYPES:
        return TextureColorSpace.SRGB
    if map_type in TECHNICAL_MAP_TYPES:
        return TextureColorSpace.LINEAR
    return TextureColorSpace.UNKNOWN


def build_colorspace_warning(path: Path, map_type: TextureMapType) -> str | None:
    recommended = recommended_colorspace_for_map_type(map_type)
    if recommended is TextureColorSpace.UNKNOWN:
        return None

    normalized_stem = _normalized_stem(path.stem)
    if not normalized_stem:
        return None

    if recommended is TextureColorSpace.LINEAR and _contains_any_alias(normalized_stem, COLOR_NAME_ALIASES):
        return "Техническая карта ожидает Linear, но в имени есть color/emissive-алиас"

    if recommended is TextureColorSpace.SRGB and _contains_any_alias(normalized_stem, TECHNICAL_NAME_ALIASES):
        return "Color-карта ожидает sRGB, но в имени есть technical-алиас"

    return None


def item_preflight_warnings(item: QueueItem) -> tuple[str, ...]:
    base_warnings = list(item.metadata.warnings if item.metadata is not None else ())
    colorspace_warning = build_colorspace_warning(item.path, item.effective_map_type)
    if colorspace_warning and colorspace_warning not in base_warnings:
        base_warnings.append(colorspace_warning)
    semantic_warning = filename_semantic_warning(item)
    if semantic_warning and semantic_warning not in base_warnings:
        base_warnings.append(semantic_warning)
    for warning in item.validation_warnings:
        if warning not in base_warnings:
            base_warnings.append(warning)
    return tuple(base_warnings)


def item_warning_count(item: QueueItem) -> int:
    return len(item_preflight_warnings(item))


def item_warning_summary(item: QueueItem) -> str:
    return "; ".join(item_preflight_warnings(item))


def _contains_any_alias(normalized_stem: str, aliases: tuple[str, ...]) -> bool:
    tokens = {token for token in normalized_stem.split("_") if token}
    collapsed = normalized_stem.replace("_", "")

    for alias in aliases:
        normalized_alias = _normalized_stem(alias)
        if not normalized_alias:
            continue

        alias_tokens = tuple(token for token in normalized_alias.split("_") if token)
        alias_collapsed = normalized_alias.replace("_", "")

        if normalized_stem == normalized_alias:
            return True
        if normalized_stem.endswith(f"_{normalized_alias}"):
            return True
        if len(alias_collapsed) >= 4 and collapsed.endswith(alias_collapsed):
            return True
        if normalized_alias in tokens:
            return True
        if alias_tokens and all(token in tokens for token in alias_tokens):
            return True

    return False


def _normalized_stem(stem: str) -> str:
    with_camel_breaks = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", stem)
    with_camel_breaks = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", with_camel_breaks)
    normalized = re.sub(r"[^a-zA-Z0-9]+", "_", with_camel_breaks).strip("_").lower()
    return re.sub(r"_+", "_", normalized)
