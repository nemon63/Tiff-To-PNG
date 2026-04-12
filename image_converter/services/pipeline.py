from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

from PIL import Image

from image_converter.domain.models import ConversionOptions, ResizeMode

RESAMPLING_LANCZOS = getattr(Image, "Resampling", Image).LANCZOS


class ImageProcessor(Protocol):
    def process(self, image: Image.Image, options: ConversionOptions) -> Image.Image:
        """Преобразует изображение в рамках конвейера."""


class NormalizeModeProcessor:
    def process(self, image: Image.Image, options: ConversionOptions) -> Image.Image:
        mode = image.mode
        has_alpha = mode in ("RGBA", "LA") or (mode == "P" and "transparency" in image.info)

        if options.force_rgba:
            return image.convert("RGBA")

        if has_alpha:
            if mode == "P":
                return image.convert("RGBA")
            return image

        if mode not in ("RGB", "L"):
            return image.convert("RGB")
        return image


class ResizeProcessor:
    def process(self, image: Image.Image, options: ConversionOptions) -> Image.Image:
        if options.resize_mode is ResizeMode.NONE:
            return image

        width, height = image.size
        if width <= 0 or height <= 0:
            return image

        if options.resize_mode is ResizeMode.PERCENT:
            scale = options.resize_percent / 100.0
            if scale <= 0:
                return image
            new_width = max(1, int(round(width * scale)))
            new_height = max(1, int(round(height * scale)))
        elif options.resize_mode is ResizeMode.MAX_SIDE:
            longest = max(width, height)
            if options.max_side <= 0 or longest <= options.max_side:
                return image
            scale = options.max_side / float(longest)
            new_width = max(1, int(round(width * scale)))
            new_height = max(1, int(round(height * scale)))
        else:
            return image

        if (new_width, new_height) == (width, height):
            return image

        return image.resize((new_width, new_height), RESAMPLING_LANCZOS)


class PaletteProcessor:
    def process(self, image: Image.Image, options: ConversionOptions) -> Image.Image:
        if not options.png8:
            return image

        dither_mode = Image.FLOYDSTEINBERG if options.dither else Image.NONE

        if "A" in image.getbands():
            rgba = image if image.mode == "RGBA" else image.convert("RGBA")
            return rgba.quantize(
                colors=options.png8_colors,
                method=Image.FASTOCTREE,
                dither=dither_mode,
            )

        rgb = image if image.mode == "RGB" else image.convert("RGB")
        return rgb.quantize(
            colors=options.png8_colors,
            method=Image.MEDIANCUT,
            dither=dither_mode,
        )


class ConversionPipeline:
    """Расширяемый конвейер постобработки изображения."""

    def __init__(self, processors: Iterable[ImageProcessor] | None = None):
        self._processors = list(processors or self.default_processors())

    @staticmethod
    def default_processors() -> list[ImageProcessor]:
        return [
            NormalizeModeProcessor(),
            ResizeProcessor(),
            PaletteProcessor(),
        ]

    def register(self, processor: ImageProcessor, *, index: int | None = None) -> None:
        if index is None:
            self._processors.append(processor)
            return
        self._processors.insert(index, processor)

    def process(self, image: Image.Image, options: ConversionOptions) -> Image.Image:
        current = image
        for processor in self._processors:
            current = processor.process(current, options)
        return current
