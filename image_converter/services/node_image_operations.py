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
    offset_x = round(_property_int(node, "offset_x", 0) * offset_scale[0])
    offset_y = round(_property_int(node, "offset_y", 0) * offset_scale[1])
    left = (output_width - scaled_size[0]) // 2 + offset_x
    top = (output_height - scaled_size[1]) // 2 + offset_y
    address_mode = str(node.properties.get("address_mode", "repeat")).lower()
    return _resampled_canvas(
        source, scaled_size, output_size, left, top,
        address_mode=address_mode,
        resampling=image_resampling(node.properties.get("filter", "bilinear")),
    )


def resize_canvas(
    image: Image.Image,
    output_size: tuple[int, int],
    node: GraphNode,
    *,
    natural_input_size: tuple[int, int] | None = None,
    natural_output_size: tuple[int, int] | None = None,
) -> Image.Image:
    source = image.convert("RGBA")
    target_width, target_height = output_size
    mode = str(node.properties.get("resize_mode", "stretch")).lower()
    resampling = image_resampling(node.properties.get("filter", "lanczos"))
    if mode == "stretch":
        return source.resize(output_size, resampling)

    # Layout is defined in document pixels, independently of preview resolution.
    input_width, input_height = natural_input_size or source.size
    canvas_width, canvas_height = natural_output_size or output_size
    if mode == "crop":
        logical_size = (input_width, input_height)
    else:
        width_ratio = canvas_width / input_width
        height_ratio = canvas_height / input_height
        scale = max(width_ratio, height_ratio) if mode == "fill" else min(width_ratio, height_ratio)
        if mode == "pad":
            scale = min(1.0, scale)
        logical_size = (
            max(1, round(input_width * scale)),
            max(1, round(input_height * scale)),
        )

    if mode == "fill":
        logical_left = -max(0, (logical_size[0] - canvas_width) // 2)
        logical_top = -max(0, (logical_size[1] - canvas_height) // 2)
    else:
        logical_left = (canvas_width - logical_size[0]) // 2
        logical_top = (canvas_height - logical_size[1]) // 2

    scale_x = target_width / canvas_width
    scale_y = target_height / canvas_height
    left = round(logical_left * scale_x)
    top = round(logical_top * scale_y)
    resized_size = (
        max(1, round((logical_left + logical_size[0]) * scale_x) - left),
        max(1, round((logical_top + logical_size[1]) * scale_y) - top),
    )
    resized = source if resized_size == source.size else source.resize(resized_size, resampling)
    if mode == "fill":
        return resized.crop((-left, -top, target_width - left, target_height - top))

    canvas = Image.new("RGBA", output_size, (0, 0, 0, 0))
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
    return _resampled_canvas(
        source, source.size, output_size, left, top,
        address_mode="clamp", resampling=Image.Resampling.NEAREST,
    )


def _canvas_axis_spans(
    tile_length: int,
    output_length: int,
    offset: int,
    address_mode: str,
) -> list[tuple[int, int, int, int, bool]]:
    """Return destination/source intervals for a clamp or one repeat period."""
    if address_mode == "clamp":
        start = max(0, min(offset, output_length))
        end = max(0, min(offset + tile_length, output_length))
        spans = []
        if start:
            spans.append((0, start, 0, 1, False))
        if end > start:
            spans.append((start, end, start - offset, end - offset, False))
        if end < output_length:
            spans.append((end, output_length, tile_length - 1, tile_length, False))
        return spans

    mirrored = address_mode == "mirror"
    length = min(output_length, tile_length * (2 if mirrored else 1))
    spans = []
    position = 0
    while position < length:
        tile_index, local = divmod(position - offset, tile_length)
        count = min(length - position, tile_length - local)
        flipped = mirrored and bool(tile_index % 2)
        start = tile_length - local - count if flipped else local
        spans.append((position, position + count, start, start + count, flipped))
        position += count
    return spans


@lru_cache(maxsize=128)
def _nearest_resize_offset(start: int, input_length: int, scaled_length: int) -> float:
    # Match Pillow's full-image nearest sampling, including accumulated rounding
    # at exact pixel boundaries. Multiplication instead of addition changes ties.
    step = input_length / scaled_length
    position = step * 0.5
    for _ in range(start):
        position += step
    return position - step * 0.5


def _resampled_canvas(
    source: Image.Image,
    scaled_size: tuple[int, int],
    output_size: tuple[int, int],
    left: int,
    top: int,
    *,
    address_mode: str,
    resampling: Image.Resampling,
) -> Image.Image:
    """Sample only visible regions; never materialize an enlarged texture/padding."""
    output_width, output_height = output_size
    scaled_width, scaled_height = scaled_size
    if scaled_size == source.size:
        scaled_source = source
    elif scaled_width <= source.width and scaled_height <= source.height:
        # Downsampling once retains the filter's full footprint and is cheap to tile.
        scaled_source = source.resize(scaled_size, resampling)
    else:
        scaled_source = None
    sampling_source = source
    if scaled_source is None and resampling != Image.Resampling.NEAREST:
        # Match Pillow's premultiplied-alpha resize without converting the input
        # again for each visible region.
        sampling_source = source.convert("RGBa")

    def sample(box: tuple[int, int, int, int]) -> Image.Image:
        if scaled_source is not None:
            return scaled_source.crop(box)
        x0, y0, x1, y1 = box
        size = (x1 - x0, y1 - y0)
        scale_x = source.width / scaled_width
        scale_y = source.height / scaled_height
        if resampling == Image.Resampling.NEAREST:
            return source.transform(
                size, Image.Transform.AFFINE,
                (
                    scale_x, 0, _nearest_resize_offset(x0, source.width, scaled_width),
                    0, scale_y, _nearest_resize_offset(y0, source.height, scaled_height),
                ),
                resample=resampling,
            )
        return sampling_source.resize(
            size, resampling,
            box=(x0 * scale_x, y0 * scale_y, x1 * scale_x, y1 * scale_y),
        ).convert("RGBA")

    x_spans = _canvas_axis_spans(scaled_width, output_width, left, address_mode)
    y_spans = _canvas_axis_spans(scaled_height, output_height, top, address_mode)
    canvas = Image.new("RGBA", output_size, (0, 0, 0, 0))
    for dy0, dy1, sy0, sy1, flip_y in y_spans:
        for dx0, dx1, sx0, sx1, flip_x in x_spans:
            tile = sample((sx0, sy0, sx1, sy1))
            if flip_x:
                tile = tile.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
            if flip_y:
                tile = tile.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
            if address_mode == "clamp":
                if tile.size != (dx1 - dx0, dy1 - dy0):
                    tile = tile.resize((dx1 - dx0, dy1 - dy0), Image.Resampling.NEAREST)
                canvas.paste(tile, (dx0, dy0))
            else:
                canvas.alpha_composite(tile, (dx0, dy0))

    # Replicate the rendered period by doubling, rather than compositing thousands
    # of individual tiles when the texture is scaled down to 1%.
    filled_width = x_spans[-1][1]
    filled_height = y_spans[-1][1]
    while filled_width < output_width:
        count = min(filled_width, output_width - filled_width)
        canvas.paste(canvas.crop((0, 0, count, filled_height)), (filled_width, 0))
        filled_width += count
    while filled_height < output_height:
        count = min(filled_height, output_height - filled_height)
        canvas.paste(canvas.crop((0, 0, output_width, count)), (0, filled_height))
        filled_height += count
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
