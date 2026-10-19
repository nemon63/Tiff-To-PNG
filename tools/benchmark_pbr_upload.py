"""Real-driver upload smoke test/benchmark; run from the repository root.

python -m tools.benchmark_pbr_upload --sizes 256 1024 --iterations 15
Uses an invisible native OpenGL surface. No application settings or assets change.
GPU synchronization is intentional here, never in the interactive renderer.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
import json
import math
import platform
from statistics import median
from time import perf_counter

from PIL import __version__ as pillow_version
from PyQt6.QtCore import QT_VERSION_STR
from PyQt6.QtGui import QOffscreenSurface, QOpenGLContext, QSurfaceFormat
from PyQt6.QtOpenGL import QOpenGLFunctions_2_0
from PyQt6.QtWidgets import QApplication

from image_converter.domain.node_graph import GraphConnection, NodeGraph, NodeType, create_graph_node
from image_converter.services.node_graph_executor import NodeGraphExecutor, NodeGraphPreviewCache
from image_converter.ui.pbr_gl_widget import PbrOpenGLWidget


def measure(size, iterations, functions):
    shader = create_graph_node(NodeType.PBR_SHADER)
    nodes = [shader]
    connections = []
    roughness = create_graph_node(NodeType.CONSTANT_CHANNEL, properties={"value": 64})
    for name in ("basecolor", "normal", "emissive", "roughness"):
        source = roughness if name == "roughness" else create_graph_node(
            NodeType.COLOR, properties={"width": size, "height": size},
        )
        nodes.append(source)
        connections.append(GraphConnection(
            name, source.node_id, "out" if name == "roughness" else "image", shader.node_id, name,
        ))
    graph = NodeGraph(nodes, connections)
    cache = NodeGraphPreviewCache()
    widget = PbrOpenGLWidget()
    results = {}
    try:
        for incremental in (False, True):
            widget.set_material(None)
            widget._upload_material()
            samples = []
            for index in range(iterations + 3):
                roughness.properties["value"] = (index * 13) % 256
                cache.invalidate_node_and_downstream(graph, roughness.node_id)
                material = NodeGraphExecutor().render_pbr_material_node(
                    graph, shader, preview_cache=cache, max_side=size,
                )
                if not incremental:
                    material = replace(material, texture_keys=None)
                previous_ids = tuple(texture.textureId() for texture in widget._textures)
                functions.glFinish()
                started = perf_counter()
                widget.set_material(material)
                widget._upload_material()
                functions.glFinish()
                elapsed_ms = (perf_counter() - started) * 1000
                if index >= 3:
                    samples.append(elapsed_ms)
                current_ids = tuple(texture.textureId() for texture in widget._textures)
                if previous_ids:
                    changed = {i for i in range(4) if previous_ids[i] != current_ids[i]}
                    assert changed == ({2} if incremental else {0, 1, 2, 3}), changed
                assert functions.glGetError() == 0, "OpenGL upload error"
                assert all(texture.isStorageAllocated() for texture in widget._textures)
                assert all(texture.mipLevels() == 1 + math.floor(math.log2(size)) for texture in widget._textures)
            results["incremental_ms" if incremental else "full_upload_ms"] = round(median(samples), 3)

        previous_ids = tuple(texture.textureId() for texture in widget._textures)
        widget.set_material(replace(material, normal_is_directx=True))
        widget._upload_material()
        assert previous_ids == tuple(texture.textureId() for texture in widget._textures)
        assert widget._uses_directx_normal()
        results["size"] = size
        results["iterations"] = iterations
        results["upload_smoke_passed"] = True
        return results
    finally:
        widget._destroy_textures()
        widget.close()
        widget.deleteLater()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", type=int, nargs="+", default=[256, 1024])
    parser.add_argument("--iterations", type=int, default=15)
    args = parser.parse_args()
    if args.iterations < 1 or any(size < 16 or size > 4096 for size in args.sizes):
        parser.error("Use sizes 16..4096 and at least one iteration.")

    app = QApplication.instance() or QApplication([])
    surface_format = QSurfaceFormat()
    surface_format.setVersion(2, 0)
    context = QOpenGLContext()
    context.setFormat(surface_format)
    if not context.create():
        raise RuntimeError("Could not create a native OpenGL context.")
    surface = QOffscreenSurface()
    surface.setFormat(context.format())
    surface.create()
    if not context.makeCurrent(surface):
        raise RuntimeError("Could not make the OpenGL context current.")
    functions = QOpenGLFunctions_2_0()
    if not functions.initializeOpenGLFunctions():
        raise RuntimeError("OpenGL 2.0 functions unavailable.")
    try:
        report = {
            "python": platform.python_version(), "qt": QT_VERSION_STR, "pillow": pillow_version,
            "renderer": functions.glGetString(0x1F01),
            "scope": "PIL-to-QImage + texture creation + mipmaps + glFinish; excludes graph evaluation and draw",
            "results": [measure(size, args.iterations, functions) for size in args.sizes],
        }
        print(json.dumps(report, indent=2))
    finally:
        context.doneCurrent()
        surface.destroy()
        app.processEvents()


if __name__ == "__main__":
    main()
