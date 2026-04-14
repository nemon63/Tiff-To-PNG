from __future__ import annotations

import re
from pathlib import Path

from image_converter.domain.models import TextureMapType

MAP_TYPE_PATTERNS: tuple[tuple[TextureMapType, tuple[str, ...]], ...] = (
    (
        TextureMapType.BASECOLOR,
        ("basecolor", "base_color", "albedo", "diffuse", "diff", "color"),
    ),
    (
        TextureMapType.NORMAL,
        ("normal", "normalmap", "normal_gl", "normal_dx", "nrm", "nor"),
    ),
    (
        TextureMapType.ROUGHNESS,
        ("roughness", "rough", "rgh"),
    ),
    (
        TextureMapType.METALLIC,
        ("metallic", "metalness", "metal", "mtl"),
    ),
    (
        TextureMapType.AO,
        ("ao", "ambientocclusion", "ambient_occlusion", "occlusion"),
    ),
    (
        TextureMapType.OPACITY,
        ("opacity", "alpha", "mask", "transparency", "transparent"),
    ),
    (
        TextureMapType.EMISSIVE,
        ("emissive", "emission", "emit", "glow", "selfillum"),
    ),
    (
        TextureMapType.HEIGHT,
        ("height", "displacement", "displace", "disp", "bump"),
    ),
)

CANONICAL_MAP_SUFFIXES: dict[TextureMapType, str] = {
    TextureMapType.BASECOLOR: "basecolor",
    TextureMapType.NORMAL: "normal",
    TextureMapType.ROUGHNESS: "roughness",
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
            alias_tokens = tuple(token for token in alias.lower().split("_") if token)
            alias_collapsed = "".join(alias_tokens)

            if alias_collapsed and alias_collapsed == collapsed:
                return map_type

            if alias_collapsed and alias_collapsed in collapsed:
                return map_type

            if alias_tokens and all(token in tokens for token in alias_tokens):
                return map_type

            if alias in tokens:
                return map_type

    return TextureMapType.UNKNOWN
