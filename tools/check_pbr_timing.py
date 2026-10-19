"""Native OpenGL timing/lifecycle smoke test; run from the repository root.

python -X faulthandler -m tools.check_pbr_timing
Uses an invisible widget with a real driver; no settings or user assets change.
Unlike the upload benchmark, this check never calls glFinish or waits for a query.
"""
from __future__ import annotations

from dataclasses import asdict, replace
import json
from time import perf_counter

from PIL import Image
from PyQt6.QtCore import QEventLoop, QTimer, QT_VERSION_STR, Qt
from PyQt6.QtWidgets import QApplication

from image_converter.services.pbr_preview import PBR_TEXTURE_NAMES, PbrMaterialData
from image_converter.ui.pbr_gl_widget import PbrOpenGLWidget


def wait_until(predicate, *, timeout_ms=3000):
    loop = QEventLoop()
    poll = QTimer()
    poll.setInterval(10)
    poll.timeout.connect(lambda: loop.quit() if predicate() else None)
    deadline = QTimer()
    deadline.setSingleShot(True)
    deadline.timeout.connect(loop.quit)
    poll.start()
    deadline.start(timeout_ms)
    if not predicate():
        loop.exec()
    poll.stop()
    deadline.stop()
    if not predicate():
        raise AssertionError("Native preview did not reach the expected state before timeout.")


def main():
    app = QApplication.instance() or QApplication([])
    widget = PbrOpenGLWidget()
    widget.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
    widget.resize(256, 256)
    material = PbrMaterialData(
        Image.new("RGBA", (64, 64), (180, 100, 40, 255)),
        Image.new("RGB", (64, 64), (128, 128, 255)),
        Image.new("RGBA", (64, 64), (255, 120, 0, 255)),
        Image.new("RGB", (64, 64)), False, (), texture_keys=(1, 1, 1, 1),
    )
    try:
        widget.set_timing_enabled(True)
        widget.set_material(material)
        widget.show()
        app.processEvents()
        if not widget.isValid() or widget._initialization_error:
            raise RuntimeError(widget._initialization_error or "Native OpenGL widget is unavailable.")
        supported = widget._gpu_timer.supported

        def sampled():
            timing = widget._timing
            return timing is not None and timing.draw_cpu_ms is not None and (
                not supported or timing.draw_gpu_ms is not None
            )

        wait_until(sampled)
        initial = widget._timing
        assert initial.uploaded_maps == PBR_TEXTURE_NAMES
        previous_ids = tuple(texture.textureId() for texture in widget._textures)
        material = replace(
            material, properties=Image.new("RGBA", (64, 64), (255, 180, 0, 255)),
            texture_keys=(1, 1, 2, 1),
        )
        widget.set_material(material)
        wait_until(sampled)
        incremental = widget._timing
        assert incremental.uploaded_maps == ("properties",)
        current_ids = tuple(texture.textureId() for texture in widget._textures)
        assert {i for i in range(4) if previous_ids[i] != current_ids[i]} == {2}

        # An explicit query.destroy() followed by wrapper deletion crashes in
        # Qt 6.11. Exercise the real C++ destructor, not only a fake query.
        for _ in range(5):
            widget.set_timing_enabled(False)
            assert widget._gpu_timer._query is None
            assert widget._timing is None
            widget.set_timing_enabled(True)
            wait_until(sampled)

        # Publish another scene while the old draw query is still pending.
        widget.set_timing_enabled(False)
        widget.set_timing_enabled(True)
        widget.makeCurrent()
        widget.paintGL()
        widget.doneCurrent()
        assert not supported or widget._gpu_timer.pending
        old_serial = widget._material_serial
        widget.set_material(replace(material, normal_is_directx=True))
        assert widget._material_serial != old_serial
        assert widget._timing.draw_gpu_ms is None
        wait_until(sampled)
        assert widget._timing.uploaded_maps == ()
        widget.set_material(None)
        wait_until(lambda: widget._timing is None and not widget._textures)
        assert widget._timing is None and not widget._textures
        assert not widget._gpu_poll_timer.isActive()

        widget.set_material(material)
        wait_until(sampled)
        widget.makeCurrent()
        renderer = widget._functions.glGetString(0x1F01)
        widget._last_timing_sample = float("-inf")
        widget.paintGL()  # Cleanup with a pending query must not read its result.
        widget.doneCurrent()
        assert not supported or widget._gpu_timer.pending
        started = perf_counter()
        widget.cleanup()
        cleanup_ms = (perf_counter() - started) * 1000
        assert widget._gpu_timer._query is None
        assert not widget._gpu_poll_timer.isActive()
        print(json.dumps({
            "qt": QT_VERSION_STR, "renderer": renderer, "gpu_timer_supported": supported,
            "initial": asdict(initial), "incremental": asdict(incremental),
            "toggle_cycles": 5, "pending_cleanup_cpu_ms": cleanup_ms,
            "scope": "CPU upload/submission and GPU draw only; not FPS or GPU upload duration",
            "smoke_passed": True,
        }, indent=2))
    finally:
        widget.cleanup()
        widget.close()
        widget.deleteLater()
        app.processEvents()


if __name__ == "__main__":
    main()
