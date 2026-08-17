from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

from PIL import Image, ImageOps

from image_converter.domain.constants import SUPPORTED_SOURCE_EXTENSIONS
from image_converter.domain.models import (
    BatchRequest,
    BatchSource,
    BatchSummary,
    ChannelPackingMode,
    ConversionOptions,
    ConversionResult,
    ConversionStatus,
    TextureMapType,
)
from image_converter.services.map_types import detect_channel_pack_layout, detect_texture_map_type
from image_converter.services.naming import build_output_filename
from image_converter.services.image_loading import copy_first_frame_preserving_alpha
from image_converter.services.packing import (
    build_channel_pack_jobs,
    build_traditional_sources,
    expand_packed_sources,
    execute_channel_pack_job,
    packed_source_map_types,
    summarize_channel_pack_jobs,
)
from image_converter.services.pipeline import ConversionPipeline

Logger = Callable[[str], None]


class ImageConverter:
    def __init__(self, pipeline: ConversionPipeline | None = None):
        self._pipeline = pipeline or ConversionPipeline()

    def convert(self, source: Path, destination: Path, options: ConversionOptions) -> ConversionResult:
        return self.convert_source(BatchSource(path=source), destination, options)

    def convert_source(
        self,
        source: BatchSource,
        destination: Path,
        options: ConversionOptions,
    ) -> ConversionResult:
        if destination.exists() and not options.overwrite:
            return ConversionResult(
                source=source.path,
                destination=destination,
                status=ConversionStatus.SKIPPED,
                message=f"пропуск (уже есть): {destination.name}",
            )

        with Image.open(source.path) as image:
            working_image = copy_first_frame_preserving_alpha(image)
            if source.source_channel is not None:
                working_image = working_image.convert("RGBA").getchannel(source.source_channel)
            if source.invert_channel:
                working_image = ImageOps.invert(working_image)
            processed = self._pipeline.process(working_image, options)
            output_image = processed.copy()

        destination.parent.mkdir(parents=True, exist_ok=True)
        output_image.save(
            destination,
            format="PNG",
            optimize=options.optimize,
            compress_level=options.compress_level,
        )

        return ConversionResult(
            source=source.path,
            destination=destination,
            status=ConversionStatus.SUCCESS,
            message=f"успех: {destination.name}",
        )

class BatchConversionService:
    def __init__(self, converter: ImageConverter | None = None):
        self._converter = converter or ImageConverter()

    def run(
        self,
        request: BatchRequest,
        logger: Logger | None = None,
        *,
        on_item_start: Callable[[Path], None] | None = None,
        on_item_complete: Callable[[ConversionResult], None] | None = None,
    ) -> BatchSummary:
        write_log = logger or (lambda _message: None)
        summary = BatchSummary(packed_only_mode=request.options.packing.mode is ChannelPackingMode.PACK_ONLY)
        source_specs = list(self.iter_request_sources(request))
        conversion_sources = self._conversion_sources_for_request(source_specs, request.options)
        conversion_sources.sort(
            key=lambda source: self._virtual_output_overwrites_source(
                source,
                request.output_root,
                request.options,
            )
        )
        virtual_delete_state: dict[Path, tuple[bool, Path]] = {}

        if conversion_sources:
            for source_spec in conversion_sources:
                source = source_spec.path
                destination = self.build_destination_for_source(
                    source_spec,
                    request.output_root,
                    request.options,
                )
                try:
                    if on_item_start is not None:
                        on_item_start(source)
                    result = self._converter.convert_source(source_spec, destination, request.options)
                    write_log(f"{source.name} -> {result.message}")
                    summary.register(result)
                    if on_item_complete is not None:
                        on_item_complete(result)
                    if source_spec.source_channel is not None:
                        previous_success, first_destination = virtual_delete_state.get(
                            source,
                            (True, destination),
                        )
                        if self._paths_equal(source, destination):
                            first_destination = destination
                        virtual_delete_state[source] = (
                            previous_success and result.is_success,
                            first_destination,
                        )
                    elif result.is_success and request.options.delete_source:
                        self._delete_source(source, destination, write_log)
                except Exception as exc:
                    failed_result = ConversionResult(
                        source=source,
                        destination=destination,
                        status=ConversionStatus.FAILED,
                        message=f"ОШИБКА: {exc}",
                    )
                    write_log(f"{source.name} -> {failed_result.message}")
                    summary.register(failed_result)
                    if on_item_complete is not None:
                        on_item_complete(failed_result)
                    if source_spec.source_channel is not None:
                        _previous_success, first_destination = virtual_delete_state.get(
                            source,
                            (True, destination),
                        )
                        virtual_delete_state[source] = (False, first_destination)

            if request.options.delete_source:
                for source, (all_succeeded, destination) in virtual_delete_state.items():
                    if all_succeeded:
                        self._delete_source(source, destination, write_log)
        elif source_specs:
            write_log("---- Source Conversion ----")
            if request.options.packing.enabled and request.options.packing.mode is ChannelPackingMode.PACK_ONLY:
                write_log("Skipped regular PNG export. Pack Only mode is enabled.")
            else:
                write_log("Skipped regular PNG export. Nothing outside the packed channels needs export.")

        if request.options.packing.enabled:
            pack_jobs = build_channel_pack_jobs(source_specs, request.output_root, request.options)
            layout = request.options.packing.layout.label
            write_log(f"---- Channel Packing {layout} ----")
            write_log(summarize_channel_pack_jobs(pack_jobs))

            pack_created = 0
            pack_skipped = 0
            pack_failed = 0

            for job in pack_jobs:
                try:
                    pack_result = execute_channel_pack_job(job, request.options)
                except Exception as exc:
                    write_log(f"PACK {job.layout.label} {job.group_name} -> ОШИБКА: {exc}")
                    pack_failed += 1
                    summary.register_packed(ConversionStatus.FAILED)
                    continue

                write_log(f"PACK {job.layout.label} {job.group_name} -> {pack_result.message}")
                if pack_result.status is ConversionStatus.SUCCESS:
                    pack_created += 1
                elif pack_result.status is ConversionStatus.SKIPPED:
                    pack_skipped += 1
                else:
                    pack_failed += 1
                summary.register_packed(pack_result.status)

            write_log(
                f"Packing итоги: создано={pack_created}, пропущено={pack_skipped}, ошибок={pack_failed}"
            )

        return summary

    @staticmethod
    def _conversion_sources_for_request(
        source_specs: list[BatchSource],
        options: ConversionOptions,
    ) -> list[BatchSource]:
        if not options.packing.enabled:
            if options.unpack_packed:
                return list(build_traditional_sources(source_specs))
            return list(source_specs)

        if options.packing.mode is ChannelPackingMode.PACK_ONLY:
            return []

        expanded_sources = list(expand_packed_sources(source_specs))

        if options.packing.mode is ChannelPackingMode.PACK_WITH_REMAINDER:
            packed_map_types = packed_source_map_types(options.packing.layout)
            return [
                source_spec
                for source_spec in expanded_sources
                if source_spec.map_type not in packed_map_types
            ]

        return expanded_sources

    def iter_request_sources(self, request: BatchRequest) -> Iterator[BatchSource]:
        if request.sources:
            yield from request.sources
            return

        input_path = request.input_path
        if input_path is None:
            return

        if input_path.is_file():
            if input_path.suffix.lower() in SUPPORTED_SOURCE_EXTENSIONS:
                yield BatchSource(
                    path=input_path,
                    root=input_path.parent,
                    map_type=detect_texture_map_type(input_path),
                    packed_layout=detect_channel_pack_layout(input_path),
                )
            return

        for path in self.iter_sources(input_path, request.options.recursive):
            yield BatchSource(
                path=path,
                root=input_path,
                map_type=detect_texture_map_type(path),
                packed_layout=detect_channel_pack_layout(path),
            )

    @staticmethod
    def iter_sources(root: Path, recursive: bool) -> Iterator[Path]:
        if root.is_file():
            if root.suffix.lower() in SUPPORTED_SOURCE_EXTENSIONS:
                yield root
            return

        iterator = root.rglob("*") if recursive else root.glob("*")
        for path in iterator:
            if path.is_file() and path.suffix.lower() in SUPPORTED_SOURCE_EXTENSIONS:
                yield path

    @staticmethod
    def build_destination_path(
        source: Path,
        input_path: Path,
        output_root: Path | None,
        options: ConversionOptions,
        *,
        map_type: TextureMapType = TextureMapType.UNKNOWN,
    ) -> Path:
        output_name = build_output_filename(source, options.naming, map_type)

        if output_root is None:
            return source.with_name(output_name)

        if input_path.is_dir():
            relative_path = source.relative_to(input_path)
            return output_root / relative_path.with_name(output_name)

        return output_root / output_name

    @classmethod
    def build_destination_for_source(
        cls,
        source: BatchSource,
        output_root: Path | None,
        options: ConversionOptions,
    ) -> Path:
        source_path = source.path
        if source.output_stem:
            source_path = source.path.with_name(f"{source.output_stem}{source.path.suffix}")

        source_root = source.root
        if source_root is None:
            return cls.build_destination_path(
                source_path,
                source.path.parent,
                output_root,
                options,
                map_type=source.map_type,
            )
        return cls.build_destination_path(
            source_path,
            source_root,
            output_root,
            options,
            map_type=source.map_type,
        )

    @staticmethod
    def _delete_source(source: Path, destination: Path, logger: Logger) -> None:
        try:
            if source.resolve() == destination.resolve():
                logger(f"{source.name} -> исходник не удален (совпадает с выходным файлом)")
                return
            source.unlink()
            logger(f"{source.name} -> исходник удален")
        except Exception as exc:
            logger(f"{source.name} -> не удалось удалить исходник: {exc}")

    @classmethod
    def _virtual_output_overwrites_source(
        cls,
        source: BatchSource,
        output_root: Path | None,
        options: ConversionOptions,
    ) -> bool:
        if source.source_channel is None:
            return False
        destination = cls.build_destination_for_source(source, output_root, options)
        return cls._paths_equal(source.path, destination)

    @staticmethod
    def _paths_equal(first: Path, second: Path) -> bool:
        try:
            return first.resolve(strict=False) == second.resolve(strict=False)
        except OSError:
            return str(first).casefold() == str(second).casefold()
