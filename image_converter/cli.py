from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from image_converter.domain.errors import ValidationError
from image_converter.domain.models import (
    BatchRequest,
    ChannelPackLayout,
    ChannelPackingMode,
    ChannelPackingOptions,
    ConversionOptions,
    NamingRules,
    ResizeMode,
)
from image_converter.services.conversion import BatchConversionService
from image_converter.services.validation import validate_request


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Batch convert DDS/PNG/TIFF/TGA/JPEG/BMP/GIF/WEBP/PSD to PNG "
            "with optional resize and PNG-8 quantization."
        )
    )
    parser.add_argument(
        "input",
        type=str,
        help="Input file or folder with .dds/.png/.tif/.tiff/.tga/.jpg/.jpeg/.bmp/.gif/.webp/.psd",
    )
    parser.add_argument(
        "--out",
        type=str,
        default="",
        help="Output folder. If empty, writes PNG next to the source.",
    )
    parser.add_argument("--recursive", action="store_true", help="Recurse into subfolders")
    parser.add_argument("--force-rgba", action="store_true", help="Force output as RGBA")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing PNGs")
    parser.add_argument("--delete-source", action="store_true", help="Delete source after success")
    parser.add_argument(
        "--lowercase-names",
        action="store_true",
        help="Lowercase output PNG filenames",
    )
    parser.add_argument(
        "--replace-spaces",
        action="store_true",
        help="Replace spaces with underscores in output filenames",
    )
    parser.add_argument(
        "--normalize-suffix",
        action="store_true",
        help="Normalize texture map suffixes such as albedo -> basecolor",
    )
    parser.add_argument(
        "--pack-layout",
        type=str,
        choices=[layout.value for layout in ChannelPackLayout],
        default="",
        help="Build packed texture after batch conversion: orm, rma, mra, unity_urp or unity_hdrp",
    )
    parser.add_argument(
        "--pack-only",
        action="store_true",
        help="Build only packed textures and skip regular per-file PNG conversion",
    )
    parser.add_argument(
        "--unpack-packed",
        action="store_true",
        help=(
            "Unpack detected ORM/RMA/MRA/Unity packed textures to separate "
            "AO/Roughness/Metallic/Detail Mask maps"
        ),
    )
    parser.add_argument(
        "--compress-level",
        type=int,
        default=6,
        choices=range(0, 10),
        metavar="0..9",
        help="PNG compress level: 0 fastest/largest, 9 slowest/smallest",
    )
    parser.add_argument(
        "--optimize",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Enable/disable PNG optimize pass",
    )
    resize_group = parser.add_mutually_exclusive_group()
    resize_group.add_argument(
        "--resize-percent",
        type=int,
        default=None,
        help="Resize to percent of original size (e.g. 50)",
    )
    resize_group.add_argument(
        "--max-side",
        type=int,
        default=None,
        help="Resize so longer side is <= this value in px",
    )
    parser.add_argument("--png8", action="store_true", help="Save as indexed PNG-8")
    parser.add_argument(
        "--png8-colors",
        type=int,
        default=256,
        metavar="2..256",
        help="Palette size for PNG-8",
    )
    parser.add_argument(
        "--dither",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Enable/disable dithering for PNG-8",
    )
    return parser


def build_request_from_args(args: argparse.Namespace) -> BatchRequest:
    resize_mode = ResizeMode.NONE
    resize_percent = 100
    max_side = 2048

    if args.resize_percent is not None:
        resize_mode = ResizeMode.PERCENT
        resize_percent = args.resize_percent
    elif args.max_side is not None:
        resize_mode = ResizeMode.MAX_SIDE
        max_side = args.max_side

    packing = ChannelPackingOptions()
    if args.pack_layout:
        packing = ChannelPackingOptions(
            enabled=True,
            layout=ChannelPackLayout(args.pack_layout),
            mode=ChannelPackingMode.PACK_ONLY if args.pack_only else ChannelPackingMode.AFTER_CONVERSION,
        )

    return BatchRequest(
        input_path=Path(args.input),
        output_root=Path(args.out) if args.out else None,
        options=ConversionOptions(
            recursive=args.recursive,
            force_rgba=args.force_rgba,
            overwrite=args.overwrite,
            delete_source=args.delete_source,
            optimize=args.optimize,
            compress_level=args.compress_level,
            resize_mode=resize_mode,
            resize_percent=resize_percent,
            max_side=max_side,
            png8=args.png8,
            png8_colors=args.png8_colors,
            dither=args.dither,
            unpack_packed=args.unpack_packed,
            naming=NamingRules(
                lowercase=args.lowercase_names,
                replace_spaces=args.replace_spaces,
                normalize_map_suffix=args.normalize_suffix,
            ),
            packing=packing,
        ),
    )


def run_cli(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    request = build_request_from_args(args)

    try:
        validate_request(request)
    except ValidationError as exc:
        print(exc)
        return 2

    service = BatchConversionService()
    summary = service.run(request, logger=print)
    print(f"\n{summary.as_text()}")
    return 0
