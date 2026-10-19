from __future__ import annotations

import io
import os
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from PIL import Image

from image_converter.cli import run_cli
from image_converter.domain.errors import ValidationError
from image_converter.domain.models import (
    BatchRequest, BatchSource, ChannelPackLayout, ChannelPackingMode, ChannelPackingOptions,
    ConversionOptions, ConversionStatus, NamingRules, TextureMapType,
)
from image_converter.services.atomic_io import save_png_atomic
from image_converter.services.conversion import BatchConversionService, ImageConverter
from image_converter.services.output_safety import PlannedImageWrite, validate_batch_output_paths
from image_converter.services.packing import build_channel_pack_jobs, execute_channel_pack_job


class BatchOutputSafetyTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.output = self.root / "out"
        self.service = BatchConversionService()

    def source(self, name, map_type=TextureMapType.UNKNOWN):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (8, 8), (32, 120, 220)).save(path)
        return BatchSource(path, path.parent, map_type)

    def request(self, sources, options=None):
        return BatchRequest(None, self.output, options or ConversionOptions(overwrite=True, delete_source=True), tuple(sources))

    def snapshot(self):
        return {path.relative_to(self.root): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}

    def assert_blocked(self, request):
        before = self.snapshot()
        with self.assertRaisesRegex(ValidationError, "Batch export blocked"):
            self.service.run(request)
        self.assertEqual(before, self.snapshot())

    def packing_options(self, mode=ChannelPackingMode.PACK_ONLY):
        return ConversionOptions(overwrite=True, delete_source=True, packing=ChannelPackingOptions(
            enabled=True, layout=ChannelPackLayout.ORM, mode=mode,
        ))

    def maps(self, folder):
        return [self.source(f"{folder}/mat_{suffix}.png", kind) for suffix, kind in (
            ("ao", TextureMapType.AO), ("roughness", TextureMapType.ROUGHNESS), ("metallic", TextureMapType.METALLIC),
        )]

    def test_duplicate_stems_block_entire_batch_before_writing_or_deleting(self):
        sources = [self.source("src/safe.tif"), self.source("src/mat.tif"), self.source("src/mat.png")]
        for overwrite in (False, True):
            with self.subTest(overwrite=overwrite):
                self.assert_blocked(self.request(sources, ConversionOptions(overwrite=overwrite, delete_source=True)))
        self.assertFalse(self.output.exists())

    def test_normalized_names_are_checked(self):
        sources = [self.source("src/mat_ao.tif", TextureMapType.AO),
                   self.source("src/mat_occlusion.tif", TextureMapType.AO)]
        self.assert_blocked(self.request(sources, ConversionOptions(
            overwrite=True, naming=NamingRules(normalize_map_suffix=True),
        )))

    def test_pack_outputs_from_different_source_roots_collide(self):
        self.assert_blocked(self.request(self.maps("a") + self.maps("b"), self.packing_options()))

    def test_regular_and_packed_outputs_share_one_preflight(self):
        sources = self.maps("src") + [self.source("src/mat_orm.tif")]
        self.assert_blocked(self.request(sources, self.packing_options(ChannelPackingMode.AFTER_CONVERSION)))

    def test_pack_cannot_replace_an_unused_input(self):
        sources = self.maps("src") + [self.source("src/mat_orm.png")]
        self.assert_blocked(replace(self.request(sources, self.packing_options()), output_root=None))

    def test_in_place_conversion_cannot_change_inputs_before_packing(self):
        request = self.request(self.maps("src"), self.packing_options(ChannelPackingMode.AFTER_CONVERSION))
        self.assert_blocked(replace(request, output_root=None))

    def test_output_cannot_replace_another_source_even_if_its_reader_runs_first(self):
        first = replace(self.source("out/target.png"), output_stem="saved")
        second = replace(self.source("src/new.tif"), output_stem="target")
        self.assert_blocked(self.request([first, second]))

    def test_in_place_repack_after_last_reader_is_allowed(self):
        source = replace(self.source("src/mat_orm.png"), packed_layout=ChannelPackLayout.ORM)
        request = replace(self.request([source], self.packing_options()), output_root=None)
        self.assertEqual(1, self.service.run(request).packed_created)
        with Image.open(source.path) as result:
            self.assertEqual((32, 120, 220), result.getpixel((0, 0)))

    def test_partial_unpack_failure_cannot_destroy_shared_packed_input(self):
        source = replace(self.source("src/mat_ao.png"), packed_layout=ChannelPackLayout.ORM)
        original_bytes = source.path.read_bytes()
        converter = ImageConverter()
        original_convert = converter.convert_source

        def convert(spec, destination, options):
            if spec.source_channel == "G":
                raise OSError("Disk full")
            return original_convert(spec, destination, options)

        request = replace(self.request([source], ConversionOptions(
            overwrite=True, delete_source=True, unpack_packed=True,
        )), output_root=None)
        with patch.object(converter, "convert_source", side_effect=convert):
            summary = BatchConversionService(converter).run(request)
        self.assertEqual(2, summary.failed)
        self.assertEqual(original_bytes, source.path.read_bytes())

    def test_failed_pack_cannot_be_followed_by_destructive_in_place_pack(self):
        source = replace(self.source("src/mat_orm.png"), packed_layout=ChannelPackLayout.ORM)
        before = source.path.read_bytes()
        options = replace(self.packing_options(), packing=replace(self.packing_options().packing, layout=ChannelPackLayout.RMA))
        job = build_channel_pack_jobs([source], self.output, options)[0]
        in_place_job = replace(job, output_path=source.path)

        def execute(current, settings):
            if current is job:
                raise OSError("Disk full")
            return execute_channel_pack_job(current, settings)

        with (
            patch("image_converter.services.conversion.build_channel_pack_jobs", return_value=(job, in_place_job)),
            patch("image_converter.services.conversion.execute_channel_pack_job", side_effect=execute) as run,
        ):
            summary = self.service.run(self.request([source], options))
        self.assertEqual(2, summary.packed_failed)
        self.assertEqual(1, run.call_count)
        self.assertEqual(before, source.path.read_bytes())

    def test_existing_hard_link_is_recognized_as_an_input(self):
        source = self.source("src/input.png")
        alias = self.root / "alias.png"
        os.link(source.path, alias)
        with self.assertRaises(ValidationError):
            validate_batch_output_paths([source.path], [PlannedImageWrite(alias, (), "other output")], overwrite=True)

    def test_hard_link_outputs_collide(self):
        source = self.source("a.png")
        alias = self.root / "b.png"
        os.link(source.path, alias)
        with self.assertRaisesRegex(ValidationError, "Conflicting outputs"):
            validate_batch_output_paths([], [PlannedImageWrite(path, (), str(path)) for path in (source.path, alias)], overwrite=False)

    @unittest.skipUnless(os.name == "nt", "Windows case-insensitive paths")
    def test_case_only_output_difference_collides(self):
        with self.assertRaisesRegex(ValidationError, "Conflicting outputs"):
            validate_batch_output_paths([], [
                PlannedImageWrite(self.root / name, (), name) for name in ("New.png", "NEW.PNG")
            ], overwrite=True)

    def test_cli_collision_returns_failure_without_touching_files(self):
        self.source("src/mat.tif")
        self.source("src/mat.tga")
        before = self.snapshot()
        with redirect_stdout(io.StringIO()) as log:
            code = run_cli([str(self.root / "src"), "--out", str(self.output), "--overwrite", "--delete-source"])
        self.assertEqual(1, code)
        self.assertIn("Conflicting outputs", log.getvalue())
        self.assertEqual(before, self.snapshot())


class AtomicPngTests(unittest.TestCase):
    def test_partial_encoder_failure_preserves_existing_file_and_cleans_temporary(self):
        for exists in (False, True):
            with self.subTest(existing=exists), TemporaryDirectory() as directory:
                root = Path(directory)
                source, destination = root / "input.tif", root / "output.png"
                Image.new("RGB", (8, 8), "red").save(source)
                if exists:
                    destination.write_bytes(b"previous file")

                def broken_save(image, stream, **kwargs):
                    stream.write(b"partial PNG")
                    raise OSError("Disk full")

                with patch.object(Image.Image, "save", broken_save), self.assertRaises(OSError):
                    ImageConverter().convert(source, destination, ConversionOptions(overwrite=True))
                self.assertEqual(b"previous file" if exists else None, destination.read_bytes() if exists else None)
                self.assertEqual({source.name} | ({destination.name} if exists else set()), {p.name for p in root.iterdir()})

    def test_publish_failure_preserves_previous_pack_and_sources(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source, output = root / "mat_orm.tif", root / "mat_rma.png"
            Image.new("RGB", (8, 8), (20, 100, 200)).save(source)
            output.write_bytes(b"previous pack")
            with patch("image_converter.services.atomic_io.os.replace", side_effect=OSError("Access denied")):
                summary = BatchConversionService().run(BatchRequest(source, root, ConversionOptions(
                    overwrite=True, delete_source=True, packing=ChannelPackingOptions(
                        enabled=True, layout=ChannelPackLayout.RMA, mode=ChannelPackingMode.PACK_ONLY,
                    ),
                )))
            self.assertEqual(1, summary.packed_failed)
            self.assertEqual(b"previous pack", output.read_bytes())
            self.assertEqual({source.name, output.name}, {p.name for p in root.iterdir()})

    def test_no_overwrite_is_enforced_at_publication_not_only_before_encoding(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "output.png"
            operation = "rename" if os.name == "nt" else "link"
            publish = getattr(os, operation)

            def race(source, destination):
                path.write_bytes(b"another writer won")
                return publish(source, destination)

            with patch(f"image_converter.services.atomic_io.os.{operation}", side_effect=race):
                saved = save_png_atomic(Image.new("RGB", (8, 8)), path, overwrite=False, optimize=True, compress_level=6)
            self.assertFalse(saved)
            self.assertEqual(b"another writer won", path.read_bytes())
            self.assertEqual([path], list(path.parent.iterdir()))

    def test_existing_in_place_source_survives_failed_encoding(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "mat.png"
            Image.new("RGB", (8, 8), "red").save(path)
            before = path.read_bytes()
            with patch.object(Image.Image, "save", side_effect=OSError("Disk full")):
                summary = BatchConversionService().run(BatchRequest(path, None, ConversionOptions(overwrite=True, delete_source=True)))
            self.assertEqual(1, summary.failed)
            self.assertEqual(before, path.read_bytes())
            self.assertEqual([path], list(path.parent.iterdir()))


if __name__ == "__main__":
    unittest.main()
