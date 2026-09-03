from __future__ import annotations

import math
from functools import lru_cache

from PIL import Image, ImageEnhance, ImageFilter, ImageMath

from image_converter.domain.node_graph import GraphNode


def image_resampling(value: object):
    name = str(value or "bilinear").lower()
    if name == "nearest":
        return Image.Resampling.NEAREST
    if name == "bicubic":
        return Image.Resampling.BICUBIC
    if name == "lanczos":
        return Image.Resampling.LANCZOS
    return Image.Resampling.BILINEAR


@lru_cache(maxsize=128)
def _normal_vector_lut(
    strength_percent: int = 100,
    flip_green: bool = False,
) -> ImageFilter.Color3DLUT:
    strength = max(0, strength_percent) / 100.0

    def transform(red: float, green: float, blue: float) -> tuple[float, float, float]:
        x = (red * 2.0 - 1.0) * strength
        y = (green * 2.0 - 1.0) * strength
        if flip_green:
            y = -y
        z = blue * 2.0 - 1.0
        length = math.sqrt(x * x + y * y + z * z)
        if length <= 1e-8:
            x, y, z = 0.0, 0.0, 1.0
        else:
            x, y, z = x / length, y / length, z / length
        return (
            min(max(x * 0.5 + 0.5, 0.0), 1.0),
            min(max(y * 0.5 + 0.5, 0.0), 1.0),
            min(max(z * 0.5 + 0.5, 0.0), 1.0),
        )

    return ImageFilter.Color3DLUT.generate(33, transform, channels=3)


def height_to_normal(height: Image.Image, node: GraphNode) -> Image.Image:
    source = height.convert("L")
    radius = max(0.0, min(_property_float(node, "radius", 1.0), 32.0))
    if radius > 0.0:
        source = source.filter(ImageFilter.GaussianBlur(radius))

    padded = _clamped_canvas(
        source.convert("RGBA"),
        (source.width + 2, source.height + 2),
        1,
        1,
    ).convert("L")
    gradient_x = padded.filter(
        ImageFilter.Kernel(
            (3, 3),
            (-1, 0, 1, -2, 0, 2, -1, 0, 1),
            scale=8,
            offset=128,
        )
    )
    gradient_y = padded.filter(
        ImageFilter.Kernel(
            (3, 3),
            (-1, -2, -1, 0, 0, 0, 1, 2, 1),
            scale=8,
            offset=128,
        )
    )
    gradient_x = gradient_x.crop((1, 1, source.width + 1, source.height + 1))
    gradient_y = gradient_y.crop((1, 1, source.width + 1, source.height + 1))
    neutral = Image.new("L", source.size, 255)
    gradients = Image.merge("RGB", (gradient_x, gradient_y, neutral))

    strength = max(0, min(_property_int(node, "strength", 100), 1000))
    directx = str(node.properties.get("convention", "opengl")).lower() == "directx"

    # A surface normal points against the height derivative. DirectX reverses Y.
    lut = _height_gradient_lut(strength, directx)
    normal = gradients.filter(lut)
    return normal.convert("RGBA")


@lru_cache(maxsize=128)
def _height_gradient_lut(
    strength_percent: int,
    directx: bool,
) -> ImageFilter.Color3DLUT:
    strength = strength_percent / 100.0

    def transform(red: float, green: float, _blue: float) -> tuple[float, float, float]:
        x = -(red * 2.0 - 1.0) * strength
        y = -(green * 2.0 - 1.0) * strength
        if directx:
            y = -y
        z = 1.0
        length = math.sqrt(x * x + y * y + z * z)
        return (
            x / length * 0.5 + 0.5,
            y / length * 0.5 + 0.5,
            z / length * 0.5 + 0.5,
        )

    return ImageFilter.Color3DLUT.generate(33, transform, channels=3)


def blend_normals_rnm(
    base_image: Image.Image,
    detail_image: Image.Image,
    node: GraphNode,
    mask: Image.Image | None = None,
) -> Image.Image:
    base = base_image.convert("RGBA")
    detail = detail_image.convert("RGBA")
    base_rgb = base.convert("RGB").filter(_normal_vector_lut())
    detail_strength = max(0, min(_property_int(node, "detail_strength", 100), 400))
    detail_rgb = detail.convert("RGB").filter(_normal_vector_lut(detail_strength))

    base_r, base_g, base_b = (band.convert("F") for band in base_rgb.split())
    detail_r, detail_g, detail_b = (band.convert("F") for band in detail_rgb.split())

    x1 = _image_math(lambda a: a["v"] / 127.5 - 1.0, v=base_r)
    y1 = _image_math(lambda a: a["v"] / 127.5 - 1.0, v=base_g)
    z1 = _image_math(lambda a: a["v"] / 127.5 - 1.0, v=base_b)
    x2 = _image_math(lambda a: a["v"] / 127.5 - 1.0, v=detail_r)
    y2 = _image_math(lambda a: a["v"] / 127.5 - 1.0, v=detail_g)
    z2 = _image_math(lambda a: a["v"] / 127.5 - 1.0, v=detail_b)

    # Reoriented Normal Mapping (RNM), self-shadow formulation by Hill/McGuire.
    tz = _image_math(lambda a: a["z"] + 1.000001, z=z1)
    dot = _image_math(
        lambda a: a["x1"] * (-a["x2"])
        + a["y1"] * (-a["y2"])
        + a["tz"] * a["z2"],
        x1=x1,
        y1=y1,
        x2=x2,
        y2=y2,
        tz=tz,
        z2=z2,
    )
    out_x = _image_math(
        lambda a: a["x1"] * a["dot"] / a["tz"] + a["x2"],
        x1=x1,
        x2=x2,
        dot=dot,
        tz=tz,
    )
    out_y = _image_math(
        lambda a: a["y1"] * a["dot"] / a["tz"] + a["y2"],
        y1=y1,
        y2=y2,
        dot=dot,
        tz=tz,
    )
    out_z = _image_math(
        lambda a: (a["z1"] + 1.0) * a["dot"] / a["tz"] - a["z2"],
        z1=z1,
        z2=z2,
        dot=dot,
        tz=tz,
    )

    encoded = Image.merge(
        "RGB",
        tuple(
            _image_math(lambda a: (a["v"] + 1.0) * 127.5, v=value).convert("L")
            for value in (out_x, out_y, out_z)
        ),
    ).filter(_normal_vector_lut())
    result = Image.merge("RGBA", (*encoded.split(), base.getchannel("A")))
    if mask is not None:
        result = Image.composite(result, base, mask.convert("L"))
    return result


def color_adjust(image: Image.Image, node: GraphNode) -> Image.Image:
    source = image.convert("RGBA")
    alpha = source.getchannel("A")
    exposure = max(-10.0, min(_property_float(node, "exposure", 0.0), 10.0))
    brightness = max(-100, min(_property_int(node, "brightness", 0), 100))
    contrast = max(0, min(_property_int(node, "contrast", 100), 400))
    gamma = max(0.05, min(_property_float(node, "gamma", 1.0), 8.0))
    rgb = source.convert("RGB").point(
        _color_adjust_lut(exposure, brightness, contrast, gamma)
    )

    saturation = max(0, min(_property_int(node, "saturation", 100), 400))
    if saturation != 100:
        rgb = ImageEnhance.Color(rgb).enhance(saturation / 100.0)

    hue = max(-180, min(_property_int(node, "hue", 0), 180))
    if hue:
        hue_channel, sat_channel, value_channel = rgb.convert("HSV").split()
        shift = round(hue / 360.0 * 256.0)
        hue_channel = hue_channel.point(tuple((value + shift) % 256 for value in range(256)))
        rgb = Image.merge("HSV", (hue_channel, sat_channel, value_channel)).convert("RGB")
    return Image.merge("RGBA", (*rgb.split(), alpha))


@lru_cache(maxsize=512)
def _color_adjust_lut(
    exposure: float,
    brightness: int,
    contrast: int,
    gamma: float,
) -> tuple[int, ...]:
    exposure_gain = 2.0**exposure
    brightness_offset = brightness / 100.0
    contrast_gain = contrast / 100.0
    inverse_gamma = 1.0 / gamma
    values: list[int] = []
    for value in range(256):
        normalized = value / 255.0
        normalized = normalized * exposure_gain + brightness_offset
        normalized = (normalized - 0.5) * contrast_gain + 0.5
        normalized = min(max(normalized, 0.0), 1.0) ** inverse_gamma
        values.append(round(normalized * 255.0))
    return tuple(values * 3)


def transform_2d(
    image: Image.Image,
    node: GraphNode,
    output_size: tuple[int, int],
    *,
    offset_scale: tuple[float, float] = (1.0, 1.0),
) -> Image.Image:
    source = image.convert("RGBA")
    if bool(node.properties.get("flip_horizontal", False)):
        source = source.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
    if bool(node.properties.get("flip_vertical", False)):
        source = source.transpose(Image.Transpose.FLIP_TOP_BOTTOM)

    rotation = _normalized_rotation(node.properties.get("rotation", 0))
    if rotation == 90:
        source = source.transpose(Image.Transpose.ROTATE_270)
    elif rotation == 180:
        source = source.transpose(Image.Transpose.ROTATE_180)
    elif rotation == 270:
        source = source.transpose(Image.Transpose.ROTATE_90)

    output_width, output_height = output_size
    scale = max(1, min(_property_int(node, "scale", 100), 1000)) / 100.0
    scaled_size = (
        max(1, round(source.width * scale)),
        max(1, round(source.height * scale)),
    )
    if scaled_size != source.size:
        source = source.resize(
            scaled_size,
            image_resampling(node.properties.get("filter", "bilinear")),
        )

    offset_x = round(_property_int(node, "offset_x", 0) * offset_scale[0])
    offset_y = round(_property_int(node, "offset_y", 0) * offset_scale[1])
    left = (output_width - source.width) // 2 + offset_x
    top = (output_height - source.height) // 2 + offset_y
    address_mode = str(node.properties.get("address_mode", "repeat")).lower()
    if address_mode == "clamp":
        return _clamped_canvas(source, output_size, left, top)
    return _tiled_canvas(
        source,
        output_size,
        left,
        top,
        mirrored=address_mode == "mirror",
    )


def resize_canvas(
    image: Image.Image,
    output_size: tuple[int, int],
    node: GraphNode,
) -> Image.Image:
    source = image.convert("RGBA")
    target_width, target_height = output_size
    mode = str(node.properties.get("resize_mode", "stretch")).lower()
    resampling = image_resampling(node.properties.get("filter", "lanczos"))
    if mode == "stretch":
        return source.resize(output_size, resampling)

    if mode == "crop":
        resized = source
    else:
        width_ratio = target_width / source.width
        height_ratio = target_height / source.height
        scale = max(width_ratio, height_ratio) if mode == "fill" else min(width_ratio, height_ratio)
        if mode == "pad":
            scale = min(1.0, scale)
        resized_size = (
            max(1, round(source.width * scale)),
            max(1, round(source.height * scale)),
        )
        resized = source if resized_size == source.size else source.resize(resized_size, resampling)

    if mode == "fill":
        left = max(0, (resized.width - target_width) // 2)
        top = max(0, (resized.height - target_height) // 2)
        return resized.crop((left, top, left + target_width, top + target_height))

    canvas = Image.new("RGBA", output_size, (0, 0, 0, 0))
    left = (target_width - resized.width) // 2
    top = (target_height - resized.height) // 2
    canvas.alpha_composite(resized, (left, top))
    return canvas


def resize_output_size(input_size: tuple[int, int], node: GraphNode) -> tuple[int, int]:
    size_mode = str(node.properties.get("size_mode", "exact")).lower()
    if size_mode == "exact":
        return (
            max(1, min(_property_int(node, "width", 2048), 16384)),
            max(1, min(_property_int(node, "height", 2048), 16384)),
        )
    return tuple(_power_of_two(value, size_mode) for value in input_size)


def transformed_output_size(input_size: tuple[int, int], node: GraphNode) -> tuple[int, int]:
    if _normalized_rotation(node.properties.get("rotation", 0)) in (90, 270):
        return (input_size[1], input_size[0])
    return input_size


def _normalized_rotation(value: object) -> int:
    try:
        rotation = int(value) % 360
    except (TypeError, ValueError):
        return 0
    return rotation if rotation in (0, 90, 180, 270) else 0


def _power_of_two(value: int, mode: str) -> int:
    value = max(1, min(int(value), 16384))
    lower = 1 << (value.bit_length() - 1)
    upper = min(16384, lower if lower == value else lower << 1)
    if mode == "pot_down":
        return lower
    if mode == "pot_nearest":
        return lower if value - lower <= upper - value else upper
    return upper


def _clamped_canvas(
    source: Image.Image,
    output_size: tuple[int, int],
    left: int,
    top: int,
) -> Image.Image:
    output_width, output_height = output_size
    pad_left = max(0, left)
    pad_top = max(0, top)
    pad_right = max(0, output_width - left - source.width)
    pad_bottom = max(0, output_height - top - source.height)
    extended = Image.new(
        "RGBA",
        (pad_left + source.width + pad_right, pad_top + source.height + pad_bottom),
    )
    extended.paste(source, (pad_left, pad_top))
    if pad_left:
        extended.paste(
            source.crop((0, 0, 1, source.height)).resize((pad_left, source.height)),
            (0, pad_top),
        )
    if pad_right:
        extended.paste(
            source.crop((source.width - 1, 0, source.width, source.height)).resize(
                (pad_right, source.height)
            ),
            (pad_left + source.width, pad_top),
        )
    if pad_top:
        row = extended.crop((0, pad_top, extended.width, pad_top + 1))
        extended.paste(row.resize((extended.width, pad_top)), (0, 0))
    if pad_bottom:
        row_y = pad_top + source.height - 1
        row = extended.crop((0, row_y, extended.width, row_y + 1))
        extended.paste(row.resize((extended.width, pad_bottom)), (0, row_y + 1))
    crop_left = -left + pad_left
    crop_top = -top + pad_top
    return extended.crop(
        (crop_left, crop_top, crop_left + output_width, crop_top + output_height)
    )


def _tiled_canvas(
    source: Image.Image,
    output_size: tuple[int, int],
    left: int,
    top: int,
    *,
    mirrored: bool,
) -> Image.Image:
    output_width, output_height = output_size
    canvas = Image.new("RGBA", output_size, (0, 0, 0, 0))
    first_x = math.floor(-left / source.width) - 1
    last_x = math.ceil((output_width - left) / source.width) + 1
    first_y = math.floor(-top / source.height) - 1
    last_y = math.ceil((output_height - top) / source.height) + 1
    tiles = {(False, False): source}
    if mirrored:
        tiles[(True, False)] = source.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
        tiles[(False, True)] = source.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
        tiles[(True, True)] = source.transpose(Image.Transpose.ROTATE_180)
    for tile_y in range(first_y, last_y + 1):
        for tile_x in range(first_x, last_x + 1):
            tile = tiles[(bool(tile_x % 2), bool(tile_y % 2))] if mirrored else source
            canvas.alpha_composite(
                tile,
                (left + tile_x * source.width, top + tile_y * source.height),
            )
    return canvas


def _image_math(expression, **images: Image.Image) -> Image.Image:
    return ImageMath.lambda_eval(expression, **images)


def _property_int(node: GraphNode, key: str, default: int) -> int:
    try:
        return int(node.properties.get(key, default))
    except (TypeError, ValueError):
        return default


def _property_float(node: GraphNode, key: str, default: float) -> float:
    try:
        return float(node.properties.get(key, default))
    except (TypeError, ValueError):
        return default
