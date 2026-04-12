from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class ResizeMode(str, Enum):
    NONE = "none"
    PERCENT = "percent"
    MAX_SIDE = "max_side"


class ConversionStatus(str, Enum):
    SUCCESS = "success"
    SKIPPED = "skipped"
    FAILED = "failed"


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
class BatchRequest:
    input_path: Path | None
    output_root: Path | None
    options: ConversionOptions


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
