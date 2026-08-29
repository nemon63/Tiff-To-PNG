from __future__ import annotations

import re
from pathlib import Path

from image_converter.domain.models import ChannelPackLayout, TextureMapType


PACK_LAYOUT_SUFFIXES: dict[ChannelPackLayout, tuple[str, ...]] = {
    ChannelPackLayout.ORM: (
        "orm",
        "arm",
        "occlusion_roughness_metallic",
        "ambient_occlusion_roughness_metallic",
    ),
    ChannelPackLayout.RMA: ("rma", "roughness_metallic_ao"),
    ChannelPackLayout.MRA: ("mra", "metallic_roughness_ao"),
    ChannelPackLayout.UNITY_URP: (
        "metallicsmoothness",
        "metallic_smoothness",
        "urp_metallicsmoothness",
        "urp_metallic_smoothness",
        "metallicglossmap",
        "metallic_gloss_map",
    ),
    ChannelPackLayout.UNITY_HDRP: (
        "maskmap",
        "mask_map",
        "hdrp_maskmap",
        "hdrp_mask_map",
    ),
}

MAP_TYPE_PATTERNS: tuple[tuple[TextureMapType, tuple[str, ...]], ...] = (
    (
        TextureMapType.BASECOLOR,
        (
            "basecolor",
            "base_color",
            "albedo",
            "alb",
            "diffuse",
            "diff",
            "dif",
            "df",
            "color",
            "col",
        ),
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
        TextureMapType.DETAIL_MASK,
        ("detailmask", "detail_mask"),
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
    TextureMapType.DETAIL_MASK: "detailmask",
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


def detect_channel_pack_layout(path: Path) -> ChannelPackLayout | None:
    normalized_stem = _normalized_stem(path.stem)
    collapsed_stem = normalized_stem.replace("_", "")
    for layout, aliases in PACK_LAYOUT_SUFFIXES.items():
        for alias in aliases:
            normalized_alias = _normalized_stem(alias)
            if not normalized_alias:
                continue
            collapsed_alias = normalized_alias.replace("_", "")
            if normalized_stem == normalized_alias:
                return layout
            if normalized_stem.endswith(f"_{normalized_alias}"):
                return layout
            if len(collapsed_alias) >= 5 and collapsed_stem.endswith(collapsed_alias):
                return layout
    return None


def strip_channel_pack_suffix(stem: str) -> str:
    normalized_stem = _normalized_stem(stem)
    for aliases in PACK_LAYOUT_SUFFIXES.values():
        for alias in sorted(aliases, key=len, reverse=True):
            normalized_alias = _normalized_stem(alias)
            if normalized_stem == normalized_alias:
                return ""
            suffix = f"_{normalized_alias}"
            if normalized_stem.endswith(suffix):
                return normalized_stem[: -len(suffix)].strip("_")
    return normalized_stem


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


def _normalized_stem(stem: str) -> str:
    with_camel_breaks = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", stem)
    with_camel_breaks = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", with_camel_breaks)
    normalized = re.sub(r"[^a-zA-Z0-9]+", "_", with_camel_breaks).strip("_").lower()
    return re.sub(r"_+", "_", normalized)
