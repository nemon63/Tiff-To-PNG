from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from image_converter.domain.constants import SUPPORTED_SOURCE_EXTENSIONS
from image_converter.domain.models import AssetKind, AssetMetadata, BatchSource, QueueItem, QueueStatus


@dataclass(slots=True, frozen=True)
class AssetScanResult:
    items: tuple[QueueItem, ...]
    ignored_messages: tuple[str, ...]


class AssetScanner:
    def scan_paths(self, paths: Iterable[Path], *, recursive: bool) -> AssetScanResult:
        items: list[QueueItem] = []
        ignored_messages: list[str] = []
        seen_paths: set[Path] = set()

        for path in paths:
            expanded_sources, ignored = self._expand_path(path, recursive=recursive)
            ignored_messages.extend(ignored)

            for source in expanded_sources:
                resolved_path = source.path.resolve()
                if resolved_path in seen_paths:
                    continue
                seen_paths.add(resolved_path)
                items.append(self._build_queue_item(source))

        items.sort(key=lambda item: str(item.path).lower())
        return AssetScanResult(items=tuple(items), ignored_messages=tuple(ignored_messages))

    def _expand_path(self, path: Path, *, recursive: bool) -> tuple[list[BatchSource], list[str]]:
        if not path.exists():
            return [], [f"Путь не найден: {path}"]

        if path.is_file():
            if path.suffix.lower() not in SUPPORTED_SOURCE_EXTENSIONS:
                return [], [f"Пропуск неподдерживаемого файла: {path.name}"]
            return [BatchSource(path=path, root=path.parent)], []

        iterator = path.rglob("*") if recursive else path.glob("*")
        sources: list[BatchSource] = []
        ignored: list[str] = []
        unsupported_count = 0

        for file_path in iterator:
            if not file_path.is_file():
                continue
            if file_path.suffix.lower() in SUPPORTED_SOURCE_EXTENSIONS:
                sources.append(BatchSource(path=file_path, root=path))
            else:
                unsupported_count += 1

        if sources:
            if unsupported_count:
                ignored.append(
                    f"В папке {path.name} пропущено неподдерживаемых файлов: {unsupported_count}"
                )
            return sources, ignored

        return [], [f"В папке не найдено поддерживаемых файлов: {path}"]

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
        with Image.open(path) as image:
            width, height = image.size
            mode = image.mode
            frame_count = getattr(image, "n_frames", 1)
            has_alpha = "A" in image.getbands() or (
                image.mode == "P" and "transparency" in image.info
            )
            format_name = (image.format or path.suffix.removeprefix(".")).upper()
            alpha_fully_opaque = False
            if has_alpha and "A" in image.getbands():
                alpha_min, alpha_max = image.getchannel("A").getextrema()
                alpha_fully_opaque = alpha_min == 255 and alpha_max == 255

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
        if alpha_fully_opaque:
            warnings.append("Альфа-канал есть, но полностью непрозрачный")
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
            frame_count=frame_count,
            warnings=tuple(warnings),
        )

    @staticmethod
    def _is_power_of_two(value: int) -> bool:
        return value > 0 and (value & (value - 1)) == 0
