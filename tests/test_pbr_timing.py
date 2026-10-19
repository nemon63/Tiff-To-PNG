from __future__ import annotations

from dataclasses import replace
import unittest
from unittest.mock import Mock, patch

from PIL import Image
from PyQt6.QtWidgets import QApplication

from image_converter.services.pbr_preview import PbrMaterialData
from image_converter.ui.pbr_gl_widget import PbrOpenGLWidget
from image_converter.ui.pbr_preview import PbrPreviewPanel
from image_converter.ui.pbr_timing import GpuDrawTimer, PbrPreviewTiming, supports_gpu_timer


class _Clock:
    def __init__(self):
        self.seconds = 0.0

    def __call__(self):
        return self.seconds

    def advance(self, value):
        self.seconds += value


class _Query:
    def __init__(self):
        self.available = False
        self.create = Mock(return_value=True)
        self.begin = Mock()
        self.end = Mock()
        self.destroy = Mock()
        self.reads = 0

    def isResultAvailable(self):
        return self.available

    def waitForResult(self):
        if not self.available:
            raise AssertionError("Blocking read attempted")
        self.reads += 1
        return 3_500_000


def _material():
    return PbrMaterialData(
        Image.new("RGBA", (8, 8)), Image.new("RGB", (8, 8)),
        Image.new("RGBA", (8, 8)), Image.new("RGB", (8, 8)), False, (),
    )


class GpuDrawTimerTests(unittest.TestCase):
    def setUp(self):
        self.clock = _Clock()
        self.query = _Query()
        self.factory = Mock(return_value=self.query)
        self.timer = GpuDrawTimer(
            query_factory=self.factory, dispose_query=lambda query: query.destroy(), clock=self.clock,
        )
        self.timer.supported = True

    def test_results_are_only_read_when_available_and_query_is_reused(self):
        self.assertTrue(self.timer.begin((1, 1)))
        self.timer.end()
        self.assertIsNone(self.timer.poll())
        self.assertEqual(0, self.query.reads)
        self.assertFalse(self.timer.begin((1, 2)))
        self.query.available = True
        self.assertEqual(((1, 1), 3.5), self.timer.poll())
        self.assertEqual(1, self.query.reads)
        self.assertTrue(self.timer.begin((1, 2)))
        self.timer.end()
        self.assertEqual(((1, 2), 3.5), self.timer.poll())
        self.factory.assert_called_once_with()

    def test_timeout_disables_query_without_reading_or_waiting(self):
        self.timer.begin("sample")
        self.timer.end()
        self.clock.advance(3)
        self.assertIsNone(self.timer.poll())
        self.assertFalse(self.timer.pending)
        self.assertFalse(self.timer.supported)
        self.assertIn("timed out", self.timer.status)
        self.assertEqual(0, self.query.reads)
        self.query.destroy.assert_called_once_with()

    def test_unavailable_query_does_not_affect_rendering(self):
        self.query.create.return_value = False
        self.assertFalse(self.timer.begin("sample"))
        self.assertFalse(self.timer.pending)
        self.assertFalse(self.timer.supported)
        self.query.begin.assert_not_called()
        self.assertIsNone(self.timer.poll())

    def test_unsupported_context_never_allocates_query(self):
        self.timer.supported = False
        self.assertFalse(self.timer.begin("sample"))
        self.factory.assert_not_called()

    def test_release_drops_pending_results_without_reading(self):
        self.timer.begin("old-context")
        self.timer.end()
        self.timer.release()
        self.timer.release()  # Explicit shutdown and context destruction may both run.
        self.assertFalse(self.timer.pending)
        self.assertIsNone(self.timer.poll())
        self.assertEqual(0, self.query.reads)
        self.query.destroy.assert_called_once_with()

    def test_capability_checks_cover_desktop_extensions_and_es(self):
        context = Mock()
        context.isOpenGLES.return_value = False
        context.format.return_value.majorVersion.return_value = 2
        context.format.return_value.minorVersion.return_value = 0
        context.hasExtension.return_value = False
        self.assertFalse(supports_gpu_timer(context))
        context.hasExtension.side_effect = lambda name: name == b"GL_EXT_timer_query"
        self.assertTrue(supports_gpu_timer(context))
        context.format.return_value.majorVersion.return_value = 3
        context.format.return_value.minorVersion.return_value = 3
        context.hasExtension.side_effect = None
        context.hasExtension.return_value = False
        self.assertTrue(supports_gpu_timer(context))
        context.isOpenGLES.return_value = True
        self.assertFalse(supports_gpu_timer(context))
        self.assertFalse(supports_gpu_timer(None))


class PbrTimingWidgetTests(unittest.TestCase):
    def setUp(self):
        self.app = QApplication.instance() or QApplication([])
        self.widget = PbrOpenGLWidget()
        self.clock, self.query = _Clock(), _Query()
        self.widget._gpu_timer = GpuDrawTimer(
            query_factory=lambda: self.query, dispose_query=lambda query: query.destroy(), clock=self.clock,
        )
        self.widget._gpu_timer.supported = True
        self.widget._functions = Mock()
        self.widget._functions.glDrawArrays.side_effect = lambda *_: self.clock.advance(0.002)
        self.widget._program = Mock()
        self.widget._vertex_buffer = Mock()
        self.widget._textures = [Mock() for _ in range(4)]
        self.widget.set_material(_material())

        def upload():
            self.clock.advance(0.007)
            self.widget._uploaded_material = self.widget._material
            return ("properties",)

        self.upload_patch = patch.object(self.widget, "_upload_material", side_effect=upload)
        self.upload = self.upload_patch.start()
        self.clock_patch = patch("image_converter.ui.pbr_gl_widget.perf_counter", self.clock)
        self.clock_patch.start()

    def tearDown(self):
        self.widget._gpu_poll_timer.stop()
        self.widget._timing_sample_timer.stop()
        self.widget._gpu_timer.release()
        self.upload_patch.stop()
        self.clock_patch.stop()
        self.widget._functions = self.widget._program = self.widget._vertex_buffer = None
        self.widget._textures.clear()
        self.widget.close()
        self.widget.deleteLater()
        self.app.processEvents()

    def test_disabled_by_default_and_does_not_issue_gpu_queries(self):
        self.widget.paintGL()
        self.assertFalse(self.widget._timing_enabled)
        self.assertIsNone(self.widget._timing)
        self.query.create.assert_not_called()

    def test_cpu_upload_and_gpu_draw_are_separate_and_last_upload_survives_rotation(self):
        self.widget.set_timing_enabled(True)
        self.widget.paintGL()
        timing = self.widget._timing
        self.assertAlmostEqual(7, timing.upload_cpu_ms)
        self.assertAlmostEqual(2, timing.draw_cpu_ms)
        self.assertEqual(("properties",), timing.uploaded_maps)
        self.assertIsNone(timing.draw_gpu_ms)
        self.assertEqual("GPU result pending", timing.gpu_status)
        self.query.available = True
        self.widget._collect_gpu_timing()
        self.assertEqual(3.5, self.widget._timing.draw_gpu_ms)
        self.assertFalse(self.widget._gpu_poll_timer.isActive())
        self.clock.advance(0.2)
        self.widget.set_object_rotation(30, 0)
        self.widget.paintGL()
        self.assertAlmostEqual(7, self.widget._timing.upload_cpu_ms)
        self.upload.assert_called_once_with()
        self.widget._functions.glFinish.assert_not_called()

    def test_scene_change_drops_previous_query_result(self):
        self.widget.set_timing_enabled(True)
        self.widget.paintGL()
        serial = self.widget._material_serial
        self.widget.set_material(_material())
        self.assertGreater(self.widget._material_serial, serial)
        self.assertIsNone(self.widget._timing.draw_cpu_ms)
        self.query.available = True
        self.widget._collect_gpu_timing()
        self.assertIsNone(self.widget._timing.draw_gpu_ms)
        self.clock.advance(0.11)
        self.widget.paintGL()
        self.assertIsNotNone(self.widget._timing.draw_cpu_ms)
        self.widget.set_material(None)
        self.widget._collect_gpu_timing()
        self.assertIsNone(self.widget._timing)

    def test_unsupported_gpu_still_reports_cpu(self):
        self.widget._gpu_timer.supported = False
        self.widget.set_timing_enabled(True)
        self.widget.paintGL()
        self.assertAlmostEqual(2, self.widget._timing.draw_cpu_ms)
        self.assertIsNone(self.widget._timing.draw_gpu_ms)
        self.assertIn("unavailable", self.widget._timing.gpu_status)
        self.assertFalse(self.widget._gpu_poll_timer.isActive())

    def test_draw_queries_are_throttled_and_completion_does_not_redraw(self):
        self.widget.set_timing_enabled(True)
        self.widget.paintGL()
        self.query.available = True
        with patch.object(self.widget, "update") as update:
            self.widget._collect_gpu_timing()
            update.assert_not_called()
        self.widget.paintGL()
        self.query.begin.assert_called_once_with()
        self.clock.advance(0.11)
        self.widget.paintGL()
        self.assertEqual(2, self.query.begin.call_count)

    def test_hidden_widget_stops_polling_without_reading_result(self):
        self.widget.set_timing_enabled(True)
        self.widget.paintGL()
        self.query.available = True
        with (
            patch.object(self.widget, "isValid", return_value=True),
            patch.object(self.widget, "isVisible", return_value=False),
        ):
            self.widget._poll_gpu_timing()
        self.assertEqual(0, self.query.reads)
        self.assertFalse(self.widget._gpu_poll_timer.isActive())
        self.assertTrue(self.widget._gpu_timer.pending)

    def test_sampling_limit_survives_material_changes_and_defers_final_sample(self):
        self.widget.set_timing_enabled(True)
        self.widget.paintGL()
        self.query.available = True
        self.widget._collect_gpu_timing()
        self.widget.set_material(_material())
        self.widget.paintGL()
        self.query.begin.assert_called_once_with()
        self.assertIsNone(self.widget._timing.draw_cpu_ms)
        self.assertTrue(self.widget._timing_sample_timer.isActive())
        self.clock.advance(0.11)
        self.widget._timing_sample_timer.stop()  # Simulate its single-shot timeout.
        self.widget.paintGL()
        self.assertEqual(2, self.query.begin.call_count)
        self.assertIsNotNone(self.widget._timing.draw_cpu_ms)
        self.widget.set_material(None)
        self.assertFalse(self.widget._timing_sample_timer.isActive())

    def test_timeout_reports_unavailability_and_stops_polling(self):
        self.widget.set_timing_enabled(True)
        self.widget.paintGL()
        self.clock.advance(3)
        self.widget._collect_gpu_timing()
        self.assertIn("timed out", self.widget._timing.gpu_status)
        self.assertIsNone(self.widget._timing.draw_gpu_ms)
        self.assertFalse(self.widget._gpu_poll_timer.isActive())
        self.assertEqual(0, self.query.reads)

    def test_cleanup_stops_polling_and_resets_measurements(self):
        self.widget.set_timing_enabled(True)
        self.widget.paintGL()
        with (
            patch.object(self.widget, "isValid", return_value=True),
            patch.object(self.widget, "makeCurrent"), patch.object(self.widget, "doneCurrent"),
        ):
            self.widget.cleanup()
        self.assertFalse(self.widget._gpu_poll_timer.isActive())
        self.assertIsNone(self.widget._timing.upload_cpu_ms)
        self.query.destroy.assert_called_once_with()


class PbrTimingPanelTests(unittest.TestCase):
    def setUp(self):
        self.app = QApplication.instance() or QApplication([])
        self.panel = PbrPreviewPanel()

    def tearDown(self):
        self.panel.shutdown_background_jobs(wait_ms=1000)
        self.panel.close()
        self.panel.deleteLater()
        self.app.processEvents()

    def test_opt_in_hidden_updates_and_label_precision(self):
        self.assertFalse(self.panel.timings_button.isChecked())
        self.assertTrue(self.panel.timing_label.isHidden())
        self.panel.timings_button.setChecked(True)
        self.panel.gl_preview.set_material(_material())
        timing = PbrPreviewTiming(
            self.panel.gl_preview._material_serial, (8, 8), 12.25, ("properties",),
            0.4, 0.75, (400, 400), "GPU timer ready",
        )
        self.panel._on_timing_ready(timing)
        self.assertFalse(self.panel._timing_ui_timer.isActive())
        self.panel.gl_preview.hide()  # UI-only test, no native GL context required.
        self.panel.show()
        self.app.processEvents()
        self.assertIn("Upload CPU: 12.25 ms", self.panel.timing_label.text())
        self.assertIn("1/4 maps", self.panel.timing_label.text())
        self.assertIn("GPU 0.75 ms", self.panel.timing_label.text())
        self.assertIn("not GPU execution", self.panel.timing_label.toolTip())
        self.panel._on_timing_ready(replace(timing, draw_gpu_ms=1.2))
        self.assertTrue(self.panel._timing_ui_timer.isActive())
        self.panel.gl_preview.set_material(None)
        self.assertFalse(self.panel._timing_ui_timer.isActive())
        self.assertIsNone(self.panel._latest_timing)
        self.assertIn("No material", self.panel.timing_label.text())
        self.panel._on_timing_ready(timing)
        self.assertIsNone(self.panel._latest_timing)
        self.panel.timings_button.setChecked(False)
        self.assertTrue(self.panel.timing_label.isHidden())


if __name__ == "__main__":
    unittest.main()
