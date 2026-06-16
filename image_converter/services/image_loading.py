from __future__ import annotations

from typing import Any

from PIL import Image, ImageSequence


TIFF_TAG_PHOTOMETRIC = 262
TIFF_TAG_SAMPLES_PER_PIXEL = 277
TIFF_TAG_EXTRA_SAMPLES = 338
TIFF_PHOTOMETRIC_RGB = 2


def copy_first_frame_preserving_alpha(image: Image.Image) -> Image.Image:
    if getattr(image, "is_animated", False) or getattr(image, "n_frames", 1) > 1:
        frame = next(ImageSequence.Iterator(image))
        return copy_preserving_alpha(frame)
    return copy_preserving_alpha(image)


def copy_preserving_alpha(image: Image.Image) -> Image.Image:
    _promote_tiff_extra_sample_alpha(image)
    copied = image.copy()
    _close_source_image(image)
    return copied


def image_has_alpha(image: Image.Image) -> bool:
    return "A" in image.getbands() or (image.mode == "P" and "transparency" in image.info)


def _promote_tiff_extra_sample_alpha(image: Image.Image) -> None:
    if image.format != "TIFF":
        return
    if image.mode != "RGB" or "A" in image.getbands():
        return
    if _tag_int(image, TIFF_TAG_PHOTOMETRIC, 0) != TIFF_PHOTOMETRIC_RGB:
        return
    if _tag_int(image, TIFF_TAG_SAMPLES_PER_PIXEL, 0) < 4:
        return
    if not _has_tiff_extra_sample(image):
        return
    if not _uses_libtiff_rgbx_tile(image):
        return

    _replace_tile_raw_mode(image, "RGBA")
    setattr(image, "_mode", "RGBA")


def _uses_libtiff_rgbx_tile(image: Image.Image) -> bool:
    tiles = getattr(image, "tile", ())
    if not tiles:
        return False
    for tile in tiles:
        args = getattr(tile, "args", ())
        if not args or args[0] != "RGBX":
            return False
    return True


def _replace_tile_raw_mode(image: Image.Image, raw_mode: str) -> None:
    updated_tiles = []
    for tile in getattr(image, "tile", ()):
        args = getattr(tile, "args", ())
        next_args = (raw_mode, *args[1:])
        if hasattr(tile, "_replace"):
            updated_tiles.append(tile._replace(args=next_args))
        else:
            updated_tiles.append(tile)
    image.tile = updated_tiles


def _close_source_image(image: Image.Image) -> None:
    try:
        image.close()
        return
    except Exception:
        pass

    try:
        public_fp = getattr(image, "fp", None)
        private_fp = getattr(image, "_fp", None)
    except Exception:
        return
    if public_fp is not None or private_fp is None:
        return
    if getattr(private_fp, "closed", True):
        return
    try:
        private_fp.close()
    except OSError:
        return


def _has_tiff_extra_sample(image: Image.Image) -> bool:
    value = _tag_value(image, TIFF_TAG_EXTRA_SAMPLES)
    if value is None:
        return False
    if isinstance(value, int):
        return True
    try:
        return len(tuple(value)) > 0
    except TypeError:
        return False


def _tag_int(image: Image.Image, tag_id: int, default: int) -> int:
    value = _tag_value(image, tag_id)
    try:
        if isinstance(value, tuple):
            value = value[0]
        return int(value)
    except (TypeError, ValueError, IndexError):
        return default


def _tag_value(image: Image.Image, tag_id: int) -> Any:
    tag_v2 = getattr(image, "tag_v2", None)
    if tag_v2 is None:
        return None
    try:
        return tag_v2.get(tag_id)
    except Exception:
        return None
