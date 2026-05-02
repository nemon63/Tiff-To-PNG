from __future__ import annotations

import gc
import json
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from PIL import Image
from PyQt6.QtCore import QEventLoop, QTimer
from PyQt6.QtWidgets import QApplication

from image_converter.domain.models import AppSettings
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
from image_converter.services.node_graph_executor import NodeGraphExecutor
from image_converter.services.node_graph_project import GRAPH_PROJECT_FILENAME, NodeGraphProjectRepository
from image_converter.services.settings import AppSettingsRepository
from image_converter.ui.graph_commands import AddNodesCommand, ReplaceInputConnectionCommand
from image_converter.ui.main_window import MainWindow
from image_converter.ui.node_editor import GraphNodeItem, GraphWorkspace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def _app() -> QApplication:
    return QApplication.instance() or QApplication(sys.argv)


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
        with TemporaryDirectory() as tmp:
            settings_path = Path(tmp) / "settings.json"
            repository = AppSettingsRepository(settings_path)
            repository.save(
                AppSettings(
                    workspace_mode="batch",
                    recent_graph_projects=("A.texturegraph", "B.texturegraph"),
                )
            )
            loaded = repository.load()
        self.assertEqual("batch", loaded.workspace_mode)
        self.assertEqual(("A.texturegraph", "B.texturegraph"), loaded.recent_graph_projects)

    def test_main_window_switches_between_batch_and_graph_modes(self) -> None:
        window = MainWindow()
        try:
            window._set_workspace_mode("batch")
            self.assertEqual("batch", window._workspace_mode)
            self.assertIs(window.workspace_stack.currentWidget(), window.batch_workspace)
            self.assertFalse(window.run_batch_button.isHidden())
            self.assertTrue(window.export_graph_button.isHidden())

            window._set_workspace_mode("graph")
            self.assertEqual("graph", window._workspace_mode)
            self.assertIs(window.workspace_stack.currentWidget(), window.graph_workspace)
            self.assertTrue(window.run_batch_button.isHidden())
            self.assertFalse(window.export_graph_button.isHidden())
        finally:
            window.setParent(None)
            window.deleteLater()
            self.app.processEvents()

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

    def test_deleted_undo_stack_does_not_break_dirty_state_label(self) -> None:
        self.workspace.undo_stack.deleteLater()
        self.app.processEvents()
        self.assertFalse(self.workspace.has_unsaved_changes())
        self.workspace._update_project_label()


if __name__ == "__main__":
    unittest.main()
