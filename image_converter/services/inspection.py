from __future__ import annotations

from dataclasses import dataclass

from image_converter.domain.models import AssetMetadata, ConversionOptions, ResizeMode


@dataclass(slots=True, frozen=True)
class OutputEstimate:
    resolution_text: str
    format_text: str
    mode_text: str
    summary: str


def build_output_estimate(metadata: AssetMetadata, options: ConversionOptions) -> OutputEstimate:
    width, height = _estimate_output_size(metadata.width, metadata.height, options)
    resolution_text = f"{width}x{height}" if width > 0 and height > 0 else "-"

    if options.png8:
        color_count = max(2, min(options.png8_colors, 256))
        format_text = "PNG-8"
        mode_text = "Indexed + alpha" if metadata.has_alpha else "Indexed"
        summary = f"{resolution_text} · {format_text} · {color_count} colors · {mode_text}"
        return OutputEstimate(
            resolution_text=resolution_text,
            format_text=format_text,
            mode_text=mode_text,
            summary=summary,
        )

    normalized_mode = _estimate_normalized_mode(metadata)
    format_text = "PNG"
    summary = f"{resolution_text} · {format_text} · {normalized_mode}"
    return OutputEstimate(
        resolution_text=resolution_text,
        format_text=format_text,
        mode_text=normalized_mode,
        summary=summary,
    )


def _estimate_output_size(width: int, height: int, options: ConversionOptions) -> tuple[int, int]:
    if width <= 0 or height <= 0:
        return width, height

    if options.resize_mode is ResizeMode.NONE:
        return width, height

    if options.resize_mode is ResizeMode.PERCENT:
        scale = options.resize_percent / 100.0
        if scale <= 0:
            return width, height
        return max(1, int(round(width * scale))), max(1, int(round(height * scale)))

    if options.resize_mode is ResizeMode.MAX_SIDE:
        longest = max(width, height)
        if options.max_side <= 0 or longest <= options.max_side:
            return width, height
        scale = options.max_side / float(longest)
        return max(1, int(round(width * scale))), max(1, int(round(height * scale)))

    return width, height


def _estimate_normalized_mode(metadata: AssetMetadata) -> str:
    if metadata.has_alpha:
        if metadata.mode == "P":
            return "RGBA"
        return metadata.mode

    if metadata.mode in ("RGB", "L"):
        return metadata.mode

    return "RGBA" if metadata.mode == "LA" else "RGB"
