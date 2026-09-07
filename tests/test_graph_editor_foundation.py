from __future__ import annotations

import gc
import json
import os
import struct
import sys
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

from PIL import Image, ImageFilter
from PyQt6.QtCore import QEventLoop, QObject, QPoint, QTimer, Qt
from PyQt6.QtCore import QItemSelectionModel
from PyQt6.QtGui import QColor
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QLabel

from image_converter.domain.models import (
    AppSettings,
    AssetKind,
    AssetMetadata,
    BatchRequest,
    BatchSource,
    ChannelPackLayout,
    ChannelPackingMode,
    ChannelPackingOptions,
    ConversionOptions,
    NamingRules,
    PreviewChannel,
    QueueItem,
    TextureMapType,
)
from image_converter.domain.node_graph import (
    GraphConnection,
    GraphValidationIssue,
    GraphValidationSeverity,
    NodeGraph,
    NodeGraphProject,
    NodeType,
    OutputMode,
    OutputProfile,
    SocketType,
    create_graph_node,
    make_connection_id,
    socket_definitions,
)
from image_converter.services.asset_queue import AssetScanner
from image_converter.services.conversion import BatchConversionService, ImageConverter
from image_converter.services.image_loading import copy_first_frame_preserving_alpha
from image_converter.services.map_types import detect_texture_map_type
from image_converter.services.colorspace import item_preflight_warnings
from image_converter.services.material_validation import (
    detect_normal_map_orientation,
    graph_normal_orientation_issues,
    graph_roughness_glossiness_issues,
    texture_set_items_for_item,
    texture_set_pair_warnings,
    validate_texture_sets,
)
from image_converter.services.node_graph_executor import (
    GraphExecutionCancelled,
    GraphExecutionError,
    NodeGraphExecutor,
    NodeGraphPreviewCache,
)
from image_converter.services.packing import build_channel_pack_jobs, summarize_channel_pack_jobs
from image_converter.services.pbr_preview import (
    PBR_SOLO_NORMAL_CHECK,
    PbrPreviewService,
    PbrTextureSource,
)
from image_converter.services.node_graph_project import (
    GRAPH_PROJECT_FILE_FILTER,
    GRAPH_PROJECT_FILENAME,
    NodeGraphProjectRepository,
)
from image_converter.services.presets import SYSTEM_PRESETS
from image_converter.services.settings import AppSettingsRepository
from image_converter.application.graph_export import GraphExportRequest
from image_converter.application.asset_scan import (
    AlphaAnalysisRequest,
    AssetScanRequest,
    AssetScanWorker,
    file_revision,
)
from image_converter.application.thumbnail import ThumbnailController
from image_converter.ui.graph_commands import AddNodesCommand, MoveNodesCommand, ReplaceInputConnectionCommand
from image_converter.ui.main_window import MainWindow
from image_converter.ui.asset_browser import GraphAssetsPanel
from image_converter.ui.graph_canvas import (
    ConnectionItem,
    GraphNodeItem,
    is_disconnect_shake,
)
from image_converter.ui.node_help import node_help_content
from image_converter.ui.node_editor import GraphPreviewWorker, GraphWorkspace
from image_converter.ui.node_editor import DRAFT_PREVIEW_MAX_SIDE, PREVIEW_MODE_DRAFT, PREVIEW_MODE_FULL
from image_converter.ui.node_properties import NodePropertiesPanel
from image_converter.ui.preview import PreviewPanel

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def _app() -> QApplication:
    return QApplication.instance() or QApplication(sys.argv)


def _write_rgb_tiff_with_unspecified_alpha(path: Path) -> None:
    width, height = 2, 1
    pixel_data = bytes((10, 20, 30, 40, 50, 60, 70, 200))
    entries = []
    extra_data = bytearray()
    data_offset = 8 + 2 + 10 * 12 + 4

    def add_entry(tag: int, field_type: int, count: int, value: int) -> None:
        entries.append((tag, field_type, count, value))

    bits_offset = data_offset + len(extra_data)
    extra_data.extend(struct.pack("<4H", 8, 8, 8, 8))
    pixel_offset = data_offset + len(extra_data)
    extra_data.extend(pixel_data)

    add_entry(256, 4, 1, width)
    add_entry(257, 4, 1, height)
    add_entry(258, 3, 4, bits_offset)
    add_entry(259, 3, 1, 1)
    add_entry(262, 3, 1, 2)
    add_entry(273, 4, 1, pixel_offset)
    add_entry(277, 3, 1, 4)
    add_entry(278, 4, 1, height)
    add_entry(279, 4, 1, len(pixel_data))
    add_entry(338, 3, 1, 0)

    payload = bytearray(b"II" + struct.pack("<H", 42) + struct.pack("<I", 8))
    payload.extend(struct.pack("<H", len(entries)))
    for tag, field_type, count, value in sorted(entries):
        payload.extend(struct.pack("<HHI", tag, field_type, count))
        if field_type == 3 and count == 1:
            payload.extend(struct.pack("<H", value) + b"\x00\x00")
        else:
            payload.extend(struct.pack("<I", value))
    payload.extend(struct.pack("<I", 0))
    payload.extend(extra_data)
    path.write_bytes(payload)


class ImageLoadingTests(unittest.TestCase):
    def test_tiff_unspecified_extra_sample_is_treated_as_alpha(self) -> None:
        with TemporaryDirectory() as tmp:
            source = Path(tmp) / "photoshop_alpha.tif"
            destination = Path(tmp) / "photoshop_alpha.png"
            _write_rgb_tiff_with_unspecified_alpha(source)

            with Image.open(source) as raw_image:
                self.assertEqual("RGB", raw_image.mode)
                self.assertEqual((0,), raw_image.tag_v2.get(338))
                loaded = copy_first_frame_preserving_alpha(raw_image)
            try:
                self.assertEqual("RGBA", loaded.mode)
                self.assertEqual((40, 200), loaded.getchannel("A").getextrema())
            finally:
                loaded.close()

            item = AssetScanner().scan_paths([source], recursive=False).items[0]
            self.assertIsNotNone(item.metadata)
            self.assertTrue(item.metadata.has_alpha)
            self.assertEqual("RGBA", item.metadata.mode)

            result = ImageConverter().convert(
                source,
                destination,
                ConversionOptions(overwrite=True),
            )
            self.assertTrue(result.is_success)
            with Image.open(destination) as converted:
                self.assertEqual("RGBA", converted.mode)
                self.assertEqual((40, 200), converted.getchannel("A").getextrema())

    def test_emissive_short_alias_ems_is_detected_in_queue(self) -> None:
        with TemporaryDirectory() as tmp:
            source = Path(tmp) / "anglerfish_ems.png"
            Image.new("RGB", (4, 4), (255, 64, 0)).save(source)

            self.assertEqual(TextureMapType.EMISSIVE, detect_texture_map_type(source))

            item = AssetScanner().scan_paths([source], recursive=False).items[0]
            self.assertIsNotNone(item.metadata)
            self.assertEqual(TextureMapType.EMISSIVE, item.metadata.map_type)

    def test_basecolor_short_alias_df_is_detected_in_queue(self) -> None:
        source = Path("bike_damaged_df.png")

        self.assertEqual(TextureMapType.BASECOLOR, detect_texture_map_type(source))

    def test_roughness_short_alias_raf_is_detected_in_queue(self) -> None:
        source = Path("barrels_iron_raf.png")

        self.assertEqual(TextureMapType.ROUGHNESS, detect_texture_map_type(source))


class MaterialValidationTests(unittest.TestCase):
    def test_manual_roughness_assignment_warns_for_glossiness_filename(self) -> None:
        item = QueueItem(
            BatchSource(Path("bike_glossiness.png")),
            AssetKind.IMAGE,
            None,
            map_type_override=TextureMapType.ROUGHNESS,
        )

        warnings = item_preflight_warnings(item)

        self.assertTrue(any("Вероятная ошибка" in warning for warning in warnings))
        self.assertTrue(any("нужна инверсия" in warning for warning in warnings))

    def test_texture_pair_comparison_accepts_inverse_and_warns_for_duplicate(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            roughness_path = root / "bike_roughness.png"
            smoothness_path = root / "bike_glossiness.png"
            roughness = Image.frombytes("L", (4, 1), bytes((0, 64, 128, 255)))
            roughness.save(roughness_path)

            smoothness = Image.eval(roughness, lambda value: 255 - value)
            smoothness.save(smoothness_path)
            scanned_items = AssetScanner().scan_paths(
                (roughness_path, smoothness_path),
                recursive=False,
            ).items
            self.assertEqual(
                {},
                texture_set_pair_warnings(scanned_items),
            )

            roughness.save(smoothness_path)
            scanned_items = AssetScanner().scan_paths(
                (roughness_path, smoothness_path),
                recursive=False,
            ).items
            warnings = texture_set_pair_warnings(scanned_items)

            self.assertEqual(2, len(warnings))
            self.assertTrue(
                all(
                    "почти одинаковы" in path_warnings[0]
                    for path_warnings in warnings.values()
                )
            )

    def test_graph_validator_requires_invert_between_glossiness_and_unreal(self) -> None:
        smoothness = create_graph_node(
            NodeType.TEXTURE_INPUT,
            properties={"path": "D:/textures/bike_glossiness.png"},
        )
        invert = create_graph_node(NodeType.INVERT_CHANNEL)
        output = create_graph_node(
            NodeType.OUTPUT_RGBA,
            properties={"profile": OutputProfile.UNREAL_ORM.value},
        )

        direct_graph = NodeGraph(
            nodes=[smoothness, output],
            connections=[
                GraphConnection(
                    make_connection_id(),
                    smoothness.node_id,
                    "r",
                    output.node_id,
                    "g",
                )
            ],
        )
        direct_issues = graph_roughness_glossiness_issues(direct_graph)
        self.assertEqual(1, len(direct_issues))
        self.assertIn("ожидает Roughness", direct_issues[0].message)
        self.assertIn("получает Smoothness", direct_issues[0].message)

        corrected_graph = NodeGraph(
            nodes=[smoothness, invert, output],
            connections=[
                GraphConnection(
                    make_connection_id(),
                    smoothness.node_id,
                    "r",
                    invert.node_id,
                    "in",
                ),
                GraphConnection(
                    make_connection_id(),
                    invert.node_id,
                    "out",
                    output.node_id,
                    "g",
                ),
            ],
        )
        self.assertEqual((), graph_roughness_glossiness_issues(corrected_graph))

        invert.properties["enabled"] = False
        disabled_issues = graph_roughness_glossiness_issues(corrected_graph)
        self.assertEqual(1, len(disabled_issues))
        self.assertIn("получает Smoothness", disabled_issues[0].message)

    def test_graph_validator_understands_packed_smoothness_channel(self) -> None:
        mask_map = create_graph_node(
            NodeType.TEXTURE_INPUT,
            properties={"path": "D:/textures/bike_maskmap.png"},
        )
        output = create_graph_node(
            NodeType.OUTPUT_RGBA,
            properties={"profile": OutputProfile.UNREAL_ORM.value},
        )
        graph = NodeGraph(
            nodes=[mask_map, output],
            connections=[
                GraphConnection(
                    make_connection_id(),
                    mask_map.node_id,
                    "a",
                    output.node_id,
                    "g",
                )
            ],
        )

        issues = graph_roughness_glossiness_issues(graph)

        self.assertEqual(1, len(issues))
        self.assertIs(GraphValidationSeverity.ERROR, issues[0].severity)
        self.assertIn("Ошибка схемы", issues[0].message)
        self.assertIn("получает Smoothness", issues[0].message)

    def test_graph_validator_requires_invert_for_roughness_in_unity_alpha(self) -> None:
        roughness = create_graph_node(
            NodeType.TEXTURE_INPUT,
            properties={"path": "D:/textures/bike_roughness.png"},
        )
        invert = create_graph_node(NodeType.INVERT_CHANNEL)
        output = create_graph_node(
            NodeType.OUTPUT_RGBA,
            properties={"profile": OutputProfile.UNITY_HDRP.value},
        )
        direct_graph = NodeGraph(
            nodes=[roughness, output],
            connections=[
                GraphConnection(
                    make_connection_id(), roughness.node_id, "r", output.node_id, "a"
                )
            ],
        )
        self.assertIn(
            "ожидает Smoothness",
            graph_roughness_glossiness_issues(direct_graph)[0].message,
        )

        corrected_graph = NodeGraph(
            nodes=[roughness, invert, output],
            connections=[
                GraphConnection(
                    make_connection_id(), roughness.node_id, "r", invert.node_id, "in"
                ),
                GraphConnection(
                    make_connection_id(), invert.node_id, "out", output.node_id, "a"
                ),
            ],
        )
        self.assertEqual((), graph_roughness_glossiness_issues(corrected_graph))

    def test_graph_validator_marks_ambiguous_texture_for_review(self) -> None:
        ambiguous = create_graph_node(
            NodeType.TEXTURE_INPUT,
            properties={"path": "D:/textures/bike_surface_data.png"},
        )
        output = create_graph_node(
            NodeType.OUTPUT_RGBA,
            properties={"profile": OutputProfile.UNREAL_ORM.value},
        )
        graph = NodeGraph(
            nodes=[ambiguous, output],
            connections=[
                GraphConnection(
                    make_connection_id(), ambiguous.node_id, "r", output.node_id, "g"
                )
            ],
        )

        issues = graph_roughness_glossiness_issues(graph)

        self.assertEqual(1, len(issues))
        self.assertIs(GraphValidationSeverity.WARNING, issues[0].severity)
        self.assertIn("Требует проверки", issues[0].message)

    def test_texture_set_validator_reports_resolution_duplicates_and_missing_maps(self) -> None:
        root = Path("D:/textures")
        basecolor = QueueItem(
            BatchSource(root / "bike_basecolor.png", root),
            AssetKind.IMAGE,
            AssetMetadata("PNG", 4096, 4096, "RGB", False, 64, TextureMapType.BASECOLOR),
        )
        normal = QueueItem(
            BatchSource(root / "bike_normal.png", root),
            AssetKind.IMAGE,
            AssetMetadata("PNG", 2048, 2048, "RGB", False, 64, TextureMapType.NORMAL),
        )
        roughness = QueueItem(
            BatchSource(root / "bike_roughness.png", root),
            AssetKind.IMAGE,
            AssetMetadata("PNG", 4096, 4096, "L", False, 64, TextureMapType.ROUGHNESS),
        )
        duplicate_roughness = QueueItem(
            BatchSource(root / "bike_rough.png", root),
            AssetKind.IMAGE,
            AssetMetadata("PNG", 4096, 4096, "L", False, 64, TextureMapType.ROUGHNESS),
        )

        result = validate_texture_sets(
            (basecolor, normal, roughness, duplicate_roughness),
            ConversionOptions(unpack_packed=True),
        )

        self.assertEqual(1, len(result.reports))
        report = result.reports[0]
        self.assertIn("разные разрешения", "\n".join(report.issues))
        self.assertIn("несколько карт Roughness", "\n".join(report.issues))
        self.assertIn("AO, Metallic", "\n".join(report.issues))
        self.assertEqual(0, result.ready_count)
        self.assertEqual(1, result.warning_count)

    def test_traditional_validator_accepts_complete_hdrp_source_set(self) -> None:
        root = Path("D:/textures")
        metadata = AssetMetadata("PNG", 4096, 4096, "RGBA", True, 64)
        items = (
            QueueItem(
                BatchSource(root / "bike_basecolor.png", root),
                AssetKind.IMAGE,
                replace(metadata, map_type=TextureMapType.BASECOLOR),
            ),
            QueueItem(
                BatchSource(root / "bike_normal.png", root),
                AssetKind.IMAGE,
                replace(metadata, map_type=TextureMapType.NORMAL),
            ),
            QueueItem(
                BatchSource(
                    root / "bike_maskmap.png",
                    root,
                    packed_layout=ChannelPackLayout.UNITY_HDRP,
                ),
                AssetKind.IMAGE,
                metadata,
            ),
        )

        result = validate_texture_sets(
            items,
            ConversionOptions(unpack_packed=True),
        )

        self.assertEqual(1, len(result.reports))
        self.assertTrue(result.reports[0].is_ready)
        self.assertEqual({}, result.warnings_by_path)

    def test_pack_only_validator_does_not_require_basecolor_or_normal(self) -> None:
        root = Path("D:/textures")
        items = (
            QueueItem(
                BatchSource(root / "bike_metallic.png", root),
                AssetKind.IMAGE,
                AssetMetadata(
                    "PNG", 2048, 2048, "L", False, 64, TextureMapType.METALLIC
                ),
            ),
            QueueItem(
                BatchSource(root / "bike_roughness.png", root),
                AssetKind.IMAGE,
                AssetMetadata(
                    "PNG", 2048, 2048, "L", False, 64, TextureMapType.ROUGHNESS
                ),
            ),
        )
        options = ConversionOptions(
            packing=ChannelPackingOptions(
                enabled=True,
                layout=ChannelPackLayout.UNITY_URP,
                mode=ChannelPackingMode.PACK_ONLY,
            )
        )

        result = validate_texture_sets(items, options)

        self.assertEqual(1, len(result.reports))
        self.assertTrue(result.reports[0].is_ready)

    def test_normal_orientation_suffixes_are_detected(self) -> None:
        self.assertEqual(
            "directx",
            detect_normal_map_orientation(Path("bike_normal_dx.png")),
        )
        self.assertEqual(
            "opengl",
            detect_normal_map_orientation(Path("bike_nml_OpenGL.tif")),
        )
        self.assertIsNone(
            detect_normal_map_orientation(Path("bike_normal.png"))
        )

    def test_normal_validator_warns_when_orientation_conflicts_with_unity(self) -> None:
        root = Path("D:/textures")
        normal = QueueItem(
            BatchSource(root / "bike_normal_dx.png", root),
            AssetKind.IMAGE,
            AssetMetadata("PNG", 2048, 2048, "RGB", False, 64, TextureMapType.NORMAL),
        )
        options = ConversionOptions(
            packing=ChannelPackingOptions(
                enabled=True,
                layout=ChannelPackLayout.UNITY_HDRP,
                mode=ChannelPackingMode.PACK_WITH_REMAINDER,
            )
        )

        result = validate_texture_sets((normal,), options)
        issues = "\n".join(result.reports[0].issues)

        self.assertIn("DirectX (Y−)", issues)
        self.assertIn("ожидает OpenGL (Y+)", issues)
        self.assertIn("Flip Green", issues)

    def test_normal_validator_warns_when_orientation_conflicts_with_unreal(self) -> None:
        root = Path("D:/textures")
        normal = QueueItem(
            BatchSource(root / "bike_normal_gl.png", root),
            AssetKind.IMAGE,
            AssetMetadata("PNG", 2048, 2048, "RGB", False, 64, TextureMapType.NORMAL),
        )
        options = ConversionOptions(
            packing=ChannelPackingOptions(
                enabled=True,
                layout=ChannelPackLayout.ORM,
                mode=ChannelPackingMode.PACK_WITH_REMAINDER,
            )
        )

        result = validate_texture_sets((normal,), options)
        issues = "\n".join(result.reports[0].issues)

        self.assertIn("OpenGL (Y+)", issues)
        self.assertIn("ожидает DirectX (Y−)", issues)
        self.assertIn("Flip Green", issues)

    def test_normal_validator_does_not_guess_ambiguous_orientation(self) -> None:
        root = Path("D:/textures")
        normal = QueueItem(
            BatchSource(root / "bike_normal.png", root),
            AssetKind.IMAGE,
            AssetMetadata("PNG", 2048, 2048, "RGB", False, 64, TextureMapType.NORMAL),
        )
        options = ConversionOptions(
            packing=ChannelPackingOptions(
                enabled=True,
                layout=ChannelPackLayout.UNITY_URP,
                mode=ChannelPackingMode.PACK_WITH_REMAINDER,
            )
        )

        result = validate_texture_sets((normal,), options)

        self.assertFalse(
            any("Flip Green" in issue for issue in result.reports[0].issues)
        )

    def test_manual_normal_override_still_checks_mode_and_orientation(self) -> None:
        root = Path("D:/textures")
        item = QueueItem(
            BatchSource(root / "bike_height_dx.png", root),
            AssetKind.IMAGE,
            AssetMetadata("PNG", 2048, 2048, "L", False, 64),
        )
        item.map_type_override = TextureMapType.NORMAL
        options = ConversionOptions(
            packing=ChannelPackingOptions(
                enabled=True,
                layout=ChannelPackLayout.UNITY_HDRP,
                mode=ChannelPackingMode.PACK_WITH_REMAINDER,
            )
        )

        result = validate_texture_sets((item,), options)
        issues = "\n".join(result.reports[0].issues)

        self.assertIn("ожидается RGB или RGBA", issues)
        self.assertIn("ожидает OpenGL (Y+)", issues)

    def test_normal_validator_accepts_flat_tangent_space_normal(self) -> None:
        with TemporaryDirectory() as tmp:
            normal_path = Path(tmp) / "bike_normal.png"
            Image.new("RGB", (8, 8), (128, 128, 255)).save(normal_path)
            item = AssetScanner().scan_paths(
                (normal_path,),
                recursive=False,
            ).items[0]

            result = validate_texture_sets((item,), ConversionOptions())

            self.assertEqual(1, len(result.reports))
            self.assertTrue(result.reports[0].is_ready)
            self.assertIsNotNone(item.metadata.normal_map_statistics)

    def test_normal_validator_rejects_grayscale_and_non_normal_pixels(self) -> None:
        with TemporaryDirectory() as tmp:
            normal_path = Path(tmp) / "bike_normal.png"
            Image.new("L", (8, 8), 32).save(normal_path)
            item = AssetScanner().scan_paths(
                (normal_path,),
                recursive=False,
            ).items[0]

            result = validate_texture_sets((item,), ConversionOptions())
            issues = "\n".join(result.reports[0].issues)

            self.assertIn("ожидается RGB или RGBA", issues)
            self.assertIn("не похоже на обычную tangent-space", issues)


class PbrPreviewTests(unittest.TestCase):
    def test_texture_set_selection_does_not_mix_materials_in_same_folder(self) -> None:
        root = Path("D:/textures")
        metadata = AssetMetadata("PNG", 8, 8, "RGB", False, 64)
        bike_base = QueueItem(
            BatchSource(root / "bike_basecolor.png", root),
            AssetKind.IMAGE,
            replace(metadata, map_type=TextureMapType.BASECOLOR),
        )
        bike_normal = QueueItem(
            BatchSource(root / "bike_normal.png", root),
            AssetKind.IMAGE,
            replace(metadata, map_type=TextureMapType.NORMAL),
        )
        car_base = QueueItem(
            BatchSource(root / "car_basecolor.png", root),
            AssetKind.IMAGE,
            replace(metadata, map_type=TextureMapType.BASECOLOR),
        )

        selected = texture_set_items_for_item(
            (bike_base, bike_normal, car_base),
            bike_normal,
        )

        self.assertEqual(
            {"bike_basecolor.png", "bike_normal.png"},
            {item.path.name for item in selected},
        )

    def test_pbr_preview_prepares_orm_channels_for_gpu(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            orm_path = root / "bike_orm.png"
            Image.new("RGB", (8, 8), (64, 128, 240)).save(orm_path)
            source = PbrTextureSource(
                orm_path,
                TextureMapType.UNKNOWN,
                ChannelPackLayout.ORM,
            )
            material = PbrPreviewService().load((source,))

            self.assertEqual(
                (128, 240, 64, 255),
                material.properties.getpixel((4, 4)),
            )

    def test_pbr_preview_converts_directx_normal_for_display(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            normal_path = root / "bike_normal_dx.png"
            Image.new("RGB", (8, 8), (128, 32, 255)).save(normal_path)
            source = PbrTextureSource(normal_path, TextureMapType.NORMAL)

            material = PbrPreviewService().load((source,))

            self.assertEqual((128, 32, 255), material.normal.getpixel((4, 4)))
            self.assertTrue(material.normal_is_directx)

    def test_pbr_preview_inverts_separate_smoothness(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            smoothness_path = root / "bike_smoothness.png"
            Image.new("L", (8, 8), 200).save(smoothness_path)
            source = PbrTextureSource(smoothness_path, TextureMapType.SMOOTHNESS)

            material = PbrPreviewService().load((source,))

            self.assertEqual(55, material.properties.getpixel((4, 4))[0])

    def test_graph_pbr_shader_decodes_unreal_orm_and_separate_override(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            base_path = root / "bike_basecolor.png"
            normal_path = root / "bike_normal_dx.png"
            packed_path = root / "bike_orm.png"
            roughness_path = root / "bike_roughness.png"
            Image.new("RGB", (8, 8), (120, 80, 40)).save(base_path)
            Image.new("RGB", (8, 8), (128, 96, 255)).save(normal_path)
            Image.new("RGB", (8, 8), (64, 128, 240)).save(packed_path)
            Image.new("L", (8, 8), 32).save(roughness_path)

            base = create_graph_node(NodeType.TEXTURE_INPUT, properties={"path": str(base_path)})
            normal = create_graph_node(NodeType.TEXTURE_INPUT, properties={"path": str(normal_path)})
            packed = create_graph_node(NodeType.TEXTURE_INPUT, properties={"path": str(packed_path)})
            roughness = create_graph_node(
                NodeType.TEXTURE_INPUT,
                properties={"path": str(roughness_path)},
            )
            shader = create_graph_node(
                NodeType.PBR_SHADER,
                properties={"workflow": "unreal_orm"},
            )
            graph = NodeGraph(
                nodes=[base, normal, packed, roughness, shader],
                connections=[
                    GraphConnection("c1", base.node_id, "image", shader.node_id, "basecolor"),
                    GraphConnection("c2", normal.node_id, "image", shader.node_id, "normal"),
                    GraphConnection("c3", packed.node_id, "image", shader.node_id, "packed"),
                    GraphConnection("c4", roughness.node_id, "r", shader.node_id, "roughness"),
                ],
            )

            material = NodeGraphExecutor().render_pbr_material_node(graph, shader)

            self.assertEqual((32, 240, 64, 255), material.properties.getpixel((4, 4)))
            self.assertTrue(material.normal_is_directx)
            self.assertEqual("Unreal ORM", material.workflow_label)

    def test_graph_pbr_shader_warns_when_unreal_receives_opengl_normal(self) -> None:
        normal = create_graph_node(
            NodeType.TEXTURE_INPUT,
            properties={"path": "D:/textures/bike_normal_gl.png"},
        )
        shader = create_graph_node(
            NodeType.PBR_SHADER,
            properties={"workflow": "unreal_orm"},
        )
        graph = NodeGraph(
            nodes=[normal, shader],
            connections=[
                GraphConnection("c1", normal.node_id, "image", shader.node_id, "normal")
            ],
        )

        issues = graph_normal_orientation_issues(graph)

        self.assertEqual(1, len(issues))
        self.assertIn("DirectX", issues[0].message)
        self.assertIn("OpenGL", issues[0].message)

    def test_graph_pbr_shader_tracks_flip_green_before_unity(self) -> None:
        normal = create_graph_node(
            NodeType.TEXTURE_INPUT,
            properties={"path": "D:/textures/bike_normal_dx.png"},
        )
        flip = create_graph_node(
            NodeType.NORMAL_MAP,
            properties={"flip_green": True},
        )
        shader = create_graph_node(
            NodeType.PBR_SHADER,
            properties={"workflow": "unity_hdrp"},
        )
        graph = NodeGraph(
            nodes=[normal, flip, shader],
            connections=[
                GraphConnection("c1", normal.node_id, "image", flip.node_id, "image"),
                GraphConnection("c2", flip.node_id, "out", shader.node_id, "normal"),
            ],
        )

        self.assertEqual((), graph_normal_orientation_issues(graph))


class NodeGraphExecutorPerformanceTests(unittest.TestCase):
    def test_interactive_preview_keeps_mix_and_mask_at_preview_resolution(self) -> None:
        with TemporaryDirectory() as tmp:
            mask_path = Path(tmp) / "mask.png"
            Image.new("L", (1024, 1024), 128).save(mask_path)
            red = create_graph_node(
                NodeType.COLOR,
                properties={"red": 255, "width": 1024, "height": 1024},
            )
            blue = create_graph_node(
                NodeType.COLOR,
                properties={"blue": 255, "width": 1024, "height": 1024},
            )
            mask = create_graph_node(
                NodeType.TEXTURE_INPUT,
                properties={"path": str(mask_path)},
            )
            mix = create_graph_node(NodeType.MIX_IMAGE)
            graph = NodeGraph(
                nodes=[red, blue, mask, mix],
                connections=[
                    GraphConnection("a", red.node_id, "image", mix.node_id, "a"),
                    GraphConnection("b", blue.node_id, "image", mix.node_id, "b"),
                    GraphConnection("mask", mask.node_id, "r", mix.node_id, "mask"),
                ],
            )

            class TrackingExecutor(NodeGraphExecutor):
                def __init__(self) -> None:
                    super().__init__()
                    self.working_sizes: list[tuple[int, int]] = []
                    self.mask_sizes: list[tuple[int, int]] = []

                def _image_operation_size(self, *args, **kwargs):
                    size = super()._image_operation_size(*args, **kwargs)
                    self.working_sizes.append(size)
                    return size

                def _evaluate_channel_socket_cached(self, graph, connection, target_size, visiting, cache):
                    if connection.source_node_id == mask.node_id:
                        self.mask_sizes.append(target_size)
                    return super()._evaluate_channel_socket_cached(
                        graph,
                        connection,
                        target_size,
                        visiting,
                        cache,
                    )

            executor = TrackingExecutor()
            preview_cache = NodeGraphPreviewCache(
                max_side=64,
                interactive_preview=True,
            )
            image, _meta = executor.render_display_node(
                graph,
                mix,
                preview_cache=preview_cache,
                max_side=64,
            )

            self.assertEqual((64, 64), image.size)
            self.assertEqual([(64, 64)], executor.working_sizes)
            self.assertEqual([(64, 64)], executor.mask_sizes)

    def test_preview_executor_honors_cancellation_before_render(self) -> None:
        color = create_graph_node(NodeType.COLOR)
        with self.assertRaises(GraphExecutionCancelled):
            NodeGraphExecutor(cancel_requested=lambda: True).render_display_node(
                NodeGraph(nodes=[color]),
                color,
            )

    def test_preview_worker_emits_canceled_for_obsolete_request(self) -> None:
        color = create_graph_node(NodeType.COLOR)
        worker = GraphPreviewWorker(
            7,
            NodeGraphProject(graph=NodeGraph(nodes=[color])),
            color,
        )
        canceled: list[int] = []
        worker.canceled.connect(canceled.append)

        worker.cancel()
        worker.run()

        self.assertEqual([7], canceled)

    def test_mix_image_uses_alpha_mask_and_output_channel_overrides(self) -> None:
        with TemporaryDirectory() as tmp:
            mask_path = Path(tmp) / "mask.png"
            mask = Image.new("RGBA", (2, 1), (0, 0, 0, 0))
            mask.putpixel((1, 0), (0, 0, 0, 255))
            mask.save(mask_path)

            red = create_graph_node(
                NodeType.COLOR,
                properties={"red": 255, "green": 0, "blue": 0, "width": 2, "height": 1},
            )
            blue = create_graph_node(
                NodeType.COLOR,
                properties={"red": 0, "green": 0, "blue": 255, "width": 2, "height": 1},
            )
            mask_texture = create_graph_node(
                NodeType.TEXTURE_INPUT,
                properties={"path": str(mask_path)},
            )
            mix = create_graph_node(NodeType.MIX_IMAGE)
            green_override = create_graph_node(
                NodeType.CONSTANT_CHANNEL,
                properties={"value": 64},
            )
            output = create_graph_node(NodeType.OUTPUT_RGBA)
            view = create_graph_node(NodeType.VIEW)
            graph = NodeGraph(
                nodes=[red, blue, mask_texture, mix, green_override, output, view],
                connections=[
                    GraphConnection(make_connection_id(), red.node_id, "image", mix.node_id, "a"),
                    GraphConnection(make_connection_id(), blue.node_id, "image", mix.node_id, "b"),
                    GraphConnection(make_connection_id(), mask_texture.node_id, "a", mix.node_id, "mask"),
                    GraphConnection(make_connection_id(), mix.node_id, "image", output.node_id, "image"),
                    GraphConnection(make_connection_id(), green_override.node_id, "out", output.node_id, "g"),
                    GraphConnection(make_connection_id(), mix.node_id, "image", view.node_id, "image"),
                ],
            )

            executor = NodeGraphExecutor()
            output_image = executor.render_output_node(graph, output)
            view_image = executor.render_view_node(graph, view, fallback_size=(2, 1), max_side=0)

            self.assertEqual(
                [(255, 64, 0, 255), (0, 64, 255, 255)],
                [output_image.getpixel((x, 0)) for x in range(2)],
            )
            self.assertEqual(
                [(255, 0, 0, 255), (0, 0, 255, 255)],
                [view_image.getpixel((x, 0)) for x in range(2)],
            )

    def test_output_channel_override_is_reported_in_validation_and_preview_meta(self) -> None:
        image = create_graph_node(
            NodeType.COLOR,
            properties={
                "red": 64,
                "green": 96,
                "blue": 128,
                "alpha": 32,
                "width": 1,
                "height": 1,
            },
        )
        alpha_override = create_graph_node(
            NodeType.CONSTANT_CHANNEL,
            properties={"value": 255},
        )
        output = create_graph_node(
            NodeType.OUTPUT_RGBA,
            properties={"alpha_input_mode": "replace"},
        )
        project = NodeGraphProject(
            graph=NodeGraph(
                nodes=[image, alpha_override, output],
                connections=[
                    GraphConnection(
                        make_connection_id(),
                        image.node_id,
                        "image",
                        output.node_id,
                        "image",
                    ),
                    GraphConnection(
                        make_connection_id(),
                        alpha_override.node_id,
                        "out",
                        output.node_id,
                        "a",
                    ),
                ],
            )
        )
        executor = NodeGraphExecutor()

        issues = executor.validate_issues(project)
        rendered, meta = executor.render_display_node(project.graph, output)

        self.assertTrue(
            any(
                issue.node_id == output.node_id
                and "A input overrides the same channel from Image" in issue.message
                for issue in issues
            )
        )
        self.assertIn("channel overrides: A", meta)
        self.assertEqual((64, 96, 128, 255), rendered.getpixel((0, 0)))

    def test_output_alpha_input_multiplies_existing_image_alpha_by_default(self) -> None:
        color = create_graph_node(
            NodeType.COLOR,
            properties={
                "red": 64,
                "green": 96,
                "blue": 128,
                "alpha": 255,
                "width": 1,
                "height": 1,
            },
        )
        first_mask = create_graph_node(
            NodeType.CONSTANT_CHANNEL,
            properties={"value": 128},
        )
        apply_mask = create_graph_node(NodeType.SET_ALPHA)
        output_alpha = create_graph_node(
            NodeType.CONSTANT_CHANNEL,
            properties={"value": 128},
        )
        output = create_graph_node(NodeType.OUTPUT_RGBA)
        graph = NodeGraph(
            nodes=[color, first_mask, apply_mask, output_alpha, output],
            connections=[
                GraphConnection(
                    make_connection_id(),
                    color.node_id,
                    "image",
                    apply_mask.node_id,
                    "image",
                ),
                GraphConnection(
                    make_connection_id(),
                    first_mask.node_id,
                    "out",
                    apply_mask.node_id,
                    "alpha",
                ),
                GraphConnection(
                    make_connection_id(),
                    apply_mask.node_id,
                    "out",
                    output.node_id,
                    "image",
                ),
                GraphConnection(
                    make_connection_id(),
                    output_alpha.node_id,
                    "out",
                    output.node_id,
                    "a",
                ),
            ],
        )
        executor = NodeGraphExecutor()

        rendered, meta = executor.render_display_node(graph, output)

        self.assertEqual((64, 96, 128, 64), rendered.getpixel((0, 0)))
        self.assertIn("alpha: Image × A", meta)
        self.assertNotIn("channel overrides: A", meta)

    def test_mix_image_uses_factor_without_mask(self) -> None:
        red = create_graph_node(
            NodeType.COLOR,
            properties={"red": 255, "green": 0, "blue": 0, "width": 1, "height": 1},
        )
        blue = create_graph_node(
            NodeType.COLOR,
            properties={"red": 0, "green": 0, "blue": 255, "width": 1, "height": 1},
        )
        mix = create_graph_node(NodeType.MIX_IMAGE, properties={"factor": 50})
        output = create_graph_node(NodeType.OUTPUT_RGBA)
        graph = NodeGraph(
            nodes=[red, blue, mix, output],
            connections=[
                GraphConnection(make_connection_id(), red.node_id, "image", mix.node_id, "a"),
                GraphConnection(make_connection_id(), blue.node_id, "image", mix.node_id, "b"),
                GraphConnection(make_connection_id(), mix.node_id, "image", output.node_id, "image"),
            ],
        )

        image = NodeGraphExecutor().render_output_node(graph, output)

        self.assertEqual((127, 0, 127, 255), image.getpixel((0, 0)))

    def test_split_combine_and_set_alpha_round_trip_rgba(self) -> None:
        color = create_graph_node(
            NodeType.COLOR,
            properties={
                "red": 10,
                "green": 20,
                "blue": 30,
                "alpha": 40,
                "width": 1,
                "height": 1,
            },
        )
        split = create_graph_node(NodeType.SPLIT_RGBA)
        combine = create_graph_node(NodeType.COMBINE_RGBA)
        replacement_alpha = create_graph_node(
            NodeType.CONSTANT_CHANNEL,
            properties={"value": 99},
        )
        set_alpha = create_graph_node(NodeType.SET_ALPHA)
        output = create_graph_node(NodeType.OUTPUT_RGBA)
        graph = NodeGraph(
            nodes=[color, split, combine, replacement_alpha, set_alpha, output],
            connections=[
                GraphConnection(make_connection_id(), color.node_id, "image", split.node_id, "image"),
                *[
                    GraphConnection(make_connection_id(), split.node_id, channel, combine.node_id, channel)
                    for channel in ("r", "g", "b", "a")
                ],
                GraphConnection(make_connection_id(), combine.node_id, "image", set_alpha.node_id, "image"),
                GraphConnection(make_connection_id(), replacement_alpha.node_id, "out", set_alpha.node_id, "alpha"),
                GraphConnection(make_connection_id(), set_alpha.node_id, "out", output.node_id, "image"),
            ],
        )

        image = NodeGraphExecutor().render_output_node(graph, output)

        self.assertEqual((10, 20, 30, 99), image.getpixel((0, 0)))

    def test_set_alpha_without_alpha_input_preserves_original_alpha(self) -> None:
        color = create_graph_node(
            NodeType.COLOR,
            properties={
                "red": 10,
                "green": 20,
                "blue": 30,
                "alpha": 40,
                "width": 1,
                "height": 1,
            },
        )
        set_alpha = create_graph_node(NodeType.SET_ALPHA)
        output = create_graph_node(NodeType.OUTPUT_RGBA)
        graph = NodeGraph(
            nodes=[color, set_alpha, output],
            connections=[
                GraphConnection(make_connection_id(), color.node_id, "image", set_alpha.node_id, "image"),
                GraphConnection(make_connection_id(), set_alpha.node_id, "out", output.node_id, "image"),
            ],
        )

        executor = NodeGraphExecutor()
        image = executor.render_output_node(graph, output)
        issues = executor.validate_issues(NodeGraphProject(graph=graph))

        self.assertEqual((10, 20, 30, 40), image.getpixel((0, 0)))
        self.assertFalse(any(issue.node_id == set_alpha.node_id for issue in issues))

    def test_apply_mask_multiply_rgb_matches_three_blend_channel_nodes(self) -> None:
        with TemporaryDirectory() as tmp:
            mask_path = Path(tmp) / "mask.png"
            mask = Image.new("RGBA", (3, 1))
            for x, value in enumerate((0, 128, 255)):
                mask.putpixel((x, 0), (0, 0, 0, value))
            mask.save(mask_path)

            color = create_graph_node(
                NodeType.COLOR,
                properties={
                    "red": 120,
                    "green": 80,
                    "blue": 40,
                    "alpha": 200,
                    "width": 3,
                    "height": 1,
                },
            )
            mask_texture = create_graph_node(
                NodeType.TEXTURE_INPUT,
                properties={"path": str(mask_path)},
            )

            old_blends = [
                create_graph_node(
                    NodeType.BLEND_CHANNEL,
                    properties={"mode": "multiply", "opacity": 100},
                )
                for _channel in ("r", "g", "b")
            ]
            old_output = create_graph_node(NodeType.OUTPUT_RGBA)
            old_connections = []
            for channel, blend in zip(("r", "g", "b"), old_blends, strict=True):
                old_connections.extend(
                    (
                        GraphConnection(make_connection_id(), mask_texture.node_id, "a", blend.node_id, "a"),
                        GraphConnection(make_connection_id(), color.node_id, channel, blend.node_id, "b"),
                        GraphConnection(make_connection_id(), blend.node_id, "out", old_output.node_id, channel),
                    )
                )
            old_connections.append(
                GraphConnection(make_connection_id(), color.node_id, "a", old_output.node_id, "a")
            )
            old_graph = NodeGraph(
                nodes=[color, mask_texture, *old_blends, old_output],
                connections=old_connections,
            )

            apply_mask = create_graph_node(
                NodeType.SET_ALPHA,
                properties={"mask_mode": "multiply_rgb"},
            )
            new_output = create_graph_node(NodeType.OUTPUT_RGBA)
            new_graph = NodeGraph(
                nodes=[color, mask_texture, apply_mask, new_output],
                connections=[
                    GraphConnection(make_connection_id(), color.node_id, "image", apply_mask.node_id, "image"),
                    GraphConnection(make_connection_id(), mask_texture.node_id, "a", apply_mask.node_id, "alpha"),
                    GraphConnection(make_connection_id(), apply_mask.node_id, "out", new_output.node_id, "image"),
                ],
            )

            old_image = NodeGraphExecutor().render_output_node(old_graph, old_output)
            new_image = NodeGraphExecutor().render_output_node(new_graph, new_output)

            self.assertEqual(old_image.size, new_image.size)
            self.assertEqual(old_image.tobytes(), new_image.tobytes())

    def test_image_and_output_resolution_sources_are_explicit(self) -> None:
        with TemporaryDirectory() as tmp:
            a_path = Path(tmp) / "a.png"
            b_path = Path(tmp) / "b.png"
            Image.new("RGBA", (2, 2), (255, 0, 0, 255)).save(a_path)
            Image.new("RGBA", (4, 3), (0, 0, 255, 255)).save(b_path)
            a = create_graph_node(NodeType.TEXTURE_INPUT, properties={"path": str(a_path)})
            b = create_graph_node(NodeType.TEXTURE_INPUT, properties={"path": str(b_path)})
            mix = create_graph_node(
                NodeType.MIX_IMAGE,
                properties={"resolution_source": "b", "factor": 50},
            )
            output = create_graph_node(NodeType.OUTPUT_RGBA)
            graph = NodeGraph(
                nodes=[a, b, mix, output],
                connections=[
                    GraphConnection(make_connection_id(), a.node_id, "image", mix.node_id, "a"),
                    GraphConnection(make_connection_id(), b.node_id, "image", mix.node_id, "b"),
                    GraphConnection(make_connection_id(), mix.node_id, "image", output.node_id, "image"),
                ],
            )

            executor = NodeGraphExecutor()
            self.assertEqual((4, 3), executor.render_output_node(graph, output).size)

            output.properties.update(
                {
                    "resolution_source": "custom",
                    "resolution_width": 5,
                    "resolution_height": 2,
                }
            )
            self.assertEqual((5, 2), executor.render_output_node(graph, output).size)

            output.properties["resolution_source"] = "auto"
            mix.properties.update(
                {
                    "resolution_source": "custom",
                    "resolution_width": 6,
                    "resolution_height": 4,
                }
            )
            self.assertEqual((6, 4), executor.render_output_node(graph, output).size)

    def test_apply_mask_filter_controls_mask_resampling(self) -> None:
        with TemporaryDirectory() as tmp:
            mask_path = Path(tmp) / "mask.png"
            mask = Image.new("RGBA", (2, 1), (0, 0, 0, 0))
            mask.putpixel((1, 0), (0, 0, 0, 255))
            mask.save(mask_path)
            color = create_graph_node(
                NodeType.COLOR,
                properties={"red": 255, "green": 255, "blue": 255, "width": 3, "height": 1},
            )
            mask_texture = create_graph_node(NodeType.TEXTURE_INPUT, properties={"path": str(mask_path)})
            apply_mask = create_graph_node(
                NodeType.SET_ALPHA,
                properties={"mask_mode": "replace_alpha", "mask_filter": "nearest"},
            )
            output = create_graph_node(NodeType.OUTPUT_RGBA)
            graph = NodeGraph(
                nodes=[color, mask_texture, apply_mask, output],
                connections=[
                    GraphConnection(make_connection_id(), color.node_id, "image", apply_mask.node_id, "image"),
                    GraphConnection(make_connection_id(), mask_texture.node_id, "a", apply_mask.node_id, "alpha"),
                    GraphConnection(make_connection_id(), apply_mask.node_id, "out", output.node_id, "image"),
                ],
            )

            nearest = NodeGraphExecutor().render_output_node(graph, output).getchannel("A")
            apply_mask.properties["mask_filter"] = "bilinear"
            bilinear = NodeGraphExecutor().render_output_node(graph, output).getchannel("A")

            self.assertNotEqual(nearest.getpixel((1, 0)), bilinear.getpixel((1, 0)))
            self.assertIn(nearest.getpixel((1, 0)), (0, 255))
            self.assertGreater(bilinear.getpixel((1, 0)), 0)
            self.assertLess(bilinear.getpixel((1, 0)), 255)

    def test_validation_warns_about_size_and_aspect_mismatch(self) -> None:
        with TemporaryDirectory() as tmp:
            a_path = Path(tmp) / "a.png"
            b_path = Path(tmp) / "b.png"
            Image.new("RGBA", (4, 4)).save(a_path)
            Image.new("RGBA", (8, 4)).save(b_path)
            a = create_graph_node(NodeType.TEXTURE_INPUT, properties={"path": str(a_path)})
            b = create_graph_node(NodeType.TEXTURE_INPUT, properties={"path": str(b_path)})
            mix = create_graph_node(NodeType.MIX_IMAGE)
            output = create_graph_node(NodeType.OUTPUT_RGBA)
            graph = NodeGraph(
                nodes=[a, b, mix, output],
                connections=[
                    GraphConnection(make_connection_id(), a.node_id, "image", mix.node_id, "a"),
                    GraphConnection(make_connection_id(), b.node_id, "image", mix.node_id, "b"),
                    GraphConnection(make_connection_id(), mix.node_id, "image", output.node_id, "image"),
                ],
            )

            issues = NodeGraphExecutor().validate_issues(NodeGraphProject(graph=graph))

            mismatch = next(issue for issue in issues if issue.node_id == mix.node_id and issue.socket_id == "resolution")
            self.assertIn("input sizes differ", mismatch.message)
            self.assertIn("Aspect ratios differ", mismatch.message)

    def test_all_new_image_nodes_can_render_from_display_flag(self) -> None:
        a = create_graph_node(
            NodeType.COLOR,
            properties={"red": 10, "green": 20, "blue": 30, "alpha": 40},
        )
        b = create_graph_node(
            NodeType.COLOR,
            properties={"red": 100, "green": 110, "blue": 120, "alpha": 130},
        )
        mix = create_graph_node(NodeType.MIX_IMAGE)
        blend = create_graph_node(NodeType.BLEND_IMAGE)
        normal_map = create_graph_node(NodeType.NORMAL_MAP)
        split = create_graph_node(NodeType.SPLIT_RGBA)
        combine = create_graph_node(NodeType.COMBINE_RGBA)
        set_alpha = create_graph_node(NodeType.SET_ALPHA)
        graph = NodeGraph(
            nodes=[a, b, mix, blend, normal_map, split, combine, set_alpha],
            connections=[
                GraphConnection(make_connection_id(), a.node_id, "image", mix.node_id, "a"),
                GraphConnection(make_connection_id(), b.node_id, "image", mix.node_id, "b"),
                GraphConnection(make_connection_id(), a.node_id, "image", blend.node_id, "a"),
                GraphConnection(make_connection_id(), b.node_id, "image", blend.node_id, "b"),
                GraphConnection(
                    make_connection_id(),
                    a.node_id,
                    "image",
                    normal_map.node_id,
                    "image",
                ),
                GraphConnection(make_connection_id(), a.node_id, "image", split.node_id, "image"),
                GraphConnection(make_connection_id(), a.node_id, "r", combine.node_id, "r"),
                GraphConnection(make_connection_id(), a.node_id, "g", combine.node_id, "g"),
                GraphConnection(make_connection_id(), a.node_id, "b", combine.node_id, "b"),
                GraphConnection(make_connection_id(), a.node_id, "a", combine.node_id, "a"),
                GraphConnection(make_connection_id(), a.node_id, "image", set_alpha.node_id, "image"),
                GraphConnection(make_connection_id(), b.node_id, "a", set_alpha.node_id, "alpha"),
            ],
        )
        executor = NodeGraphExecutor()

        for node in (mix, blend, normal_map, split, combine, set_alpha):
            with self.subTest(node_type=node.node_type):
                image, _meta = executor.render_display_node(
                    graph,
                    node,
                    fallback_size=(2, 1),
                    max_side=0,
                )
                self.assertEqual("RGBA", image.mode)
                self.assertEqual((1024, 1024), image.size)

    def test_normal_map_node_flips_green_and_preserves_alpha(self) -> None:
        source = create_graph_node(
            NodeType.COLOR,
            properties={
                "red": 64,
                "green": 32,
                "blue": 255,
                "alpha": 77,
                "width": 1,
                "height": 1,
            },
        )
        normal_map = create_graph_node(
            NodeType.NORMAL_MAP,
            properties={"flip_green": True},
        )
        output = create_graph_node(NodeType.OUTPUT_RGBA)
        graph = NodeGraph(
            nodes=[source, normal_map, output],
            connections=[
                GraphConnection(
                    make_connection_id(), source.node_id, "image", normal_map.node_id, "image"
                ),
                GraphConnection(
                    make_connection_id(), normal_map.node_id, "out", output.node_id, "image"
                ),
            ],
        )

        image = NodeGraphExecutor().render_output_node(graph, output)

        self.assertEqual((64, 223, 255, 77), image.getpixel((0, 0)))

    def test_normal_map_node_reconstructs_normalizes_and_adjusts_strength(self) -> None:
        source = create_graph_node(
            NodeType.COLOR,
            properties={
                "red": 255,
                "green": 128,
                "blue": 0,
                "alpha": 41,
                "width": 1,
                "height": 1,
            },
        )
        normal_map = create_graph_node(
            NodeType.NORMAL_MAP,
            properties={"reconstruct_blue": True, "strength": 0},
        )
        output = create_graph_node(NodeType.OUTPUT_RGBA)
        graph = NodeGraph(
            nodes=[source, normal_map, output],
            connections=[
                GraphConnection(
                    make_connection_id(), source.node_id, "image", normal_map.node_id, "image"
                ),
                GraphConnection(
                    make_connection_id(), normal_map.node_id, "out", output.node_id, "image"
                ),
            ],
        )

        flat = NodeGraphExecutor().render_output_node(graph, output).getpixel((0, 0))
        self.assertLessEqual(abs(flat[0] - 128), 1)
        self.assertLessEqual(abs(flat[1] - 128), 1)
        self.assertEqual(255, flat[2])
        self.assertEqual(41, flat[3])

        normal_map.properties.update(
            {"reconstruct_blue": False, "normalize": True, "strength": 100}
        )
        normalized = NodeGraphExecutor().render_output_node(graph, output).getpixel((0, 0))
        self.assertLessEqual(abs(normalized[0] - 218), 2)
        self.assertLessEqual(abs(normalized[1] - 128), 2)
        self.assertLessEqual(abs(normalized[2] - 37), 2)
        self.assertEqual(41, normalized[3])

    def test_blend_image_applies_mask_to_rgba_result(self) -> None:
        with TemporaryDirectory() as tmp:
            mask_path = Path(tmp) / "blend_mask.png"
            mask = Image.new("RGBA", (2, 1), (0, 0, 0, 0))
            mask.putpixel((1, 0), (0, 0, 0, 255))
            mask.save(mask_path)

            a = create_graph_node(
                NodeType.COLOR,
                properties={"red": 100, "green": 50, "blue": 20, "alpha": 200, "width": 2, "height": 1},
            )
            b = create_graph_node(
                NodeType.COLOR,
                properties={"red": 20, "green": 30, "blue": 40, "alpha": 20, "width": 2, "height": 1},
            )
            mask_texture = create_graph_node(NodeType.TEXTURE_INPUT, properties={"path": str(mask_path)})
            blend = create_graph_node(
                NodeType.BLEND_IMAGE,
                properties={"mode": "add", "opacity": 100},
            )
            output = create_graph_node(NodeType.OUTPUT_RGBA)
            graph = NodeGraph(
                nodes=[a, b, mask_texture, blend, output],
                connections=[
                    GraphConnection(make_connection_id(), a.node_id, "image", blend.node_id, "a"),
                    GraphConnection(make_connection_id(), b.node_id, "image", blend.node_id, "b"),
                    GraphConnection(make_connection_id(), mask_texture.node_id, "a", blend.node_id, "mask"),
                    GraphConnection(make_connection_id(), blend.node_id, "image", output.node_id, "image"),
                ],
            )

            image = NodeGraphExecutor().render_output_node(graph, output)

            self.assertEqual((100, 50, 20, 200), image.getpixel((0, 0)))
            self.assertEqual((120, 80, 60, 220), image.getpixel((1, 0)))

    def test_validation_rejects_image_to_channel_connection(self) -> None:
        color = create_graph_node(NodeType.COLOR)
        output = create_graph_node(NodeType.OUTPUT_RGBA)
        project = NodeGraphProject(
            graph=NodeGraph(
                nodes=[color, output],
                connections=[
                    GraphConnection(
                        make_connection_id(),
                        color.node_id,
                        "image",
                        output.node_id,
                        "r",
                    )
                ],
            )
        )

        issues = NodeGraphExecutor().validate_issues(project)

        self.assertTrue(
            any(
                issue.node_id == output.node_id
                and issue.socket_id == "r"
                and SocketType.IMAGE.value in issue.message
                and SocketType.CHANNEL.value in issue.message
                for issue in issues
            )
        )

    def test_validation_ignores_unfinished_image_nodes_outside_output_branches(self) -> None:
        color = create_graph_node(NodeType.COLOR)
        unfinished_mix = create_graph_node(NodeType.MIX_IMAGE)
        output = create_graph_node(NodeType.OUTPUT_RGBA)
        project = NodeGraphProject(
            graph=NodeGraph(
                nodes=[color, unfinished_mix, output],
                connections=[
                    GraphConnection(
                        make_connection_id(),
                        color.node_id,
                        "image",
                        output.node_id,
                        "image",
                    )
                ],
            )
        )

        issues = NodeGraphExecutor().validate_issues(project)

        self.assertFalse(any(issue.node_id == unfinished_mix.node_id for issue in issues))

    def test_output_cannot_overwrite_source_texture(self) -> None:
        with TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.png"
            Image.new("RGB", (4, 4), (12, 34, 56)).save(source)
            original_bytes = source.read_bytes()
            texture = create_graph_node(
                NodeType.TEXTURE_INPUT,
                properties={"path": str(source)},
            )
            output = create_graph_node(
                NodeType.OUTPUT_RGBA,
                properties={"output_path": str(source)},
            )
            graph = NodeGraph(
                nodes=[texture, output],
                connections=[
                    GraphConnection(
                        make_connection_id(),
                        texture.node_id,
                        channel,
                        output.node_id,
                        channel,
                    )
                    for channel in ("r", "g", "b")
                ],
            )

            with self.assertRaises(GraphExecutionError):
                NodeGraphExecutor().export_enabled_outputs(
                    NodeGraphProject(graph=graph),
                    Path(tmp),
                    ConversionOptions(overwrite=True),
                )

            self.assertEqual(original_bytes, source.read_bytes())

    def test_failed_atomic_save_preserves_existing_destination(self) -> None:
        with TemporaryDirectory() as tmp:
            destination = Path(tmp) / "result.png"
            destination.write_bytes(b"original")

            def fail_after_partial_write(_image, path, **_kwargs):
                Path(path).write_bytes(b"partial")
                raise OSError("simulated write failure")

            with mock.patch.object(
                Image.Image,
                "save",
                autospec=True,
                side_effect=fail_after_partial_write,
            ):
                with self.assertRaises(OSError):
                    NodeGraphExecutor()._save_output_image(
                        Image.new("RGBA", (4, 4)),
                        destination,
                        ConversionOptions(overwrite=True),
                    )

            self.assertEqual(b"original", destination.read_bytes())
            self.assertEqual([], list(destination.parent.glob(f".{destination.name}.*.tmp")))

    def test_asset_scan_worker_emits_bounded_batches(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            for index in range(130):
                Image.new("RGB", (1, 1)).save(root / f"asset_{index:03d}.png")
            request = AssetScanRequest(target="queue", paths=(root,), recursive=False)
            worker = AssetScanWorker(request)
            batch_sizes: list[int] = []
            completed: list[bool] = []
            worker.items_found.connect(
                lambda _request, items: batch_sizes.append(len(items))
            )
            worker.finished.connect(lambda _request, _result: completed.append(True))

            worker.run()

            self.assertEqual([64, 64, 2], batch_sizes)
            self.assertEqual([True], completed)

    def test_stale_thumbnail_and_alpha_results_are_discarded(self) -> None:
        class ThumbnailReceiver(QObject):
            def __init__(self):
                super().__init__()
                self.received: list[Path] = []

            def on_asset_thumbnail_ready(self, path: Path, _image: object) -> None:
                self.received.append(path)

        with TemporaryDirectory() as tmp:
            app = _app()
            source = Path(tmp) / "source.png"
            Image.new("RGBA", (2, 2), (1, 2, 3, 255)).save(source)
            receiver = ThumbnailReceiver()
            thumbnails = ThumbnailController(receiver)
            stale_key = thumbnails.cache_key(source)
            self.assertIsNotNone(stale_key)
            thumbnails._in_flight.add(stale_key)
            thumbnails._active_key = stale_key
            old_revision = file_revision(source)
            Image.new("RGBA", (3, 3), (4, 5, 6, 255)).save(source)

            thumbnails._on_ready(source, stale_key, Image.new("RGBA", (2, 2)))

            self.assertEqual([], receiver.received)

            window = MainWindow()
            scanned = AssetScanner().scan_paths([source], recursive=False).items[0]
            window._queue_items = [scanned]
            original_metadata = scanned.metadata
            request = AlphaAnalysisRequest(
                source,
                original_metadata,
                revision=old_revision,
            )
            analyzed = replace(original_metadata, alpha_fully_opaque=True)
            try:
                window.on_alpha_analysis_finished(request, analyzed)
                self.assertIs(original_metadata, scanned.metadata)
            finally:
                window.close()
                window.deleteLater()
                app.processEvents()

    def test_export_collision_blocks_all_writes(self) -> None:
        with TemporaryDirectory() as tmp:
            texture_path = Path(tmp) / "source.png"
            Image.new("RGB", (4, 4), (32, 64, 96)).save(texture_path)
            texture = create_graph_node(NodeType.TEXTURE_INPUT, properties={"path": str(texture_path)})
            export_root = Path(tmp) / "exports"
            outputs = [
                create_graph_node(
                    NodeType.OUTPUT_RGBA,
                    title="Output relative",
                    properties={"output_path": "nested/../same.png"},
                ),
                create_graph_node(
                    NodeType.OUTPUT_RGBA,
                    title="Output absolute",
                    properties={"output_path": str(export_root / "SAME.PNG")},
                ),
            ]
            connections = [
                GraphConnection(make_connection_id(), texture.node_id, channel, output.node_id, channel)
                for output in outputs
                for channel in ("r", "g", "b")
            ]
            project = NodeGraphProject(graph=NodeGraph(nodes=[texture, *outputs], connections=connections))

            with self.assertRaises(GraphExecutionError):
                NodeGraphExecutor().export_enabled_outputs(
                    project,
                    export_root,
                    ConversionOptions(overwrite=True),
                )

            self.assertFalse((export_root / "same.png").exists())

    def test_preview_cache_enforces_global_budget_and_skips_oversized_entries(self) -> None:
        cache = NodeGraphPreviewCache(max_bytes=64)
        first = Image.new("RGBA", (4, 4))
        second = Image.new("L", (8, 8))
        oversized = Image.new("RGBA", (5, 5))

        cache.put_image("channel", ("first",), first)
        cache.put_image("texture-channel", ("second",), second)
        self.assertIsNone(cache.get_image("channel", ("first",)))
        self.assertIsNotNone(cache.get_image("texture-channel", ("second",)))
        self.assertLessEqual(cache.current_bytes, 64)

        cache.put_image("texture-preview", ("oversized",), oversized)
        self.assertIsNone(cache.get_image("texture-preview", ("oversized",)))
        self.assertLessEqual(cache.current_bytes, 64)

    def test_asset_header_scan_defers_alpha_extrema(self) -> None:
        with TemporaryDirectory() as tmp:
            source = Path(tmp) / "opaque.png"
            Image.new("RGBA", (8, 8), (20, 40, 60, 255)).save(source)
            scanner = AssetScanner()

            item = scanner.scan_paths([source], recursive=False).items[0]
            self.assertIsNone(item.metadata.alpha_fully_opaque)
            self.assertNotIn("Альфа-канал есть, но полностью непрозрачный", item.metadata.warnings)

            analyzed = scanner.analyze_alpha(source, item.metadata)
            self.assertTrue(analyzed.alpha_fully_opaque)
            self.assertIn("Альфа-канал есть, но полностью непрозрачный", analyzed.warnings)

    def test_color_node_exports_solid_rgba_image_at_configured_size(self) -> None:
        with TemporaryDirectory() as tmp:
            color = create_graph_node(
                NodeType.COLOR,
                properties={
                    "red": 12,
                    "green": 34,
                    "blue": 56,
                    "alpha": 78,
                    "width": 3,
                    "height": 2,
                },
            )
            output = create_graph_node(
                NodeType.OUTPUT_RGBA,
                properties={"filename": "solid.png"},
            )
            graph = NodeGraph(
                nodes=[color, output],
                connections=[
                    GraphConnection(
                        make_connection_id(),
                        color.node_id,
                        channel,
                        output.node_id,
                        channel,
                    )
                    for channel in ("r", "g", "b", "a")
                ],
            )

            summary = NodeGraphExecutor().export_output(
                NodeGraphProject(graph=graph),
                output,
                Path(tmp),
                ConversionOptions(overwrite=True),
            )

            self.assertEqual(1, summary.succeeded)
            with Image.open(Path(tmp) / "solid.png") as exported:
                self.assertEqual((3, 2), exported.size)
                self.assertEqual("RGBA", exported.mode)
                self.assertEqual((12, 34, 56, 78), exported.getpixel((0, 0)))

    def test_texture_size_takes_priority_over_color_node_size(self) -> None:
        with TemporaryDirectory() as tmp:
            texture_path = Path(tmp) / "source.png"
            Image.new("RGB", (5, 4), (10, 20, 30)).save(texture_path)
            texture = create_graph_node(
                NodeType.TEXTURE_INPUT,
                properties={"path": str(texture_path)},
            )
            color = create_graph_node(
                NodeType.COLOR,
                properties={"red": 200, "width": 2, "height": 2},
            )
            output = create_graph_node(NodeType.OUTPUT_RGBA)
            graph = NodeGraph(
                nodes=[texture, color, output],
                connections=[
                    GraphConnection(make_connection_id(), color.node_id, "r", output.node_id, "r"),
                    GraphConnection(make_connection_id(), texture.node_id, "g", output.node_id, "g"),
                    GraphConnection(make_connection_id(), texture.node_id, "b", output.node_id, "b"),
                ],
            )

            image = NodeGraphExecutor().render_output_node(graph, output)

            self.assertEqual((5, 4), image.size)
            self.assertEqual((200, 20, 30, 255), image.getpixel((0, 0)))

    def test_render_output_node_reuses_cached_texture_image_for_multiple_channels(self) -> None:
        with TemporaryDirectory() as tmp:
            texture_path = Path(tmp) / "shared.png"
            Image.new("RGBA", (4, 4), (24, 96, 180, 200)).save(texture_path)

            texture = create_graph_node(
                NodeType.TEXTURE_INPUT,
                properties={"path": str(texture_path)},
            )
            output = create_graph_node(NodeType.OUTPUT_RGBA)
            graph = NodeGraph(
                nodes=[texture, output],
                connections=[
                    GraphConnection(make_connection_id(), texture.node_id, "r", output.node_id, "r"),
                    GraphConnection(make_connection_id(), texture.node_id, "g", output.node_id, "g"),
                    GraphConnection(make_connection_id(), texture.node_id, "b", output.node_id, "b"),
                    GraphConnection(make_connection_id(), texture.node_id, "a", output.node_id, "a"),
                ],
            )

            executor = NodeGraphExecutor()
            with (
                mock.patch.object(
                    executor,
                    "_load_texture_preview_image",
                    wraps=executor._load_texture_preview_image,
                ) as preview_loader,
                mock.patch.object(
                    executor,
                    "_load_texture_size",
                    wraps=executor._load_texture_size,
                ) as size_loader,
            ):
                image = executor.render_output_node(graph, output)

            self.assertEqual((4, 4), image.size)
            self.assertEqual((24, 96, 180, 200), image.getpixel((0, 0)))
            self.assertEqual(1, preview_loader.call_count)
            self.assertEqual(1, size_loader.call_count)

    def test_export_enabled_outputs_reuses_shared_branch_across_outputs(self) -> None:
        with TemporaryDirectory() as tmp:
            texture_path = Path(tmp) / "mask.png"
            Image.new("RGBA", (8, 8), (48, 96, 144, 255)).save(texture_path)

            texture = create_graph_node(
                NodeType.TEXTURE_INPUT,
                properties={"path": str(texture_path)},
            )
            levels = create_graph_node(
                NodeType.LEVELS_CHANNEL,
                properties={"black": 16, "white": 224, "gamma": 1.0, "out_min": 0, "out_max": 255},
            )
            output_a = create_graph_node(
                NodeType.OUTPUT_RGBA,
                properties={"filename": "packed_a.png"},
            )
            output_b = create_graph_node(
                NodeType.OUTPUT_RGBA,
                properties={"filename": "packed_b.png"},
            )
            graph = NodeGraph(
                nodes=[texture, levels, output_a, output_b],
                connections=[
                    GraphConnection(make_connection_id(), texture.node_id, "r", levels.node_id, "in"),
                    GraphConnection(make_connection_id(), levels.node_id, "out", output_a.node_id, "r"),
                    GraphConnection(make_connection_id(), levels.node_id, "out", output_a.node_id, "g"),
                    GraphConnection(make_connection_id(), levels.node_id, "out", output_a.node_id, "b"),
                    GraphConnection(make_connection_id(), levels.node_id, "out", output_b.node_id, "r"),
                    GraphConnection(make_connection_id(), levels.node_id, "out", output_b.node_id, "g"),
                    GraphConnection(make_connection_id(), levels.node_id, "out", output_b.node_id, "b"),
                ],
            )

            executor = NodeGraphExecutor()
            with (
                mock.patch.object(
                    executor,
                    "_load_texture_preview_image",
                    wraps=executor._load_texture_preview_image,
                ) as preview_loader,
                mock.patch.object(
                    executor,
                    "_apply_levels",
                    wraps=executor._apply_levels,
                ) as levels_apply,
            ):
                summary = executor.export_enabled_outputs(
                    NodeGraphProject(graph=graph),
                    Path(tmp),
                    ConversionOptions(overwrite=True),
                )

            self.assertEqual(2, summary.succeeded)
            self.assertEqual(1, preview_loader.call_count)
            self.assertEqual(1, levels_apply.call_count)
            self.assertTrue((Path(tmp) / "packed_a.png").exists())
            self.assertTrue((Path(tmp) / "packed_b.png").exists())

    def test_export_output_exports_only_requested_node_even_when_disabled(self) -> None:
        with TemporaryDirectory() as tmp:
            texture_path = Path(tmp) / "source.png"
            Image.new("RGB", (4, 4), (32, 96, 160)).save(texture_path)
            texture = create_graph_node(
                NodeType.TEXTURE_INPUT,
                properties={"path": str(texture_path)},
            )
            requested = create_graph_node(
                NodeType.OUTPUT_RGBA,
                properties={"filename": "requested.png", "enabled": False},
            )
            other = create_graph_node(
                NodeType.OUTPUT_RGBA,
                properties={"filename": "other.png", "enabled": True},
            )
            connections = []
            for output in (requested, other):
                for channel in ("r", "g", "b"):
                    connections.append(
                        GraphConnection(
                            make_connection_id(),
                            texture.node_id,
                            channel,
                            output.node_id,
                            channel,
                        )
                    )
            project = NodeGraphProject(
                graph=NodeGraph(nodes=[texture, requested, other], connections=connections)
            )

            summary = NodeGraphExecutor().export_output(
                project,
                requested,
                Path(tmp),
                ConversionOptions(overwrite=True),
            )

            self.assertEqual(1, summary.total)
            self.assertEqual(1, summary.succeeded)
            self.assertTrue((Path(tmp) / "requested.png").exists())
            self.assertFalse((Path(tmp) / "other.png").exists())

    def test_channel_operations_keep_expected_values(self) -> None:
        executor = NodeGraphExecutor()
        source = Image.frombytes("L", (5, 1), bytes((0, 64, 128, 192, 255)))
        blur_source = Image.frombytes("L", (5, 1), bytes((0, 0, 255, 0, 0)))
        morphology_source = Image.frombytes(
            "L",
            (5, 5),
            bytes(
                (
                    0, 0, 0, 0, 0,
                    0, 0, 0, 0, 0,
                    0, 0, 255, 0, 0,
                    0, 0, 0, 0, 0,
                    0, 0, 0, 0, 0,
                )
            ),
        )

        levels_node = create_graph_node(
            NodeType.LEVELS_CHANNEL,
            properties={"black": 32, "white": 224, "gamma": 2.0, "out_min": 10, "out_max": 240},
        )
        remap_node = create_graph_node(
            NodeType.REMAP_CHANNEL,
            properties={"in_min": 64, "in_max": 192, "out_min": 32, "out_max": 224},
        )
        clamp_node = create_graph_node(
            NodeType.CLAMP_CHANNEL,
            properties={"min": 40, "max": 180},
        )
        threshold_node = create_graph_node(
            NodeType.THRESHOLD_CHANNEL,
            properties={"threshold": 120},
        )
        blur_node = create_graph_node(
            NodeType.BLUR_CHANNEL,
            properties={"radius": 1},
        )
        dilate_node = create_graph_node(
            NodeType.DILATE_CHANNEL,
            properties={"radius": 1},
        )
        erode_node = create_graph_node(
            NodeType.ERODE_CHANNEL,
            properties={"radius": 1},
        )

        levels_image = executor._apply_levels(source, levels_node)
        remap_image = executor._apply_remap(source, remap_node)
        clamp_image = executor._apply_clamp(source, clamp_node)
        threshold_image = executor._apply_threshold(source, threshold_node)
        blur_image = executor._apply_blur(blur_source, blur_node)
        dilate_image = executor._apply_dilate(morphology_source, dilate_node)
        erode_image = executor._apply_erode(morphology_source, erode_node)

        expected_levels = []
        for value in (0, 64, 128, 192, 255):
            normalized = min(max((value - 32) / 192.0, 0.0), 1.0)
            adjusted = normalized ** 0.5
            expected_levels.append(round(10 + adjusted * 230))

        self.assertEqual(expected_levels, list(levels_image.getdata()))
        self.assertEqual([32, 32, 128, 224, 224], list(remap_image.getdata()))
        self.assertEqual([40, 64, 128, 180, 180], list(clamp_image.getdata()))
        self.assertEqual([0, 0, 255, 255, 255], list(threshold_image.getdata()))
        self.assertEqual(
            list(blur_source.filter(ImageFilter.BoxBlur(1)).getdata()),
            list(blur_image.getdata()),
        )
        self.assertEqual(
            list(morphology_source.filter(ImageFilter.MaxFilter(3)).getdata()),
            list(dilate_image.getdata()),
        )
        self.assertEqual(
            list(morphology_source.filter(ImageFilter.MinFilter(3)).getdata()),
            list(erode_image.getdata()),
        )


class TextureProcessingNodeTests(unittest.TestCase):
    @staticmethod
    def _render_image_node(
        nodes: list,
        connections: list[GraphConnection],
        image_node,
    ) -> Image.Image:
        output = create_graph_node(NodeType.OUTPUT_RGBA)
        graph = NodeGraph(
            nodes=[*nodes, output],
            connections=[
                *connections,
                GraphConnection(
                    make_connection_id(),
                    image_node.node_id,
                    socket_definitions(image_node.node_type)[-1].socket_id,
                    output.node_id,
                    "image",
                ),
            ],
        )
        return NodeGraphExecutor().render_output_node(graph, output)

    def test_new_processing_nodes_have_defaults_sockets_and_help(self) -> None:
        expected = {
            NodeType.HEIGHT_TO_NORMAL: ("height", "normal"),
            NodeType.NORMAL_BLEND: ("base", "detail", "mask", "normal"),
            NodeType.COLOR_ADJUST: ("image", "out"),
            NodeType.TRANSFORM_2D: ("image", "out"),
            NodeType.RESIZE_CANVAS: ("image", "out"),
        }
        for node_type, socket_ids in expected.items():
            with self.subTest(node_type=node_type):
                node = create_graph_node(node_type)
                self.assertEqual(
                    socket_ids,
                    tuple(socket.socket_id for socket in socket_definitions(node_type)),
                )
                self.assertTrue(node.properties)
                self.assertIsNotNone(node_help_content(node_type))

    def test_height_to_normal_switches_green_for_directx(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "height.png"
            height = Image.new("L", (5, 5))
            height.putdata([row * 60 for row in range(5) for _column in range(5)])
            height.save(path)
            texture = create_graph_node(NodeType.TEXTURE_INPUT, properties={"path": str(path)})
            normal = create_graph_node(
                NodeType.HEIGHT_TO_NORMAL,
                properties={"radius": 0.0, "strength": 100, "convention": "opengl"},
            )
            connection = GraphConnection(
                make_connection_id(), texture.node_id, "r", normal.node_id, "height"
            )

            opengl = self._render_image_node([texture, normal], [connection], normal)
            normal.properties["convention"] = "directx"
            directx = self._render_image_node([texture, normal], [connection], normal)

            gl_pixel = opengl.getpixel((2, 2))
            dx_pixel = directx.getpixel((2, 2))
            self.assertAlmostEqual(gl_pixel[0], dx_pixel[0], delta=2)
            self.assertAlmostEqual(gl_pixel[1] + dx_pixel[1], 255, delta=3)
            self.assertGreater(gl_pixel[2], 180)

    def test_normal_blend_rnm_preserves_detail_over_flat_base(self) -> None:
        base = create_graph_node(
            NodeType.COLOR,
            properties={"red": 128, "green": 128, "blue": 255, "alpha": 73, "width": 2, "height": 2},
        )
        detail = create_graph_node(
            NodeType.COLOR,
            properties={"red": 180, "green": 110, "blue": 243, "alpha": 255, "width": 2, "height": 2},
        )
        blend = create_graph_node(NodeType.NORMAL_BLEND)
        connections = [
            GraphConnection(make_connection_id(), base.node_id, "image", blend.node_id, "base"),
            GraphConnection(make_connection_id(), detail.node_id, "image", blend.node_id, "detail"),
        ]

        image = self._render_image_node([base, detail, blend], connections, blend)
        pixel = image.getpixel((0, 0))

        self.assertAlmostEqual(pixel[0], 180, delta=3)
        self.assertAlmostEqual(pixel[1], 110, delta=3)
        self.assertAlmostEqual(pixel[2], 243, delta=3)
        self.assertEqual(73, pixel[3])

        mask = create_graph_node(NodeType.CONSTANT_CHANNEL, properties={"value": 0})
        masked_connections = [
            *connections,
            GraphConnection(make_connection_id(), mask.node_id, "out", blend.node_id, "mask"),
        ]
        masked = self._render_image_node(
            [base, detail, mask, blend],
            masked_connections,
            blend,
        )
        self.assertEqual((128, 128, 255, 73), masked.getpixel((0, 0)))

    def test_color_adjust_preserves_alpha(self) -> None:
        color = create_graph_node(
            NodeType.COLOR,
            properties={"red": 64, "green": 32, "blue": 16, "alpha": 91, "width": 1, "height": 1},
        )
        adjust = create_graph_node(NodeType.COLOR_ADJUST, properties={"exposure": 1.0})
        connection = GraphConnection(
            make_connection_id(), color.node_id, "image", adjust.node_id, "image"
        )

        pixel = self._render_image_node([color, adjust], [connection], adjust).getpixel((0, 0))

        self.assertEqual((128, 64, 32, 91), pixel)

    def test_transform_repeat_offset_and_rotation(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "stripe.png"
            source = Image.new("RGBA", (2, 1))
            source.putdata([(255, 0, 0, 255), (0, 0, 255, 255)])
            source.save(path)
            texture = create_graph_node(NodeType.TEXTURE_INPUT, properties={"path": str(path)})
            transform = create_graph_node(
                NodeType.TRANSFORM_2D,
                properties={"offset_x": 1, "address_mode": "repeat", "filter": "nearest"},
            )
            connection = GraphConnection(
                make_connection_id(), texture.node_id, "image", transform.node_id, "image"
            )

            shifted = self._render_image_node([texture, transform], [connection], transform)
            self.assertEqual([(0, 0, 255, 255), (255, 0, 0, 255)], list(shifted.getdata()))

            transform.properties.update({"offset_x": 0, "rotation": 90})
            rotated = self._render_image_node([texture, transform], [connection], transform)
            self.assertEqual((1, 2), rotated.size)
            self.assertEqual((255, 0, 0, 255), rotated.getpixel((0, 0)))
            self.assertEqual((0, 0, 255, 255), rotated.getpixel((0, 1)))

    def test_resize_canvas_exact_fit_and_power_of_two(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "source.png"
            Image.new("RGBA", (3, 5), (20, 40, 60, 255)).save(path)
            texture = create_graph_node(NodeType.TEXTURE_INPUT, properties={"path": str(path)})
            resize = create_graph_node(
                NodeType.RESIZE_CANVAS,
                properties={
                    "size_mode": "exact",
                    "width": 4,
                    "height": 4,
                    "resize_mode": "fit",
                    "filter": "nearest",
                },
            )
            connection = GraphConnection(
                make_connection_id(), texture.node_id, "image", resize.node_id, "image"
            )

            fitted = self._render_image_node([texture, resize], [connection], resize)
            self.assertEqual((4, 4), fitted.size)
            self.assertEqual(0, fitted.getpixel((0, 0))[3])
            self.assertEqual(255, fitted.getpixel((1, 1))[3])

            resize.properties.update({"size_mode": "pot_up", "resize_mode": "stretch"})
            pot = self._render_image_node([texture, resize], [connection], resize)
            self.assertEqual((4, 8), pot.size)

    def test_new_processing_nodes_round_trip_in_project_file(self) -> None:
        nodes = [
            create_graph_node(NodeType.HEIGHT_TO_NORMAL, properties={"strength": 275}),
            create_graph_node(NodeType.NORMAL_BLEND, properties={"detail_strength": 65}),
            create_graph_node(NodeType.COLOR_ADJUST, properties={"hue": -30}),
            create_graph_node(NodeType.TRANSFORM_2D, properties={"rotation": 270}),
            create_graph_node(NodeType.RESIZE_CANVAS, properties={"size_mode": "pot_up"}),
        ]
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "processing.texturegraph"
            repository = NodeGraphProjectRepository()
            repository.save(NodeGraphProject(graph=NodeGraph(nodes=nodes)), path)
            loaded = repository.load(path)

        self.assertEqual([node.node_type for node in nodes], [node.node_type for node in loaded.graph.nodes])
        self.assertEqual(275, loaded.graph.nodes[0].properties["strength"])
        self.assertEqual("pot_up", loaded.graph.nodes[-1].properties["size_mode"])


class GraphAssetsPanelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = _app()
        self.panel = GraphAssetsPanel()

    def tearDown(self) -> None:
        self.panel.setParent(None)
        self.panel.deleteLater()
        self.app.processEvents()
        del self.panel
        gc.collect()

    def test_filter_and_selection_are_owned_by_assets_panel(self) -> None:
        metadata = AssetMetadata(
            format_name="PNG",
            width=4,
            height=4,
            mode="RGBA",
            has_alpha=True,
            file_size_bytes=64,
            map_type=TextureMapType.BASECOLOR,
        )
        albedo = QueueItem(
            BatchSource(Path("albedo.png"), map_type=TextureMapType.BASECOLOR),
            AssetKind.IMAGE,
            metadata,
        )
        normal = QueueItem(
            BatchSource(Path("normal.png"), map_type=TextureMapType.NORMAL),
            AssetKind.IMAGE,
            metadata,
        )
        remove_requests: list[bool] = []
        self.panel.remove_requested.connect(lambda: remove_requests.append(True))

        self.panel.set_assets([albedo, normal])
        self.panel.table.selectRow(1)
        self.panel.filter_edit.setText("normal")
        self.app.processEvents()

        self.assertEqual((normal,), self.panel.rows)
        self.assertEqual([normal], self.panel.selected_items())
        self.panel.remove_button.click()
        self.assertEqual([True], remove_requests)


class NodePropertiesPanelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = _app()
        self.panel = NodePropertiesPanel()

    def tearDown(self) -> None:
        self.panel.setParent(None)
        self.panel.deleteLater()
        self.app.processEvents()
        del self.panel
        gc.collect()

    def test_pbr_shader_shows_and_updates_workflow_controls(self) -> None:
        node = create_graph_node(NodeType.PBR_SHADER)
        self.panel.set_node(node)
        changes: list[dict] = []
        self.panel.node_changed.connect(
            lambda _node, _title, properties, _rebuild, _mode: changes.append(
                properties
            )
        )

        self.assertFalse(self.panel.pbr_workflow_combo.isHidden())
        self.assertFalse(self.panel.pbr_normal_convention_combo.isHidden())
        self.panel.pbr_workflow_combo.setCurrentIndex(3)
        self.panel.pbr_normal_convention_combo.setCurrentIndex(2)

        self.assertTrue(changes)
        self.assertEqual("unreal_orm", changes[-1]["workflow"])
        self.assertEqual("directx", changes[-1]["normal_convention"])

    def test_slider_drag_emits_draft_then_full_preview(self) -> None:
        node = create_graph_node(NodeType.LEVELS_CHANNEL)
        self.panel.set_node(node)

        changed_modes: list[str] = []
        refresh_modes: list[str] = []
        self.panel.node_changed.connect(
            lambda _node, _title, _properties, _needs_rebuild, preview_mode: changed_modes.append(preview_mode)
        )
        self.panel.preview_refresh_requested.connect(
            lambda _node, preview_mode: refresh_modes.append(preview_mode)
        )

        self.panel.level_black_slider.sliderPressed.emit()
        self.panel.level_black_slider.setValue(32)
        self.app.processEvents()
        self.panel.level_black_slider.sliderReleased.emit()
        self.app.processEvents()

        self.assertIn(PREVIEW_MODE_DRAFT, changed_modes)
        self.assertIn(PREVIEW_MODE_FULL, changed_modes)
        self.assertEqual([], refresh_modes)

    def test_slider_drag_coalesces_rapid_draft_updates(self) -> None:
        node = create_graph_node(NodeType.LEVELS_CHANNEL)
        self.panel.set_node(node)
        changed_modes: list[str] = []
        self.panel.node_changed.connect(
            lambda _node, _title, _properties, _needs_rebuild, preview_mode: changed_modes.append(preview_mode)
        )

        self.panel.level_black_slider.sliderPressed.emit()
        self.panel.level_black_slider.setValue(20)
        self.panel.level_black_slider.setValue(40)
        self.app.processEvents()
        self.assertEqual([PREVIEW_MODE_DRAFT], changed_modes)

        loop = QEventLoop()
        QTimer.singleShot(75, loop.quit)
        loop.exec()
        self.app.processEvents()
        self.assertEqual([PREVIEW_MODE_DRAFT, PREVIEW_MODE_DRAFT], changed_modes)

        self.panel.level_black_slider.sliderReleased.emit()
        self.app.processEvents()
        self.assertEqual(PREVIEW_MODE_FULL, changed_modes[-1])

    def test_numeric_spin_uses_debounced_full_preview(self) -> None:
        node = create_graph_node(NodeType.THRESHOLD_CHANNEL)
        self.panel.set_node(node)

        changed_modes: list[str] = []
        self.panel.node_changed.connect(
            lambda _node, _title, _properties, _needs_rebuild, preview_mode: changed_modes.append(preview_mode)
        )

        self.panel.threshold_spin.setValue(200)
        self.app.processEvents()
        self.assertEqual([], changed_modes)

        loop = QEventLoop()
        QTimer.singleShot(250, loop.quit)
        loop.exec()
        self.app.processEvents()

        self.assertEqual([PREVIEW_MODE_FULL], changed_modes)

    def test_new_mask_nodes_show_expected_controls(self) -> None:
        self.panel.set_node(create_graph_node(NodeType.REMAP_CHANNEL))
        self.assertFalse(self.panel.remap_in_min_host.isHidden())
        self.assertFalse(self.panel.remap_in_max_host.isHidden())
        self.assertFalse(self.panel.remap_out_min_host.isHidden())
        self.assertFalse(self.panel.remap_out_max_host.isHidden())
        self.assertTrue(self.panel.blur_radius_host.isHidden())

        self.panel.set_node(create_graph_node(NodeType.BLUR_CHANNEL))
        self.assertFalse(self.panel.blur_radius_host.isHidden())
        self.assertTrue(self.panel.dilate_radius_host.isHidden())
        self.assertTrue(self.panel.erode_radius_host.isHidden())

        self.panel.set_node(create_graph_node(NodeType.DILATE_CHANNEL))
        self.assertFalse(self.panel.dilate_radius_host.isHidden())
        self.assertTrue(self.panel.erode_radius_host.isHidden())

        self.panel.set_node(create_graph_node(NodeType.ERODE_CHANNEL))
        self.assertFalse(self.panel.erode_radius_host.isHidden())

    def test_image_nodes_show_mix_and_blend_controls(self) -> None:
        mix = create_graph_node(NodeType.MIX_IMAGE, properties={"factor": 35})
        changes: list[dict[str, object]] = []
        self.panel.node_changed.connect(
            lambda _node, _title, properties, _needs_rebuild, _preview_mode: changes.append(properties)
        )

        self.panel.set_node(mix)
        self.assertFalse(self.panel.mix_factor_host.isHidden())
        self.assertTrue(self.panel.blend_opacity_host.isHidden())
        self.assertFalse(self.panel.image_resolution_source_combo.isHidden())
        self.assertFalse(self.panel.mask_filter_combo.isHidden())
        self.assertEqual(35, self.panel.mix_factor_spin.value())

        self.panel.mix_factor_spin.setValue(70)
        self.panel._flush_numeric_preview()
        self.assertEqual(70, changes[-1]["factor"])

        self.panel.set_node(create_graph_node(NodeType.BLEND_IMAGE))
        self.assertTrue(self.panel.mix_factor_host.isHidden())
        self.assertFalse(self.panel.blend_mode_combo.isHidden())
        self.assertFalse(self.panel.blend_opacity_host.isHidden())

        apply_mask = create_graph_node(
            NodeType.SET_ALPHA,
            properties={"mask_mode": "multiply_rgb", "mask_filter": "nearest"},
        )
        self.panel.set_node(apply_mask)
        self.assertFalse(self.panel.mask_mode_combo.isHidden())
        self.assertFalse(self.panel.mask_filter_combo.isHidden())
        self.assertEqual("multiply_rgb", self.panel.mask_mode_combo.currentData())
        self.assertEqual("nearest", self.panel.mask_filter_combo.currentData())

        output = create_graph_node(
            NodeType.OUTPUT_RGBA,
            properties={
                "resolution_source": "custom",
                "resolution_width": 2048,
                "resolution_height": 1024,
            },
        )
        self.panel.set_node(output)
        self.assertFalse(self.panel.output_resolution_source_combo.isHidden())
        self.assertFalse(self.panel.output_alpha_input_mode_combo.isHidden())
        self.assertEqual("multiply", self.panel.output_alpha_input_mode_combo.currentData())
        self.assertFalse(self.panel.custom_resolution_host.isHidden())
        self.assertEqual(2048, self.panel.resolution_width_spin.value())
        self.assertEqual(1024, self.panel.resolution_height_spin.value())

        self.panel.output_alpha_input_mode_combo.setCurrentIndex(1)
        self.assertEqual("replace", changes[-1]["alpha_input_mode"])

    def test_texture_processing_nodes_show_their_controls(self) -> None:
        self.panel.set_node(
            create_graph_node(
                NodeType.HEIGHT_TO_NORMAL,
                properties={"strength": 250, "radius": 2.5, "convention": "directx"},
            )
        )
        self.assertFalse(self.panel.height_strength_host.isHidden())
        self.assertEqual(250, self.panel.height_strength_spin.value())
        self.assertEqual(2.5, self.panel.height_radius_spin.value())
        self.assertEqual("directx", self.panel.height_convention_combo.currentData())

        self.panel.set_node(create_graph_node(NodeType.NORMAL_BLEND))
        self.assertFalse(self.panel.normal_blend_strength_host.isHidden())
        self.assertFalse(self.panel.mask_filter_combo.isHidden())

        self.panel.set_node(create_graph_node(NodeType.COLOR_ADJUST))
        self.assertFalse(self.panel.color_exposure_spin.isHidden())
        self.assertFalse(self.panel.color_saturation_host.isHidden())
        self.assertTrue(self.panel.transform_offset_host.isHidden())

        self.panel.set_node(create_graph_node(NodeType.TRANSFORM_2D))
        self.assertFalse(self.panel.transform_offset_host.isHidden())
        self.assertFalse(self.panel.transform_address_combo.isHidden())

        self.panel.set_node(create_graph_node(NodeType.RESIZE_CANVAS))
        self.assertFalse(self.panel.resize_size_mode_combo.isHidden())
        self.assertFalse(self.panel.resize_resolution_host.isHidden())
        self.panel.resize_size_mode_combo.setCurrentIndex(1)
        self.assertTrue(self.panel.resize_resolution_host.isHidden())

    def test_normal_map_node_shows_and_updates_processing_controls(self) -> None:
        node = create_graph_node(
            NodeType.NORMAL_MAP,
            properties={
                "flip_green": True,
                "reconstruct_blue": True,
                "normalize": True,
                "strength": 175,
            },
        )
        changes: list[dict[str, object]] = []
        self.panel.node_changed.connect(
            lambda _node, _title, properties, _needs_rebuild, _preview_mode: changes.append(
                properties
            )
        )

        self.panel.set_node(node)

        self.assertFalse(self.panel.normal_flip_red_checkbox.isHidden())
        self.assertFalse(self.panel.normal_flip_green_checkbox.isHidden())
        self.assertFalse(self.panel.normal_reconstruct_blue_checkbox.isHidden())
        self.assertFalse(self.panel.normal_normalize_checkbox.isHidden())
        self.assertFalse(self.panel.normal_strength_host.isHidden())
        self.assertTrue(self.panel.normal_flip_green_checkbox.isChecked())
        self.assertTrue(self.panel.normal_reconstruct_blue_checkbox.isChecked())
        self.assertTrue(self.panel.normal_normalize_checkbox.isChecked())
        self.assertEqual(175, self.panel.normal_strength_spin.value())

        self.panel.normal_flip_red_checkbox.setChecked(True)
        self.panel.normal_strength_spin.setValue(220)
        self.panel._flush_numeric_preview()

        self.assertTrue(changes[-1]["flip_red"])
        self.assertEqual(220, changes[-1]["strength"])

    def test_output_export_button_requests_current_output(self) -> None:
        output = create_graph_node(
            NodeType.OUTPUT_RGBA,
            properties={"enabled": False},
        )
        requested = []
        self.panel.output_export_requested.connect(requested.append)

        self.panel.set_node(output)
        self.panel.export_output_button.click()

        self.assertEqual([output], requested)
        self.assertFalse(self.panel.export_output_button.isHidden())

        self.panel.set_node(create_graph_node(NodeType.CONSTANT_CHANNEL))
        self.assertTrue(self.panel.export_output_button.isHidden())

    def test_color_node_shows_rgba_and_resolution_controls(self) -> None:
        color = create_graph_node(
            NodeType.COLOR,
            properties={
                "red": 10,
                "green": 20,
                "blue": 30,
                "alpha": 40,
                "width": 512,
                "height": 256,
            },
        )
        changes = []
        self.panel.node_changed.connect(
            lambda _node, _title, properties, _needs_rebuild, _preview_mode: changes.append(properties)
        )

        self.panel.set_node(color)

        self.assertFalse(self.panel.color_button.isHidden())
        self.assertFalse(self.panel.color_components_host.isHidden())
        self.assertFalse(self.panel.color_resolution_host.isHidden())
        self.assertEqual("#0A141E28", self.panel.color_button.text())
        self.assertEqual(512, self.panel.color_width_spin.value())
        self.assertEqual(256, self.panel.color_height_spin.value())

        self.panel.color_red_spin.setValue(99)
        self.panel._flush_numeric_preview()

        self.assertEqual(99, changes[-1]["red"])

    def test_color_dialog_preview_is_transient_until_accepted(self) -> None:
        color = create_graph_node(
            NodeType.COLOR,
            properties={"red": 10, "green": 20, "blue": 30, "alpha": 40},
        )
        previews: list[tuple[dict[str, object], str]] = []
        self.panel.transient_preview_requested.connect(
            lambda _node, properties, mode: previews.append((properties, mode))
        )
        self.panel.set_node(color)

        self.panel._preview_selected_color(QColor(100, 110, 120, 130))

        self.assertEqual(10, color.properties["red"])
        self.assertEqual(100, self.panel.color_red_spin.value())
        self.assertEqual(130, self.panel.color_alpha_spin.value())
        self.assertEqual(100, previews[-1][0]["red"])
        self.assertEqual(130, previews[-1][0]["alpha"])
        self.assertEqual(PREVIEW_MODE_DRAFT, previews[-1][1])

    def test_color_node_has_rgba_outputs_and_defaults(self) -> None:
        color = create_graph_node(NodeType.COLOR)

        self.assertEqual(
            ["image", "r", "g", "b", "a"],
            [socket.socket_id for socket in socket_definitions(color.node_type)],
        )
        self.assertEqual(255, color.properties["red"])
        self.assertEqual(1024, color.properties["width"])


class ChannelPackingPlanTests(unittest.TestCase):
    def test_raf_roughness_groups_with_metallic_for_unity_urp(self) -> None:
        root = Path("E:/textures/barrels_iron")
        metallic_path = root / "barrels_iron_met.png"
        roughness_path = root / "barrels_iron_raf.png"
        sources = [
            BatchSource(
                metallic_path,
                root,
                detect_texture_map_type(metallic_path),
            ),
            BatchSource(
                roughness_path,
                root,
                detect_texture_map_type(roughness_path),
            ),
        ]
        options = ConversionOptions(
            packing=ChannelPackingOptions(
                enabled=True,
                layout=ChannelPackLayout.UNITY_URP,
            )
        )

        jobs = build_channel_pack_jobs(sources, None, options)

        self.assertEqual(1, len(jobs))
        self.assertEqual("barrels_iron", jobs[0].group_name)
        self.assertTrue(jobs[0].is_ready)
        self.assertEqual("barrels_iron_metallicsmoothness.png", jobs[0].output_path.name)

    def test_packing_summary_lists_output_mapping_and_missing_maps(self) -> None:
        options = ConversionOptions(
            naming=NamingRules(lowercase=True),
            packing=ChannelPackingOptions(
                enabled=True,
                layout=ChannelPackLayout.UNITY_HDRP,
            ),
        )
        sources = [
            BatchSource(Path("D:/textures/Props/Sword/sword_metallic.png"), Path("D:/textures"), TextureMapType.METALLIC),
            BatchSource(Path("D:/textures/Props/Sword/sword_roughness.png"), Path("D:/textures"), TextureMapType.ROUGHNESS),
            BatchSource(Path("D:/textures/Props/Shield/shield_metallic.png"), Path("D:/textures"), TextureMapType.METALLIC),
            BatchSource(Path("D:/textures/Props/Shield/shield_ao.png"), Path("D:/textures"), TextureMapType.AO),
            BatchSource(Path("D:/textures/Props/Shield/shield_smoothness.png"), Path("D:/textures"), TextureMapType.SMOOTHNESS),
        ]

        jobs = build_channel_pack_jobs(sources, None, options)
        summary = summarize_channel_pack_jobs(jobs)

        self.assertIn("Packing plan (Unity HDRP): ready 1, incomplete 1.", summary)
        self.assertIn("- Props/Sword/sword -> Props/Sword/sword_maskmap.png (missing: AO)", summary)
        self.assertIn("R=sword_metallic.png", summary)
        self.assertIn("A=Invert(sword_roughness.png)", summary)
        self.assertIn("B=Detail Mask(1)", summary)
        self.assertIn("- Props/Shield/shield -> Props/Shield/shield_maskmap.png", summary)
        self.assertIn("G=shield_ao.png", summary)
        self.assertIn("A=shield_smoothness.png", summary)

    def test_packing_summary_truncates_long_job_list(self) -> None:
        options = ConversionOptions(
            packing=ChannelPackingOptions(
                enabled=True,
                layout=ChannelPackLayout.ORM,
            ),
        )
        sources = []
        for index in range(5):
            base = f"crate_{index:02d}"
            root = Path("D:/library")
            sources.extend(
                [
                    BatchSource(root / f"{base}_ao.png", root, TextureMapType.AO),
                    BatchSource(root / f"{base}_roughness.png", root, TextureMapType.ROUGHNESS),
                    BatchSource(root / f"{base}_metallic.png", root, TextureMapType.METALLIC),
                ]
            )

        jobs = build_channel_pack_jobs(sources, None, options)
        summary = summarize_channel_pack_jobs(jobs)

        self.assertIn("Packing plan (ORM): ready 5, incomplete 0.", summary)
        self.assertIn("... and 1 more set(s).", summary)

    def test_pack_only_mode_creates_packed_texture_without_regular_png_exports(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "textures"
            output_root = Path(tmp) / "out"
            root.mkdir()
            Image.new("L", (8, 8), 64).save(root / "sword_ao.png")
            Image.new("L", (8, 8), 128).save(root / "sword_roughness.png")
            Image.new("L", (8, 8), 196).save(root / "sword_metallic.png")

            request = BatchRequest(
                input_path=root,
                output_root=output_root,
                options=ConversionOptions(
                    packing=ChannelPackingOptions(
                        enabled=True,
                        layout=ChannelPackLayout.ORM,
                        mode=ChannelPackingMode.PACK_ONLY,
                    ),
                ),
            )

            summary = BatchConversionService().run(request)

            self.assertTrue((output_root / "sword_orm.png").exists())
            self.assertFalse((output_root / "sword_ao.png").exists())
            self.assertFalse((output_root / "sword_roughness.png").exists())
            self.assertFalse((output_root / "sword_metallic.png").exists())
            self.assertEqual(0, summary.total)
            self.assertEqual(1, summary.packed_created)
            self.assertIn("packed only", summary.as_text())

    def test_system_presets_include_clear_packing_workflows(self) -> None:
        preset_names = {preset.name: preset for preset in SYSTEM_PRESETS}

        self.assertIn("Unreal ORM Pack", preset_names)
        self.assertIn("Traditional / Non-Packed Workflow", preset_names)
        self.assertIn("Unity URP Pack", preset_names)
        self.assertIn("Unity HDRP Mask Map", preset_names)
        self.assertIn("Fast Preview 1K", preset_names)
        self.assertIn("Portfolio PNG 2K", preset_names)
        self.assertIn("Mask Authoring", preset_names)
        self.assertIn("Mobile Texture 1K", preset_names)
        self.assertIn("Substance Export Cleanup", preset_names)
        self.assertEqual(
            ChannelPackingMode.PACK_WITH_REMAINDER,
            preset_names["Unreal ORM Pack"].options.packing.mode,
        )
        self.assertEqual(
            ChannelPackLayout.ORM,
            preset_names["Unreal ORM Pack"].options.packing.layout,
        )
        self.assertTrue(
            preset_names["Traditional / Non-Packed Workflow"].options.unpack_packed
        )
        self.assertFalse(
            preset_names["Traditional / Non-Packed Workflow"].options.packing.enabled
        )
        self.assertIn(
            "длинная сторона больше 4096 px",
            preset_names["Game Texture 4K"].description,
        )

    def test_pack_with_remainder_mode_skips_duplicate_channel_exports(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "src"
            root.mkdir()
            output_root = Path(tmp) / "out"
            for name in ("key_dif.png", "key_nml.png", "key_ao.png", "key_rgh.png", "key_met.png"):
                Image.new("RGB", (4, 4), (128, 128, 128)).save(root / name)

            request = BatchRequest(
                input_path=None,
                output_root=output_root,
                sources=(
                    BatchSource(root / "key_dif.png", root, TextureMapType.BASECOLOR),
                    BatchSource(root / "key_nml.png", root, TextureMapType.NORMAL),
                    BatchSource(root / "key_ao.png", root, TextureMapType.AO),
                    BatchSource(root / "key_rgh.png", root, TextureMapType.ROUGHNESS),
                    BatchSource(root / "key_met.png", root, TextureMapType.METALLIC),
                ),
                options=ConversionOptions(
                    packing=ChannelPackingOptions(
                        enabled=True,
                        layout=ChannelPackLayout.ORM,
                        mode=ChannelPackingMode.PACK_WITH_REMAINDER,
                    ),
                ),
            )

            summary = BatchConversionService().run(request)

            self.assertTrue((output_root / "key_dif.png").exists())
            self.assertTrue((output_root / "key_nml.png").exists())
            self.assertTrue((output_root / "key_orm.png").exists())
            self.assertFalse((output_root / "key_ao.png").exists())
            self.assertFalse((output_root / "key_rgh.png").exists())
            self.assertFalse((output_root / "key_met.png").exists())
            self.assertEqual(2, summary.total)
            self.assertEqual(2, summary.succeeded)
            self.assertEqual(1, summary.packed_created)


class GraphEditorFoundationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = _app()
        self.workspace = GraphWorkspace()

    def tearDown(self) -> None:
        self.workspace.shutdown_background_jobs(wait_ms=100)
        self.workspace.setParent(None)
        self.workspace.deleteLater()
        self.app.processEvents()
        del self.workspace
        gc.collect()

    def test_save_dialog_creates_visible_texturegraph_file(self) -> None:
        with TemporaryDirectory() as tmp:
            project_path = Path(tmp) / "barrels_iron.texturegraph"
            with mock.patch(
                "image_converter.ui.node_editor.QFileDialog.getSaveFileName",
                return_value=(str(project_path), GRAPH_PROJECT_FILE_FILTER),
            ):
                self.workspace.save_project_dialog()

            self.assertTrue(project_path.is_file())
            self.assertEqual(project_path, self.workspace.project_path)
            self.assertEqual(project_path.parent, self.workspace.project_dir)
            payload = json.loads(project_path.read_text(encoding="utf-8"))
            self.assertEqual("barrels_iron", payload["name"])

    def test_save_reuses_current_path_and_save_as_always_opens_dialog(self) -> None:
        with TemporaryDirectory() as tmp:
            first_path = Path(tmp) / "first.texturegraph"
            second_path = Path(tmp) / "second.texturegraph"
            self.assertTrue(self.workspace.save_project(first_path))

            with mock.patch(
                "image_converter.ui.node_editor.QFileDialog.getSaveFileName"
            ) as save_dialog:
                self.assertTrue(self.workspace.save_project_dialog())
                save_dialog.assert_not_called()

            with mock.patch(
                "image_converter.ui.node_editor.QFileDialog.getSaveFileName",
                return_value=(str(second_path), GRAPH_PROJECT_FILE_FILTER),
            ) as save_as_dialog:
                self.assertTrue(self.workspace.save_project_as_dialog())
                save_as_dialog.assert_called_once()

            self.assertTrue(second_path.is_file())
            self.assertEqual(second_path, self.workspace.project_path)

    def test_load_dialog_opens_texturegraph_file(self) -> None:
        with TemporaryDirectory() as tmp:
            project_path = Path(tmp) / "loaded_material.texturegraph"
            NodeGraphProjectRepository().save(
                NodeGraphProject(name="Loaded Material"),
                project_path,
            )
            with mock.patch(
                "image_converter.ui.node_editor.QFileDialog.getOpenFileName",
                return_value=(str(project_path), GRAPH_PROJECT_FILE_FILTER),
            ):
                self.workspace.load_project_dialog()

            self.assertEqual(project_path, self.workspace.project_path)
            self.assertEqual("Loaded Material", self.workspace.project.name)

    def test_y_knife_mode_uses_custom_blade_cursor(self) -> None:
        self.assertIsNone(QApplication.overrideCursor())
        QTest.keyPress(self.workspace.view, Qt.Key.Key_Y)
        try:
            cursor = QApplication.overrideCursor()
            self.assertTrue(self.workspace.view._knife_active)
            self.assertTrue(self.workspace.view._knife_cursor_override_active)
            self.assertIsNotNone(cursor)
            assert cursor is not None
            self.assertFalse(cursor.pixmap().isNull())
            self.assertEqual(QPoint(30, 2), cursor.hotSpot())
        finally:
            QTest.keyRelease(self.workspace.view, Qt.Key.Key_Y)

        self.assertFalse(self.workspace.view._knife_active)
        self.assertFalse(self.workspace.view._knife_cursor_override_active)
        self.assertIsNone(QApplication.overrideCursor())

    def test_disconnect_shake_requires_fast_horizontal_reversals(self) -> None:
        shake = [
            (0.00, 100.0, 100.0),
            (0.12, 145.0, 102.0),
            (0.24, 95.0, 99.0),
            (0.36, 150.0, 101.0),
        ]
        slow_shake = [
            (0.0, 100.0, 100.0),
            (0.5, 145.0, 100.0),
            (1.0, 95.0, 100.0),
            (1.5, 150.0, 100.0),
        ]
        vertical_motion = [
            (0.00, 100.0, 100.0),
            (0.12, 145.0, 180.0),
            (0.24, 95.0, 260.0),
            (0.36, 150.0, 340.0),
        ]

        self.assertTrue(is_disconnect_shake(shake))
        self.assertFalse(is_disconnect_shake(slow_shake))
        self.assertFalse(is_disconnect_shake(vertical_motion))

    def test_shake_disconnect_moves_node_and_is_one_undoable_action(self) -> None:
        source = create_graph_node(NodeType.COLOR, position=(0.0, 0.0))
        adjust = create_graph_node(NodeType.COLOR_ADJUST, position=(240.0, 0.0))
        output = create_graph_node(NodeType.OUTPUT_RGBA, position=(480.0, 0.0))
        connections = [
            GraphConnection("in", source.node_id, "image", adjust.node_id, "image"),
            GraphConnection("out", adjust.node_id, "out", output.node_id, "image"),
        ]
        self.workspace.project.graph.nodes = [source, adjust, output]
        self.workspace.project.graph.connections = connections
        self.workspace._scene.rebuild()

        self.workspace._on_node_shake_disconnect_requested(
            adjust,
            {adjust.node_id: (240.0, 0.0)},
            {adjust.node_id: (275.0, 12.0)},
        )

        self.assertEqual(1, len(self.workspace.project.graph.connections))
        bypass = self.workspace.project.graph.connections[0]
        self.assertEqual(source.node_id, bypass.source_node_id)
        self.assertEqual("image", bypass.source_socket_id)
        self.assertEqual(output.node_id, bypass.target_node_id)
        self.assertEqual("image", bypass.target_socket_id)
        self.assertEqual((275.0, 12.0), adjust.position)
        self.assertEqual("Shake bypass node", self.workspace.undo_stack.undoText())

        self.workspace.undo_stack.undo()
        self.assertEqual((240.0, 0.0), adjust.position)
        self.assertEqual({"in", "out"}, {item.connection_id for item in self.workspace.project.graph.connections})

    def test_shake_release_disconnects_through_scene_signal(self) -> None:
        source = create_graph_node(NodeType.COLOR, position=(0.0, 0.0))
        adjust = create_graph_node(NodeType.COLOR_ADJUST, position=(240.0, 0.0))
        output = create_graph_node(NodeType.OUTPUT_RGBA, position=(480.0, 0.0))
        input_connection = GraphConnection(
            "in",
            source.node_id,
            "image",
            adjust.node_id,
            "image",
        )
        output_connection = GraphConnection(
            "out",
            adjust.node_id,
            "out",
            output.node_id,
            "image",
        )
        self.workspace.project.graph.nodes = [source, adjust, output]
        self.workspace.project.graph.connections = [
            input_connection,
            output_connection,
        ]
        self.workspace._scene.rebuild()
        node_item = self.workspace._scene.node_items[adjust.node_id]

        self.workspace._scene.begin_node_move(adjust.node_id)
        node_item.setPos(280.0, 10.0)
        self.assertTrue(
            self.workspace._scene.mark_node_shake_disconnect(adjust.node_id)
        )
        self.assertFalse(
            self.workspace._scene.connection_items[
                input_connection.connection_id
            ].isVisible()
        )
        self.workspace._scene.finish_node_move()
        self.app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 100)

        self.assertEqual(1, len(self.workspace.project.graph.connections))
        bypass = self.workspace.project.graph.connections[0]
        self.assertEqual(source.node_id, bypass.source_node_id)
        self.assertEqual(output.node_id, bypass.target_node_id)
        self.assertEqual((280.0, 10.0), adjust.position)
        self.assertEqual(
            "Shake bypass node",
            self.workspace.undo_stack.undoText(),
        )

    def test_shake_bypass_restores_primary_path_to_every_consumer(self) -> None:
        primary = create_graph_node(NodeType.COLOR)
        secondary = create_graph_node(NodeType.COLOR)
        mix = create_graph_node(NodeType.MIX_IMAGE)
        first_output = create_graph_node(NodeType.OUTPUT_RGBA)
        second_output = create_graph_node(NodeType.OUTPUT_RGBA)
        connections = [
            GraphConnection("a", primary.node_id, "image", mix.node_id, "a"),
            GraphConnection("b", secondary.node_id, "image", mix.node_id, "b"),
            GraphConnection(
                "first",
                mix.node_id,
                "image",
                first_output.node_id,
                "image",
            ),
            GraphConnection(
                "second",
                mix.node_id,
                "image",
                second_output.node_id,
                "image",
            ),
        ]
        self.workspace.project.graph.nodes = [
            primary,
            secondary,
            mix,
            first_output,
            second_output,
        ]
        self.workspace.project.graph.connections = connections
        self.workspace._scene.rebuild()

        self.workspace._on_node_shake_disconnect_requested(
            mix,
            {mix.node_id: mix.position},
            {mix.node_id: (320.0, 80.0)},
        )

        bypass_connections = self.workspace.project.graph.connections
        self.assertEqual(2, len(bypass_connections))
        self.assertEqual(
            {first_output.node_id, second_output.node_id},
            {connection.target_node_id for connection in bypass_connections},
        )
        self.assertEqual(
            {primary.node_id},
            {connection.source_node_id for connection in bypass_connections},
        )
        self.assertNotIn(
            secondary.node_id,
            {connection.source_node_id for connection in bypass_connections},
        )

        self.workspace.undo_stack.undo()
        self.assertEqual(
            {"a", "b", "first", "second"},
            {
                connection.connection_id
                for connection in self.workspace.project.graph.connections
            },
        )

    def test_drop_existing_node_on_wire_inserts_and_undoes_as_one_action(self) -> None:
        source = create_graph_node(NodeType.COLOR, position=(0.0, 0.0))
        adjust = create_graph_node(NodeType.COLOR_ADJUST, position=(200.0, 160.0))
        output = create_graph_node(NodeType.OUTPUT_RGBA, position=(500.0, 0.0))
        original = GraphConnection(
            "original",
            source.node_id,
            "image",
            output.node_id,
            "image",
        )
        self.workspace.project.graph.nodes = [source, adjust, output]
        self.workspace.project.graph.connections = [original]
        self.workspace._scene.rebuild()

        self.workspace._on_node_wire_insert_requested(
            adjust,
            original,
            {adjust.node_id: (200.0, 160.0)},
            {adjust.node_id: (260.0, 0.0)},
        )

        self.assertEqual((260.0, 0.0), adjust.position)
        self.assertEqual(2, len(self.workspace.project.graph.connections))
        self.assertNotIn(
            "original",
            {item.connection_id for item in self.workspace.project.graph.connections},
        )
        self.assertTrue(
            any(
                item.source_node_id == source.node_id
                and item.target_node_id == adjust.node_id
                and item.target_socket_id == "image"
                for item in self.workspace.project.graph.connections
            )
        )
        self.assertTrue(
            any(
                item.source_node_id == adjust.node_id
                and item.source_socket_id == "out"
                and item.target_node_id == output.node_id
                for item in self.workspace.project.graph.connections
            )
        )
        self.assertEqual("Insert existing node in wire", self.workspace.undo_stack.undoText())

        self.workspace.undo_stack.undo()
        self.assertEqual((200.0, 160.0), adjust.position)
        self.assertEqual(["original"], [item.connection_id for item in self.workspace.project.graph.connections])

    def test_drop_on_wire_refuses_a_cycle_and_keeps_the_move(self) -> None:
        source = create_graph_node(NodeType.COLOR_ADJUST, position=(0.0, 0.0))
        adjust = create_graph_node(NodeType.COLOR_ADJUST, position=(200.0, 160.0))
        output = create_graph_node(NodeType.OUTPUT_RGBA, position=(500.0, 0.0))
        original = GraphConnection(
            "original",
            source.node_id,
            "out",
            output.node_id,
            "image",
        )
        back_edge = GraphConnection(
            "back",
            adjust.node_id,
            "out",
            source.node_id,
            "image",
        )
        self.workspace.project.graph.nodes = [source, adjust, output]
        self.workspace.project.graph.connections = [original, back_edge]
        self.workspace._scene.rebuild()

        self.workspace._on_node_wire_insert_requested(
            adjust,
            original,
            {adjust.node_id: (200.0, 160.0)},
            {adjust.node_id: (260.0, 0.0)},
        )

        self.assertEqual({"original", "back"}, {item.connection_id for item in self.workspace.project.graph.connections})
        self.assertEqual((260.0, 0.0), adjust.position)
        self.assertEqual("Move nodes", self.workspace.undo_stack.undoText())

    def test_compatible_wire_highlights_when_node_overlaps_it(self) -> None:
        source = create_graph_node(NodeType.COLOR, position=(0.0, 0.0))
        adjust = create_graph_node(NodeType.COLOR_ADJUST, position=(200.0, 200.0))
        output = create_graph_node(NodeType.OUTPUT_RGBA, position=(600.0, 0.0))
        connection = GraphConnection(
            "wire",
            source.node_id,
            "image",
            output.node_id,
            "image",
        )
        self.workspace.project.graph.nodes = [source, adjust, output]
        self.workspace.project.graph.connections = [connection]
        self.workspace._scene.rebuild()
        node_item = self.workspace._scene.node_items[adjust.node_id]
        wire_item = self.workspace._scene.connection_items[connection.connection_id]
        midpoint = wire_item.path().pointAtPercent(0.5)
        node_item.setPos(midpoint - node_item.rect().center())

        self.workspace._scene.begin_node_move(adjust.node_id)
        self.workspace._scene.update_node_drop_target(adjust.node_id)

        self.assertEqual("wire", self.workspace._scene.drop_target_connection_id)
        self.assertTrue(wire_item._drop_target)

    def test_drop_release_rewires_through_scene_signal(self) -> None:
        source = create_graph_node(NodeType.COLOR, position=(0.0, 0.0))
        adjust = create_graph_node(NodeType.COLOR_ADJUST, position=(200.0, 180.0))
        output = create_graph_node(NodeType.OUTPUT_RGBA, position=(600.0, 0.0))
        original = GraphConnection(
            "wire",
            source.node_id,
            "image",
            output.node_id,
            "image",
        )
        self.workspace.project.graph.nodes = [source, adjust, output]
        self.workspace.project.graph.connections = [original]
        self.workspace._scene.rebuild()
        node_item = self.workspace._scene.node_items[adjust.node_id]
        wire_item = self.workspace._scene.connection_items[original.connection_id]
        midpoint = wire_item.path().pointAtPercent(0.5)

        self.workspace._scene.begin_node_move(adjust.node_id)
        node_item.setPos(midpoint - node_item.rect().center())
        self.workspace._scene.update_node_drop_target(adjust.node_id)
        self.workspace._scene.finish_node_move()
        self.app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 100)

        self.assertEqual(2, len(self.workspace.project.graph.connections))
        self.assertTrue(
            any(
                item.source_node_id == source.node_id
                and item.target_node_id == adjust.node_id
                for item in self.workspace.project.graph.connections
            )
        )
        self.assertTrue(
            any(
                item.source_node_id == adjust.node_id
                and item.target_node_id == output.node_id
                for item in self.workspace.project.graph.connections
            )
        )
        self.assertEqual(
            "Insert existing node in wire",
            self.workspace.undo_stack.undoText(),
        )

    def test_disconnecting_shader_input_preserves_upstream_preview_cache(self) -> None:
        texture = create_graph_node(NodeType.TEXTURE_INPUT)
        shader = create_graph_node(NodeType.PBR_SHADER)
        connection = GraphConnection(
            make_connection_id(),
            texture.node_id,
            "image",
            shader.node_id,
            "basecolor",
        )
        self.workspace._push_graph_command(
            AddNodesCommand(
                self.workspace.project.graph,
                self.workspace._on_graph_command_changed,
                [texture, shader],
                [connection],
            )
        )
        cache_key = (texture.node_id, (8, 8))
        self.workspace._preview_cache.put_image(
            "texture-preview",
            cache_key,
            Image.new("RGBA", (8, 8), (32, 64, 96, 255)),
        )

        with mock.patch.object(
            self.workspace._preview_cache,
            "clear",
            wraps=self.workspace._preview_cache.clear,
        ) as clear_cache:
            self.workspace._on_connection_delete_requested(connection)

        self.assertEqual(0, clear_cache.call_count)
        self.assertIsNotNone(
            self.workspace._preview_cache.get_image("texture-preview", cache_key)
        )
        self.assertEqual([], self.workspace.project.graph.connections)

    def test_move_undo_redo_keeps_scene_items_without_rebuild(self) -> None:
        node = create_graph_node(NodeType.CONSTANT_CHANNEL, position=(10.0, 20.0))
        self.workspace._push_graph_command(
            AddNodesCommand(
                self.workspace.project.graph,
                self.workspace._on_graph_command_changed,
                [node],
            ),
            select_node_ids=[node.node_id],
        )
        original_item = self.workspace._scene.node_items[node.node_id]

        with mock.patch.object(self.workspace._scene, "rebuild", wraps=self.workspace._scene.rebuild) as rebuild:
            self.workspace._push_graph_command(
                MoveNodesCommand(
                    self.workspace.project.graph,
                    self.workspace._on_graph_command_changed,
                    {node.node_id: (10.0, 20.0)},
                    {node.node_id: (120.0, 80.0)},
                ),
                select_node_ids=[node.node_id],
            )
            self.workspace.undo_stack.undo()
            self.workspace.undo_stack.redo()

        self.assertEqual(0, rebuild.call_count)
        self.assertIs(original_item, self.workspace._scene.node_items[node.node_id])
        self.assertEqual((120.0, 80.0), node.position)
        self.assertTrue(original_item.isSelected())

    def test_main_window_uses_standard_file_menu_for_graph_projects(self) -> None:
        window = MainWindow()
        try:
            self.assertEqual("Файл", window.file_menu.title())
            self.assertEqual("Ctrl+N", window.new_graph_action.shortcut().toString())
            self.assertEqual("Ctrl+O", window.open_graph_action.shortcut().toString())
            self.assertEqual("Ctrl+S", window.save_graph_action.shortcut().toString())
            self.assertEqual(
                "Ctrl+Shift+S",
                window.save_graph_as_action.shortcut().toString(),
            )
            self.assertFalse(hasattr(window.graph_workspace, "save_button"))
            self.assertFalse(hasattr(window.graph_workspace, "recent_button"))
        finally:
            window.graph_workspace.shutdown_background_jobs(wait_ms=100)
            window.deleteLater()
            self.app.processEvents()

    def test_every_node_has_help_without_header_or_body_overlaps(self) -> None:
        for node_type in NodeType:
            with self.subTest(node_type=node_type):
                self.assertIsNotNone(node_help_content(node_type))
                item = GraphNodeItem(
                    create_graph_node(node_type),
                    self.workspace._scene.texture_visual_cache,
                )
                self.assertIsNotNone(item.help_button_item)
                self.assertIsNotNone(item.help_button_label)

                help_rect = item._help_button_rect()
                self.assertFalse(help_rect.intersects(item._display_flag_rect()))
                if item.enable_flag_item is not None:
                    self.assertFalse(help_rect.intersects(item._enable_flag_rect()))
                if item.reset_button_item is not None:
                    self.assertFalse(help_rect.intersects(item._reset_button_rect()))
                if item.render_flag_item is not None:
                    self.assertFalse(help_rect.intersects(item._render_flag_rect()))

                assert item.title_item is not None
                title_rect = item.title_item.mapRectToParent(item.title_item.boundingRect())
                self.assertLessEqual(title_rect.right() + 4, help_rect.left())

                for port in item.port_items.values():
                    port_rect = port.mapRectToParent(port.boundingRect())
                    self.assertGreaterEqual(port_rect.top(), item.rect().top())
                    self.assertLessEqual(port_rect.bottom(), item.rect().bottom())
                for label in item.port_label_items.values():
                    label_rect = label.mapRectToParent(label.boundingRect())
                    self.assertGreaterEqual(label_rect.top(), item.rect().top())
                    self.assertLessEqual(label_rect.bottom(), item.rect().bottom())

    def test_selected_connection_uses_highlight_pen_without_selection_frame(self) -> None:
        source_node = create_graph_node(NodeType.CONSTANT_CHANNEL)
        target_node = create_graph_node(NodeType.INVERT_CHANNEL)
        source_item = GraphNodeItem(source_node)
        target_item = GraphNodeItem(target_node)
        connection = GraphConnection(
            make_connection_id(),
            source_node.node_id,
            "out",
            target_node.node_id,
            "in",
        )
        item = ConnectionItem(
            connection,
            source_item.port_items["out"],
            target_item.port_items["in"],
        )

        normal_pen = item._display_pen()
        item.setSelected(True)
        selected_pen = item._display_pen()

        self.assertEqual("#5ba7ff", normal_pen.color().name())
        self.assertEqual("#ffd166", selected_pen.color().name())
        self.assertGreater(selected_pen.widthF(), normal_pen.widthF())
        self.assertEqual(Qt.PenStyle.SolidLine, selected_pen.style())

    def test_node_validation_outline_tracks_highest_issue_severity(self) -> None:
        node = create_graph_node(NodeType.TEXTURE_INPUT)
        item = GraphNodeItem(node, self.workspace._scene.texture_visual_cache)
        warning = GraphValidationIssue(
            GraphValidationSeverity.WARNING,
            "Проверьте Roughness/Glossiness.",
            node.node_id,
            "r",
        )
        error = GraphValidationIssue(
            GraphValidationSeverity.ERROR,
            "Texture не найдена.",
            node.node_id,
            "path",
        )

        item.set_validation_issues((warning,))
        self.assertIs(GraphValidationSeverity.WARNING, item._validation_severity)
        self.assertIn("Roughness/Glossiness", item.toolTip())
        self.assertEqual("#f2b84b", item._border_pen().color().name())

        item.set_validation_issues((warning, error))
        self.assertIs(GraphValidationSeverity.ERROR, item._validation_severity)
        self.assertEqual("#ff6b6b", item._border_pen().color().name())

        item.set_validation_issues(())
        self.assertIsNone(item._validation_severity)
        self.assertEqual("", item.toolTip())

    def test_mix_image_help_popup_can_pin_and_auto_close(self) -> None:
        mix = create_graph_node(NodeType.MIX_IMAGE)
        self.workspace._scene.add_node(mix)
        self.workspace.resize(900, 640)
        self.workspace.show()
        self.app.processEvents()

        node_item = self.workspace._scene.node_items[mix.node_id]
        help_center = node_item.mapToScene(node_item._help_button_rect().center())
        click_position = self.workspace.view.mapFromScene(help_center)
        self.assertTrue(self.workspace.view.viewport().rect().contains(click_position))

        QTest.mouseClick(
            self.workspace.view.viewport(),
            Qt.MouseButton.LeftButton,
            pos=click_position,
        )
        self.app.processEvents()

        popup = self.workspace.view.node_help_popup
        self.assertIsNotNone(popup)
        assert popup is not None
        self.assertTrue(popup.isVisible())
        self.assertEqual("Mix Image", popup.title_label.text())
        self.assertIn("два RGBA-изображения", popup.summary_label.text())
        self.assertIn("заменяет Factor", popup.detail_labels["Mask"].text())
        self.assertGreaterEqual(popup.summary_label.font().pixelSize(), 14)
        self.assertGreaterEqual(popup.detail_labels["Mask"].font().pixelSize(), 13)

        popup.pin_button.setChecked(True)
        self.app.processEvents()
        self.assertTrue(popup.windowFlags() & Qt.WindowType.WindowTitleHint)
        self.assertFalse(popup.windowFlags() & Qt.WindowType.FramelessWindowHint)
        self.assertFalse(popup.close_button.isVisible())

        target_position = popup.pos() + QPoint(30, 24)
        popup.move(target_position)
        popup.resize(620, 500)
        self.app.processEvents()
        self.assertLessEqual((target_position - popup.pos()).manhattanLength(), 4)
        self.assertEqual(620, popup.width())
        self.assertEqual(500, popup.height())

        popup._arm_auto_close()
        popup._check_auto_close(QPoint(-10000, -10000))
        self.assertTrue(popup.isVisible())
        self.assertTrue(popup.is_pinned)

        popup.pin_button.setChecked(False)
        self.app.processEvents()
        self.assertTrue(popup.windowFlags() & Qt.WindowType.FramelessWindowHint)
        self.assertTrue(popup.close_button.isVisible())
        popup._check_auto_close(QPoint(-10000, -10000))
        self.assertFalse(popup.isVisible())

    def test_graph_export_worker_keeps_gui_event_loop_responsive(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            texture_path = root / "source.png"
            Image.new("RGB", (1024, 1024), (24, 96, 180)).save(texture_path)
            texture = create_graph_node(NodeType.TEXTURE_INPUT, properties={"path": str(texture_path)})
            output = create_graph_node(NodeType.OUTPUT_RGBA, properties={"filename": "result.png"})
            project = NodeGraphProject(
                graph=NodeGraph(
                    nodes=[texture, output],
                    connections=[
                        GraphConnection(make_connection_id(), texture.node_id, channel, output.node_id, channel)
                        for channel in ("r", "g", "b")
                    ],
                )
            )
            window = MainWindow()
            loop = QEventLoop()
            heartbeats: list[int] = []
            heartbeat = QTimer()
            heartbeat.setInterval(1)
            heartbeat.timeout.connect(lambda: heartbeats.append(1))
            poll = QTimer()
            poll.setInterval(5)
            poll.timeout.connect(
                lambda: loop.quit() if not window._graph_export_controller.is_running else None
            )
            try:
                heartbeat.start()
                poll.start()
                window._graph_export_controller.submit(
                    GraphExportRequest(
                        project=project,
                        output_root=root / "exports",
                        options=ConversionOptions(overwrite=True),
                        show_dialogs=False,
                    )
                )
                QTimer.singleShot(5000, loop.quit)
                loop.exec()
                self.app.processEvents()

                self.assertFalse(window._graph_export_controller.is_running)
                self.assertTrue((root / "exports" / "result.png").exists())
                self.assertGreater(len(heartbeats), 0)
            finally:
                heartbeat.stop()
                poll.stop()
                window.close()
                window.deleteLater()
                self.app.processEvents()

    def test_latest_levels_edit_does_not_reuse_stale_preview_cache(self) -> None:
        with TemporaryDirectory() as tmp:
            texture_path = Path(tmp) / "source.png"
            Image.new("RGB", (8, 8), (128, 128, 128)).save(texture_path)
            texture = create_graph_node(NodeType.TEXTURE_INPUT, properties={"path": str(texture_path)})
            levels = create_graph_node(
                NodeType.LEVELS_CHANNEL,
                properties={
                    "display": True,
                    "black": 0,
                    "white": 255,
                    "gamma": 1.0,
                    "out_min": 0,
                    "out_max": 255,
                },
            )
            connection = GraphConnection(
                make_connection_id(),
                texture.node_id,
                "r",
                levels.node_id,
                "in",
            )
            self.workspace._push_graph_command(
                AddNodesCommand(
                    self.workspace.project.graph,
                    self.workspace._on_graph_command_changed,
                    [texture, levels],
                    [connection],
                ),
                select_node_ids=[levels.node_id],
            )

            latest_images: list[Image.Image] = []
            loop = QEventLoop()
            self.workspace.preview_image_requested.connect(
                lambda image, _title, _meta, _node_id: (latest_images.append(image), loop.quit())
            )
            first = dict(levels.properties)
            first["black"] = 32
            self.workspace._on_node_properties_changed(
                levels,
                levels.title,
                first,
                False,
                PREVIEW_MODE_DRAFT,
            )
            latest = dict(first)
            latest["black"] = 200
            self.workspace._on_node_properties_changed(
                levels,
                levels.title,
                latest,
                False,
                PREVIEW_MODE_DRAFT,
            )

            QTimer.singleShot(5000, loop.quit)
            loop.exec()

            self.assertTrue(latest_images)
            self.assertEqual((0, 0, 0, 255), latest_images[-1].getpixel((0, 0)))

    def test_levels_edit_refreshes_downstream_apply_mask_output_preview(self) -> None:
        with TemporaryDirectory() as tmp:
            texture_path = Path(tmp) / "mask.png"
            Image.new("L", (8, 8), 128).save(texture_path)
            texture = create_graph_node(
                NodeType.TEXTURE_INPUT,
                properties={"path": str(texture_path)},
            )
            levels = create_graph_node(NodeType.LEVELS_CHANNEL)
            color = create_graph_node(
                NodeType.COLOR,
                properties={
                    "red": 100,
                    "green": 50,
                    "blue": 25,
                    "alpha": 255,
                    "width": 8,
                    "height": 8,
                },
            )
            apply_mask = create_graph_node(NodeType.SET_ALPHA)
            output = create_graph_node(
                NodeType.OUTPUT_RGBA,
                properties={"display": True, "resolution_source": "image"},
            )
            connections = [
                GraphConnection(make_connection_id(), texture.node_id, "r", levels.node_id, "in"),
                GraphConnection(make_connection_id(), levels.node_id, "out", apply_mask.node_id, "alpha"),
                GraphConnection(make_connection_id(), color.node_id, "image", apply_mask.node_id, "image"),
                GraphConnection(make_connection_id(), apply_mask.node_id, "out", output.node_id, "image"),
            ]
            latest_images: list[Image.Image] = []
            self.workspace.preview_image_requested.connect(
                lambda image, _title, _meta, _node_id: latest_images.append(image.copy())
            )
            self.workspace._push_graph_command(
                AddNodesCommand(
                    self.workspace.project.graph,
                    self.workspace._on_graph_command_changed,
                    [texture, levels, color, apply_mask, output],
                    connections,
                ),
                select_node_ids=[levels.node_id],
            )

            initial_loop = QEventLoop()
            poll_initial = QTimer()
            poll_initial.timeout.connect(
                lambda: initial_loop.quit() if latest_images else None
            )
            poll_initial.start(10)
            QTimer.singleShot(3000, initial_loop.quit)
            initial_loop.exec()
            poll_initial.stop()
            self.assertTrue(latest_images)
            self.assertEqual(128, latest_images[-1].getpixel((0, 0))[3])

            properties = dict(levels.properties)
            properties["black"] = 200
            self.workspace._on_node_properties_changed(
                levels,
                levels.title,
                properties,
                False,
                PREVIEW_MODE_FULL,
            )

            updated_loop = QEventLoop()
            poll_updated = QTimer()
            poll_updated.timeout.connect(
                lambda: updated_loop.quit()
                if latest_images and latest_images[-1].getpixel((0, 0))[3] == 0
                else None
            )
            poll_updated.start(10)
            QTimer.singleShot(3000, updated_loop.quit)
            updated_loop.exec()
            poll_updated.stop()

            self.assertEqual(0, latest_images[-1].getpixel((0, 0))[3])

    def test_workspace_keeps_slider_drag_active_across_live_edits(self) -> None:
        levels = create_graph_node(NodeType.LEVELS_CHANNEL)
        self.workspace._push_graph_command(
            AddNodesCommand(
                self.workspace.project.graph,
                self.workspace._on_graph_command_changed,
                [levels],
            ),
            select_node_ids=[levels.node_id],
        )
        panel = self.workspace.properties_panel
        changed_modes: list[str] = []
        refresh_modes: list[str] = []
        panel.node_changed.connect(
            lambda _node, _title, _properties, _rebuild, mode: changed_modes.append(mode)
        )
        panel.preview_refresh_requested.connect(
            lambda _node, mode: refresh_modes.append(mode)
        )

        panel.level_black_slider.sliderPressed.emit()
        panel.level_black_slider.setValue(20)
        self.app.processEvents()
        self.assertTrue(panel._slider_drag_active)
        panel.level_black_slider.setValue(40)
        self.app.processEvents()
        self.assertTrue(panel._slider_drag_active)
        panel.level_black_slider.sliderReleased.emit()
        self.app.processEvents()

        self.assertEqual(1, changed_modes.count(PREVIEW_MODE_DRAFT))
        self.assertEqual(PREVIEW_MODE_FULL, changed_modes[-1])
        self.assertEqual([], refresh_modes)

    def test_workspace_defers_validation_until_slider_release(self) -> None:
        levels = create_graph_node(NodeType.LEVELS_CHANNEL)
        self.workspace._push_graph_command(
            AddNodesCommand(
                self.workspace.project.graph,
                self.workspace._on_graph_command_changed,
                [levels],
            ),
            select_node_ids=[levels.node_id],
        )
        panel = self.workspace.properties_panel

        with mock.patch.object(self.workspace, "_refresh_validation") as refresh_validation:
            panel.level_black_slider.sliderPressed.emit()
            panel.level_black_slider.setValue(32)
            self.app.processEvents()
            self.assertEqual(0, refresh_validation.call_count)

            panel.level_black_slider.sliderReleased.emit()
            self.app.processEvents()
            self.assertEqual(1, refresh_validation.call_count)

    def test_undo_redo_add_and_connect(self) -> None:
        constant = create_graph_node(NodeType.CONSTANT_CHANNEL)
        view = create_graph_node(NodeType.VIEW)
        self.workspace._push_graph_command(
            AddNodesCommand(
                self.workspace.project.graph,
                self.workspace._on_graph_command_changed,
                [constant, view],
            ),
            select_node_ids=[constant.node_id],
        )
        self.assertEqual(2, len(self.workspace.project.graph.nodes))

        connection = GraphConnection(
            make_connection_id(),
            constant.node_id,
            "out",
            view.node_id,
            "in",
        )
        self.workspace._push_graph_command(
            ReplaceInputConnectionCommand(
                self.workspace.project.graph,
                self.workspace._on_graph_command_changed,
                connection,
            ),
            select_node_ids=[view.node_id],
        )
        self.assertEqual(1, len(self.workspace.project.graph.connections))

        self.workspace.undo_stack.undo()
        self.assertEqual(0, len(self.workspace.project.graph.connections))
        self.workspace.undo_stack.redo()
        self.assertEqual(1, len(self.workspace.project.graph.connections))

    def test_connecting_output_image_clears_old_channel_inputs_and_is_undoable(self) -> None:
        image = create_graph_node(NodeType.COLOR)
        red = create_graph_node(NodeType.CONSTANT_CHANNEL)
        green = create_graph_node(NodeType.CONSTANT_CHANNEL)
        blue = create_graph_node(NodeType.CONSTANT_CHANNEL)
        alpha = create_graph_node(NodeType.CONSTANT_CHANNEL)
        output = create_graph_node(NodeType.OUTPUT_RGBA)
        channel_connections = [
            GraphConnection(make_connection_id(), source.node_id, "out", output.node_id, socket_id)
            for source, socket_id in (
                (red, "r"),
                (green, "g"),
                (blue, "b"),
                (alpha, "a"),
            )
        ]
        self.workspace._push_graph_command(
            AddNodesCommand(
                self.workspace.project.graph,
                self.workspace._on_graph_command_changed,
                [image, red, green, blue, alpha, output],
                channel_connections,
            ),
        )
        image_connection = GraphConnection(
            make_connection_id(),
            image.node_id,
            "image",
            output.node_id,
            "image",
        )

        self.workspace._on_connection_requested(image_connection)

        self.assertEqual([image_connection], self.workspace.project.graph.connections)
        self.workspace.undo_stack.undo()
        self.assertEqual(
            {connection.connection_id for connection in channel_connections},
            {
                connection.connection_id
                for connection in self.workspace.project.graph.connections
            },
        )
        self.workspace.undo_stack.redo()
        self.assertEqual([image_connection], self.workspace.project.graph.connections)

        green_override = GraphConnection(
            make_connection_id(),
            green.node_id,
            "out",
            output.node_id,
            "g",
        )
        self.workspace._on_connection_requested(green_override)
        self.assertEqual(
            {image_connection.connection_id, green_override.connection_id},
            {
                connection.connection_id
                for connection in self.workspace.project.graph.connections
            },
        )

    def test_delete_processing_node_dissolves_it_and_preserves_fanout(self) -> None:
        source = create_graph_node(NodeType.CONSTANT_CHANNEL)
        levels = create_graph_node(NodeType.LEVELS_CHANNEL)
        output = create_graph_node(NodeType.OUTPUT_RGBA)
        view = create_graph_node(NodeType.VIEW)
        original_connections = [
            GraphConnection(make_connection_id(), source.node_id, "out", levels.node_id, "in"),
            GraphConnection(make_connection_id(), levels.node_id, "out", output.node_id, "r"),
            GraphConnection(make_connection_id(), levels.node_id, "out", view.node_id, "in"),
        ]
        self.workspace._push_graph_command(
            AddNodesCommand(
                self.workspace.project.graph,
                self.workspace._on_graph_command_changed,
                [source, levels, output, view],
                original_connections,
            ),
            select_node_ids=[levels.node_id],
        )

        self.workspace._on_delete_items_requested(([levels.node_id], []))

        self.assertNotIn(levels, self.workspace.project.graph.nodes)
        self.assertEqual(
            {
                (source.node_id, "out", output.node_id, "r"),
                (source.node_id, "out", view.node_id, "in"),
            },
            {
                (
                    connection.source_node_id,
                    connection.source_socket_id,
                    connection.target_node_id,
                    connection.target_socket_id,
                )
                for connection in self.workspace.project.graph.connections
            },
        )

        self.workspace.undo_stack.undo()
        self.assertIn(levels, self.workspace.project.graph.nodes)
        self.assertEqual(
            {connection.connection_id for connection in original_connections},
            {
                connection.connection_id
                for connection in self.workspace.project.graph.connections
            },
        )

        self.workspace.undo_stack.redo()
        self.assertNotIn(levels, self.workspace.project.graph.nodes)
        self.assertEqual(2, len(self.workspace.project.graph.connections))

    def test_delete_blend_node_bypasses_through_primary_a_input(self) -> None:
        source_a = create_graph_node(NodeType.CONSTANT_CHANNEL)
        source_b = create_graph_node(NodeType.CONSTANT_CHANNEL)
        blend = create_graph_node(NodeType.BLEND_CHANNEL)
        view = create_graph_node(NodeType.VIEW)
        connections = [
            GraphConnection(make_connection_id(), source_a.node_id, "out", blend.node_id, "a"),
            GraphConnection(make_connection_id(), source_b.node_id, "out", blend.node_id, "b"),
            GraphConnection(make_connection_id(), blend.node_id, "out", view.node_id, "in"),
        ]
        self.workspace._push_graph_command(
            AddNodesCommand(
                self.workspace.project.graph,
                self.workspace._on_graph_command_changed,
                [source_a, source_b, blend, view],
                connections,
            ),
            select_node_ids=[blend.node_id],
        )

        self.workspace._on_delete_items_requested(([blend.node_id], []))

        self.assertEqual(1, len(self.workspace.project.graph.connections))
        replacement = self.workspace.project.graph.connections[0]
        self.assertEqual(source_a.node_id, replacement.source_node_id)
        self.assertEqual("out", replacement.source_socket_id)
        self.assertEqual(view.node_id, replacement.target_node_id)
        self.assertEqual("in", replacement.target_socket_id)

    def test_delete_apply_mask_node_bypasses_its_image_input(self) -> None:
        image = create_graph_node(NodeType.COLOR)
        mask = create_graph_node(NodeType.CONSTANT_CHANNEL)
        apply_mask = create_graph_node(NodeType.SET_ALPHA)
        output = create_graph_node(NodeType.OUTPUT_RGBA)
        connections = [
            GraphConnection(make_connection_id(), image.node_id, "image", apply_mask.node_id, "image"),
            GraphConnection(make_connection_id(), mask.node_id, "out", apply_mask.node_id, "alpha"),
            GraphConnection(make_connection_id(), apply_mask.node_id, "out", output.node_id, "image"),
        ]
        self.workspace._push_graph_command(
            AddNodesCommand(
                self.workspace.project.graph,
                self.workspace._on_graph_command_changed,
                [image, mask, apply_mask, output],
                connections,
            ),
            select_node_ids=[apply_mask.node_id],
        )

        self.workspace._on_delete_items_requested(([apply_mask.node_id], []))

        self.assertEqual(1, len(self.workspace.project.graph.connections))
        replacement = self.workspace.project.graph.connections[0]
        self.assertEqual(image.node_id, replacement.source_node_id)
        self.assertEqual("image", replacement.source_socket_id)
        self.assertEqual(output.node_id, replacement.target_node_id)
        self.assertEqual("image", replacement.target_socket_id)

    def test_delete_split_node_does_not_create_incompatible_connection(self) -> None:
        image = create_graph_node(NodeType.COLOR)
        split = create_graph_node(NodeType.SPLIT_RGBA)
        output = create_graph_node(NodeType.OUTPUT_RGBA)
        connections = [
            GraphConnection(make_connection_id(), image.node_id, "image", split.node_id, "image"),
            GraphConnection(make_connection_id(), split.node_id, "r", output.node_id, "r"),
        ]
        self.workspace._push_graph_command(
            AddNodesCommand(
                self.workspace.project.graph,
                self.workspace._on_graph_command_changed,
                [image, split, output],
                connections,
            ),
            select_node_ids=[split.node_id],
        )

        self.workspace._on_delete_items_requested(([split.node_id], []))

        self.assertEqual([], self.workspace.project.graph.connections)

    def test_copy_paste_preserves_internal_connections(self) -> None:
        constant = create_graph_node(NodeType.CONSTANT_CHANNEL)
        view = create_graph_node(NodeType.VIEW)
        connection = GraphConnection(
            make_connection_id(),
            constant.node_id,
            "out",
            view.node_id,
            "in",
        )
        self.workspace._push_graph_command(
            AddNodesCommand(
                self.workspace.project.graph,
                self.workspace._on_graph_command_changed,
                [constant, view],
                [connection],
            ),
            select_node_ids=[constant.node_id, view.node_id],
        )
        self.workspace._copy_selection()
        self.workspace._paste_clipboard()
        self.assertEqual(4, len(self.workspace.project.graph.nodes))
        self.assertEqual(2, len(self.workspace.project.graph.connections))

    def test_auto_layout_arranges_nodes_by_flow_and_is_undoable(self) -> None:
        texture = create_graph_node(NodeType.TEXTURE_INPUT, position=(0, 0))
        levels = create_graph_node(NodeType.LEVELS_CHANNEL, position=(0, 0))
        output = create_graph_node(NodeType.OUTPUT_RGBA, position=(0, 0))
        connections = [
            GraphConnection(make_connection_id(), texture.node_id, "r", levels.node_id, "in"),
            GraphConnection(make_connection_id(), levels.node_id, "out", output.node_id, "r"),
        ]
        self.workspace._push_graph_command(
            AddNodesCommand(
                self.workspace.project.graph,
                self.workspace._on_graph_command_changed,
                [texture, levels, output],
                connections,
            ),
        )

        self.workspace.auto_layout_nodes()

        self.assertLess(texture.position[0], levels.position[0])
        self.assertLess(levels.position[0], output.position[0])
        self.workspace.undo_stack.undo()
        self.assertEqual((0, 0), texture.position)
        self.assertEqual((0, 0), levels.position)
        self.assertEqual((0, 0), output.position)

    def test_texture_node_visual_stays_compact_with_larger_thumbnail(self) -> None:
        texture = create_graph_node(
            NodeType.TEXTURE_INPUT,
            title="mushket_maskmap",
            properties={"path": "mushket_maskmap.png"},
        )
        item = GraphNodeItem(texture)

        self.assertEqual(260, item.rect().width())
        self.assertLessEqual(item.rect().height(), 150)

    def test_texture_node_refresh_updates_thumbnail_in_place(self) -> None:
        with TemporaryDirectory() as tmp:
            texture_path = Path(tmp) / "albedo.png"
            Image.new("RGBA", (8, 8), (255, 0, 0, 255)).save(texture_path)
            texture = create_graph_node(
                NodeType.TEXTURE_INPUT,
                title="albedo",
                properties={"path": str(texture_path)},
            )
            self.workspace._push_graph_command(
                AddNodesCommand(
                    self.workspace.project.graph,
                    self.workspace._on_graph_command_changed,
                    [texture],
                ),
                select_node_ids=[texture.node_id],
            )
            item = self.workspace._scene.node_items[texture.node_id]
            first_ready = QEventLoop()
            self.workspace._scene.texture_visual_cache.visual_ready.connect(
                lambda _path: first_ready.quit()
            )
            if item.thumbnail_item.pixmap().isNull():
                QTimer.singleShot(3000, first_ready.quit)
                first_ready.exec()
            thumbnail_before = item.thumbnail_item.pixmap().toImage()
            self.assertEqual(255, thumbnail_before.pixelColor(0, 0).red())

            Image.new("RGBA", (8, 8), (0, 255, 0, 255)).save(texture_path)
            second_ready = QEventLoop()
            self.workspace._scene.texture_visual_cache.visual_ready.connect(
                lambda _path: second_ready.quit()
            )
            self.workspace.refresh_asset_paths([texture_path])
            QTimer.singleShot(3000, second_ready.quit)
            second_ready.exec()

            thumbnail_after = item.thumbnail_item.pixmap().toImage()
            self.assertEqual(0, thumbnail_after.pixelColor(0, 0).red())
            self.assertEqual(255, thumbnail_after.pixelColor(0, 0).green())

    def test_texture_display_flag_renders_composite_color_preview(self) -> None:
        with TemporaryDirectory() as tmp:
            texture_path = Path(tmp) / "basecolor.png"
            Image.new("RGBA", (2, 1), (20, 80, 160, 255)).save(texture_path)
            texture = create_graph_node(
                NodeType.TEXTURE_INPUT,
                title="basecolor",
                properties={"path": str(texture_path)},
            )

            image, meta = NodeGraphExecutor().render_display_node(NodeGraph(nodes=[texture]), texture)

        self.assertEqual("RGBA", image.mode)
        self.assertEqual((20, 80, 160, 255), image.getpixel((0, 0)))
        self.assertIn("Display flag · Texture", meta)
        self.assertNotIn("· R ·", meta)

    def test_output_display_flag_can_preview_constant_only_graph(self) -> None:
        red = create_graph_node(NodeType.CONSTANT_CHANNEL, title="R", properties={"value": 100})
        green = create_graph_node(NodeType.CONSTANT_CHANNEL, title="G", properties={"value": 150})
        blue = create_graph_node(NodeType.CONSTANT_CHANNEL, title="B", properties={"value": 200})
        alpha = create_graph_node(NodeType.CONSTANT_CHANNEL, title="A", properties={"value": 255})
        output = create_graph_node(
            NodeType.OUTPUT_RGBA,
            title="Output 1",
            properties={"display": True},
        )
        graph = NodeGraph(
            nodes=[red, green, blue, alpha, output],
            connections=[
                GraphConnection(make_connection_id(), red.node_id, "out", output.node_id, "r"),
                GraphConnection(make_connection_id(), green.node_id, "out", output.node_id, "g"),
                GraphConnection(make_connection_id(), blue.node_id, "out", output.node_id, "b"),
                GraphConnection(make_connection_id(), alpha.node_id, "out", output.node_id, "a"),
            ],
        )

        image, meta = NodeGraphExecutor().render_display_node(graph, output)

        self.assertEqual((256, 256), image.size)
        self.assertEqual((100, 150, 200, 255), image.getpixel((0, 0)))
        self.assertIn("Output preview", meta)

    def test_validation_reports_missing_output_inputs(self) -> None:
        output = create_graph_node(NodeType.OUTPUT_RGBA)
        project = NodeGraphProject(graph=NodeGraph(nodes=[output]))
        issues = NodeGraphExecutor().validate_issues(project)
        messages = [issue.message for issue in issues]
        self.assertTrue(any("не подключены каналы" in message for message in messages))

    def test_project_load_applies_new_default_properties(self) -> None:
        with TemporaryDirectory() as tmp:
            bundle_dir = Path(tmp)
            payload = {
                "version": 1,
                "name": "Old Graph",
                "graph": {
                    "nodes": [
                        {
                            "id": "node_texture",
                            "type": "texture_input",
                            "title": "Texture",
                            "position": [0, 0],
                            "properties": {"path": "source.png"},
                        },
                        {
                            "id": "node_output",
                            "type": "output_rgba",
                            "title": "Output",
                            "position": [220, 0],
                            "properties": {},
                        },
                    ],
                    "connections": [],
                },
            }
            (bundle_dir / GRAPH_PROJECT_FILENAME).write_text(
                json.dumps(payload),
                encoding="utf-8",
            )
            project = NodeGraphProjectRepository().load(bundle_dir)
        texture, output = project.graph.nodes
        self.assertIn("color_space", texture.properties)
        self.assertIn("data_role", texture.properties)
        self.assertIn("profile", output.properties)
        self.assertEqual("multiply", output.properties["alpha_input_mode"])
        self.assertIn("display", output.properties)

    def test_recent_graph_projects_round_trip_in_app_settings(self) -> None:
        self.assertEqual("batch", AppSettings().workspace_mode)
        with TemporaryDirectory() as tmp:
            settings_path = Path(tmp) / "settings.json"
            repository = AppSettingsRepository(settings_path)
            repository.save(
                AppSettings(
                    workspace_mode="batch",
                    graph_auto_watch=True,
                    graph_auto_export=True,
                    recent_graph_projects=("A.texturegraph", "B.texturegraph"),
                )
            )
            loaded = repository.load()
        self.assertEqual("batch", loaded.workspace_mode)
        self.assertTrue(loaded.graph_auto_watch)
        self.assertTrue(loaded.graph_auto_export)
        self.assertEqual(("A.texturegraph", "B.texturegraph"), loaded.recent_graph_projects)

    def test_graph_workspace_auto_watch_tracks_texture_paths(self) -> None:
        with TemporaryDirectory() as tmp:
            texture_path = Path(tmp) / "albedo.png"
            Image.new("RGBA", (8, 8), (255, 0, 0, 255)).save(texture_path)
            texture = create_graph_node(
                NodeType.TEXTURE_INPUT,
                properties={"path": str(texture_path)},
            )
            self.workspace._push_graph_command(
                AddNodesCommand(
                    self.workspace.project.graph,
                    self.workspace._on_graph_command_changed,
                    [texture],
                ),
                select_node_ids=[texture.node_id],
            )

            self.workspace.set_auto_watch_enabled(True)

            watched_paths = self.workspace.watched_texture_paths()
            self.assertEqual((texture_path,), watched_paths)

    def test_graph_workspace_auto_watch_emits_changed_texture_path(self) -> None:
        with TemporaryDirectory() as tmp:
            texture_path = Path(tmp) / "albedo.png"
            Image.new("RGBA", (8, 8), (255, 0, 0, 255)).save(texture_path)
            texture = create_graph_node(
                NodeType.TEXTURE_INPUT,
                properties={"path": str(texture_path)},
            )
            self.workspace._push_graph_command(
                AddNodesCommand(
                    self.workspace.project.graph,
                    self.workspace._on_graph_command_changed,
                    [texture],
                ),
                select_node_ids=[texture.node_id],
            )
            self.workspace.set_auto_watch_enabled(True)

            received: list[tuple[Path, ...]] = []
            loop = QEventLoop()

            def _capture(paths: tuple[Path, ...]) -> None:
                received.append(paths)
                loop.quit()

            self.workspace.assets_changed.connect(_capture)
            self.workspace._on_watched_file_changed(str(texture_path))
            QTimer.singleShot(1200, loop.quit)
            loop.exec()

            self.assertTrue(received)
            self.assertEqual((texture_path,), received[-1])

    def test_main_window_switches_between_batch_and_graph_modes(self) -> None:
        window = MainWindow()
        try:
            window._set_workspace_mode("batch")
            self.assertEqual("batch", window._workspace_mode)
            self.assertIs(window.workspace_stack.currentWidget(), window.batch_workspace)
            self.assertTrue(window.export_graph_button.isHidden())
            self.assertTrue(window.top_output_edit.isHidden())
            self.assertTrue(window.assets_dock.isHidden())
            self.assertTrue(window.node_properties_dock.isHidden())
            self.assertFalse(window.inspector_dock.isHidden())

            window._set_workspace_mode("graph")
            self.assertEqual("graph", window._workspace_mode)
            self.assertIs(window.workspace_stack.currentWidget(), window.graph_workspace)
            self.assertTrue(window.export_graph_button.isHidden())
            self.assertFalse(window.top_output_edit.isHidden())
            self.assertFalse(window.assets_dock.isHidden())
            self.assertTrue(window.node_properties_dock.isHidden())
            self.assertTrue(window.inspector_dock.isHidden())
            self.assertFalse(window.graph_workspace.export_button.isHidden())
        finally:
            window.setParent(None)
            window.deleteLater()
            self.app.processEvents()

    def test_file_drop_routes_to_active_workspace(self) -> None:
        window = MainWindow()
        queue_drops: list[list[str]] = []
        window.queue_paths_received.connect(queue_drops.append)
        try:
            with mock.patch.object(window, "_add_graph_assets_from_paths") as add_assets:
                window._set_workspace_mode("graph")
                window._route_dropped_paths(["graph_texture.png"])
                add_assets.assert_called_once_with(["graph_texture.png"])
                self.assertEqual([], queue_drops)

                window._set_workspace_mode("batch")
                window._route_dropped_paths(["batch_texture.png"])
                self.assertEqual([["batch_texture.png"]], queue_drops)
        finally:
            window.setParent(None)
            window.deleteLater()
            self.app.processEvents()

    def test_main_window_layout_does_not_force_extreme_minimum_width(self) -> None:
        window = MainWindow()
        try:
            self.assertLessEqual(window.minimumSizeHint().width(), 1200)
            self.assertLessEqual(window.top_toolbar.minimumSizeHint().width(), 900)
            self.assertLessEqual(window.preview_panel.minimumSizeHint().width(), 260)
        finally:
            window.setParent(None)
            window.deleteLater()
            self.app.processEvents()

    def test_pbr_preview_is_available_from_queue_and_view_menu(self) -> None:
        window = MainWindow()
        try:
            self.assertEqual("PBR Preview", window.queue_panel.pbr_preview_button.text())
            self.assertEqual("PBR Preview", window.pbr_preview_dock.windowTitle())
            self.assertIn(
                window.pbr_preview_dock.toggleViewAction(),
                window.view_menu.actions(),
            )
            self.assertEqual(2, window.pbr_preview_panel.geometry_combo.count())
            self.assertEqual(7, window.pbr_preview_panel.solo_combo.count())
            normal_check_index = window.pbr_preview_panel.solo_combo.findData(
                PBR_SOLO_NORMAL_CHECK
            )
            window.pbr_preview_panel.solo_combo.setCurrentIndex(normal_check_index)
            self.assertFalse(window.pbr_preview_panel.normal_check_label.isHidden())
            self.assertIn("ЛЕВАЯ ПОЛОВИНА", window.pbr_preview_panel.normal_check_label.text())
            self.assertIn("ПРАВАЯ ПОЛОВИНА", window.pbr_preview_panel.normal_check_label.text())
            self.assertEqual(
                "PbrOpenGLWidget",
                type(window.pbr_preview_panel.gl_preview).__name__,
            )
            window.pbr_preview_panel.gl_preview.set_light_orientation(75.0, 30.0)
            self.assertEqual(75.0, window.pbr_preview_panel.gl_preview._light_rotation)
            self.assertEqual(30.0, window.pbr_preview_panel.gl_preview._light_elevation)
            window.pbr_preview_panel.gl_preview.set_object_rotation(45.0, -20.0)
            self.assertEqual(45.0, window.pbr_preview_panel.gl_preview._rotation_yaw)
            self.assertEqual(-20.0, window.pbr_preview_panel.gl_preview._rotation_pitch)
            window.pbr_preview_panel.gl_preview.reset_object_rotation()
            self.assertEqual(0.0, window.pbr_preview_panel.gl_preview._rotation_yaw)
            self.assertEqual(0.0, window.pbr_preview_panel.gl_preview._rotation_pitch)
            window.pbr_preview_panel.gl_preview.set_zoom(1.5)
            self.assertEqual(1.5, window.pbr_preview_panel.gl_preview.zoom)
            window.pbr_preview_panel.gl_preview.set_pan(0.25, -0.4)
            self.assertEqual(0.25, window.pbr_preview_panel.gl_preview._pan_x)
            self.assertEqual(-0.4, window.pbr_preview_panel.gl_preview._pan_y)
            window.pbr_preview_panel.gl_preview.reset_object_view()
            self.assertEqual(1.0, window.pbr_preview_panel.gl_preview.zoom)
            self.assertEqual(0.0, window.pbr_preview_panel.gl_preview._pan_x)
            self.assertEqual(0.0, window.pbr_preview_panel.gl_preview._pan_y)
        finally:
            window.setParent(None)
            window.deleteLater()
            self.app.processEvents()

    def test_batch_workspace_does_not_duplicate_mode_header(self) -> None:
        window = MainWindow()
        try:
            panel_titles = [
                label.text()
                for label in window.batch_workspace.findChildren(QLabel)
                if label.objectName() == "PanelTitle"
            ]
            self.assertNotIn("Batch Converter", panel_titles)
        finally:
            window.setParent(None)
            window.deleteLater()
            self.app.processEvents()

    def test_mode_buttons_have_room_for_full_text(self) -> None:
        window = MainWindow()
        try:
            self.assertGreaterEqual(window.batch_mode_button.minimumHeight(), 30)
            self.assertGreaterEqual(window.batch_mode_button.minimumWidth(), 140)
            self.assertGreaterEqual(window.graph_mode_button.minimumWidth(), 140)
        finally:
            window.setParent(None)
            window.deleteLater()
            self.app.processEvents()

    def test_graph_asset_browser_can_remove_selected_assets(self) -> None:
        window = MainWindow()
        try:
            metadata = AssetMetadata(
                format_name="PNG",
                width=4,
                height=4,
                mode="RGBA",
                has_alpha=True,
                file_size_bytes=64,
                map_type=TextureMapType.BASECOLOR,
            )
            items = [
                QueueItem(BatchSource(Path("albedo.png")), AssetKind.IMAGE, metadata),
                QueueItem(BatchSource(Path("normal.png")), AssetKind.IMAGE, metadata),
            ]
            window.add_queue_items(items)
            window.graph_workspace.set_assets(items)
            window._render_asset_browser()

            window.asset_table.selectRow(0)
            window._remove_selected_assets()

            self.assertEqual(2, len(window._queue_items))
            self.assertEqual(1, len(window.graph_workspace.assets()))
            self.assertEqual("normal.png", window.graph_workspace.assets()[0].path.name)
        finally:
            window.setParent(None)
            window.deleteLater()
            self.app.processEvents()

    def test_open_selected_set_in_graph_uses_only_current_queue_group(self) -> None:
        window = MainWindow()
        try:
            metadata = AssetMetadata("PNG", 4, 4, "RGBA", True, 64, TextureMapType.BASECOLOR)
            normal_metadata = AssetMetadata("PNG", 4, 4, "RGB", False, 64, TextureMapType.NORMAL)
            items = [
                QueueItem(
                    BatchSource(Path("D:/textures/SetA/a_basecolor.png"), Path("D:/textures"), TextureMapType.BASECOLOR),
                    AssetKind.IMAGE,
                    metadata,
                ),
                QueueItem(
                    BatchSource(Path("D:/textures/SetA/a_normal.png"), Path("D:/textures"), TextureMapType.NORMAL),
                    AssetKind.IMAGE,
                    normal_metadata,
                ),
                QueueItem(
                    BatchSource(Path("D:/textures/SetB/b_basecolor.png"), Path("D:/textures"), TextureMapType.BASECOLOR),
                    AssetKind.IMAGE,
                    metadata,
                ),
            ]
            window.add_queue_items(items)

            window.queue_panel.table.selectRow(1)
            window._open_selected_set_in_graph()

            graph_assets = window.graph_workspace.assets()
            self.assertEqual(2, len(graph_assets))
            self.assertEqual({"a_basecolor.png", "a_normal.png"}, {item.path.name for item in graph_assets})
        finally:
            window.setParent(None)
            window.deleteLater()
            self.app.processEvents()

    def test_open_selected_files_in_graph_uses_only_selected_queue_rows(self) -> None:
        window = MainWindow()
        try:
            metadata = AssetMetadata("PNG", 4, 4, "RGBA", True, 64, TextureMapType.BASECOLOR)
            items = [
                QueueItem(
                    BatchSource(Path("D:/textures/SetA/a_basecolor.png"), Path("D:/textures"), TextureMapType.BASECOLOR),
                    AssetKind.IMAGE,
                    metadata,
                ),
                QueueItem(
                    BatchSource(Path("D:/textures/SetB/b_basecolor.png"), Path("D:/textures"), TextureMapType.BASECOLOR),
                    AssetKind.IMAGE,
                    metadata,
                ),
            ]
            window.add_queue_items(items)

            selection_model = window.queue_panel.table.selectionModel()
            first_index = window.queue_panel.table.model().index(1, 0)
            second_index = window.queue_panel.table.model().index(3, 0)
            selection_model.select(
                first_index,
                QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows,
            )
            selection_model.select(
                second_index,
                QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows,
            )

            window._open_selected_files_in_graph()

            graph_assets = window.graph_workspace.assets()
            self.assertEqual(2, len(graph_assets))
            self.assertEqual({"a_basecolor.png", "b_basecolor.png"}, {item.path.name for item in graph_assets})
        finally:
            window.setParent(None)
            window.deleteLater()
            self.app.processEvents()

    def test_graph_apply_preflight_reports_when_template_is_not_ready(self) -> None:
        window = MainWindow()
        try:
            text = window.queue_panel.graph_apply_preflight_label.text()
            self.assertIn("Graph template пока не задан", text)
        finally:
            window.setParent(None)
            window.deleteLater()
            self.app.processEvents()

    def test_graph_apply_preflight_lists_compatible_and_skipped_sets(self) -> None:
        window = MainWindow()
        try:
            color_metadata = AssetMetadata("PNG", 4, 4, "RGBA", True, 64, TextureMapType.BASECOLOR)
            normal_metadata = AssetMetadata("PNG", 4, 4, "RGB", False, 64, TextureMapType.NORMAL)
            items = [
                QueueItem(
                    BatchSource(Path("D:/textures/SetA/a_basecolor.png"), Path("D:/textures"), TextureMapType.BASECOLOR),
                    AssetKind.IMAGE,
                    color_metadata,
                ),
                QueueItem(
                    BatchSource(Path("D:/textures/SetA/a_normal.png"), Path("D:/textures"), TextureMapType.NORMAL),
                    AssetKind.IMAGE,
                    normal_metadata,
                ),
                QueueItem(
                    BatchSource(Path("D:/textures/SetB/b_basecolor.png"), Path("D:/textures"), TextureMapType.BASECOLOR),
                    AssetKind.IMAGE,
                    color_metadata,
                ),
            ]
            window.add_queue_items(items)
            window.graph_workspace.set_assets(items)

            texture_color = create_graph_node(
                NodeType.TEXTURE_INPUT,
                properties={"path": "D:/textures/SetA/a_basecolor.png"},
            )
            texture_normal = create_graph_node(
                NodeType.TEXTURE_INPUT,
                properties={"path": "D:/textures/SetA/a_normal.png"},
            )
            output = create_graph_node(NodeType.OUTPUT_RGBA)
            window.graph_workspace.project = NodeGraphProject(
                graph=NodeGraph(nodes=[texture_color, texture_normal, output])
            )
            window._refresh_graph_apply_preflight()

            text = window.queue_panel.graph_apply_preflight_label.text()
            self.assertIn("Graph template: BaseColor, Normal", text)
            self.assertIn("Подойдут наборы: 1", text)
            self.assertIn("Будут пропущены: 1", text)
            self.assertIn("textures/SetB: нет Normal", text)
        finally:
            window.setParent(None)
            window.deleteLater()
            self.app.processEvents()

    def test_graph_template_map_types_ignore_extra_graph_assets_not_used_by_nodes(self) -> None:
        window = MainWindow()
        try:
            color_metadata = AssetMetadata("PNG", 4, 4, "RGBA", True, 64, TextureMapType.BASECOLOR)
            normal_metadata = AssetMetadata("PNG", 4, 4, "RGB", False, 64, TextureMapType.NORMAL)
            roughness_metadata = AssetMetadata("PNG", 4, 4, "L", False, 64, TextureMapType.ROUGHNESS)
            graph_assets = [
                QueueItem(
                    BatchSource(Path("D:/textures/SetA/a_basecolor.png"), Path("D:/textures"), TextureMapType.BASECOLOR),
                    AssetKind.IMAGE,
                    color_metadata,
                ),
                QueueItem(
                    BatchSource(Path("D:/textures/SetA/a_normal.png"), Path("D:/textures"), TextureMapType.NORMAL),
                    AssetKind.IMAGE,
                    normal_metadata,
                ),
                QueueItem(
                    BatchSource(Path("D:/textures/SetA/a_roughness.png"), Path("D:/textures"), TextureMapType.ROUGHNESS),
                    AssetKind.IMAGE,
                    roughness_metadata,
                ),
            ]
            window.graph_workspace.set_assets(graph_assets)
            window.graph_workspace.project = NodeGraphProject(
                graph=NodeGraph(
                    nodes=[
                        create_graph_node(
                            NodeType.TEXTURE_INPUT,
                            properties={"path": "D:/textures/SetA/a_basecolor.png"},
                        ),
                        create_graph_node(
                            NodeType.TEXTURE_INPUT,
                            properties={"path": "D:/textures/SetA/a_normal.png"},
                        ),
                        create_graph_node(NodeType.OUTPUT_RGBA),
                    ]
                )
            )

            self.assertEqual(
                {TextureMapType.BASECOLOR, TextureMapType.NORMAL},
                window._graph_template_map_types(),
            )
        finally:
            window.setParent(None)
            window.deleteLater()
            self.app.processEvents()

    def test_graph_output_path_keeps_explicit_non_png_suffix(self) -> None:
        output = create_graph_node(
            NodeType.OUTPUT_RGBA,
            properties={"output_path": "exports/maskmap.tif"},
        )

        destinations = NodeGraphExecutor().resolve_output_paths(
            NodeGraph(nodes=[output]),
            Path("C:/graph_exports"),
        )

        self.assertEqual(1, len(destinations))
        self.assertEqual(Path("C:/graph_exports/exports/maskmap.tif"), destinations[0])

    def test_graph_export_can_write_jpeg(self) -> None:
        with TemporaryDirectory() as tmp:
            texture_path = Path(tmp) / "source.png"
            Image.new("RGBA", (4, 4), (24, 96, 180, 255)).save(texture_path)
            texture = create_graph_node(
                NodeType.TEXTURE_INPUT,
                properties={"path": str(texture_path)},
            )
            output = create_graph_node(
                NodeType.OUTPUT_RGBA,
                properties={"filename": "packed_mask.jpg"},
            )
            graph = NodeGraph(
                nodes=[texture, output],
                connections=[
                    GraphConnection(make_connection_id(), texture.node_id, "r", output.node_id, "r"),
                    GraphConnection(make_connection_id(), texture.node_id, "g", output.node_id, "g"),
                    GraphConnection(make_connection_id(), texture.node_id, "b", output.node_id, "b"),
                    GraphConnection(make_connection_id(), texture.node_id, "a", output.node_id, "a"),
                ],
            )
            summary = NodeGraphExecutor().export_enabled_outputs(
                NodeGraphProject(graph=graph),
                Path(tmp),
                ConversionOptions(overwrite=True),
            )

            self.assertEqual(1, summary.succeeded)
            destination = Path(tmp) / "packed_mask.jpg"
            self.assertTrue(destination.exists())
            with Image.open(destination) as exported:
                self.assertEqual("JPEG", exported.format)
                self.assertEqual("RGB", exported.mode)

    def test_output_profile_connects_detected_texture_nodes(self) -> None:
        ao = create_graph_node(NodeType.TEXTURE_INPUT, properties={"path": "mat_ao.png"})
        roughness = create_graph_node(NodeType.TEXTURE_INPUT, properties={"path": "mat_roughness.png"})
        metallic = create_graph_node(NodeType.TEXTURE_INPUT, properties={"path": "mat_metallic.png"})
        output = create_graph_node(
            NodeType.OUTPUT_RGBA,
            properties={"profile": OutputProfile.UNREAL_ORM.value},
        )
        self.workspace._push_graph_command(
            AddNodesCommand(
                self.workspace.project.graph,
                self.workspace._on_graph_command_changed,
                [ao, roughness, metallic, output],
            ),
            select_node_ids=[output.node_id],
        )
        self.workspace._apply_output_profile(output)

        self.assertEqual(OutputMode.RGB.value, output.properties["mode"])
        self.assertEqual("unreal_orm.png", output.properties["filename"])
        by_target = {
            connection.target_socket_id: connection
            for connection in self.workspace.project.graph.connections
            if connection.target_node_id == output.node_id
        }
        self.assertEqual(ao.node_id, by_target["r"].source_node_id)
        self.assertEqual(roughness.node_id, by_target["g"].source_node_id)
        self.assertEqual(metallic.node_id, by_target["b"].source_node_id)

    def test_generic_rgba_profile_connects_basecolor_channels(self) -> None:
        basecolor = create_graph_node(NodeType.TEXTURE_INPUT, properties={"path": "anglerfish_diff.png"})
        output = create_graph_node(
            NodeType.OUTPUT_RGBA,
            properties={"profile": OutputProfile.GENERIC_RGBA.value},
        )
        self.workspace._push_graph_command(
            AddNodesCommand(
                self.workspace.project.graph,
                self.workspace._on_graph_command_changed,
                [basecolor, output],
            ),
            select_node_ids=[output.node_id],
        )
        self.workspace._apply_output_profile(output)

        by_target = {
            connection.target_socket_id: connection
            for connection in self.workspace.project.graph.connections
            if connection.target_node_id == output.node_id
        }
        self.assertEqual(basecolor.node_id, by_target["r"].source_node_id)
        self.assertEqual("r", by_target["r"].source_socket_id)
        self.assertEqual(basecolor.node_id, by_target["g"].source_node_id)
        self.assertEqual("g", by_target["g"].source_socket_id)
        self.assertEqual(basecolor.node_id, by_target["b"].source_node_id)
        self.assertEqual("b", by_target["b"].source_socket_id)
        self.assertEqual(basecolor.node_id, by_target["a"].source_node_id)
        self.assertEqual("a", by_target["a"].source_socket_id)

    def test_output_profile_summary_describes_autoconnect_plan(self) -> None:
        basecolor = create_graph_node(NodeType.TEXTURE_INPUT, properties={"path": "anglerfish_diff.png"})
        output = create_graph_node(
            NodeType.OUTPUT_RGBA,
            properties={"profile": OutputProfile.GENERIC_RGBA.value},
        )
        self.workspace._push_graph_command(
            AddNodesCommand(
                self.workspace.project.graph,
                self.workspace._on_graph_command_changed,
                [basecolor, output],
            ),
            select_node_ids=[output.node_id],
        )

        summary = self.workspace._profile_summary_text(output)

        self.assertIn("Generic RGBA -> RGBA / packed_rgba.png", summary)
        self.assertIn("Target pack: Standard RGBA texture.", summary)
        self.assertIn("R <- anglerfish_diff.png.R", summary)
        self.assertIn("G <- anglerfish_diff.png.G", summary)
        self.assertIn("B <- anglerfish_diff.png.B", summary)
        self.assertIn("A <- anglerfish_diff.png.A", summary)
        self.assertEqual("Build Auto-Connect Plan", self.workspace.properties_panel.apply_profile_button.text())

    def test_unity_urp_profile_summary_explains_metallic_smoothness_layout(self) -> None:
        roughness = create_graph_node(NodeType.TEXTURE_INPUT, properties={"path": "anglerfish_rgh.png"})
        metallic = create_graph_node(NodeType.TEXTURE_INPUT, properties={"path": "anglerfish_met.png"})
        output = create_graph_node(
            NodeType.OUTPUT_RGBA,
            properties={"profile": OutputProfile.UNITY_URP.value},
        )
        self.workspace._push_graph_command(
            AddNodesCommand(
                self.workspace.project.graph,
                self.workspace._on_graph_command_changed,
                [roughness, metallic, output],
            ),
            select_node_ids=[output.node_id],
        )

        summary = self.workspace._profile_summary_text(output)

        self.assertIn("Unity URP Metallic/Smoothness -> RGBA / unity_urp_metallicsmoothness.png", summary)
        self.assertIn("R = Metallic, G = 0, B = 0, A = Smoothness or Invert(Roughness).", summary)
        self.assertIn("AO stays separate in the URP workflow.", summary)
        self.assertIn("R <- anglerfish_met.png.R", summary)
        self.assertIn("A <- Invert(anglerfish_rgh.png.R)", summary)

    def test_unity_hdrp_profile_summary_uses_maskmap_name(self) -> None:
        ao = create_graph_node(NodeType.TEXTURE_INPUT, properties={"path": "anglerfish_ao.png"})
        roughness = create_graph_node(NodeType.TEXTURE_INPUT, properties={"path": "anglerfish_rgh.png"})
        metallic = create_graph_node(NodeType.TEXTURE_INPUT, properties={"path": "anglerfish_met.png"})
        output = create_graph_node(
            NodeType.OUTPUT_RGBA,
            properties={"profile": OutputProfile.UNITY_HDRP.value},
        )
        self.workspace._push_graph_command(
            AddNodesCommand(
                self.workspace.project.graph,
                self.workspace._on_graph_command_changed,
                [ao, roughness, metallic, output],
            ),
            select_node_ids=[output.node_id],
        )

        summary = self.workspace._profile_summary_text(output)

        self.assertIn("Unity HDRP Mask Map -> RGBA / unity_hdrp_maskmap.png", summary)
        self.assertIn("R = Metallic, G = AO, B = Detail Mask, A = Smoothness or Invert(Roughness).", summary)
        self.assertIn("G <- anglerfish_ao.png.R", summary)

    def test_apply_output_profile_autorenames_default_output_title_and_filename(self) -> None:
        ao = create_graph_node(NodeType.TEXTURE_INPUT, properties={"path": "anglerfish_ao.png"})
        roughness = create_graph_node(NodeType.TEXTURE_INPUT, properties={"path": "anglerfish_rgh.png"})
        metallic = create_graph_node(NodeType.TEXTURE_INPUT, properties={"path": "anglerfish_met.png"})
        output = create_graph_node(
            NodeType.OUTPUT_RGBA,
            title="Output 1",
            properties={
                "profile": OutputProfile.UNREAL_ORM.value,
                "filename": "packed_rgba.png",
            },
        )
        self.workspace._push_graph_command(
            AddNodesCommand(
                self.workspace.project.graph,
                self.workspace._on_graph_command_changed,
                [ao, roughness, metallic, output],
            ),
            select_node_ids=[output.node_id],
        )

        self.workspace._apply_output_profile(output)

        self.assertEqual("Unreal ORM", output.title)
        self.assertEqual("unreal_orm.png", output.properties["filename"])
        self.assertTrue(output.properties["title_auto_generated"])

    def test_apply_output_profile_keeps_custom_output_title_and_filename(self) -> None:
        ao = create_graph_node(NodeType.TEXTURE_INPUT, properties={"path": "anglerfish_ao.png"})
        roughness = create_graph_node(NodeType.TEXTURE_INPUT, properties={"path": "anglerfish_rgh.png"})
        metallic = create_graph_node(NodeType.TEXTURE_INPUT, properties={"path": "anglerfish_met.png"})
        output = create_graph_node(
            NodeType.OUTPUT_RGBA,
            title="Sword Final Pack",
            properties={
                "profile": OutputProfile.UNREAL_ORM.value,
                "filename": "sword_special.tga",
            },
        )
        self.workspace._push_graph_command(
            AddNodesCommand(
                self.workspace.project.graph,
                self.workspace._on_graph_command_changed,
                [ao, roughness, metallic, output],
            ),
            select_node_ids=[output.node_id],
        )

        self.workspace._apply_output_profile(output)

        self.assertEqual("Sword Final Pack", output.title)
        self.assertEqual("sword_special.tga", output.properties["filename"])

    def test_find_missing_textures_button_uses_updated_label(self) -> None:
        self.assertEqual("Find Missing Textures", self.workspace.remap_button.text())

    def test_output_file_selection_syncs_output_name(self) -> None:
        output = create_graph_node(
            NodeType.OUTPUT_RGBA,
            properties={
                "filename": "graph_output_1.png",
                "output_path": "exports/graph_output_1.png",
            },
        )
        self.workspace._push_graph_command(
            AddNodesCommand(
                self.workspace.project.graph,
                self.workspace._on_graph_command_changed,
                [output],
            ),
            select_node_ids=[output.node_id],
        )
        self.workspace.properties_panel.set_node(output)
        self.workspace.properties_panel.output_path_edit.setText("D:/temp/graph_output_1.tga")
        self.workspace.properties_panel._on_output_path_finished()

        self.assertEqual("graph_output_1.tga", self.workspace.properties_panel.filename_edit.text())
        self.assertEqual("D:/temp/graph_output_1.tga", self.workspace.properties_panel.output_path_edit.text())
        self.assertEqual("graph_output_1.tga", output.properties["filename"])
        self.assertEqual("D:/temp/graph_output_1.tga", output.properties["output_path"])

    def test_output_name_with_extension_updates_output_path_extension(self) -> None:
        output = create_graph_node(
            NodeType.OUTPUT_RGBA,
            properties={
                "filename": "graph_output_1.png",
                "output_path": "exports/graph_output_1.png",
            },
        )
        self.workspace._push_graph_command(
            AddNodesCommand(
                self.workspace.project.graph,
                self.workspace._on_graph_command_changed,
                [output],
            ),
            select_node_ids=[output.node_id],
        )
        self.workspace.properties_panel.set_node(output)
        self.workspace.properties_panel.filename_edit.setText("graph_output_1.tga")
        self.workspace.properties_panel._on_output_name_finished()

        self.assertEqual("graph_output_1.tga", output.properties["filename"])
        self.assertEqual(Path("exports") / "graph_output_1.tga", Path(output.properties["output_path"]))

    def test_output_name_without_explicit_output_file_keeps_output_path_empty(self) -> None:
        output = create_graph_node(
            NodeType.OUTPUT_RGBA,
            properties={
                "filename": "graph_output_1.png",
                "output_path": "",
            },
        )
        self.workspace._push_graph_command(
            AddNodesCommand(
                self.workspace.project.graph,
                self.workspace._on_graph_command_changed,
                [output],
            ),
            select_node_ids=[output.node_id],
        )
        self.workspace.properties_panel.set_node(output)
        self.workspace.properties_panel.filename_edit.setText("graph_output_1.tga")
        self.workspace.properties_panel._on_output_name_finished()

        self.assertEqual("graph_output_1.tga", output.properties["filename"])
        self.assertEqual("", output.properties["output_path"])

    def test_constant_value_uses_slider_and_spinbox_pair(self) -> None:
        constant = create_graph_node(
            NodeType.CONSTANT_CHANNEL,
            properties={"value": 96},
        )
        self.workspace._push_graph_command(
            AddNodesCommand(
                self.workspace.project.graph,
                self.workspace._on_graph_command_changed,
                [constant],
            ),
            select_node_ids=[constant.node_id],
        )
        self.workspace.properties_panel.set_node(constant)

        self.workspace.properties_panel.value_slider.setValue(144)
        loop = QEventLoop()
        QTimer.singleShot(250, loop.quit)
        loop.exec()
        self.app.processEvents()

        self.assertEqual(144, self.workspace.properties_panel.value_spin.value())
        self.assertEqual(144, constant.properties["value"])

    def test_blend_opacity_uses_slider_and_spinbox_pair(self) -> None:
        blend = create_graph_node(
            NodeType.BLEND_CHANNEL,
            properties={"opacity": 65},
        )
        self.workspace._push_graph_command(
            AddNodesCommand(
                self.workspace.project.graph,
                self.workspace._on_graph_command_changed,
                [blend],
            ),
            select_node_ids=[blend.node_id],
        )
        self.workspace.properties_panel.set_node(blend)

        self.workspace.properties_panel.blend_opacity_slider.setValue(42)
        loop = QEventLoop()
        QTimer.singleShot(250, loop.quit)
        loop.exec()
        self.app.processEvents()

        self.assertEqual(42, self.workspace.properties_panel.blend_opacity_spin.value())
        self.assertEqual(42, blend.properties["opacity"])

    def test_output_fields_show_current_format_hint(self) -> None:
        output = create_graph_node(
            NodeType.OUTPUT_RGBA,
            properties={
                "filename": "graph_output_1.tga",
                "output_path": "exports/graph_output_1.tga",
            },
        )
        self.workspace._push_graph_command(
            AddNodesCommand(
                self.workspace.project.graph,
                self.workspace._on_graph_command_changed,
                [output],
            ),
            select_node_ids=[output.node_id],
        )
        self.workspace.properties_panel.set_node(output)

        self.assertIn(".tga", self.workspace.properties_panel.filename_edit.toolTip())
        self.assertIn(".tga", self.workspace.properties_panel.output_path_edit.toolTip())
        self.assertEqual("packed_rgba.tga", self.workspace.properties_panel.filename_edit.placeholderText())

    def test_clear_output_inputs_is_undoable(self) -> None:
        constant = create_graph_node(NodeType.CONSTANT_CHANNEL)
        output = create_graph_node(NodeType.OUTPUT_RGBA)
        connection = GraphConnection(
            make_connection_id(),
            constant.node_id,
            "out",
            output.node_id,
            "r",
        )
        self.workspace._push_graph_command(
            AddNodesCommand(
                self.workspace.project.graph,
                self.workspace._on_graph_command_changed,
                [constant, output],
                [connection],
            ),
            select_node_ids=[output.node_id],
        )

        self.workspace._clear_output_inputs(output)

        self.assertEqual(0, len(self.workspace.project.graph.connections))
        self.workspace.undo_stack.undo()
        self.assertEqual(1, len(self.workspace.project.graph.connections))
        self.workspace.undo_stack.redo()
        self.assertEqual(0, len(self.workspace.project.graph.connections))

    def test_output_profile_uses_detected_hdrp_maskmap_channels(self) -> None:
        maskmap = create_graph_node(NodeType.TEXTURE_INPUT, properties={"path": "mushket_maskmap.png"})
        output = create_graph_node(
            NodeType.OUTPUT_RGBA,
            properties={"profile": OutputProfile.UNITY_HDRP.value},
        )
        self.workspace._push_graph_command(
            AddNodesCommand(
                self.workspace.project.graph,
                self.workspace._on_graph_command_changed,
                [maskmap, output],
            ),
            select_node_ids=[output.node_id],
        )
        self.workspace._apply_output_profile(output)

        by_target = {
            connection.target_socket_id: connection
            for connection in self.workspace.project.graph.connections
            if connection.target_node_id == output.node_id
        }
        self.assertEqual(maskmap.node_id, by_target["r"].source_node_id)
        self.assertEqual("r", by_target["r"].source_socket_id)
        self.assertEqual(maskmap.node_id, by_target["g"].source_node_id)
        self.assertEqual("g", by_target["g"].source_socket_id)
        self.assertEqual(maskmap.node_id, by_target["b"].source_node_id)
        self.assertEqual("b", by_target["b"].source_socket_id)
        self.assertEqual(maskmap.node_id, by_target["a"].source_node_id)
        self.assertEqual("a", by_target["a"].source_socket_id)

    def test_unreal_profile_remaps_hdrp_maskmap_with_invert(self) -> None:
        maskmap = create_graph_node(NodeType.TEXTURE_INPUT, properties={"path": "mushket_maskmap.png"})
        output = create_graph_node(
            NodeType.OUTPUT_RGBA,
            properties={"profile": OutputProfile.UNREAL_ORM.value},
        )
        self.workspace._push_graph_command(
            AddNodesCommand(
                self.workspace.project.graph,
                self.workspace._on_graph_command_changed,
                [maskmap, output],
            ),
            select_node_ids=[output.node_id],
        )
        self.workspace._apply_output_profile(output)

        by_target = {
            connection.target_socket_id: connection
            for connection in self.workspace.project.graph.connections
            if connection.target_node_id == output.node_id
        }
        self.assertEqual(maskmap.node_id, by_target["r"].source_node_id)
        self.assertEqual("g", by_target["r"].source_socket_id)
        self.assertEqual(maskmap.node_id, by_target["b"].source_node_id)
        self.assertEqual("r", by_target["b"].source_socket_id)
        invert_node = next(
            node
            for node in self.workspace.project.graph.nodes
            if node.node_type is NodeType.INVERT_CHANNEL
        )
        self.assertEqual(invert_node.node_id, by_target["g"].source_node_id)
        invert_input = next(
            connection
            for connection in self.workspace.project.graph.connections
            if connection.target_node_id == invert_node.node_id
        )
        self.assertEqual(maskmap.node_id, invert_input.source_node_id)
        self.assertEqual("a", invert_input.source_socket_id)

    def test_background_preview_emits_latest_result(self) -> None:
        constant = create_graph_node(NodeType.CONSTANT_CHANNEL, properties={"value": 123})
        self.workspace._push_graph_command(
            AddNodesCommand(
                self.workspace.project.graph,
                self.workspace._on_graph_command_changed,
                [constant],
            ),
            select_node_ids=[constant.node_id],
        )
        received = []
        loop = QEventLoop()
        self.workspace.preview_image_requested.connect(
            lambda image, title, meta: (received.append((image, title, meta)), loop.quit())
        )
        self.workspace._request_preview_node(constant)
        QTimer.singleShot(3000, loop.quit)
        loop.exec()
        self.assertTrue(received)
        self.assertEqual(constant.title, received[-1][1])

    def test_display_flag_previews_set_alpha_node(self) -> None:
        color = create_graph_node(
            NodeType.COLOR,
            properties={
                "red": 12,
                "green": 34,
                "blue": 56,
                "alpha": 78,
                "width": 2,
                "height": 1,
            },
        )
        set_alpha = create_graph_node(NodeType.SET_ALPHA)
        connection = GraphConnection(
            make_connection_id(),
            color.node_id,
            "image",
            set_alpha.node_id,
            "image",
        )
        self.workspace._push_graph_command(
            AddNodesCommand(
                self.workspace.project.graph,
                self.workspace._on_graph_command_changed,
                [color, set_alpha],
                [connection],
            ),
            select_node_ids=[set_alpha.node_id],
        )

        received: list[tuple[Image.Image, str, str, str]] = []
        loop = QEventLoop()
        self.workspace.preview_image_requested.connect(
            lambda image, title, meta, node_id: (
                received.append((image, title, meta, node_id)),
                loop.quit(),
            )
        )

        self.workspace._on_display_flag_clicked(set_alpha)
        QTimer.singleShot(3000, loop.quit)
        loop.exec()

        self.assertTrue(received)
        self.assertEqual(set_alpha.node_id, received[-1][3])
        self.assertEqual((12, 34, 56, 78), received[-1][0].getpixel((0, 0)))
        self.assertTrue(set_alpha.properties["display"])

    def test_transient_color_preview_updates_downstream_without_committing(self) -> None:
        color = create_graph_node(
            NodeType.COLOR,
            properties={
                "red": 10,
                "green": 20,
                "blue": 30,
                "alpha": 255,
                "width": 2,
                "height": 1,
            },
        )
        set_alpha = create_graph_node(
            NodeType.SET_ALPHA,
            properties={"display": True},
        )
        connection = GraphConnection(
            make_connection_id(),
            color.node_id,
            "image",
            set_alpha.node_id,
            "image",
        )
        self.workspace.project.graph.nodes.extend((color, set_alpha))
        self.workspace.project.graph.connections.append(connection)
        self.workspace._scene.rebuild()

        received: list[Image.Image] = []
        loop = QEventLoop()
        self.workspace.preview_image_requested.connect(
            lambda image, _title, _meta, _node_id: (received.append(image), loop.quit())
        )
        preview_properties = dict(color.properties)
        preview_properties.update({"red": 100, "green": 110, "blue": 120})

        self.workspace._on_transient_preview_requested(
            color,
            preview_properties,
            PREVIEW_MODE_DRAFT,
        )
        QTimer.singleShot(3000, loop.quit)
        loop.exec()

        self.assertTrue(received)
        self.assertEqual((100, 110, 120, 255), received[-1].getpixel((0, 0)))
        self.assertEqual(10, color.properties["red"])
        item = self.workspace._scene.node_items[color.node_id]
        self.assertEqual("#646E78FF", item.subtitle_item.text())

    def test_failed_display_preview_reports_error_instead_of_leaving_stale_image(self) -> None:
        incomplete = create_graph_node(NodeType.COMBINE_RGBA)
        self.workspace._push_graph_command(
            AddNodesCommand(
                self.workspace.project.graph,
                self.workspace._on_graph_command_changed,
                [incomplete],
            ),
            select_node_ids=[incomplete.node_id],
        )

        failures: list[tuple[str, str, str]] = []
        loop = QEventLoop()
        self.workspace.preview_failed.connect(
            lambda title, message, node_id: (
                failures.append((title, message, node_id)),
                loop.quit(),
            )
        )

        self.workspace._on_display_flag_clicked(incomplete)
        QTimer.singleShot(3000, loop.quit)
        loop.exec()

        self.assertTrue(failures)
        self.assertEqual(incomplete.node_id, failures[-1][2])
        self.assertIn("input r is not connected", failures[-1][1])

    def test_graph_preview_error_clears_previous_image(self) -> None:
        panel = PreviewPanel(allow_detach=False)
        try:
            panel.set_graph_preview(
                Image.new("RGBA", (2, 2), (255, 0, 0, 255)),
                "Previous",
                "ready",
                node_id="old",
            )
            self.assertIsNotNone(panel.preview_canvas._source_pixmap)

            panel.set_graph_preview_error(
                "Combine RGBA",
                "input R is not connected",
                node_id="broken",
            )

            self.assertIsNone(panel.preview_canvas._source_pixmap)
            self.assertEqual("broken", panel.current_graph_preview_node_id())
            self.assertEqual("Combine RGBA", panel.asset_label.text())
            self.assertEqual("input R is not connected", panel.asset_meta_label.text())
        finally:
            panel.deleteLater()
            self.app.processEvents()

    def test_selecting_texture_node_without_display_flag_does_not_request_preview(self) -> None:
        texture = create_graph_node(
            NodeType.TEXTURE_INPUT,
            properties={"path": "D:/textures/test_basecolor.png"},
        )
        with mock.patch.object(self.workspace, "_request_preview_node") as request_preview:
            self.workspace._push_graph_command(
                AddNodesCommand(
                    self.workspace.project.graph,
                    self.workspace._on_graph_command_changed,
                    [texture],
                ),
                select_node_ids=[texture.node_id],
            )
            request_preview.reset_mock()

            self.workspace._on_node_selected(texture)

        request_preview.assert_not_called()

    def test_selection_signal_is_deferred_while_node_move_starts(self) -> None:
        texture = create_graph_node(
            NodeType.TEXTURE_INPUT,
            properties={"path": "D:/textures/test_basecolor.png"},
        )
        self.workspace._push_graph_command(
            AddNodesCommand(
                self.workspace.project.graph,
                self.workspace._on_graph_command_changed,
                [texture],
            ),
            select_node_ids=[texture.node_id],
        )

        properties_selection_events: list[GraphNode | None] = []
        selection_events: list[GraphNode | None] = []
        self.workspace._scene.node_properties_selection_changed.connect(
            properties_selection_events.append
        )
        self.workspace._scene.node_selection_changed.connect(selection_events.append)

        self.workspace._scene.begin_node_move(texture.node_id)
        self.workspace._scene._emit_selection()
        self.assertEqual(1, len(properties_selection_events))
        self.assertEqual(texture.node_id, properties_selection_events[0].node_id)
        self.assertEqual([], selection_events)

        self.workspace._scene.finish_node_move()
        self.assertEqual(1, len(selection_events))
        self.assertIsNotNone(selection_events[0])
        self.assertEqual(texture.node_id, selection_events[0].node_id)

    def test_draft_preview_uses_reduced_max_side(self) -> None:
        self.workspace._preview_cache.max_side = 2048
        self.assertEqual(DRAFT_PREVIEW_MAX_SIDE, self.workspace._preview_max_side_for_mode(PREVIEW_MODE_DRAFT))
        self.assertEqual(2048, self.workspace._preview_max_side_for_mode(PREVIEW_MODE_FULL))

    def test_graph_preview_preserves_zoom_for_same_node_refresh(self) -> None:
        panel = PreviewPanel(allow_detach=False)
        image = Image.new("RGBA", (8, 8), (24, 96, 180, 255))
        panel.set_graph_preview(image, "Output 1", "meta", node_id="node-1", preserve_zoom=False)
        panel.preview_canvas.change_zoom(3)

        panel.set_graph_preview(image, "Output 1", "meta", node_id="node-1", preserve_zoom=True)

        self.assertGreater(panel.preview_canvas._zoom_factor, 1.0)

    def test_graph_preview_preserves_selected_channel_for_same_node_refresh(self) -> None:
        panel = PreviewPanel(allow_detach=False)
        image = Image.new("RGBA", (8, 8), (24, 96, 180, 255))
        panel.set_graph_preview(image, "Output 1", "meta", node_id="node-1", preserve_zoom=False)
        panel.set_selected_channel(PreviewChannel.RED)

        panel.set_graph_preview(image, "Output 1", "meta", node_id="node-1", preserve_zoom=True)

        self.assertEqual(PreviewChannel.RED, panel._selected_channel)

    def test_graph_preview_resets_selected_channel_for_different_node(self) -> None:
        panel = PreviewPanel(allow_detach=False)
        image = Image.new("RGBA", (8, 8), (24, 96, 180, 255))
        panel.set_graph_preview(image, "Output 1", "meta", node_id="node-1", preserve_zoom=False)
        panel.set_selected_channel(PreviewChannel.RED)

        panel.set_graph_preview(image, "Output 2", "meta", node_id="node-2", preserve_zoom=False)

        self.assertEqual(PreviewChannel.COMPOSITE, panel._selected_channel)

    def test_deleted_undo_stack_does_not_break_dirty_state_label(self) -> None:
        self.workspace.undo_stack.deleteLater()
        self.app.processEvents()
        self.assertFalse(self.workspace.has_unsaved_changes())
        self.workspace._update_project_label()

    def test_batch_packing_preflight_uses_detailed_plan_text(self) -> None:
        queue_root = Path("D:/textures")
        options = ConversionOptions(
            packing=ChannelPackingOptions(
                enabled=True,
                layout=ChannelPackLayout.ORM,
            ),
        )
        window = MainWindow()
        try:
            window.settings_panel.apply_conversion_options(options)
            window._queue_items = [
                QueueItem(
                    source=BatchSource(queue_root / "Sword/sword_ao.png", queue_root, TextureMapType.AO),
                    asset_kind=AssetKind.IMAGE,
                    metadata=AssetMetadata("PNG", 1024, 1024, "L", False, 1024, TextureMapType.AO),
                ),
                QueueItem(
                    source=BatchSource(queue_root / "Sword/sword_roughness.png", queue_root, TextureMapType.ROUGHNESS),
                    asset_kind=AssetKind.IMAGE,
                    metadata=AssetMetadata("PNG", 1024, 1024, "L", False, 1024, TextureMapType.ROUGHNESS),
                ),
                QueueItem(
                    source=BatchSource(queue_root / "Sword/sword_metallic.png", queue_root, TextureMapType.METALLIC),
                    asset_kind=AssetKind.IMAGE,
                    metadata=AssetMetadata("PNG", 1024, 1024, "L", False, 1024, TextureMapType.METALLIC),
                ),
            ]

            window._refresh_packing_preflight()

            text = window.settings_panel.packing_queue_label.text()
            self.assertIn("Режим: сначала обычные PNG, затем packed texture.", text)
            self.assertIn("Packing plan (ORM): ready 1, incomplete 0.", text)
            self.assertIn("- Sword/sword -> Sword/sword_orm.png", text)
            self.assertIn("R=sword_ao.png", text)
            self.assertIn("G=sword_roughness.png", text)
            self.assertIn("B=sword_metallic.png", text)
        finally:
            window.close()
            window.deleteLater()
            self.app.processEvents()

    def test_pack_only_mode_updates_queue_output_hint_and_preflight(self) -> None:
        queue_root = Path("D:/textures")
        options = ConversionOptions(
            packing=ChannelPackingOptions(
                enabled=True,
                layout=ChannelPackLayout.ORM,
                mode=ChannelPackingMode.PACK_ONLY,
            ),
        )
        window = MainWindow()
        try:
            window.settings_panel.apply_conversion_options(options)
            window._queue_items = [
                QueueItem(
                    source=BatchSource(queue_root / "Sword/sword_ao.png", queue_root, TextureMapType.AO),
                    asset_kind=AssetKind.IMAGE,
                    metadata=AssetMetadata("PNG", 1024, 1024, "L", False, 1024, TextureMapType.AO),
                ),
                QueueItem(
                    source=BatchSource(queue_root / "Sword/sword_roughness.png", queue_root, TextureMapType.ROUGHNESS),
                    asset_kind=AssetKind.IMAGE,
                    metadata=AssetMetadata("PNG", 1024, 1024, "L", False, 1024, TextureMapType.ROUGHNESS),
                ),
                QueueItem(
                    source=BatchSource(queue_root / "Sword/sword_metallic.png", queue_root, TextureMapType.METALLIC),
                    asset_kind=AssetKind.IMAGE,
                    metadata=AssetMetadata("PNG", 1024, 1024, "L", False, 1024, TextureMapType.METALLIC),
                ),
            ]

            window._update_queue_output_paths()

            preflight_text = window.settings_panel.packing_queue_label.text()
            self.assertIn("Режим: только packed texture.", preflight_text)
            self.assertIn("Packing plan (ORM): ready 1, incomplete 0.", preflight_text)
            self.assertEqual("В составе packed texture", window.queue_panel.table.item(1, 5).text())
            self.assertIn("sword_orm.png", window.queue_panel.table.item(1, 5).toolTip())
            self.assertIn(
                "соберет только итоговый packed texture",
                window.settings_panel.packing_mode_label.text(),
            )
        finally:
            window.close()
            window.deleteLater()
            self.app.processEvents()

    def test_selecting_ui_preset_applies_options_immediately(self) -> None:
        window = MainWindow()
        try:
            presets = list(SYSTEM_PRESETS)
            window._presets_by_id = {preset.preset_id: preset for preset in presets}
            window.settings_panel.set_available_presets(presets)

            ui_preset = next(preset for preset in presets if preset.name == "UI RGBA Clean")
            for index in range(window.settings_panel.preset_combo.count()):
                if window.settings_panel.preset_combo.itemData(index) == ui_preset.preset_id:
                    window.settings_panel.preset_combo.setCurrentIndex(index)
                    break
            self.app.processEvents()

            options = window.settings_panel.build_conversion_options()
            self.assertTrue(options.force_rgba)
            self.assertFalse(options.packing.enabled)
            self.assertEqual("UI RGBA Clean", window.settings_panel.preset_combo.currentText().split(" [")[0])
            tooltip = window.settings_panel.preset_combo.toolTip()
            self.assertIn("UI RGBA Clean", tooltip)
            self.assertIn("Формат: PNG (RGBA) · Размер: исходный", tooltip)
            self.assertIn("Выход: отдельные PNG", tooltip)
            self.assertFalse(hasattr(window.settings_panel, "preset_summary_label"))
            self.assertFalse(hasattr(window.settings_panel, "preset_packing_summary_label"))
        finally:
            window.close()
            window.deleteLater()
            self.app.processEvents()

    def test_selecting_unreal_pack_preset_shows_compact_tooltip(self) -> None:
        window = MainWindow()
        try:
            presets = list(SYSTEM_PRESETS)
            window._presets_by_id = {preset.preset_id: preset for preset in presets}
            window.settings_panel.set_available_presets(presets)

            unreal_preset = next(preset for preset in presets if preset.name == "Unreal ORM Pack")
            for index in range(window.settings_panel.preset_combo.count()):
                if window.settings_panel.preset_combo.itemData(index) == unreal_preset.preset_id:
                    window.settings_panel.preset_combo.setCurrentIndex(index)
                    break
            self.app.processEvents()

            tooltip = window.settings_panel.preset_combo.toolTip()
            bundle_text = window.settings_panel.output_bundle_summary_label.text()
            self.assertIn("Unreal ORM Pack", tooltip)
            self.assertIn("ORM: R=AO, G=Roughness, B=Metallic", tooltip)
            self.assertIn("packed texture + карты вне packed-схемы", tooltip)
            self.assertIn("Размер: до 4096 px", tooltip)
            self.assertIn("Packed: ORM", bundle_text)
        finally:
            window.close()
            window.deleteLater()
            self.app.processEvents()

    def test_traditional_preset_previews_substance_style_texture_set(self) -> None:
        window = MainWindow()
        try:
            presets = list(SYSTEM_PRESETS)
            window._presets_by_id = {preset.preset_id: preset for preset in presets}
            window.settings_panel.set_available_presets(presets)
            traditional = next(
                preset
                for preset in presets
                if preset.name == "Traditional / Non-Packed Workflow"
            )

            window.settings_panel.apply_conversion_options(traditional.options)
            window._refresh_output_bundle_summary()

            bundle_text = window.settings_panel.output_bundle_summary_label.text()
            self.assertIn("Base Color / Albedo → *_basecolor.png", bundle_text)
            self.assertIn("Roughness → *_roughness.png", bundle_text)
            self.assertIn("Metallic → *_metallic.png", bundle_text)
            self.assertIn("Normal → *_normal.png", bundle_text)
            self.assertIn("Ambient Occlusion (AO) → *_ao.png", bundle_text)
            self.assertIn("Packed texture: не используется", bundle_text)
            rendered_html = window.settings_panel.output_bundle_summary_label.rendered_html
            self.assertIn('<table width="100%"', rendered_html)
            self.assertIn('bgcolor="#202832"', rendered_html)
            self.assertIn("Base Color / Albedo", rendered_html)
            self.assertIn("*_basecolor.png", rendered_html)
            self.assertIn("чистый цвет поверхности", rendered_html)

            base_metadata = AssetMetadata(
                "PNG", 4, 4, "RGB", False, 64, TextureMapType.BASECOLOR
            )
            normal_metadata = AssetMetadata(
                "PNG", 4, 4, "RGB", False, 64, TextureMapType.NORMAL
            )
            window.add_queue_items(
                [
                    QueueItem(
                        BatchSource(
                            Path("D:/textures/key_basecolor.png"),
                            Path("D:/textures"),
                            TextureMapType.BASECOLOR,
                        ),
                        AssetKind.IMAGE,
                        base_metadata,
                    ),
                    QueueItem(
                        BatchSource(
                            Path("D:/textures/key_normal.png"),
                            Path("D:/textures"),
                            TextureMapType.NORMAL,
                        ),
                        AssetKind.IMAGE,
                        normal_metadata,
                    ),
                ]
            )
            bundle_text = window.settings_panel.output_bundle_summary_label.text()
            self.assertIn("✓ Base Color / Albedo", bundle_text)
            self.assertIn("○ Roughness", bundle_text)
            self.assertIn(
                "Не будут созданы — нет исходных данных: AO, Roughness, Metallic",
                bundle_text,
            )
        finally:
            window.close()
            window.deleteLater()
            self.app.processEvents()

    def test_output_bundle_summary_lists_remaining_maps_for_unreal_pack(self) -> None:
        window = MainWindow()
        try:
            presets = list(SYSTEM_PRESETS)
            window._presets_by_id = {preset.preset_id: preset for preset in presets}
            window.settings_panel.set_available_presets(presets)
            unreal_preset = next(preset for preset in presets if preset.name == "Unreal ORM Pack")
            window.settings_panel.apply_conversion_options(unreal_preset.options)

            metadata = AssetMetadata("PNG", 4, 4, "RGBA", True, 64, TextureMapType.BASECOLOR)
            normal_metadata = AssetMetadata("PNG", 4, 4, "RGB", False, 64, TextureMapType.NORMAL)
            gray_metadata = AssetMetadata("PNG", 4, 4, "L", False, 64, TextureMapType.AO)
            window.add_queue_items([
                QueueItem(BatchSource(Path("D:/textures/key_basecolor.png"), Path("D:/textures"), TextureMapType.BASECOLOR), AssetKind.IMAGE, metadata),
                QueueItem(BatchSource(Path("D:/textures/key_normal.png"), Path("D:/textures"), TextureMapType.NORMAL), AssetKind.IMAGE, normal_metadata),
                QueueItem(BatchSource(Path("D:/textures/key_ao.png"), Path("D:/textures"), TextureMapType.AO), AssetKind.IMAGE, gray_metadata),
                QueueItem(BatchSource(Path("D:/textures/key_roughness.png"), Path("D:/textures"), TextureMapType.ROUGHNESS), AssetKind.IMAGE, gray_metadata),
                QueueItem(BatchSource(Path("D:/textures/key_metallic.png"), Path("D:/textures"), TextureMapType.METALLIC), AssetKind.IMAGE, gray_metadata),
            ])

            bundle_text = window.settings_panel.output_bundle_summary_label.text()
            self.assertIn("Отдельно: BaseColor, Normal", bundle_text)
            self.assertIn("Packed: ORM", bundle_text)
            self.assertIn("Не дублировать отдельно: AO, Roughness, Metallic", bundle_text)
        finally:
            window.close()
            window.deleteLater()
            self.app.processEvents()

    def test_queue_renders_folder_group_headers(self) -> None:
        window = MainWindow()
        try:
            metadata = AssetMetadata("PNG", 4, 4, "RGBA", True, 64, TextureMapType.BASECOLOR)
            window.add_queue_items([
                QueueItem(BatchSource(Path("D:/textures/SetA/a_basecolor.png"), Path("D:/textures"), TextureMapType.BASECOLOR), AssetKind.IMAGE, metadata),
                QueueItem(BatchSource(Path("D:/textures/SetB/b_basecolor.png"), Path("D:/textures"), TextureMapType.BASECOLOR), AssetKind.IMAGE, metadata),
            ])

            table = window.queue_panel.table
            self.assertEqual(4, table.rowCount())
            self.assertEqual("Папка: textures/SetA", table.item(0, 0).text())
            self.assertEqual("Папка: textures/SetB", table.item(2, 0).text())

            table.selectRow(3)
            selected = window._selected_queue_item()
            self.assertIsNotNone(selected)
            self.assertEqual("b_basecolor.png", selected.path.name)
        finally:
            window.close()
            window.deleteLater()
            self.app.processEvents()

    def test_queue_surfaces_roughness_glossiness_pair_warning(self) -> None:
        window = MainWindow()
        try:
            with TemporaryDirectory() as tmp:
                root = Path(tmp)
                roughness_path = root / "bike_roughness.png"
                glossiness_path = root / "bike_glossiness.png"
                duplicate = Image.frombytes(
                    "L",
                    (4, 1),
                    bytes((0, 64, 128, 255)),
                )
                duplicate.save(roughness_path)
                duplicate.save(glossiness_path)
                scanned = AssetScanner().scan_paths(
                    (roughness_path, glossiness_path),
                    recursive=False,
                )

                window.add_queue_items(list(scanned.items))

                self.assertTrue(
                    all(item.validation_warnings for item in window._queue_items)
                )
                status_texts = [
                    window.queue_panel.table.item(row, 4).text()
                    for row in range(window.queue_panel.table.rowCount())
                    if window.queue_panel.table.item(row, 4) is not None
                ]
                self.assertTrue(
                    all("предупрежд." in text for text in status_texts)
                )
                self.assertIn(
                    "требуют внимания 1",
                    window.queue_panel.texture_set_validation_label.text(),
                )
        finally:
            window.close()
            window.deleteLater()
            self.app.processEvents()

    def test_batch_request_uses_queue_as_only_input_source(self) -> None:
        window = MainWindow()
        try:
            window.settings_panel.output_edit.setText("D:/exports")
            metadata = AssetMetadata("PNG", 4, 4, "RGBA", True, 64, TextureMapType.BASECOLOR)
            window.add_queue_items([
                QueueItem(
                    BatchSource(Path("D:/textures/wood_basecolor.png"), Path("D:/textures"), TextureMapType.BASECOLOR),
                    AssetKind.IMAGE,
                    metadata,
                )
            ])

            request = window.build_request()

            self.assertIsNone(request.input_path)
            self.assertEqual(Path("D:/exports"), request.output_root)
            self.assertEqual(1, len(request.sources))
        finally:
            window.close()
            window.deleteLater()
            self.app.processEvents()

    def test_batch_tabs_use_clearer_workflow_labels(self) -> None:
        window = MainWindow()
        try:
            tabs = window.settings_panel.settings_tabs

            self.assertEqual("Сценарий", tabs.tabText(0))
            self.assertEqual("Каналы", tabs.tabText(1))
            self.assertEqual("Формат", tabs.tabText(2))
        finally:
            window.close()
            window.deleteLater()
            self.app.processEvents()

    def test_source_tab_explains_that_input_comes_from_queue(self) -> None:
        window = MainWindow()
        try:
            source_tab = window.settings_panel.settings_tabs.widget(0)
            labels = source_tab.findChildren(QLabel)
            texts = "\n".join(label.text() for label in labels)

            self.assertIn("Вход всегда берется из очереди справа.", texts)
            self.assertIn("общая папка", texts)
            self.assertIn("куда будут сохранены PNG и packed texture", texts)
            self.assertNotIn("Вход:", texts)
        finally:
            window.close()
            window.deleteLater()
            self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
