from __future__ import annotations

from typing import Any

from image_converter.domain.models import (
    ChannelPackLayout,
    ChannelPackingOptions,
    ConversionOptions,
    NamingRules,
    ResizeMode,
)


def serialize_conversion_options(options: ConversionOptions) -> dict[str, Any]:
    return {
        "recursive": options.recursive,
        "force_rgba": options.force_rgba,
        "overwrite": options.overwrite,
        "delete_source": options.delete_source,
        "optimize": options.optimize,
        "compress_level": options.compress_level,
        "resize_mode": options.resize_mode.value,
        "resize_percent": options.resize_percent,
        "max_side": options.max_side,
        "png8": options.png8,
        "png8_colors": options.png8_colors,
        "dither": options.dither,
        "naming": {
            "lowercase": options.naming.lowercase,
            "replace_spaces": options.naming.replace_spaces,
            "normalize_map_suffix": options.naming.normalize_map_suffix,
        },
        "packing": {
            "enabled": options.packing.enabled,
            "layout": options.packing.layout.value,
        },
    }


def deserialize_conversion_options(data: dict[str, Any]) -> ConversionOptions:
    try:
        resize_mode = ResizeMode(data.get("resize_mode", ResizeMode.NONE.value))
    except ValueError:
        resize_mode = ResizeMode.NONE

    naming_data = data.get("naming", {})
    if not isinstance(naming_data, dict):
        naming_data = {}

    packing_data = data.get("packing", {})
    if not isinstance(packing_data, dict):
        packing_data = {}

    try:
        packing_layout = ChannelPackLayout(
            packing_data.get("layout", ChannelPackLayout.ORM.value)
        )
    except ValueError:
        packing_layout = ChannelPackLayout.ORM

    return ConversionOptions(
        recursive=bool(data.get("recursive", True)),
        force_rgba=bool(data.get("force_rgba", False)),
        overwrite=bool(data.get("overwrite", False)),
        delete_source=bool(data.get("delete_source", False)),
        optimize=bool(data.get("optimize", True)),
        compress_level=_coerce_int(data.get("compress_level"), 6),
        resize_mode=resize_mode,
        resize_percent=_coerce_int(data.get("resize_percent"), 100),
        max_side=_coerce_int(data.get("max_side"), 2048),
        png8=bool(data.get("png8", False)),
        png8_colors=_coerce_int(data.get("png8_colors"), 256),
        dither=bool(data.get("dither", True)),
        naming=NamingRules(
            lowercase=bool(naming_data.get("lowercase", False)),
            replace_spaces=bool(naming_data.get("replace_spaces", False)),
            normalize_map_suffix=bool(naming_data.get("normalize_map_suffix", False)),
        ),
        packing=ChannelPackingOptions(
            enabled=bool(packing_data.get("enabled", False)),
            layout=packing_layout,
        ),
    )


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
