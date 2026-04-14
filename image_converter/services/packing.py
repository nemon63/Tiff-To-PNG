from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageSequence

from image_converter.domain.models import (
    BatchSource,
    ChannelPackLayout,
    ConversionOptions,
    ConversionStatus,
    TextureMapType,
)
from image_converter.services.map_types import known_map_aliases
from image_converter.services.naming import apply_naming_rules
from image_converter.services.pipeline import RESAMPLING_LANCZOS, ResizeProcessor

PACK_LAYOUTS: dict[ChannelPackLayout, tuple[tuple[str, TextureMapType], ...]] = {
    ChannelPackLayout.ORM: (
        ("R", TextureMapType.AO),
        ("G", TextureMapType.ROUGHNESS),
        ("B", TextureMapType.METALLIC),
    ),
    ChannelPackLayout.RMA: (
        ("R", TextureMapType.ROUGHNESS),
        ("G", TextureMapType.METALLIC),
        ("B", TextureMapType.AO),
    ),
    ChannelPackLayout.MRA: (
        ("R", TextureMapType.METALLIC),
        ("G", TextureMapType.ROUGHNESS),
        ("B", TextureMapType.AO),
    ),
}


@dataclass(slots=True, frozen=True)
class ChannelPackJob:
    group_name: str
    layout: ChannelPackLayout
    output_path: Path
    channel_sources: tuple[tuple[TextureMapType, BatchSource | None], ...]
    missing_maps: tuple[TextureMapType, ...]

    @property
    def is_ready(self) -> bool:
        return not self.missing_maps and all(source is not None for _map_type, source in self.channel_sources)


@dataclass(slots=True, frozen=True)
class ChannelPackResult:
    job: ChannelPackJob
    status: ConversionStatus
    message: str


def channel_pack_mapping(layout: ChannelPackLayout) -> tuple[tuple[str, TextureMapType], ...]:
    return PACK_LAYOUTS[layout]


def channel_pack_mapping_text(layout: ChannelPackLayout) -> str:
    parts = [f"{channel}={map_type.label}" for channel, map_type in channel_pack_mapping(layout)]
    return f"{layout.label}: " + ", ".join(parts)


def build_channel_pack_jobs(
    sources: tuple[BatchSource, ...] | list[BatchSource],
    output_root: Path | None,
    options: ConversionOptions,
) -> tuple[ChannelPackJob, ...]:
    required_maps = tuple(map_type for _channel, map_type in channel_pack_mapping(options.packing.layout))
    groups: dict[tuple[str, str, str], dict[TextureMapType, BatchSource]] = {}
    group_context: dict[tuple[str, str, str], tuple[Path, Path, str]] = {}

    for source in sources:
        if source.map_type is TextureMapType.UNKNOWN:
            continue
        if source.map_type not in required_maps:
            continue

        base_name = _derive_pack_base_name(source.path.stem, source.map_type)
        if not base_name:
            continue

        source_root = source.root or source.path.parent
        relative_dir = _relative_dir(source.path.parent, source_root)
        group_key = (str(source_root).lower(), str(relative_dir).lower(), base_name.lower())
        groups.setdefault(group_key, {})[source.map_type] = source
        group_context[group_key] = (source_root, relative_dir, base_name)

    jobs: list[ChannelPackJob] = []
    for group_key, sources_by_map in groups.items():
        source_root, relative_dir, base_name = group_context[group_key]
        output_name = _build_pack_output_name(base_name, options)
        target_dir = (output_root / relative_dir) if output_root is not None else (source_root / relative_dir)
        output_path = target_dir / output_name

        channel_sources = tuple((map_type, sources_by_map.get(map_type)) for map_type in required_maps)
        missing_maps = tuple(map_type for map_type, source in channel_sources if source is None)
        jobs.append(
            ChannelPackJob(
                group_name=base_name,
                layout=options.packing.layout,
                output_path=output_path,
                channel_sources=channel_sources,
                missing_maps=missing_maps,
            )
        )

    jobs.sort(key=lambda job: str(job.output_path).lower())
    return tuple(jobs)


def execute_channel_pack_job(job: ChannelPackJob, options: ConversionOptions) -> ChannelPackResult:
    if not job.is_ready:
        return ChannelPackResult(
            job=job,
            status=ConversionStatus.SKIPPED,
            message=f"пропуск (не хватает карт: {missing_map_labels(job.missing_maps)})",
        )

    if job.output_path.exists() and not options.overwrite:
        return ChannelPackResult(
            job=job,
            status=ConversionStatus.SKIPPED,
            message=f"пропуск (уже есть): {job.output_path.name}",
        )

    resize_processor = ResizeProcessor()
    channels: list[Image.Image] = []
    target_size: tuple[int, int] | None = None

    for _map_type, source in job.channel_sources:
        if source is None:
            return ChannelPackResult(
                job=job,
                status=ConversionStatus.SKIPPED,
                message=f"пропуск (не хватает карт: {missing_map_labels(job.missing_maps)})",
            )

        with Image.open(source.path) as image:
            channel_image = _extract_first_frame(image).convert("L")
            channel_image = resize_processor.process(channel_image, options)

            if target_size is None:
                target_size = channel_image.size
            elif channel_image.size != target_size:
                channel_image = channel_image.resize(target_size, RESAMPLING_LANCZOS)

            channels.append(channel_image.copy())

    if len(channels) != 3:
        return ChannelPackResult(
            job=job,
            status=ConversionStatus.FAILED,
            message="ОШИБКА: не удалось собрать три канала для packed-texture",
        )

    merged = Image.merge("RGB", channels)
    job.output_path.parent.mkdir(parents=True, exist_ok=True)
    merged.save(
        job.output_path,
        format="PNG",
        optimize=options.optimize,
        compress_level=options.compress_level,
    )
    return ChannelPackResult(
        job=job,
        status=ConversionStatus.SUCCESS,
        message=f"packed {job.layout.label}: {job.output_path.name}",
    )


def summarize_channel_pack_jobs(jobs: tuple[ChannelPackJob, ...] | list[ChannelPackJob]) -> str:
    if not jobs:
        return "Подходящих наборов для packing пока нет."

    ready_jobs = [job for job in jobs if job.is_ready]
    missing_jobs = [job for job in jobs if not job.is_ready]

    if not missing_jobs:
        return f"Готово к packing: {len(ready_jobs)} set(s)."

    missing_descriptions = [
        f"{job.group_name}: {missing_map_labels(job.missing_maps)}"
        for job in missing_jobs[:3]
    ]
    suffix = "" if len(missing_jobs) <= 3 else f" и еще {len(missing_jobs) - 3}"
    return (
        f"Готово: {len(ready_jobs)} set(s). "
        f"Не хватает карт у {len(missing_jobs)} set(s): "
        + "; ".join(missing_descriptions)
        + suffix
    )


def missing_map_labels(map_types: tuple[TextureMapType, ...] | list[TextureMapType]) -> str:
    return ", ".join(map_type.label for map_type in map_types)


def _build_pack_output_name(base_name: str, options: ConversionOptions) -> str:
    output_stem = apply_naming_rules(
        f"{base_name}_{options.packing.layout.value}",
        options.naming,
        TextureMapType.UNKNOWN,
    )
    return f"{output_stem}.png"


def _derive_pack_base_name(stem: str, map_type: TextureMapType) -> str:
    normalized_stem = _normalized_stem(stem)
    if not normalized_stem:
        return ""

    stripped = _strip_any_known_map_suffix(normalized_stem)
    if stripped:
        return stripped
    if map_type is TextureMapType.UNKNOWN:
        return normalized_stem
    return normalized_stem


def _strip_any_known_map_suffix(normalized_stem: str) -> str:
    for alias in sorted(known_map_aliases(), key=len, reverse=True):
        normalized_alias = _normalized_stem(alias)
        if not normalized_alias:
            continue
        if normalized_stem == normalized_alias:
            return ""
        suffix = f"_{normalized_alias}"
        if normalized_stem.endswith(suffix):
            return normalized_stem[: -len(suffix)].strip("_")
    return normalized_stem


def _normalized_stem(stem: str) -> str:
    with_camel_breaks = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", stem)
    with_camel_breaks = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", with_camel_breaks)
    normalized = re.sub(r"[^a-zA-Z0-9]+", "_", with_camel_breaks).strip("_").lower()
    return re.sub(r"_+", "_", normalized)


def _relative_dir(path: Path, root: Path) -> Path:
    try:
        return path.relative_to(root)
    except ValueError:
        return Path()


def _extract_first_frame(image: Image.Image) -> Image.Image:
    if getattr(image, "is_animated", False) or getattr(image, "n_frames", 1) > 1:
        return next(ImageSequence.Iterator(image)).copy()
    return image.copy()
