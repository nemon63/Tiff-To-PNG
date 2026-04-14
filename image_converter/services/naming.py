from __future__ import annotations

import re
from pathlib import Path

from image_converter.domain.models import NamingRules, TextureMapType
from image_converter.services.map_types import canonical_map_suffix, known_map_aliases


def build_output_filename(
    source: Path,
    naming_rules: NamingRules,
    map_type: TextureMapType = TextureMapType.UNKNOWN,
) -> str:
    stem = apply_naming_rules(source.stem, naming_rules, map_type)
    return f"{stem}.png"


def apply_naming_rules(
    stem: str,
    naming_rules: NamingRules,
    map_type: TextureMapType = TextureMapType.UNKNOWN,
) -> str:
    result = stem

    if naming_rules.normalize_map_suffix and map_type is not TextureMapType.UNKNOWN:
        result = normalize_map_suffix(result, map_type)

    if naming_rules.replace_spaces:
        result = re.sub(r"\s+", "_", result)

    if naming_rules.lowercase:
        result = result.lower()

    if naming_rules.replace_spaces or naming_rules.normalize_map_suffix:
        result = re.sub(r"_+", "_", result)

    result = result.strip("._- ")
    return result or "texture"


def normalize_map_suffix(stem: str, map_type: TextureMapType) -> str:
    canonical_suffix = canonical_map_suffix(map_type)
    if not canonical_suffix:
        return stem

    normalized_stem = _normalized_stem(stem)
    if not normalized_stem:
        return canonical_suffix

    base_stem = _strip_known_suffix(normalized_stem)
    if not base_stem:
        return canonical_suffix
    return f"{base_stem}_{canonical_suffix}"


def _strip_known_suffix(normalized_stem: str) -> str:
    aliases = sorted(known_map_aliases(), key=len, reverse=True)
    for alias in aliases:
        normalized_alias = _normalized_stem(alias)
        if not normalized_alias:
            continue
        if normalized_stem == normalized_alias:
            return ""
        suffix = f"_{normalized_alias}"
        if normalized_stem.endswith(suffix):
            return normalized_stem[: -len(suffix)].strip("_")
    return normalized_stem


def _normalized_stem(stem: str) -> str:
    with_camel_breaks = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", stem)
    with_camel_breaks = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", with_camel_breaks)
    normalized = re.sub(r"[^a-zA-Z0-9]+", "_", with_camel_breaks).strip("_").lower()
    return re.sub(r"_+", "_", normalized)
