from __future__ import annotations

import gc
import json
import os
import struct
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

from PIL import Image
from PyQt6.QtCore import QEventLoop, QTimer
from PyQt6.QtWidgets import QApplication

from image_converter.domain.models import (
    AppSettings,
    AssetKind,
    AssetMetadata,
    BatchSource,
    ConversionOptions,
    PreviewChannel,
    QueueItem,
    TextureMapType,
)
from image_converter.domain.node_graph import (
    GraphConnection,
    NodeGraph,
    NodeGraphProject,
    NodeType,
    OutputMode,
    OutputProfile,
    create_graph_node,
    make_connection_id,
)
from image_converter.services.asset_queue import AssetScanner
from image_converter.services.conversion import ImageConverter
from image_converter.services.image_loading import copy_first_frame_preserving_alpha
from image_converter.services.node_graph_executor import NodeGraphExecutor
from image_converter.services.node_graph_project import GRAPH_PROJECT_FILENAME, NodeGraphProjectRepository
from image_converter.services.settings import AppSettingsRepository
from image_converter.ui.graph_commands import AddNodesCommand, ReplaceInputConnectionCommand
from image_converter.ui.main_window import MainWindow
from image_converter.ui.node_editor import GraphNodeItem, GraphWorkspace
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


class NodeGraphExecutorPerformanceTests(unittest.TestCase):
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

    def test_channel_operations_keep_expected_values(self) -> None:
        executor = NodeGraphExecutor()
        source = Image.frombytes("L", (5, 1), bytes((0, 64, 128, 192, 255)))

        levels_node = create_graph_node(
            NodeType.LEVELS_CHANNEL,
            properties={"black": 32, "white": 224, "gamma": 2.0, "out_min": 10, "out_max": 240},
        )
        clamp_node = create_graph_node(
            NodeType.CLAMP_CHANNEL,
            properties={"min": 40, "max": 180},
        )
        threshold_node = create_graph_node(
            NodeType.THRESHOLD_CHANNEL,
            properties={"threshold": 120},
        )

        levels_image = executor._apply_levels(source, levels_node)
        clamp_image = executor._apply_clamp(source, clamp_node)
        threshold_image = executor._apply_threshold(source, threshold_node)

        expected_levels = []
        for value in (0, 64, 128, 192, 255):
            normalized = min(max((value - 32) / 192.0, 0.0), 1.0)
            adjusted = normalized ** 0.5
            expected_levels.append(round(10 + adjusted * 230))

        self.assertEqual(expected_levels, list(levels_image.getdata()))
        self.assertEqual([40, 64, 128, 180, 180], list(clamp_image.getdata()))
        self.assertEqual([0, 0, 255, 255, 255], list(threshold_image.getdata()))


class GraphEditorFoundationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = _app()
        self.workspace = GraphWorkspace()

    def tearDown(self) -> None:
        self.workspace.setParent(None)
        self.workspace.deleteLater()
        self.app.processEvents()
        del self.workspace
        gc.collect()

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
            thumbnail_before = item.thumbnail_item.pixmap().toImage()
            self.assertEqual(255, thumbnail_before.pixelColor(0, 0).red())

            Image.new("RGBA", (8, 8), (0, 255, 0, 255)).save(texture_path)
            self.workspace.refresh_asset_paths([texture_path])

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
            self.assertFalse(window.run_batch_button.isHidden())
            self.assertTrue(window.export_graph_button.isHidden())
            self.assertTrue(window.top_output_edit.isHidden())
            self.assertTrue(window.assets_dock.isHidden())
            self.assertTrue(window.node_properties_dock.isHidden())
            self.assertFalse(window.inspector_dock.isHidden())

            window._set_workspace_mode("graph")
            self.assertEqual("graph", window._workspace_mode)
            self.assertIs(window.workspace_stack.currentWidget(), window.graph_workspace)
            self.assertTrue(window.run_batch_button.isHidden())
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

            window.asset_table.selectRow(0)
            window._remove_selected_assets()

            self.assertEqual(1, len(window._queue_items))
            self.assertEqual("normal.png", window._queue_items[0].path.name)
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
        self.assertIn("R <- anglerfish_diff.png.R", summary)
        self.assertIn("G <- anglerfish_diff.png.G", summary)
        self.assertIn("B <- anglerfish_diff.png.B", summary)
        self.assertIn("A <- anglerfish_diff.png.A", summary)
        self.assertEqual("Auto Connect Profile", self.workspace.properties_panel.apply_profile_button.text())

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


if __name__ == "__main__":
    unittest.main()
