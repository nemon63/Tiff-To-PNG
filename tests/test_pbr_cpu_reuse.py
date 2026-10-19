from __future__ import annotations

from threading import Event
import unittest
from unittest.mock import patch

from PIL import Image, ImageOps

from image_converter.domain.node_graph import GraphConnection, NodeGraph, NodeType, create_graph_node
from image_converter.services.node_graph_executor import (
    GraphExecutionCancelled, NodeGraphExecutor, NodeGraphPreviewCache,
)
from image_converter.services.pbr_preview import PBR_TEXTURE_NAMES


class PbrCpuReuseTests(unittest.TestCase):
    def setUp(self):
        self.shader = create_graph_node(NodeType.PBR_SHADER)
        self.sources = {}
        wires = []
        for name in ("basecolor", "normal", "emissive", "roughness", "metallic", "ao", "opacity"):
            is_image = name in ("basecolor", "normal", "emissive")
            source = create_graph_node(
                NodeType.COLOR if is_image else NodeType.CONSTANT_CHANNEL,
                properties={"width": 32, "height": 32, "red": 50, "value": 80},
            )
            self.sources[name] = source
            wires.append(GraphConnection(name, source.node_id, "image" if is_image else "out", self.shader.node_id, name))
        self.graph = NodeGraph([*self.sources.values(), self.shader], wires)
        self.cache = NodeGraphPreviewCache()

    def render(self, *, executor=None, cache=None, max_side=32):
        return (executor or NodeGraphExecutor()).render_pbr_material_node(
            self.graph, self.shader, preview_cache=self.cache if cache is None else cache, max_side=max_side,
        )

    def edit(self, name, value):
        source = self.sources[name]
        source.properties["red" if source.node_type is NodeType.COLOR else "value"] = value
        self.cache.invalidate_node_and_downstream(self.graph, source.node_id)

    def test_warm_render_does_not_copy_convert_or_merge_images(self):
        previous = self.render()
        with (
            patch.object(Image.Image, "copy", side_effect=AssertionError("Unexpected copy")),
            patch.object(Image.Image, "convert", side_effect=AssertionError("Unexpected conversion")),
            patch.object(Image, "merge", side_effect=AssertionError("Unexpected merge")),
        ):
            current = self.render()
        for name in PBR_TEXTURE_NAMES:
            self.assertIs(getattr(previous, name), getattr(current, name))

    def test_roughness_edit_reuses_color_maps_without_mutating_previous_snapshot(self):
        previous = self.render()
        original_bytes = {name: getattr(previous, name).tobytes() for name in PBR_TEXTURE_NAMES}
        self.edit("roughness", 220)
        with (
            patch.object(Image.Image, "copy", side_effect=AssertionError("Unexpected copy")),
            patch.object(Image.Image, "convert", side_effect=AssertionError("Unexpected conversion")),
        ):
            current = self.render()
        self.assertEqual(220, current.properties.getpixel((0, 0))[0])
        self.assertIsNot(previous.properties, current.properties)
        for name in ("basecolor", "normal", "emissive"):
            self.assertIs(getattr(previous, name), getattr(current, name))
        for name in PBR_TEXTURE_NAMES:
            self.assertEqual(original_bytes[name], getattr(previous, name).tobytes())

    def test_basecolor_edit_reuses_properties_and_does_not_remerge(self):
        previous = self.render()
        self.edit("basecolor", 210)
        with patch.object(Image, "merge", side_effect=AssertionError("Unexpected merge")):
            current = self.render()
        self.assertIsNot(previous.basecolor, current.basecolor)
        self.assertEqual(50, previous.basecolor.getpixel((0, 0))[0])
        self.assertEqual(210, current.basecolor.getpixel((0, 0))[0])
        for name in ("normal", "emissive", "properties"):
            self.assertIs(getattr(previous, name), getattr(current, name))

    def test_packed_maps_are_not_split_or_inverted_again_on_basecolor_edit(self):
        self.shader.properties["workflow"] = "unity_hdrp"
        packed = create_graph_node(NodeType.COLOR, properties={
            "width": 32, "height": 32, "red": 10, "green": 20, "blue": 30, "alpha": 40,
        })
        self.graph.nodes.append(packed)
        self.graph.connections = [c for c in self.graph.connections if c.target_socket_id in ("basecolor", "normal", "emissive")]
        self.graph.connections.append(GraphConnection("packed", packed.node_id, "image", self.shader.node_id, "packed"))
        previous = self.render()
        self.edit("basecolor", 200)
        with (
            patch.object(Image.Image, "split", side_effect=AssertionError("Unexpected split")),
            patch.object(ImageOps, "invert", side_effect=AssertionError("Unexpected invert")),
        ):
            current = self.render()
        self.assertIs(previous.properties, current.properties)
        self.assertEqual((215, 10, 20, 255), current.properties.getpixel((0, 0)))

    def test_default_socket_reads_still_return_mutable_copies(self):
        previous = self.render()
        executor = NodeGraphExecutor()
        executor._prepare_lookup(self.graph)
        for name in ("basecolor", "roughness"):
            wire = next(c for c in self.graph.connections if c.target_socket_id == name)
            if name == "basecolor":
                image = executor._evaluate_image_socket(self.graph, wire, (32, 32), set(), self.cache)
                image.paste((255, 0, 0, 255), (0, 0, 32, 32))
            else:
                image = executor._evaluate_channel_socket_cached(self.graph, wire, (32, 32), set(), self.cache)
                image.paste(255, (0, 0, 32, 32))
        current = self.render()
        self.assertEqual(50, current.basecolor.getpixel((0, 0))[0])
        self.assertEqual(80, current.properties.getpixel((0, 0))[0])
        self.assertEqual(previous.texture_keys, current.texture_keys)

    def test_repeated_edits_replace_prepared_map_instead_of_accumulating_history(self):
        self.render()
        initial_bytes = self.cache.current_bytes
        for value in range(60):
            self.edit("roughness", value)
            self.render()
            self.assertEqual(initial_bytes, self.cache.current_bytes)
        self.assertLessEqual(self.cache.current_bytes, self.cache.max_bytes)

    def test_eviction_and_disabled_cache_preserve_pixels(self):
        self.cache = NodeGraphPreviewCache(max_bytes=5000)
        for name in ("roughness", "basecolor", "metallic", "ao", "normal"):
            self.edit(name, 170)
            actual = self.render()
            expected = self.render(cache=NodeGraphPreviewCache(max_bytes=0))
            self.assertLessEqual(self.cache.current_bytes, 5000)
            for slot in PBR_TEXTURE_NAMES:
                self.assertEqual(getattr(expected, slot).tobytes(), getattr(actual, slot).tobytes())

    def test_prepared_maps_do_not_evict_a_full_upstream_working_set(self):
        self.cache = NodeGraphPreviewCache(max_bytes=16 * 32 * 32)
        first = self.render()
        second = self.render()
        self.assertEqual(first.texture_keys, second.texture_keys)
        self.assertEqual(self.cache.max_bytes, self.cache.current_bytes)
        for wire in self.graph.connections:
            bucket = "image" if wire.target_socket_id in ("basecolor", "normal", "emissive") else "channel"
            self.assertIsNotNone(self.cache.get_image(bucket, (wire.source_node_id, wire.source_socket_id, (32, 32))))

    def test_regular_cache_inserts_evict_prepared_maps_first(self):
        cache = NodeGraphPreviewCache(max_bytes=10)
        upstream = Image.new("L", (6, 1))
        cache.put_image("channel", "upstream", upstream)
        cache.put_image("pbr-map", "prepared", Image.new("L", (4, 1)))
        cache.put_image("channel", "new", Image.new("L", (2, 1)))
        self.assertIs(upstream, cache.get_image("channel", "upstream"))
        self.assertIsNone(cache.get_image("pbr-map", "prepared"))
        cache.put_image("pbr-map", "too-large", Image.new("L", (8, 1)))
        self.assertIsNone(cache.get_image("pbr-map", "too-large"))
        self.assertIs(upstream, cache.get_image("channel", "upstream"))
        self.assertEqual(8, cache.current_bytes)

    def test_disconnecting_maps_reuses_fallbacks_and_matches_fresh_render(self):
        previous = self.render()
        self.graph.connections = [c for c in self.graph.connections if c.target_socket_id == "basecolor"]
        self.cache.invalidate_node_and_downstream(self.graph, self.shader.node_id)
        disconnected = self.render()
        expected = self.render(cache=NodeGraphPreviewCache(max_bytes=0))
        self.assertIs(previous.basecolor, disconnected.basecolor)
        for slot in PBR_TEXTURE_NAMES:
            self.assertEqual(getattr(expected, slot).tobytes(), getattr(disconnected, slot).tobytes())
        self.assertEqual((128, 128, 255), disconnected.normal.getpixel((0, 0)))
        self.assertEqual((128, 0, 255, 255), disconnected.properties.getpixel((0, 0)))
        warm = self.render()
        for slot in PBR_TEXTURE_NAMES:
            self.assertIs(getattr(warm, slot), getattr(disconnected, slot))

    def test_clear_and_resolution_switch_do_not_reuse_incorrect_maps(self):
        original = self.render()
        draft = self.render(max_side=16)
        full = self.render()
        self.assertEqual((16, 16), draft.normal.size)
        for name in PBR_TEXTURE_NAMES:
            self.assertIs(getattr(original, name), getattr(full, name))
        self.cache.clear()
        refreshed = self.render()
        for name in PBR_TEXTURE_NAMES:
            self.assertIsNot(getattr(original, name), getattr(refreshed, name))
            self.assertEqual(getattr(original, name).tobytes(), getattr(refreshed, name).tobytes())

    def test_cancellation_during_assembly_does_not_publish_partial_result(self):
        previous = self.render()
        self.edit("roughness", 190)
        canceled = Event()
        original_merge = Image.merge

        def merge_then_cancel(*args, **kwargs):
            image = original_merge(*args, **kwargs)
            canceled.set()
            return image

        with patch.object(Image, "merge", side_effect=merge_then_cancel):
            with self.assertRaises(GraphExecutionCancelled):
                self.render(executor=NodeGraphExecutor(cancel_requested=canceled.is_set))
        self.assertEqual(80, previous.properties.getpixel((0, 0))[0])
        current = self.render()
        self.assertEqual(190, current.properties.getpixel((0, 0))[0])
        self.assertIsNot(previous.properties, current.properties)


if __name__ == "__main__":
    unittest.main()
