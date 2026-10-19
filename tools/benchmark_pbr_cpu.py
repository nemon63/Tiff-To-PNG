"""Benchmark warm PBR CPU assembly, excluding file decode, GPU and UI.

Run from the repository root: python -m tools.benchmark_pbr_cpu
"""
from __future__ import annotations

import argparse
import json
import platform
from statistics import median
from time import perf_counter
from unittest.mock import patch

from PIL import Image, __version__ as pillow_version

from image_converter.domain.node_graph import GraphConnection, NodeGraph, NodeType, create_graph_node
from image_converter.services.graph_profiling import GraphEvaluationProfiler
from image_converter.services.node_graph_executor import NodeGraphExecutor, NodeGraphPreviewCache


def measure(size: int, iterations: int) -> dict:
    shader = create_graph_node(NodeType.PBR_SHADER)
    nodes = {}
    wires = []
    for name in ("basecolor", "normal", "emissive", "roughness", "metallic", "ao", "opacity"):
        is_image = name in ("basecolor", "normal", "emissive")
        node = create_graph_node(
            NodeType.COLOR if is_image else NodeType.CONSTANT_CHANNEL,
            properties={"width": size, "height": size, "value": 80},
        )
        nodes[name] = node
        wires.append(GraphConnection(name, node.node_id, "image" if is_image else "out", shader.node_id, name))
    graph = NodeGraph([*nodes.values(), shader], wires)
    results = {"size": size, "iterations": iterations}
    for scenario in ("warm", "roughness_edit", "basecolor_edit"):
        cache = NodeGraphPreviewCache()
        profiler = GraphEvaluationProfiler()

        def render(index):
            if scenario != "warm":
                name = scenario.removesuffix("_edit")
                node = nodes[name]
                node.properties["red" if name == "basecolor" else "value"] = index % 256
                cache.invalidate_node_and_downstream(graph, node.node_id)
            return NodeGraphExecutor(profiler=profiler).render_pbr_material_node(
                graph, shader, preview_cache=cache, max_side=size,
            )

        elapsed_samples, assembly_samples = [], []
        for index in range(iterations + 3):
            start = perf_counter()
            material = render(index)
            elapsed = (perf_counter() - start) * 1000
            if index >= 3:
                elapsed_samples.append(elapsed)
                assembly_samples.append(sum(
                    entry.own_ms for entry in profiler.snapshot(max_side=size).timings
                    if entry.node_id == shader.node_id
                ))
        counts = {"copy": 0, "convert": 0, "merge": 0}

        def counted(name, method):
            def call(*args, **kwargs):
                counts[name] += 1
                return method(*args, **kwargs)
            return call

        # Count in a separate iteration; instrumentation overhead is not timed.
        with (
            patch.object(Image.Image, "copy", counted("copy", Image.Image.copy)),
            patch.object(Image.Image, "convert", counted("convert", Image.Image.convert)),
            patch.object(Image, "merge", counted("merge", Image.merge)),
        ):
            material = render(iterations + 3)
        assert material.size == (size, size)
        results[scenario] = {
            "total_ms": round(median(elapsed_samples), 3),
            "shader_self_ms": round(median(assembly_samples), 3),
            "image_calls": counts,
            "cache_mib": round(cache.current_bytes / 1024**2, 2),
        }
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", type=int, nargs="+", default=[256, 1024, 2048])
    parser.add_argument("--iterations", type=int, default=15)
    args = parser.parse_args()
    if args.iterations < 1 or any(size < 16 or size > 4096 for size in args.sizes):
        parser.error("Use sizes 16..4096 and at least one iteration.")
    print(json.dumps({
        "python": platform.python_version(), "pillow": pillow_version,
        "scope": "synthetic separate maps; CPU render plus invalidation; excludes GPU and UI",
        "results": [measure(size, args.iterations) for size in args.sizes],
    }, indent=2))


if __name__ == "__main__":
    main()
