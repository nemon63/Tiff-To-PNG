from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError
from unittest.mock import patch

from PyQt6.QtWidgets import QApplication

from image_converter.domain.node_graph import (
    GraphConnection, NodeGraph, NodeGraphProject, NodeType, create_graph_node,
)
from image_converter.services.graph_profiling import GraphEvaluationProfiler
from image_converter.services.node_graph_executor import NodeGraphExecutor, NodeGraphPreviewCache
from image_converter.ui.graph_performance import GraphPerformancePanel
from image_converter.ui.node_editor import GraphPreviewWorker, GraphWorkspace


class _Clock:
    def __init__(self):
        self.seconds = 0.0

    def __call__(self):
        return self.seconds

    def advance(self, seconds):
        self.seconds += seconds


def _link(source, source_socket, target, target_socket):
    return GraphConnection(
        f"{source.node_id}-{source_socket}-{target.node_id}-{target_socket}",
        source.node_id, source_socket, target.node_id, target_socket,
    )


def _simple_profile():
    color = create_graph_node(NodeType.COLOR, title="Base Color", properties={"width": 16, "height": 16})
    profiler = GraphEvaluationProfiler()
    NodeGraphExecutor(profiler=profiler).render_display_node(NodeGraph([color]), color, max_side=16)
    return color, profiler.snapshot(max_side=16)


class GraphProfilingTests(unittest.TestCase):
    def test_nested_costs_do_not_charge_upstream_work_to_downstream_node(self) -> None:
        source = create_graph_node(NodeType.CONSTANT_CHANNEL, properties={"value": 128})
        levels = create_graph_node(NodeType.LEVELS_CHANNEL)
        remap = create_graph_node(NodeType.REMAP_CHANNEL)
        view = create_graph_node(NodeType.VIEW)
        graph = NodeGraph([source, levels, remap, view], [
            _link(source, "out", levels, "in"),
            _link(levels, "out", remap, "in"),
            _link(remap, "out", view, "in"),
        ])
        clock = _Clock()
        profiler = GraphEvaluationProfiler(clock=clock)
        executor = NodeGraphExecutor(profiler=profiler)
        apply_levels, apply_remap = executor._apply_levels, executor._apply_remap

        def slow_levels(channel, node):
            clock.advance(0.007)
            return apply_levels(channel, node)

        def slow_remap(channel, node):
            clock.advance(0.002)
            return apply_remap(channel, node)

        cache = NodeGraphPreviewCache()
        with (
            patch.object(executor, "_apply_levels", side_effect=slow_levels),
            patch.object(executor, "_apply_remap", side_effect=slow_remap),
        ):
            image, _ = executor.render_display_node(graph, view, preview_cache=cache, max_side=64)
        cold = profiler.snapshot(max_side=64)
        timings = {entry.node_id: entry for entry in cold.timings}
        self.assertAlmostEqual(9, cold.total_ms)
        self.assertAlmostEqual(7, timings[levels.node_id].own_ms)
        self.assertAlmostEqual(2, timings[remap.node_id].own_ms)
        self.assertAlmostEqual(9, timings[remap.node_id].branch_ms)
        self.assertAlmostEqual(cold.total_ms, sum(entry.own_ms for entry in cold.timings))
        self.assertEqual((64, 64), timings[levels.node_id].resolution)
        self.assertEqual(1, timings[levels.node_id].cache_misses)

        cached_image, _ = executor.render_display_node(graph, view, preview_cache=cache, max_side=64)
        warm = profiler.snapshot(max_side=64)
        self.assertEqual(image.tobytes(), cached_image.tobytes())
        self.assertAlmostEqual(0, warm.total_ms)
        self.assertEqual(1, sum(entry.cache_hits for entry in warm.timings))
        self.assertEqual(0, sum(entry.cache_misses for entry in warm.timings))
        self.assertNotIn(levels.node_id, {entry.node_id for entry in warm.timings})
        self.assertAlmostEqual(9, cold.total_ms)
        with self.assertRaises(FrozenInstanceError):
            cold.total_ms = 100

    def test_measurement_stack_recovers_after_failure(self) -> None:
        clock = _Clock()
        profiler = GraphEvaluationProfiler(clock=clock)
        with self.assertRaisesRegex(ValueError, "Failed"):
            with profiler.render(NodeGraph(), "root"):
                with profiler.measure("child", "out", (8, 8)):
                    clock.advance(0.003)
                    raise ValueError("Failed")
        self.assertAlmostEqual(3, profiler.snapshot(max_side=8).total_ms)
        with profiler.render(NodeGraph(), "next"):
            clock.advance(0.001)
        self.assertAlmostEqual(1, profiler.snapshot(max_side=8).total_ms)

    def test_independent_renderers_have_independent_profiles(self) -> None:
        first, second = GraphEvaluationProfiler(), GraphEvaluationProfiler()
        color = create_graph_node(NodeType.COLOR, properties={"width": 8, "height": 8})
        NodeGraphExecutor(profiler=first).render_display_node(NodeGraph([color]), color)
        self.assertTrue(first.snapshot(max_side=8).timings)
        self.assertEqual((), second.snapshot(max_side=8).timings)

    def test_image_path_pbr_and_keyword_calls_preserve_result(self) -> None:
        color = create_graph_node(NodeType.COLOR, properties={"width": 16, "height": 8})
        adjust = create_graph_node(NodeType.COLOR_ADJUST, properties={"brightness": 12})
        shader = create_graph_node(NodeType.PBR_SHADER)
        output = create_graph_node(NodeType.OUTPUT_RGBA)
        graph = NodeGraph([color, adjust, shader, output], [
            _link(color, "image", adjust, "image"),
            _link(adjust, "out", shader, "basecolor"),
            _link(adjust, "out", output, "image"),
        ])
        profiler = GraphEvaluationProfiler()
        executor = NodeGraphExecutor(profiler=profiler)
        expected = NodeGraphExecutor().render_pbr_material_node(graph, shader)
        actual = executor.render_pbr_material_node(graph=graph, shader_node=shader)
        for name in ("basecolor", "normal", "properties", "emissive"):
            self.assertEqual(getattr(expected, name).tobytes(), getattr(actual, name).tobytes())
        profile = profiler.snapshot(max_side=2048)
        self.assertEqual({color.node_id, adjust.node_id, shader.node_id}, {entry.node_id for entry in profile.timings})
        self.assertTrue(all(entry.resolution == (16, 8) for entry in profile.timings))
        self.assertGreater(sum(entry.cache_misses for entry in profile.timings), 0)
        self.assertEqual(
            NodeGraphExecutor().render_output_node(graph, output).tobytes(),
            executor.render_output_node(graph=graph, output_node=output).tobytes(),
        )
        self.assertNotIn(shader.node_id, {entry.node_id for entry in profiler.snapshot(max_side=16).timings})

    def test_profile_reports_actual_draft_working_sizes(self) -> None:
        color = create_graph_node(NodeType.COLOR, properties={"width": 512, "height": 256})
        canvas = create_graph_node(NodeType.RESIZE_CANVAS, properties={"width": 256, "height": 256, "resize_mode": "crop"})
        graph = NodeGraph([color, canvas], [_link(color, "image", canvas, "image")])
        profiler = GraphEvaluationProfiler()
        NodeGraphExecutor(profiler=profiler).render_display_node(
            graph=graph, node=canvas, max_side=64,
            preview_cache=NodeGraphPreviewCache(interactive_preview=True),
        )
        entries = profiler.snapshot(max_side=64).timings
        self.assertEqual({(128, 64)}, {entry.resolution for entry in entries if entry.node_id == color.node_id})
        self.assertEqual({(64, 64)}, {entry.resolution for entry in entries if entry.node_id == canvas.node_id})

    def test_same_socket_at_different_sizes_is_not_combined(self) -> None:
        color = create_graph_node(NodeType.COLOR)
        target = create_graph_node(NodeType.VIEW)
        graph = NodeGraph([color, target])
        connection = _link(color, "image", target, "image")
        profiler = GraphEvaluationProfiler()
        executor = NodeGraphExecutor(profiler=profiler)
        cache = NodeGraphPreviewCache()
        with profiler.render(graph, target.node_id):
            for size in ((8, 8), (16, 16), (8, 8)):
                executor._evaluate_image_socket(graph, connection, size, set(), cache)
        entries = {entry.resolution: entry for entry in profiler.snapshot(max_side=16).timings if entry.node_id == color.node_id}
        self.assertEqual({(8, 8), (16, 16)}, set(entries))
        self.assertEqual(2, entries[(8, 8)].calls)
        self.assertEqual(1, entries[(8, 8)].cache_hits)
        self.assertEqual(1, entries[(8, 8)].cache_misses)


class ProfilingUiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = QApplication.instance() or QApplication([])

    def test_worker_emits_profile_before_result_and_omits_canceled_reports(self) -> None:
        color = create_graph_node(NodeType.COLOR, properties={"width": 8, "height": 8})
        events = []
        worker = GraphPreviewWorker(7, NodeGraphProject(graph=NodeGraph([color])), color, max_side=8)
        worker.profile_ready.connect(lambda generation, profile: events.append(("profile", generation, profile)))
        worker.finished.connect(lambda *args: events.append(("result", args[0])))
        worker.run()
        self.assertEqual(["profile", "result"], [event[0] for event in events])
        self.assertEqual(8, events[0][2].max_side)
        events.clear()
        worker.cancel()
        worker.run()
        self.assertEqual([], events)

    def test_failed_worker_does_not_emit_a_success_profile(self) -> None:
        node = create_graph_node(NodeType.TEXTURE_INPUT, properties={"path": "missing-profile-test-file.png"})
        worker = GraphPreviewWorker(1, NodeGraphProject(graph=NodeGraph([node])), node)
        profiles, errors = [], []
        worker.profile_ready.connect(lambda *args: profiles.append(args))
        worker.failed.connect(lambda *args: errors.append(args))
        worker.run()
        self.assertEqual([], profiles)
        self.assertEqual(1, len(errors))

    def test_hidden_panel_defers_table_and_double_click_requests_node(self) -> None:
        color, profile = _simple_profile()
        panel = GraphPerformancePanel()
        try:
            requested = []
            panel.node_requested.connect(requested.append)
            panel.set_profile(profile)
            self.assertEqual(0, panel.table.rowCount())
            self.assertFalse(panel._refresh_timer.isActive())
            panel.show()
            self.app.processEvents()
            self.assertEqual(1, panel.table.rowCount())
            self.assertEqual("Base Color", panel.table.item(0, 0).text())
            self.assertIn("16", panel.table.item(0, 5).text())
            panel._select_node(0, 0)
            self.assertEqual([color.node_id], requested)
            panel.set_profile(profile)
            self.assertTrue(panel._refresh_timer.isActive())
            panel.clear_profile()
            self.assertFalse(panel._refresh_timer.isActive())
            self.assertEqual(0, panel.table.rowCount())
        finally:
            panel.close()
            panel.deleteLater()
            self.app.processEvents()

    def test_workspace_discards_stale_profiles_and_clears_on_scene_change(self) -> None:
        color, profile = _simple_profile()
        workspace = GraphWorkspace()
        try:
            workspace.project.graph.nodes = [color]
            workspace._scene.rebuild()
            workspace._preview_generation = 2
            workspace._preview_inflight_generation = 1
            workspace._on_preview_profile_ready(1, profile)
            self.assertIsNone(workspace.performance_panel.profile)
            workspace._preview_inflight_generation = 2
            workspace._on_preview_profile_ready(2, profile)
            self.assertIs(profile, workspace.performance_panel.profile)
            workspace._select_profile_node(color.node_id)
            self.assertEqual(color.node_id, workspace._scene.selected_node().node_id)
            workspace._reset_preview_for_project_change()
            self.assertIsNone(workspace.performance_panel.profile)
            workspace._on_preview_profile_ready(2, profile)
            self.assertIsNone(workspace.performance_panel.profile)
        finally:
            workspace.shutdown_background_jobs(wait_ms=1000)
            workspace.close()
            workspace.deleteLater()
            self.app.processEvents()

    def test_narrow_panel_keeps_node_names_readable(self) -> None:
        _, profile = _simple_profile()
        panel = GraphPerformancePanel()
        try:
            panel.resize(430, 440)
            panel.set_profile(profile)
            panel.show()
            self.app.processEvents()
            self.assertGreaterEqual(panel.table.columnWidth(0), 150)
            self.assertGreater(panel.table.horizontalScrollBar().maximum(), 0)
        finally:
            panel.close()
            panel.deleteLater()
            self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
