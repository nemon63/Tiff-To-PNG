from __future__ import annotations

import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from PIL import Image

from image_converter.domain.models import (
    AssetKind,
    AssetMetadata,
    BatchRequest,
    BatchSource,
    ChannelPackLayout,
    ChannelPackingMode,
    ChannelPackingOptions,
    ConversionOptions,
    QueueItem,
    TextureMapType,
)
from image_converter.services.conversion import BatchConversionService
from image_converter.services.map_types import detect_channel_pack_layout
from image_converter.services.presets import SYSTEM_PRESETS


class PackedPipelineTranscodingTests(unittest.TestCase):
    def test_manual_packed_layout_override_supports_nonstandard_filename(self) -> None:
        path = Path("bike_damaged_ao.png")
        item = QueueItem(
            source=BatchSource(path),
            asset_kind=AssetKind.IMAGE,
            metadata=AssetMetadata("PNG", 4, 4, "RGB", False, 64, TextureMapType.AO),
            packed_layout_override=ChannelPackLayout.ORM,
        )

        self.assertEqual(ChannelPackLayout.ORM, item.effective_packed_layout)
        self.assertEqual(TextureMapType.UNKNOWN, item.effective_map_type)
        self.assertEqual(ChannelPackLayout.ORM, item.batch_source.packed_layout)

    def test_manual_orm_override_strips_misleading_ao_suffix_when_unpacking(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "src"
            output_root = Path(tmp) / "out"
            root.mkdir()
            source_path = root / "bike_damaged_ao.png"
            Image.new("RGB", (2, 2), (30, 120, 220)).save(source_path)
            preset = next(
                preset
                for preset in SYSTEM_PRESETS
                if preset.name == "Traditional / Non-Packed Workflow"
            )

            BatchConversionService().run(
                BatchRequest(
                    input_path=None,
                    output_root=output_root,
                    sources=(
                        BatchSource(
                            source_path,
                            root,
                            packed_layout=ChannelPackLayout.ORM,
                        ),
                    ),
                    options=preset.options,
                )
            )

            self.assertTrue((output_root / "bike_damaged_ao.png").exists())
            self.assertTrue((output_root / "bike_damaged_roughness.png").exists())
            self.assertTrue((output_root / "bike_damaged_metallic.png").exists())
            self.assertFalse((output_root / "bike_damaged_ao_roughness.png").exists())

    def test_in_place_manual_unpack_reads_all_channels_before_overwriting_source_name(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_path = root / "bike_damaged_ao.png"
            Image.new("RGB", (2, 2), (30, 120, 220)).save(source_path)
            preset = next(
                preset
                for preset in SYSTEM_PRESETS
                if preset.name == "Traditional / Non-Packed Workflow"
            )
            options = replace(preset.options, overwrite=True)

            BatchConversionService().run(
                BatchRequest(
                    input_path=None,
                    output_root=None,
                    sources=(
                        BatchSource(
                            source_path,
                            root,
                            packed_layout=ChannelPackLayout.ORM,
                        ),
                    ),
                    options=options,
                )
            )

            expected_values = {
                "bike_damaged_ao.png": 30,
                "bike_damaged_roughness.png": 120,
                "bike_damaged_metallic.png": 220,
            }
            for filename, expected_value in expected_values.items():
                with Image.open(root / filename) as image:
                    self.assertEqual(expected_value, image.convert("L").getpixel((0, 0)))

    def test_detects_supported_packed_layout_suffixes(self) -> None:
        cases = {
            "bike_orm.png": ChannelPackLayout.ORM,
            "bike_ARM.png": ChannelPackLayout.ORM,
            "bike_rma.tga": ChannelPackLayout.RMA,
            "bike_mra.tif": ChannelPackLayout.MRA,
            "bike_metallicSmoothness.png": ChannelPackLayout.UNITY_URP,
            "bike_MetallicGlossMap.png": ChannelPackLayout.UNITY_URP,
            "bike_MaskMap.png": ChannelPackLayout.UNITY_HDRP,
        }

        for filename, expected in cases.items():
            with self.subTest(filename=filename):
                self.assertEqual(expected, detect_channel_pack_layout(Path(filename)))

    def test_repacking_orm_to_rma_remaps_channels(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "src"
            output_root = Path(tmp) / "out"
            root.mkdir()
            source_path = root / "bike_orm.png"
            Image.new("RGB", (4, 4), (30, 120, 220)).save(source_path)

            summary = BatchConversionService().run(
                BatchRequest(
                    input_path=None,
                    output_root=output_root,
                    sources=(
                        BatchSource(
                            source_path,
                            root,
                            packed_layout=ChannelPackLayout.ORM,
                        ),
                    ),
                    options=ConversionOptions(
                        packing=ChannelPackingOptions(
                            enabled=True,
                            layout=ChannelPackLayout.RMA,
                            mode=ChannelPackingMode.PACK_WITH_REMAINDER,
                        )
                    ),
                )
            )

            destination = output_root / "bike_rma.png"
            self.assertTrue(destination.exists())
            with Image.open(destination) as image:
                self.assertEqual((120, 220, 30), image.convert("RGB").getpixel((0, 0)))
            self.assertFalse((output_root / "bike_orm.png").exists())
            self.assertEqual(1, summary.packed_created)

    def test_every_packed_layout_can_be_repacked_to_every_other_layout(self) -> None:
        source_pixels = {
            ChannelPackLayout.ORM: (30, 120, 220),
            ChannelPackLayout.RMA: (120, 220, 30),
            ChannelPackLayout.MRA: (220, 120, 30),
            ChannelPackLayout.UNITY_URP: (220, 0, 0, 135),
            ChannelPackLayout.UNITY_HDRP: (220, 30, 77, 135),
        }
        suffixes = {
            ChannelPackLayout.ORM: "orm",
            ChannelPackLayout.RMA: "rma",
            ChannelPackLayout.MRA: "mra",
            ChannelPackLayout.UNITY_URP: "metallicsmoothness",
            ChannelPackLayout.UNITY_HDRP: "maskmap",
        }

        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "src"
            output_root = Path(tmp) / "out"
            root.mkdir()

            for source_index, (source_layout, source_pixel) in enumerate(source_pixels.items()):
                for target_index, target_layout in enumerate(ChannelPackLayout):
                    with self.subTest(source=source_layout, target=target_layout):
                        base_name = f"case_{source_index}_{target_index}"
                        source_path = root / f"{base_name}_{suffixes[source_layout]}.png"
                        source_mode = "RGBA" if len(source_pixel) == 4 else "RGB"
                        Image.new(source_mode, (2, 2), source_pixel).save(source_path)

                        summary = BatchConversionService().run(
                            BatchRequest(
                                input_path=None,
                                output_root=output_root,
                                sources=(
                                    BatchSource(
                                        source_path,
                                        root,
                                        packed_layout=source_layout,
                                    ),
                                ),
                                options=ConversionOptions(
                                    packing=ChannelPackingOptions(
                                        enabled=True,
                                        layout=target_layout,
                                        mode=ChannelPackingMode.PACK_ONLY,
                                    )
                                ),
                            )
                        )

                        ao = 255 if source_layout is ChannelPackLayout.UNITY_URP else 30
                        detail = 77 if source_layout is ChannelPackLayout.UNITY_HDRP else 255
                        expected_pixels = {
                            ChannelPackLayout.ORM: (ao, 120, 220),
                            ChannelPackLayout.RMA: (120, 220, ao),
                            ChannelPackLayout.MRA: (220, 120, ao),
                            ChannelPackLayout.UNITY_URP: (220, 0, 0, 135),
                            ChannelPackLayout.UNITY_HDRP: (220, ao, detail, 135),
                        }
                        destination = output_root / f"{base_name}_{suffixes[target_layout]}.png"
                        with Image.open(destination) as image:
                            expected = expected_pixels[target_layout]
                            mode = "RGBA" if len(expected) == 4 else "RGB"
                            self.assertEqual(expected, image.convert(mode).getpixel((0, 0)))
                        self.assertEqual(1, summary.packed_created)

    def test_repacking_unity_urp_to_orm_inverts_smoothness_and_fills_ao(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "src"
            output_root = Path(tmp) / "out"
            root.mkdir()
            source_path = root / "bike_metallicsmoothness.png"
            Image.new("RGBA", (4, 4), (80, 0, 0, 200)).save(source_path)

            summary = BatchConversionService().run(
                BatchRequest(
                    input_path=None,
                    output_root=output_root,
                    sources=(
                        BatchSource(
                            source_path,
                            root,
                            packed_layout=ChannelPackLayout.UNITY_URP,
                        ),
                    ),
                    options=ConversionOptions(
                        packing=ChannelPackingOptions(
                            enabled=True,
                            layout=ChannelPackLayout.ORM,
                            mode=ChannelPackingMode.PACK_ONLY,
                        )
                    ),
                )
            )

            destination = output_root / "bike_orm.png"
            self.assertTrue(destination.exists())
            with Image.open(destination) as image:
                self.assertEqual((255, 55, 80), image.convert("RGB").getpixel((0, 0)))
            self.assertEqual(1, summary.packed_created)

    def test_offline_production_unpacks_hdrp_and_keeps_other_maps(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "src"
            output_root = Path(tmp) / "out"
            root.mkdir()
            maskmap_path = root / "bike_maskmap.png"
            basecolor_path = root / "bike_basecolor.png"
            Image.new("RGBA", (4, 4), (40, 90, 150, 210)).save(maskmap_path)
            Image.new("RGB", (4, 4), (10, 20, 30)).save(basecolor_path)
            preset = next(
                preset
                for preset in SYSTEM_PRESETS
                if preset.name == "Traditional / Non-Packed Workflow"
            )

            summary = BatchConversionService().run(
                BatchRequest(
                    input_path=root,
                    output_root=output_root,
                    options=preset.options,
                )
            )

            expected_values = {
                "bike_metallic.png": 40,
                "bike_ao.png": 90,
                "bike_detailmask.png": 150,
                "bike_roughness.png": 45,
            }
            for filename, expected_value in expected_values.items():
                with self.subTest(filename=filename):
                    with Image.open(output_root / filename) as image:
                        self.assertEqual(expected_value, image.convert("L").getpixel((0, 0)))
            self.assertTrue((output_root / "bike_basecolor.png").exists())
            self.assertFalse((output_root / "bike_maskmap.png").exists())
            self.assertEqual(5, summary.succeeded)

    def test_traditional_workflow_converts_separate_smoothness_to_roughness(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "src"
            output_root = Path(tmp) / "out"
            root.mkdir()
            Image.new("L", (2, 2), 200).save(root / "bike_smoothness.png")
            preset = next(
                preset
                for preset in SYSTEM_PRESETS
                if preset.name == "Traditional / Non-Packed Workflow"
            )

            BatchConversionService().run(
                BatchRequest(
                    input_path=root,
                    output_root=output_root,
                    options=preset.options,
                )
            )

            with Image.open(output_root / "bike_roughness.png") as image:
                self.assertEqual(55, image.convert("L").getpixel((0, 0)))
            self.assertFalse((output_root / "bike_smoothness.png").exists())


if __name__ == "__main__":
    unittest.main()
