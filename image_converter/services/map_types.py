from __future__ import annotations

import re
from pathlib import Path

from image_converter.domain.models import TextureMapType

MAP_TYPE_PATTERNS: tuple[tuple[TextureMapType, tuple[str, ...]], ...] = (
    (
        TextureMapType.BASECOLOR,
        ("basecolor", "base_color", "albedo", "alb", "diffuse", "diff", "dif", "color", "col"),
    ),
    (
        TextureMapType.NORMAL,
        ("normal", "normalmap", "normal_gl", "normal_dx", "nrm", "nml", "nor"),
    ),
    (
        TextureMapType.ROUGHNESS,
        ("roughness", "rough", "rgh"),
    ),
    (
        TextureMapType.SMOOTHNESS,
        ("smoothness", "smooth", "gloss", "glossiness", "glossmap", "gls"),
    ),
    (
        TextureMapType.METALLIC,
        ("metallic", "metalness", "metal", "met", "mtl"),
    ),
    (
        TextureMapType.AO,
        ("ao", "ambientocclusion", "ambient_occlusion", "occlusion", "occ"),
    ),
    (
        TextureMapType.OPACITY,
        ("opacity", "alpha", "mask", "transparency", "transparent", "opc"),
    ),
    (
        TextureMapType.EMISSIVE,
        ("emissive", "emission", "emit", "emi", "ems", "glow", "selfillum"),
    ),
    (
        TextureMapType.HEIGHT,
        ("height", "hgt", "displacement", "displace", "disp", "bump"),
    ),
)

CANONICAL_MAP_SUFFIXES: dict[TextureMapType, str] = {
    TextureMapType.BASECOLOR: "basecolor",
    TextureMapType.NORMAL: "normal",
    TextureMapType.ROUGHNESS: "roughness",
    TextureMapType.SMOOTHNESS: "smoothness",
    TextureMapType.METALLIC: "metallic",
    TextureMapType.AO: "ao",
    TextureMapType.OPACITY: "opacity",
    TextureMapType.EMISSIVE: "emissive",
    TextureMapType.HEIGHT: "height",
}


def map_type_aliases(map_type: TextureMapType) -> tuple[str, ...]:
    for candidate, aliases in MAP_TYPE_PATTERNS:
        if candidate is map_type:
            return aliases
    return ()


def known_map_aliases() -> tuple[str, ...]:
    aliases: list[str] = []
    for _map_type, values in MAP_TYPE_PATTERNS:
        aliases.extend(values)
    return tuple(dict.fromkeys(aliases))


def canonical_map_suffix(map_type: TextureMapType) -> str:
    return CANONICAL_MAP_SUFFIXES.get(map_type, "")


def detect_texture_map_type(path: Path) -> TextureMapType:
    stem = path.stem.lower()
    normalized = re.sub(r"[^a-z0-9]+", "_", stem).strip("_")
    collapsed = normalized.replace("_", "")
    tokens = {token for token in normalized.split("_") if token}

    for map_type, aliases in MAP_TYPE_PATTERNS:
        for alias in aliases:
            if _matches_alias(normalized, collapsed, tokens, alias):
                return map_type

    return TextureMapType.UNKNOWN


def _matches_alias(
    normalized_stem: str,
    collapsed_stem: str,
    tokens: set[str],
    alias: str,
) -> bool:
    normalized_alias = re.sub(r"[^a-z0-9]+", "_", alias.lower()).strip("_")
    if not normalized_alias:
        return False

    alias_tokens = tuple(token for token in normalized_alias.split("_") if token)
    alias_collapsed = "".join(alias_tokens)

    if normalized_stem == normalized_alias or collapsed_stem == alias_collapsed:
        return True

    if normalized_stem.endswith(f"_{normalized_alias}"):
        return True

    if normalized_alias in tokens:
        return True

    if alias_tokens and all(token in tokens for token in alias_tokens):
        return True

    if len(alias_collapsed) >= 4 and collapsed_stem.endswith(alias_collapsed):
        return True

    return False
