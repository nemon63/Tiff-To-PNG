from __future__ import annotations

import unittest
from random import Random
from unittest.mock import patch

from PIL import Image, ImageChops

from image_converter.domain.node_graph import NodeType, create_graph_node
from image_converter.services.node_image_operations import (
    _clamped_canvas,
    image_resampling,
    transform_2d,
)


def reference_transform(image, properties, output_size, offset_scale=(1.0, 1.0)):
    """Small-image reference: resize the entire texture, then address its pixels."""
    source = image.convert("RGBA")
    if properties.get("flip_horizontal", False):
        source = source.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
    if properties.get("flip_vertical", False):
        source = source.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
    rotation = properties.get("rotation", 0)
    if rotation in (90, 180, 270):
        source = source.transpose({
            90: Image.Transpose.ROTATE_270,
            180: Image.Transpose.ROTATE_180,
            270: Image.Transpose.ROTATE_90,
        }[rotation])
    scale = max(1, min(properties.get("scale", 100), 1000)) / 100.0
    size = (max(1, round(source.width * scale)), max(1, round(source.height * scale)))
    if size != source.size:
        source = source.resize(size, image_resampling(properties.get("filter", "bilinear")))
    width, height = output_size
    left = (width - source.width) // 2 + round(properties.get("offset_x", 0) * offset_scale[0])
    top = (height - source.height) // 2 + round(properties.get("offset_y", 0) * offset_scale[1])
    mode = properties.get("address_mode", "repeat")

    def address(coordinate, length):
        if mode == "clamp":
            return max(0, min(coordinate, length - 1))
        index, local = divmod(coordinate, length)
        return length - local - 1 if mode == "mirror" and index % 2 else local

    pixels = []
    for y in range(height):
        sy = address(y - top, source.height)
        for x in range(width):
            pixel = source.getpixel((address(x - left, source.width), sy))
            # Legacy Repeat/Mirror alpha-composite each non-overlapping tile
            # over a transparent canvas; fully transparent RGB is discarded.
            pixels.append((0, 0, 0, 0) if mode != "clamp" and pixel[3] == 0 else pixel)
    result = Image.new("RGBA", output_size)
    result.putdata(pixels)
    return result


class TransformSamplingTests(unittest.TestCase):
    @staticmethod
    def patterned_image(size=(17, 13)):
        random = Random(153)
        image = Image.new("RGBA", size)
        image.putdata([
            (random.randrange(256), random.randrange(256), random.randrange(256),
             (0, 1, 32, 127, 255)[index % 5])
            for index in range(size[0] * size[1])
        ])
        return image

    def test_matches_full_texture_reference_across_address_modes_and_filters(self) -> None:
        image = self.patterned_image()
        original = image.tobytes()
        for mode in ("repeat", "mirror", "clamp"):
            for filter_name in ("nearest", "bilinear", "bicubic", "lanczos"):
                for scale in (1, 37, 100, 125, 250, 1000):
                    for rotation in (0, 90, 180, 270):
                        for offsets in ((0, 0), (19, -23), (-8192, 8193)):
                            with self.subTest(mode=mode, filter=filter_name, scale=scale,
                                              rotation=rotation, offsets=offsets):
                                properties = {
                                    "address_mode": mode, "filter": filter_name, "scale": scale,
                                    "rotation": rotation, "offset_x": offsets[0], "offset_y": offsets[1],
                                    "flip_horizontal": True, "flip_vertical": rotation == 180,
                                }
                                node = create_graph_node(NodeType.TRANSFORM_2D, properties=properties)
                                expected = reference_transform(image, properties, (23, 19))
                                actual = transform_2d(image, node, (23, 19))
                                if filter_name == "nearest" or scale <= 100:
                                    self.assertEqual(expected.tobytes(), actual.tobytes())
                                else:
                                    # Regional filter coefficients can round differently
                                    # at subpixel boundaries. Compare premultiplied color
                                    # so near-zero alpha does not amplify quantization.
                                    difference = ImageChops.difference(
                                        expected.convert("RGBa"), actual.convert("RGBa"),
                                    )
                                    self.assertLessEqual(max(high for _low, high in difference.getextrema()), 2)
        self.assertEqual(original, image.tobytes())

    def test_preview_offset_scaling_and_single_pixel_axes(self) -> None:
        for size in ((1, 1), (1, 13), (17, 1)):
            image = self.patterned_image(size)
            for mode in ("repeat", "mirror", "clamp"):
                for scale in (1, 100, 1000):
                    with self.subTest(size=size, mode=mode, scale=scale):
                        properties = {
                            "address_mode": mode, "filter": "nearest", "scale": scale,
                            "offset_x": -29, "offset_y": 55, "rotation": 90,
                        }
                        node = create_graph_node(NodeType.TRANSFORM_2D, properties=properties)
                        actual = transform_2d(image, node, (19, 11), offset_scale=(0.25, 0.5))
                        expected = reference_transform(image, properties, (19, 11), (0.25, 0.5))
                        self.assertEqual(expected.tobytes(), actual.tobytes())

    def test_upscale_and_extreme_offsets_do_not_allocate_enlarged_images(self) -> None:
        image = self.patterned_image((128, 96))
        maximum_pixels = 128 * 96
        methods = {name: getattr(Image.Image, name) for name in ("resize", "transform", "crop")}
        original_new = Image.new

        def check_size(size):
            self.assertGreater(size[0], 0)
            self.assertGreater(size[1], 0)
            self.assertLessEqual(size[0] * size[1], maximum_pixels)

        def new(mode, size, *args, **kwargs):
            check_size(size)
            return original_new(mode, size, *args, **kwargs)

        def resize(source, size, *args, **kwargs):
            check_size(size)
            return methods["resize"](source, size, *args, **kwargs)

        def transform(source, size, *args, **kwargs):
            check_size(size)
            return methods["transform"](source, size, *args, **kwargs)

        def crop(source, box=None):
            if box is not None:
                check_size((box[2] - box[0], box[3] - box[1]))
            return methods["crop"](source, box)

        with (
            patch.object(Image, "new", new),
            patch.object(Image.Image, "resize", resize),
            patch.object(Image.Image, "transform", transform),
            patch.object(Image.Image, "crop", crop),
        ):
            for mode in ("repeat", "mirror", "clamp"):
                for filter_name in ("nearest", "bilinear", "bicubic", "lanczos"):
                    for offset in (0, 10**9, -(10**9)):
                        with self.subTest(mode=mode, filter=filter_name, offset=offset):
                            node = create_graph_node(NodeType.TRANSFORM_2D, properties={
                                "scale": 1000, "address_mode": mode, "filter": filter_name,
                                "offset_x": offset, "offset_y": -offset,
                            })
                            result = transform_2d(image, node, image.size)
                            self.assertEqual(image.size, result.size)

    def test_tiny_tiles_are_replicated_without_thousands_of_composites(self) -> None:
        image = self.patterned_image((64, 64))
        original = Image.Image.alpha_composite
        for mode in ("repeat", "mirror"):
            count = 0

            def composite(canvas, *args, **kwargs):
                nonlocal count
                count += 1
                self.assertLessEqual(count, 9)
                return original(canvas, *args, **kwargs)

            node = create_graph_node(NodeType.TRANSFORM_2D, properties={"scale": 1, "address_mode": mode})
            with patch.object(Image.Image, "alpha_composite", composite):
                result = transform_2d(image, node, (1024, 1024))
            self.assertEqual((1024, 1024), result.size)
            self.assertEqual(result.getpixel((0, 0)), result.getpixel((1023, 1023)))

    def test_clamped_padding_preserves_height_to_normal_border_pixels(self) -> None:
        image = self.patterned_image((5, 7))
        actual = _clamped_canvas(image, (7, 9), 1, 1)
        for y in range(9):
            for x in range(7):
                self.assertEqual(
                    image.getpixel((min(4, max(0, x - 1)), min(6, max(0, y - 1)))),
                    actual.getpixel((x, y)),
                )


if __name__ == "__main__":
    unittest.main()
