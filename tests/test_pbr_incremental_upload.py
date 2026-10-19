from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from PIL import Image
from PyQt6.QtWidgets import QApplication

from image_converter.domain.node_graph import (
    GraphConnection, NodeGraph, NodeType, PbrWorkflow, create_graph_node,
)
from image_converter.services.node_graph_executor import NodeGraphExecutor, NodeGraphPreviewCache
from image_converter.services.pbr_preview import PBR_TEXTURE_NAMES
from image_converter.ui.pbr_gl_widget import PbrOpenGLWidget


def _link(source, source_socket, target, target_socket):
    return GraphConnection(
        f"{source.node_id}-{target_socket}", source.node_id, source_socket,
        target.node_id, target_socket,
    )


def _material_graph():
    shader = create_graph_node(NodeType.PBR_SHADER)
    sources = {}
    connections = []
    for name in ("basecolor", "normal", "emissive", "roughness", "metallic", "ao", "opacity"):
        is_image = name in ("basecolor", "normal", "emissive")
        sources[name] = create_graph_node(
            NodeType.COLOR if is_image else NodeType.CONSTANT_CHANNEL,
            title=name, properties={"width": 32, "height": 32, "red": 42, "value": 64},
        )
        connections.append(_link(sources[name], "image" if is_image else "out", shader, name))
    return NodeGraph([*sources.values(), shader], connections), shader, sources


def _changed_slots(before, after):
    return {
        name for index, name in enumerate(PBR_TEXTURE_NAMES)
        if before.texture_keys[index] != after.texture_keys[index]
    }


class MaterialRevisionTests(unittest.TestCase):
    def setUp(self):
        self.graph, self.shader, self.sources = _material_graph()
        self.cache = NodeGraphPreviewCache()

    def render(self, **kwargs):
        return NodeGraphExecutor().render_pbr_material_node(
            self.graph, self.shader, preview_cache=self.cache, **kwargs,
        )

    def test_warm_material_retains_all_keys(self):
        first, second = self.render(), self.render()
        self.assertEqual(set(), _changed_slots(first, second))
        for name in PBR_TEXTURE_NAMES:
            self.assertIs(getattr(first, name), getattr(second, name))
            self.assertEqual(getattr(first, name).tobytes(), getattr(second, name).tobytes())

    def test_each_branch_changes_only_its_material_slot(self):
        previous = self.render()
        for name, source in self.sources.items():
            with self.subTest(name=name):
                property_name = "red" if source.node_type is NodeType.COLOR else "value"
                source.properties[property_name] = 220
                self.cache.invalidate_node_and_downstream(self.graph, source.node_id)
                current = self.render()
                expected = name if name in ("basecolor", "normal", "emissive") else "properties"
                self.assertEqual({expected}, _changed_slots(previous, current))
                self.assertNotEqual(getattr(previous, expected).tobytes(), getattr(current, expected).tobytes())
                previous = current

    def test_disconnect_and_reconnect_replace_only_affected_slot(self):
        for name in self.sources:
            with self.subTest(name=name):
                before = self.render()
                wire = next(c for c in self.graph.connections if c.target_socket_id == name)
                self.graph.connections.remove(wire)
                self.cache.invalidate_node_and_downstream(self.graph, self.shader.node_id)
                disconnected = self.render()
                expected = name if name in ("basecolor", "normal", "emissive") else "properties"
                self.assertEqual({expected}, _changed_slots(before, disconnected))
                self.graph.connections.append(wire)
                reconnected = self.render()
                self.assertEqual({expected}, _changed_slots(disconnected, reconnected))
                self.assertEqual(before.texture_keys, reconnected.texture_keys)

    def test_normal_convention_changes_uniform_not_texture_keys(self):
        before = self.render()
        self.shader.properties["normal_convention"] = "directx"
        self.cache.invalidate_node_and_downstream(self.graph, self.shader.node_id)
        after = self.render()
        self.assertFalse(before.normal_is_directx)
        self.assertTrue(after.normal_is_directx)
        self.assertEqual(before.texture_keys, after.texture_keys)

    def test_resolution_change_and_new_scene_cannot_reuse_old_keys(self):
        before = self.render(max_side=32)
        draft = self.render(max_side=16)
        self.assertEqual(set(PBR_TEXTURE_NAMES), _changed_slots(before, draft))
        self.cache = NodeGraphPreviewCache()
        new_scene = self.render(max_side=32)
        self.assertEqual(set(PBR_TEXTURE_NAMES), _changed_slots(before, new_scene))
        self.cache.clear()
        self.assertEqual(set(PBR_TEXTURE_NAMES), _changed_slots(new_scene, self.render(max_side=32)))

    def test_missing_cache_entries_force_safe_reupload(self):
        self.cache = NodeGraphPreviewCache(max_bytes=0)
        before, after = self.render(), self.render()
        self.assertEqual(set(PBR_TEXTURE_NAMES), _changed_slots(before, after))
        self.assertEqual(0, self.cache.current_bytes)

    def test_reloaded_file_at_same_path_changes_key(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "texture.png"
            Image.new("RGBA", (32, 32), "red").save(path)
            source = self.sources["basecolor"]
            source.node_type = NodeType.TEXTURE_INPUT
            source.properties = {"path": str(path)}
            before = self.render()
            Image.new("RGBA", (32, 32), "blue").save(path)
            self.cache.invalidate_node_and_downstream(self.graph, source.node_id)
            after = self.render()
            self.assertEqual({"basecolor"}, _changed_slots(before, after))
            self.assertEqual((0, 0, 255, 255), after.basecolor.getpixel((0, 0)))

    def test_packed_workflow_and_overrides_have_distinct_recipes(self):
        packed = create_graph_node(NodeType.COLOR, properties={
            "width": 32, "height": 32, "red": 10, "green": 20, "blue": 30, "alpha": 40,
        })
        self.graph.nodes.append(packed)
        self.graph.connections = [c for c in self.graph.connections if c.target_socket_id in ("basecolor", "normal", "emissive")]
        self.graph.connections.append(_link(packed, "image", self.shader, "packed"))
        previous = None
        for workflow, expected in (
            (PbrWorkflow.UNITY_URP, (215, 10, 255, 255)),
            (PbrWorkflow.UNITY_HDRP, (215, 10, 20, 255)),
            (PbrWorkflow.UNREAL_ORM, (20, 30, 10, 255)),
            (PbrWorkflow.UNREAL_MRA, (20, 10, 30, 255)),
            (PbrWorkflow.UNREAL_RMA, (10, 20, 30, 255)),
        ):
            with self.subTest(workflow=workflow):
                self.shader.properties["workflow"] = workflow.value
                material = self.render()
                self.assertEqual(expected, material.properties.getpixel((0, 0)))
                if previous is not None:
                    self.assertEqual({"properties"}, _changed_slots(previous, material))
                previous = material
        for name in ("roughness", "metallic", "ao"):
            self.graph.connections.append(_link(self.sources[name], "out", self.shader, name))
        before = self.render()
        packed.properties["red"] = 200
        self.cache.invalidate_node_and_downstream(self.graph, packed.node_id)
        after = self.render()
        self.assertEqual(before.texture_keys, after.texture_keys)
        self.assertEqual((64, 64, 64, 255), after.properties.getpixel((0, 0)))

    def test_switching_same_input_between_roughness_and_smoothness_changes_key(self):
        before = self.render()
        self.graph.connections = [c for c in self.graph.connections if c.target_socket_id != "roughness"]
        self.graph.connections.append(_link(self.sources["roughness"], "out", self.shader, "smoothness"))
        after = self.render()
        self.assertEqual({"properties"}, _changed_slots(before, after))
        self.assertEqual(191, after.properties.getpixel((0, 0))[0])


class CacheRevisionTests(unittest.TestCase):
    def test_revisions_follow_writes_eviction_invalidation_and_clear(self):
        cache = NodeGraphPreviewCache(max_bytes=4)
        key = ("a", "out", (2, 2))
        image = Image.new("L", (2, 2))
        self.assertIsNot(cache.image_revision("channel", key), cache.image_revision("channel", key))
        cache.put_image("channel", key, image)
        first = cache.image_revision("channel", key)
        self.assertIs(first, cache.image_revision("channel", key))
        cache.put_image("channel", key, image)
        second = cache.image_revision("channel", key)
        self.assertIsNot(first, second)
        cache.put_image("channel", ("b",), image)
        self.assertIsNone(cache.get_image("channel", key))
        cache.put_image("channel", key, image)
        self.assertIsNot(second, cache.image_revision("channel", key))
        cache.invalidate_node_and_downstream(NodeGraph(), "a")
        self.assertEqual(0, cache.current_bytes)
        cache.put_image("channel", key, image)
        cache.clear()
        self.assertIsNone(cache.get_image("channel", key))


class IncrementalUploadTests(unittest.TestCase):
    def setUp(self):
        self.app = QApplication.instance() or QApplication([])
        self.widget = PbrOpenGLWidget()
        self.created = []

        def create(image):
            texture = Mock()
            self.created.append(texture)
            return texture

        self.create_patch = patch.object(self.widget, "_create_texture", side_effect=create)
        self.factory = self.create_patch.start()
        self.graph, self.shader, self.sources = _material_graph()
        self.cache = NodeGraphPreviewCache()

    def tearDown(self):
        self.widget._destroy_textures()
        self.create_patch.stop()
        self.widget.close()
        self.widget.deleteLater()
        self.app.processEvents()

    def upload(self):
        material = NodeGraphExecutor().render_pbr_material_node(
            self.graph, self.shader, preview_cache=self.cache,
        )
        self.widget.set_material(material)
        self.widget._upload_material()
        return material

    def test_roughness_edit_creates_one_texture_and_preserves_other_objects(self):
        self.upload()
        old_textures = list(self.widget._textures)
        self.assertEqual(4, self.factory.call_count)
        self.sources["roughness"].properties["value"] = 200
        self.cache.invalidate_node_and_downstream(self.graph, self.sources["roughness"].node_id)
        self.upload()
        self.assertEqual(5, self.factory.call_count)
        for index in (0, 1, 3):
            self.assertIs(old_textures[index], self.widget._textures[index])
            old_textures[index].destroy.assert_not_called()
        old_textures[2].destroy.assert_called_once_with()
        self.assertIsNot(old_textures[2], self.widget._textures[2])

    def test_uniform_and_warm_updates_do_not_create_textures(self):
        self.upload()
        self.upload()
        self.shader.properties["normal_convention"] = "directx"
        material = self.upload()
        self.assertTrue(self.widget._uses_directx_normal())
        self.assertIs(material, self.widget._uploaded_material)
        self.assertEqual(4, self.factory.call_count)

    def test_untracked_material_is_uploaded_conservatively(self):
        material = replace(self.upload(), texture_keys=None)
        self.widget.set_material(material)
        self.widget._upload_material()
        self.assertEqual(8, self.factory.call_count)

    def test_clear_and_context_cleanup_reset_gpu_revisions(self):
        material = self.upload()
        self.widget.set_material(None)
        self.widget._upload_material()
        self.assertEqual([], self.widget._textures)
        self.assertEqual((), self.widget._uploaded_texture_keys)
        self.widget.set_material(material)
        self.widget._upload_material()
        self.assertEqual(8, self.factory.call_count)
        with (
            patch.object(self.widget, "isValid", return_value=True),
            patch.object(self.widget, "makeCurrent"),
            patch.object(self.widget, "doneCurrent"),
        ):
            self.widget.cleanup()
        self.assertIsNone(self.widget._uploaded_material)
        self.assertEqual((), self.widget._uploaded_texture_keys)
        self.widget._upload_material()
        self.assertEqual(12, self.factory.call_count)

    def test_failed_replacement_preserves_complete_previous_material(self):
        material = self.upload()
        old_textures = list(self.widget._textures)
        new_texture = Mock()
        self.factory.side_effect = [new_texture, RuntimeError("Upload failed")]
        self.widget.set_material(replace(material, texture_keys=None))
        with self.assertRaisesRegex(RuntimeError, "Upload failed"):
            self.widget._upload_material()
        new_texture.destroy.assert_called_once_with()
        self.assertIs(material, self.widget._uploaded_material)
        self.assertEqual(old_textures, self.widget._textures)
        for texture in old_textures:
            texture.destroy.assert_not_called()


if __name__ == "__main__":
    unittest.main()
