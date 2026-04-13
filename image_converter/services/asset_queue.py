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
        sources = [
            BatchSource(path=file_path, root=path)
            for file_path in iterator
            if file_path.is_file() and file_path.suffix.lower() in SUPPORTED_SOURCE_EXTENSIONS
        ]

        if sources:
            return sources, []

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
            has_alpha = "A" in image.getbands() or (
                image.mode == "P" and "transparency" in image.info
            )
            format_name = (image.format or path.suffix.removeprefix(".")).upper()

        warnings: list[str] = []
        if width <= 0 or height <= 0:
            warnings.append("Некорректное разрешение")
        if width > 8192 or height > 8192:
            warnings.append("Очень большое изображение")
        if file_size <= 0:
            warnings.append("Пустой файл")

        return AssetMetadata(
            format_name=format_name,
            width=width,
            height=height,
            mode=mode,
            has_alpha=has_alpha,
            file_size_bytes=file_size,
            warnings=tuple(warnings),
        )
