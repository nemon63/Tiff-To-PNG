from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, replace
import math
from pathlib import Path

from PIL import Image

from image_converter.domain.constants import SUPPORTED_SOURCE_EXTENSIONS
from image_converter.domain.models import (
    AssetKind,
    AssetMetadata,
    BatchSource,
    NormalMapStatistics,
    QueueItem,
    QueueStatus,
    TextureMapType,
)
from image_converter.services.image_loading import (
    copy_first_frame_preserving_alpha,
    image_has_alpha,
    prepare_image_header_preserving_alpha,
)
from image_converter.services.map_types import detect_channel_pack_layout, detect_texture_map_type


@dataclass(slots=True, frozen=True)
class AssetScanResult:
    items: tuple[QueueItem, ...]
    ignored_messages: tuple[str, ...]


class AssetScanner:
    def scan_paths(
        self,
        paths: Iterable[Path],
        *,
        recursive: bool,
        item_callback: Callable[[QueueItem], None] | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> AssetScanResult:
        items: list[QueueItem] = []
        ignored_messages: list[str] = []
        seen_paths: set[Path] = set()

        for path in paths:
            for source in self._iter_sources(
                path,
                recursive=recursive,
                ignored_messages=ignored_messages,
                cancelled=cancelled,
            ):
                if cancelled is not None and cancelled():
                    break
                resolved_path = source.path.resolve()
                if resolved_path in seen_paths:
                    continue
                seen_paths.add(resolved_path)
                item = self._build_queue_item(source)
                items.append(item)
                if item_callback is not None:
                    item_callback(item)
            if cancelled is not None and cancelled():
                break

        items.sort(key=lambda item: str(item.path).lower())
        return AssetScanResult(items=tuple(items), ignored_messages=tuple(ignored_messages))

    def _iter_sources(
        self,
        path: Path,
        *,
        recursive: bool,
        ignored_messages: list[str],
        cancelled: Callable[[], bool] | None,
    ) -> Iterator[BatchSource]:
        if not path.exists():
            ignored_messages.append(f"Путь не найден: {path}")
            return

        if path.is_file():
            if path.suffix.lower() not in SUPPORTED_SOURCE_EXTENSIONS:
                ignored_messages.append(f"Пропуск неподдерживаемого файла: {path.name}")
                return
            yield BatchSource(
                path=path,
                root=path.parent,
                packed_layout=detect_channel_pack_layout(path),
            )
            return

        iterator = path.rglob("*") if recursive else path.glob("*")
        supported_count = 0
        unsupported_count = 0
        for file_path in iterator:
            if cancelled is not None and cancelled():
                return
            if not file_path.is_file():
                continue
            if file_path.suffix.lower() in SUPPORTED_SOURCE_EXTENSIONS:
                supported_count += 1
                yield BatchSource(
                    path=file_path,
                    root=path,
                    packed_layout=detect_channel_pack_layout(file_path),
                )
            else:
                unsupported_count += 1

        if unsupported_count:
            ignored_messages.append(
                f"В папке {path.name} пропущено неподдерживаемых файлов: {unsupported_count}"
            )
        if supported_count == 0:
            ignored_messages.append(f"В папке не найдено поддерживаемых файлов: {path}")

    def _build_queue_item(self, source: BatchSource) -> QueueItem:
        try:
            metadata = self._read_metadata(source.path)
            message = "; ".join(metadata.warnings)
            return QueueItem(
                source=source,
                asset_kind=AssetKind.IMAGE,
                metadata=metadata,
                status=QueueStatus.READY,
                message=message,
            )
        except Exception as exc:
            return QueueItem(
                source=source,
                asset_kind=AssetKind.UNKNOWN,
                metadata=None,
                status=QueueStatus.ERROR,
                message=str(exc),
            )

    def _read_metadata(self, path: Path) -> AssetMetadata:
        file_size = path.stat().st_size
        map_type = detect_texture_map_type(path)
        validation_grayscale_sample: bytes | None = None
        normal_map_statistics: NormalMapStatistics | None = None
        with Image.open(path) as image:
            prepare_image_header_preserving_alpha(image)
            width, height = image.size
            mode = image.mode
            frame_count = getattr(image, "n_frames", 1)
            has_alpha = image_has_alpha(image)
            format_name = (image.format or path.suffix.removeprefix(".")).upper()
            if map_type in (TextureMapType.ROUGHNESS, TextureMapType.SMOOTHNESS):
                validation_image = copy_first_frame_preserving_alpha(image).convert("L")
                validation_image = validation_image.resize(
                    (128, 128),
                    Image.Resampling.BILINEAR,
                )
                validation_grayscale_sample = validation_image.tobytes()
            elif map_type is TextureMapType.NORMAL:
                validation_image = copy_first_frame_preserving_alpha(image).convert("RGB")
                validation_image = validation_image.resize(
                    (128, 128),
                    Image.Resampling.BILINEAR,
                )
                normal_map_statistics = self._normal_map_statistics(validation_image)

        warnings: list[str] = []
        if width <= 0 or height <= 0:
            warnings.append("Некорректное разрешение")
        if width > 8192 or height > 8192:
            warnings.append("Очень большое изображение")
        if not self._is_power_of_two(width) or not self._is_power_of_two(height):
            warnings.append("Разрешение не кратно power-of-two")
        if mode == "CMYK":
            warnings.append("CMYK требует проверки перед экспортом")
        if frame_count > 1:
            warnings.append("Будет использован только первый кадр/слой")
        if file_size > 128 * 1024 * 1024:
            warnings.append("Очень большой файл")
        if file_size <= 0:
            warnings.append("Пустой файл")

        return AssetMetadata(
            format_name=format_name,
            width=width,
            height=height,
            mode=mode,
            has_alpha=has_alpha,
            file_size_bytes=file_size,
            map_type=map_type,
            frame_count=frame_count,
            warnings=tuple(warnings),
            alpha_fully_opaque=None if has_alpha else False,
            validation_grayscale_sample=validation_grayscale_sample,
            normal_map_statistics=normal_map_statistics,
        )

    def analyze_alpha(self, path: Path, metadata: AssetMetadata) -> AssetMetadata:
        if metadata.alpha_fully_opaque is not None:
            return metadata
        fully_opaque = False
        with Image.open(path) as image:
            working_image = copy_first_frame_preserving_alpha(image)
            if image_has_alpha(working_image):
                alpha = working_image.convert("RGBA").getchannel("A")
                alpha_min, alpha_max = alpha.getextrema()
                fully_opaque = alpha_min == 255 and alpha_max == 255
        warning = "Альфа-канал есть, но полностью непрозрачный"
        warnings = tuple(item for item in metadata.warnings if item != warning)
        if fully_opaque:
            warnings = (*warnings, warning)
        return replace(
            metadata,
            warnings=warnings,
            alpha_fully_opaque=fully_opaque,
        )

    @staticmethod
    def _is_power_of_two(value: int) -> bool:
        return value > 0 and (value & (value - 1)) == 0

    @staticmethod
    def _normal_map_statistics(image: Image.Image) -> NormalMapStatistics:
        raw = image.tobytes()
        pixel_count = max(1, len(raw) // 3)
        red_total = 0
        green_total = 0
        blue_total = 0
        negative_z_count = 0
        length_outlier_count = 0
        for index in range(0, len(raw), 3):
            red, green, blue = raw[index : index + 3]
            red_total += red
            green_total += green
            blue_total += blue
            x = red / 127.5 - 1.0
            y = green / 127.5 - 1.0
            z = blue / 127.5 - 1.0
            if z < 0.0:
                negative_z_count += 1
            if abs(math.sqrt(x * x + y * y + z * z) - 1.0) > 0.35:
                length_outlier_count += 1
        return NormalMapStatistics(
            mean_rgb=(
                red_total / pixel_count,
                green_total / pixel_count,
                blue_total / pixel_count,
            ),
            negative_z_ratio=negative_z_count / pixel_count,
            vector_length_outlier_ratio=length_outlier_count / pixel_count,
        )
