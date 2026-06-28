from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class ResizeMode(str, Enum):
    NONE = "none"
    PERCENT = "percent"
    MAX_SIDE = "max_side"


class PresetScope(str, Enum):
    SYSTEM = "system"
    USER = "user"


class AssetKind(str, Enum):
    IMAGE = "image"
    UNKNOWN = "unknown"


class TextureMapType(str, Enum):
    UNKNOWN = "unknown"
    BASECOLOR = "basecolor"
    NORMAL = "normal"
    ROUGHNESS = "roughness"
    SMOOTHNESS = "smoothness"
    METALLIC = "metallic"
    AO = "ao"
    OPACITY = "opacity"
    EMISSIVE = "emissive"
    HEIGHT = "height"

    @property
    def label(self) -> str:
        mapping = {
            TextureMapType.UNKNOWN: "Unknown",
            TextureMapType.BASECOLOR: "BaseColor",
            TextureMapType.NORMAL: "Normal",
            TextureMapType.ROUGHNESS: "Roughness",
            TextureMapType.SMOOTHNESS: "Smoothness",
            TextureMapType.METALLIC: "Metallic",
            TextureMapType.AO: "AO",
            TextureMapType.OPACITY: "Opacity",
            TextureMapType.EMISSIVE: "Emissive",
            TextureMapType.HEIGHT: "Height",
        }
        return mapping[self]


class TextureColorSpace(str, Enum):
    UNKNOWN = "unknown"
    SRGB = "srgb"
    LINEAR = "linear"

    @property
    def label(self) -> str:
        mapping = {
            TextureColorSpace.UNKNOWN: "Auto",
            TextureColorSpace.SRGB: "sRGB",
            TextureColorSpace.LINEAR: "Linear",
        }
        return mapping[self]


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


class ChannelPackLayout(str, Enum):
    ORM = "orm"
    RMA = "rma"
    MRA = "mra"
    UNITY_URP = "unity_urp"
    UNITY_HDRP = "unity_hdrp"

    @property
    def label(self) -> str:
        mapping = {
            ChannelPackLayout.ORM: "ORM",
            ChannelPackLayout.RMA: "RMA",
            ChannelPackLayout.MRA: "MRA",
            ChannelPackLayout.UNITY_URP: "Unity URP",
            ChannelPackLayout.UNITY_HDRP: "Unity HDRP",
        }
        return mapping[self]


class ChannelPackingMode(str, Enum):
    AFTER_CONVERSION = "after_conversion"
    PACK_WITH_REMAINDER = "pack_with_remainder"
    PACK_ONLY = "pack_only"

    @property
    def label(self) -> str:
        mapping = {
            ChannelPackingMode.AFTER_CONVERSION: "Сначала PNG, потом Packed",
            ChannelPackingMode.PACK_WITH_REMAINDER: "Packed + нужные карты",
            ChannelPackingMode.PACK_ONLY: "Только Packed",
        }
        return mapping[self]


@dataclass(slots=True, frozen=True)
class NamingRules:
    lowercase: bool = False
    replace_spaces: bool = False
    normalize_map_suffix: bool = False

    @property
    def is_enabled(self) -> bool:
        return self.lowercase or self.replace_spaces or self.normalize_map_suffix


@dataclass(slots=True, frozen=True)
class ChannelPackingOptions:
    enabled: bool = False
    layout: ChannelPackLayout = ChannelPackLayout.ORM
    mode: ChannelPackingMode = ChannelPackingMode.AFTER_CONVERSION


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
    naming: NamingRules = field(default_factory=NamingRules)
    packing: ChannelPackingOptions = field(default_factory=ChannelPackingOptions)


@dataclass(slots=True, frozen=True)
class ConversionPreset:
    preset_id: str
    name: str
    options: ConversionOptions
    scope: PresetScope
    description: str = ""

    @property
    def is_system(self) -> bool:
        return self.scope is PresetScope.SYSTEM


@dataclass(slots=True, frozen=True)
class BatchSource:
    path: Path
    root: Path | None = None
    map_type: TextureMapType = TextureMapType.UNKNOWN


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
    map_type: TextureMapType = TextureMapType.UNKNOWN
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
    map_type_override: TextureMapType | None = None

    @property
    def path(self) -> Path:
        return self.source.path

    @property
    def root(self) -> Path | None:
        return self.source.root

    @property
    def effective_map_type(self) -> TextureMapType:
        if self.map_type_override is not None:
            return self.map_type_override
        if self.metadata is not None:
            return self.metadata.map_type
        return TextureMapType.UNKNOWN

    @property
    def batch_source(self) -> BatchSource:
        return BatchSource(
            path=self.path,
            root=self.root,
            map_type=self.effective_map_type,
        )



@dataclass(slots=True, frozen=True)
class ConversionResult:
    source: Path
    destination: Path | None
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
    packed_created: int = 0
    packed_skipped: int = 0
    packed_failed: int = 0
    packed_only_mode: bool = False

    def register(self, result: ConversionResult) -> None:
        self.total += 1
        if result.status is ConversionStatus.SUCCESS:
            self.succeeded += 1
        elif result.status is ConversionStatus.SKIPPED:
            self.skipped += 1
        else:
            self.failed += 1

    def register_packed(self, status: ConversionStatus) -> None:
        if status is ConversionStatus.SUCCESS:
            self.packed_created += 1
        elif status is ConversionStatus.SKIPPED:
            self.packed_skipped += 1
        else:
            self.packed_failed += 1

    def as_text(self) -> str:
        if self.packed_only_mode:
            return (
                "Готово. packed only: "
                f"создано={self.packed_created}, "
                f"пропущено={self.packed_skipped}, "
                f"ошибок={self.packed_failed}"
            )

        base_text = (
            f"Готово. всего={self.total}, успешно={self.succeeded}, "
            f"пропущено={self.skipped}, ошибок={self.failed}"
        )
        if self.packed_created or self.packed_skipped or self.packed_failed:
            return (
                base_text
                + "; packed textures: "
                + f"создано={self.packed_created}, "
                + f"пропущено={self.packed_skipped}, "
                + f"ошибок={self.packed_failed}"
            )
        return base_text


@dataclass(slots=True, frozen=True)
class AppSettings:
    input_path: str = ""
    output_path: str = ""
    workspace_mode: str = "batch"
    graph_auto_watch: bool = False
    graph_auto_export: bool = False
    recent_graph_projects: tuple[str, ...] = ()
    options: ConversionOptions = field(default_factory=ConversionOptions)
    window_width: int = 1280
    window_height: int = 820
    splitter_sizes: tuple[int, ...] = (300, 980)
    workspace_splitter_sizes: tuple[int, ...] = (980, 340)
    detail_splitter_sizes: tuple[int, ...] = (640, 180)
    inspector_splitter_sizes: tuple[int, ...] = (1,)
    window_geometry: str = ""
    window_state: str = ""
