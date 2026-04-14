from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageOps, ImageSequence

from image_converter.domain.models import PreviewChannel


class TexturePreviewService:
    def render(
        self,
        path: Path,
        channel: PreviewChannel,
        *,
        max_size: int | None = 512,
    ) -> Image.Image:
        with Image.open(path) as image:
            working_image = self._extract_first_frame(image)
            preview_image = self._to_preview_image(working_image, channel)
            if max_size is not None and max_size > 0:
                preview_image.thumbnail((max_size, max_size), getattr(Image, "Resampling", Image).LANCZOS)
            return preview_image.copy()

    @staticmethod
    def _extract_first_frame(image: Image.Image) -> Image.Image:
        if getattr(image, "is_animated", False) or getattr(image, "n_frames", 1) > 1:
            return next(ImageSequence.Iterator(image)).copy()
        return image.copy()

    def _to_preview_image(self, image: Image.Image, channel: PreviewChannel) -> Image.Image:
        rgba_image = image.convert("RGBA")

        if channel is PreviewChannel.COMPOSITE:
            return rgba_image

        if channel is PreviewChannel.LUMA:
            grayscale = ImageOps.grayscale(rgba_image.convert("RGB"))
            return Image.merge("RGBA", (grayscale, grayscale, grayscale, Image.new("L", grayscale.size, 255)))

        channel_mapping = {
            PreviewChannel.RED: "R",
            PreviewChannel.GREEN: "G",
            PreviewChannel.BLUE: "B",
            PreviewChannel.ALPHA: "A",
        }
        band_name = channel_mapping[channel]
        band = rgba_image.getchannel(band_name)
        return Image.merge(
            "RGBA",
            (
                band,
                band,
                band,
                Image.new("L", band.size, 255),
            ),
        )
