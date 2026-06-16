from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps

from image_converter.domain.models import (
    BatchSource,
    ChannelPackLayout,
    ConversionOptions,
    ConversionStatus,
    TextureMapType,
)
from image_converter.services.image_loading import copy_first_frame_preserving_alpha
from image_converter.services.map_types import known_map_aliases
from image_converter.services.naming import apply_naming_rules
from image_converter.services.pipeline import RESAMPLING_LANCZOS, ResizeProcessor


@dataclass(slots=True, frozen=True)
class PackSourceCandidate:
    map_type: TextureMapType
    invert: bool = False


@dataclass(slots=True, frozen=True)
class PackChannelRule:
    channel: str
    label: str
    candidates: tuple[PackSourceCandidate, ...] = ()
    fill_value: int | None = None


@dataclass(slots=True, frozen=True)
class ResolvedPackChannel:
    channel: str
    label: str
    source: BatchSource | None
    fill_value: int | None = None
    invert: bool = False


PACK_LAYOUTS: dict[ChannelPackLayout, tuple[PackChannelRule, ...]] = {
    ChannelPackLayout.ORM: (
        PackChannelRule(
            channel="R",
            label=TextureMapType.AO.label,
            candidates=(PackSourceCandidate(TextureMapType.AO),),
        ),
        PackChannelRule(
            channel="G",
            label=TextureMapType.ROUGHNESS.label,
            candidates=(PackSourceCandidate(TextureMapType.ROUGHNESS),),
        ),
        PackChannelRule(
            channel="B",
            label=TextureMapType.METALLIC.label,
            candidates=(PackSourceCandidate(TextureMapType.METALLIC),),
        ),
    ),
    ChannelPackLayout.RMA: (
        PackChannelRule(
            channel="R",
            label=TextureMapType.ROUGHNESS.label,
            candidates=(PackSourceCandidate(TextureMapType.ROUGHNESS),),
        ),
        PackChannelRule(
            channel="G",
            label=TextureMapType.METALLIC.label,
            candidates=(PackSourceCandidate(TextureMapType.METALLIC),),
        ),
        PackChannelRule(
            channel="B",
            label=TextureMapType.AO.label,
            candidates=(PackSourceCandidate(TextureMapType.AO),),
        ),
    ),
    ChannelPackLayout.MRA: (
        PackChannelRule(
            channel="R",
            label=TextureMapType.METALLIC.label,
            candidates=(PackSourceCandidate(TextureMapType.METALLIC),),
        ),
        PackChannelRule(
            channel="G",
            label=TextureMapType.ROUGHNESS.label,
            candidates=(PackSourceCandidate(TextureMapType.ROUGHNESS),),
        ),
        PackChannelRule(
            channel="B",
            label=TextureMapType.AO.label,
            candidates=(PackSourceCandidate(TextureMapType.AO),),
        ),
    ),
    ChannelPackLayout.UNITY_URP: (
        PackChannelRule(
            channel="R",
            label=TextureMapType.METALLIC.label,
            candidates=(PackSourceCandidate(TextureMapType.METALLIC),),
        ),
        PackChannelRule(channel="G", label="Black", fill_value=0),
        PackChannelRule(channel="B", label="Black", fill_value=0),
        PackChannelRule(
            channel="A",
            label=TextureMapType.SMOOTHNESS.label,
            candidates=(
                PackSourceCandidate(TextureMapType.SMOOTHNESS),
                PackSourceCandidate(TextureMapType.ROUGHNESS, invert=True),
            ),
        ),
    ),
    ChannelPackLayout.UNITY_HDRP: (
        PackChannelRule(
            channel="R",
            label=TextureMapType.METALLIC.label,
            candidates=(PackSourceCandidate(TextureMapType.METALLIC),),
        ),
        PackChannelRule(
            channel="G",
            label=TextureMapType.AO.label,
            candidates=(PackSourceCandidate(TextureMapType.AO),),
        ),
        PackChannelRule(channel="B", label="Detail Mask", fill_value=255),
        PackChannelRule(
            channel="A",
            label=TextureMapType.SMOOTHNESS.label,
            candidates=(
                PackSourceCandidate(TextureMapType.SMOOTHNESS),
                PackSourceCandidate(TextureMapType.ROUGHNESS, invert=True),
            ),
        ),
    ),
}


@dataclass(slots=True, frozen=True)
class ChannelPackJob:
    group_name: str
    layout: ChannelPackLayout
    output_path: Path
    channel_sources: tuple[ResolvedPackChannel, ...]
    missing_maps: tuple[str, ...]

    @property
    def is_ready(self) -> bool:
        return not self.missing_maps and all(
            source.source is not None or source.fill_value is not None for source in self.channel_sources
        )


@dataclass(slots=True, frozen=True)
class ChannelPackResult:
    job: ChannelPackJob
    status: ConversionStatus
    message: str


def channel_pack_mapping(layout: ChannelPackLayout) -> tuple[tuple[str, str], ...]:
    rules = PACK_LAYOUTS[layout]
    return tuple((rule.channel, _describe_channel_rule(rule)) for rule in rules)


def channel_pack_mapping_text(layout: ChannelPackLayout) -> str:
    parts = [f"{channel}={description}" for channel, description in channel_pack_mapping(layout)]
    return f"{layout.label}: " + ", ".join(parts)


def build_channel_pack_jobs(
    sources: tuple[BatchSource, ...] | list[BatchSource],
    output_root: Path | None,
    options: ConversionOptions,
) -> tuple[ChannelPackJob, ...]:
    layout_rules = PACK_LAYOUTS[options.packing.layout]
    relevant_map_types = {
        candidate.map_type
        for rule in layout_rules
        for candidate in rule.candidates
    }
    groups: dict[tuple[str, str, str], dict[TextureMapType, BatchSource]] = {}
    group_context: dict[tuple[str, str, str], tuple[Path, Path, str]] = {}

    for source in sources:
        if source.map_type is TextureMapType.UNKNOWN:
            continue
        if source.map_type not in relevant_map_types:
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

        channel_sources = tuple(_resolve_channel_rule(rule, sources_by_map) for rule in layout_rules)
        missing_maps = tuple(
            resolved_channel.label
            for resolved_channel in channel_sources
            if resolved_channel.source is None and resolved_channel.fill_value is None
        )
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
    target_size = _determine_target_size(job.channel_sources, options, resize_processor)
    if target_size is None:
        return ChannelPackResult(
            job=job,
            status=ConversionStatus.FAILED,
            message="ОШИБКА: не удалось определить размер packed-texture",
        )

    for resolved_channel in job.channel_sources:
        if resolved_channel.source is None and resolved_channel.fill_value is None:
            return ChannelPackResult(
                job=job,
                status=ConversionStatus.SKIPPED,
                message=f"пропуск (не хватает карт: {missing_map_labels(job.missing_maps)})",
            )

        if resolved_channel.source is not None:
            channel_image = _load_channel_image(
                resolved_channel.source,
                options,
                resize_processor,
            )
            if channel_image.size != target_size:
                channel_image = channel_image.resize(target_size, RESAMPLING_LANCZOS)
            if resolved_channel.invert:
                channel_image = ImageOps.invert(channel_image)
        else:
            channel_image = Image.new("L", target_size, resolved_channel.fill_value or 0)

        channels.append(channel_image.copy())

    if len(channels) not in (3, 4):
        return ChannelPackResult(
            job=job,
            status=ConversionStatus.FAILED,
            message="ОШИБКА: не удалось собрать корректный набор каналов для packed-texture",
        )

    merged = Image.merge("RGBA" if len(channels) == 4 else "RGB", tuple(channels))
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


def missing_map_labels(map_types: tuple[str, ...] | list[str]) -> str:
    return ", ".join(map_types)


def _build_pack_output_name(base_name: str, options: ConversionOptions) -> str:
    output_stem = apply_naming_rules(
        f"{base_name}_{_pack_output_suffix(options.packing.layout)}",
        options.naming,
        TextureMapType.UNKNOWN,
    )
    return f"{output_stem}.png"


def _pack_output_suffix(layout: ChannelPackLayout) -> str:
    mapping = {
        ChannelPackLayout.ORM: "orm",
        ChannelPackLayout.RMA: "rma",
        ChannelPackLayout.MRA: "mra",
        ChannelPackLayout.UNITY_URP: "metallicsmoothness",
        ChannelPackLayout.UNITY_HDRP: "maskmap",
    }
    return mapping[layout]


def _resolve_channel_rule(
    rule: PackChannelRule,
    sources_by_map: dict[TextureMapType, BatchSource],
) -> ResolvedPackChannel:
    for candidate in rule.candidates:
        source = sources_by_map.get(candidate.map_type)
        if source is not None:
            return ResolvedPackChannel(
                channel=rule.channel,
                label=rule.label,
                source=source,
                invert=candidate.invert,
            )

    return ResolvedPackChannel(
        channel=rule.channel,
        label=rule.label,
        source=None,
        fill_value=rule.fill_value,
    )


def _describe_channel_rule(rule: PackChannelRule) -> str:
    parts = []
    for candidate in rule.candidates:
        label = candidate.map_type.label
        if candidate.invert:
            label = f"1-{label}"
        parts.append(label)

    if rule.fill_value is not None:
        fill_text = "1" if rule.fill_value >= 255 else "0" if rule.fill_value <= 0 else str(rule.fill_value)
        if rule.label == "Detail Mask":
            fill_text = f"{rule.label}={fill_text}"
        elif rule.label != "Black":
            fill_text = f"{rule.label}={fill_text}"
        parts.append(fill_text)

    if not parts:
        return rule.label
    return " / ".join(parts)


def _determine_target_size(
    channel_sources: tuple[ResolvedPackChannel, ...],
    options: ConversionOptions,
    resize_processor: ResizeProcessor,
) -> tuple[int, int] | None:
    for resolved_channel in channel_sources:
        if resolved_channel.source is None:
            continue
        channel_image = _load_channel_image(resolved_channel.source, options, resize_processor)
        return channel_image.size
    return None


def _load_channel_image(
    source: BatchSource,
    options: ConversionOptions,
    resize_processor: ResizeProcessor,
) -> Image.Image:
    with Image.open(source.path) as image:
        channel_image = _extract_first_frame(image).convert("L")
        channel_image = resize_processor.process(channel_image, options)
        return channel_image.copy()


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
    return copy_first_frame_preserving_alpha(image)
