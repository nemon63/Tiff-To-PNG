from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class ResizeMode(str, Enum):
    NONE = "none"
    PERCENT = "percent"
    MAX_SIDE = "max_side"


class AssetKind(str, Enum):
    IMAGE = "image"
    UNKNOWN = "unknown"


class ConversionStatus(str, Enum):
    SUCCESS = "success"
    SKIPPED = "skipped"
    FAILED = "failed"


class QueueStatus(str, Enum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    DONE = "done"
    SKIPPED = "skipped"
    ERROR = "error"


class PreviewChannel(str, Enum):
    COMPOSITE = "composite"
    RED = "red"
    GREEN = "green"
    BLUE = "blue"
    ALPHA = "alpha"
    LUMA = "luma"


@dataclass(slots=True, frozen=True)
class ConversionOptions:
    recursive: bool = True
    force_rgba: bool = False
    overwrite: bool = False
    delete_source: bool = False
    optimize: bool = True
    compress_level: int = 6
    resize_mode: ResizeMode = ResizeMode.NONE
    resize_percent: int = 100
    max_side: int = 2048
    png8: bool = False
    png8_colors: int = 256
    dither: bool = True


@dataclass(slots=True, frozen=True)
class BatchSource:
    path: Path
    root: Path | None = None


@dataclass(slots=True, frozen=True)
class BatchRequest:
    input_path: Path | None
    output_root: Path | None
    options: ConversionOptions
    sources: tuple[BatchSource, ...] = ()


@dataclass(slots=True, frozen=True)
class AssetMetadata:
    format_name: str
    width: int
    height: int
    mode: str
    has_alpha: bool
    file_size_bytes: int
    frame_count: int = 1
    warnings: tuple[str, ...] = ()

    @property
    def resolution_text(self) -> str:
        if self.width <= 0 or self.height <= 0:
            return "-"
        return f"{self.width}x{self.height}"

    @property
    def size_text(self) -> str:
        if self.file_size_bytes <= 0:
            return "0 B"
        size = float(self.file_size_bytes)
        units = ["B", "KB", "MB", "GB"]
        unit_index = 0
        while size >= 1024 and unit_index < len(units) - 1:
            size /= 1024.0
            unit_index += 1
        if unit_index == 0:
            return f"{int(size)} {units[unit_index]}"
        return f"{size:.1f} {units[unit_index]}"

    @property
    def warning_count(self) -> int:
        return len(self.warnings)

    @property
    def warning_summary(self) -> str:
        if not self.warnings:
            return ""
        return "; ".join(self.warnings)


@dataclass(slots=True)
class QueueItem:
    source: BatchSource
    asset_kind: AssetKind
    metadata: AssetMetadata | None
    status: QueueStatus = QueueStatus.READY
    output_path: Path | None = None
    message: str = ""

    @property
    def path(self) -> Path:
        return self.source.path

    @property
    def root(self) -> Path | None:
        return self.source.root



@dataclass(slots=True, frozen=True)
class ConversionResult:
    source: Path
    destination: Path
    status: ConversionStatus
    message: str

    @property
    def is_success(self) -> bool:
        return self.status is ConversionStatus.SUCCESS


@dataclass(slots=True)
class BatchSummary:
    total: int = 0
    succeeded: int = 0
    skipped: int = 0
    failed: int = 0

    def register(self, result: ConversionResult) -> None:
        self.total += 1
        if result.status is ConversionStatus.SUCCESS:
            self.succeeded += 1
        elif result.status is ConversionStatus.SKIPPED:
            self.skipped += 1
        else:
            self.failed += 1

    def as_text(self) -> str:
        return (
            f"Готово. всего={self.total}, успешно={self.succeeded}, "
            f"пропущено={self.skipped}, ошибок={self.failed}"
        )


@dataclass(slots=True, frozen=True)
class AppSettings:
    input_path: str = ""
    output_path: str = ""
    options: ConversionOptions = field(default_factory=ConversionOptions)
    window_width: int = 1280
    window_height: int = 820
    splitter_sizes: tuple[int, int] = (340, 940)
    workspace_splitter_sizes: tuple[int, int] = (500, 440)
    detail_splitter_sizes: tuple[int, int] = (420, 220)
    inspector_splitter_sizes: tuple[int, int] = (500, 190)
