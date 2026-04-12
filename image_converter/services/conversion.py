from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

from PIL import Image, ImageSequence

from image_converter.domain.constants import SUPPORTED_SOURCE_EXTENSIONS
from image_converter.domain.models import (
    BatchRequest,
    BatchSummary,
    ConversionOptions,
    ConversionResult,
    ConversionStatus,
)
from image_converter.services.pipeline import ConversionPipeline

Logger = Callable[[str], None]


class ImageConverter:
    def __init__(self, pipeline: ConversionPipeline | None = None):
        self._pipeline = pipeline or ConversionPipeline()

    def convert(self, source: Path, destination: Path, options: ConversionOptions) -> ConversionResult:
        if destination.exists() and not options.overwrite:
            return ConversionResult(
                source=source,
                destination=destination,
                status=ConversionStatus.SKIPPED,
                message=f"пропуск (уже есть): {destination.name}",
            )

        with Image.open(source) as image:
            working_image = self._extract_first_frame(image)
            processed = self._pipeline.process(working_image, options)
            destination.parent.mkdir(parents=True, exist_ok=True)
            processed.save(
                destination,
                format="PNG",
                optimize=options.optimize,
                compress_level=options.compress_level,
            )

        return ConversionResult(
            source=source,
            destination=destination,
            status=ConversionStatus.SUCCESS,
            message=f"успех: {destination.name}",
        )

    @staticmethod
    def _extract_first_frame(image: Image.Image) -> Image.Image:
        if getattr(image, "is_animated", False) or getattr(image, "n_frames", 1) > 1:
            return next(ImageSequence.Iterator(image)).copy()
        return image.copy()


class BatchConversionService:
    def __init__(self, converter: ImageConverter | None = None):
        self._converter = converter or ImageConverter()

    def run(self, request: BatchRequest, logger: Logger | None = None) -> BatchSummary:
        write_log = logger or (lambda _message: None)
        summary = BatchSummary()
        input_path = request.input_path

        if input_path is None:
            return summary

        for source in self.iter_sources(input_path, request.options.recursive):
            destination = self.build_destination_path(source, input_path, request.output_root)
            try:
                result = self._converter.convert(source, destination, request.options)
                write_log(f"{source.name} -> {result.message}")
                summary.register(result)
                if result.is_success and request.options.delete_source:
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

        return summary

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
    def build_destination_path(source: Path, input_path: Path, output_root: Path | None) -> Path:
        if output_root is None:
            return source.with_suffix(".png")

        if input_path.is_dir():
            relative_path = source.relative_to(input_path)
            return output_root / relative_path.with_suffix(".png")

        return output_root / source.with_suffix(".png").name

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
