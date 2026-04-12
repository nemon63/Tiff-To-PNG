from __future__ import annotations

from image_converter.domain.constants import SUPPORTED_SOURCE_EXTENSIONS
from image_converter.domain.errors import ValidationError
from image_converter.domain.models import BatchRequest, ResizeMode


def validate_request(request: BatchRequest) -> None:
    input_path = request.input_path
    output_root = request.output_root
    options = request.options

    if input_path is None:
        raise ValidationError("Выберите входной файл или папку.")

    if not input_path.exists():
        raise ValidationError(f"Входной путь не существует:\n{input_path}")

    if input_path.is_file() and input_path.suffix.lower() not in SUPPORTED_SOURCE_EXTENSIONS:
        supported = ", ".join(sorted(SUPPORTED_SOURCE_EXTENSIONS))
        raise ValidationError(
            f"Неподдерживаемый формат: {input_path.suffix}\nПоддерживаются: {supported}"
        )

    if output_root is not None and output_root.exists() and output_root.is_file():
        raise ValidationError("Выходной путь указывает на файл, а не на папку.")

    if not 0 <= options.compress_level <= 9:
        raise ValidationError("Степень сжатия должна быть в диапазоне 0..9.")

    if not 2 <= options.png8_colors <= 256:
        raise ValidationError("Количество цветов PNG-8 должно быть в диапазоне 2..256.")

    if options.resize_mode is ResizeMode.PERCENT and options.resize_percent <= 0:
        raise ValidationError("Процент масштабирования должен быть больше 0.")

    if options.resize_mode is ResizeMode.MAX_SIDE and options.max_side <= 0:
        raise ValidationError("Максимальная сторона должна быть больше 0.")
