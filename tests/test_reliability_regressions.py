from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
from unittest.mock import patch

from PIL import Image
from PyQt6.QtWidgets import QApplication

from image_converter.application import bootstrap
from image_converter.cli import run_cli
from image_converter.domain.models import (
    AppSettings, BatchRequest, ChannelPackLayout, ChannelPackingMode,
    ChannelPackingOptions, ConversionOptions,
)
from image_converter.domain.node_graph import (
    GraphConnection, NodeGraph, NodeGraphProject, NodeType, create_graph_node,
)
from image_converter.services.atomic_io import write_json_atomic
from image_converter.services.conversion import BatchConversionService, ImageConverter
from image_converter.services.node_graph_executor import NodeGraphExecutor, NodeGraphPreviewCache
from image_converter.services.node_graph_project import NodeGraphProjectRepository
from image_converter.services.presets import PresetRepository
from image_converter.services.settings import AppSettingsRepository
from image_converter.services.user_data import migrate_legacy_user_data
from image_converter.ui.node_editor import GraphPreviewWorker, GraphWorkspace


class SourceDeletionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "input"
        self.source.mkdir()
        self.output = self.root / "output"
        for suffix, value in (("ao", 250), ("roughness", 120), ("metallic", 20)):
            Image.new("L", (8, 8), value).save(self.source / f"mat_{suffix}.tif")
        self.options = ConversionOptions(
            delete_source=True,
            packing=ChannelPackingOptions(
                enabled=True, layout=ChannelPackLayout.ORM,
                mode=ChannelPackingMode.AFTER_CONVERSION,
            ),
        )

    def run_batch(self, options: ConversionOptions | None = None):
        return BatchConversionService().run(BatchRequest(
            input_path=self.source, output_root=self.output, options=options or self.options,
        ))

    def test_sources_are_deleted_only_after_pack_reads_them(self) -> None:
        summary = self.run_batch()
        self.assertEqual(0, summary.failed + summary.packed_failed)
        self.assertEqual(1, summary.packed_created)
        self.assertEqual([], list(self.source.iterdir()))
        with Image.open(self.output / "mat_orm.png") as image:
            self.assertEqual((250, 120, 20), image.getpixel((0, 0)))

    def test_packing_failure_preserves_sources(self) -> None:
        with patch("image_converter.services.conversion.execute_channel_pack_job", side_effect=OSError("Disk full")):
            summary = self.run_batch()
        self.assertEqual(1, summary.packed_failed)
        self.assertEqual(3, len(list(self.source.iterdir())))

    def test_skipped_pack_does_not_authorize_deletion(self) -> None:
        self.output.mkdir()
        Image.new("RGB", (8, 8), "red").save(self.output / "mat_orm.png")
        summary = self.run_batch()
        self.assertEqual(1, summary.packed_skipped)
        self.assertEqual(3, len(list(self.source.iterdir())))

    def test_skipped_conversion_preserves_its_source(self) -> None:
        self.output.mkdir()
        Image.new("L", (8, 8), 0).save(self.output / "mat_ao.png")
        summary = self.run_batch()
        self.assertEqual(1, summary.skipped)
        self.assertEqual(1, summary.packed_created)
        self.assertEqual(["mat_ao.tif"], [path.name for path in self.source.iterdir()])

    def test_regular_conversion_still_deletes_successful_sources(self) -> None:
        summary = self.run_batch(replace(self.options, packing=ChannelPackingOptions()))
        self.assertEqual(3, summary.succeeded)
        self.assertEqual([], list(self.source.iterdir()))

    def test_pack_only_does_not_start_deleting_unconverted_sources(self) -> None:
        options = replace(self.options, packing=replace(self.options.packing, mode=ChannelPackingMode.PACK_ONLY))
        self.assertEqual(1, self.run_batch(options).packed_created)
        self.assertEqual(3, len(list(self.source.iterdir())))

    def test_repacking_keeps_shared_source_until_all_channels_are_consumed(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "mat_orm.tif"
            Image.new("RGB", (8, 8), (250, 120, 20)).save(source)
            options = replace(self.options, packing=replace(self.options.packing, layout=ChannelPackLayout.RMA))
            summary = BatchConversionService().run(BatchRequest(
                input_path=source, output_root=root / "out", options=options,
            ))
            self.assertEqual(1, summary.packed_created)
            self.assertFalse(source.exists())
            with Image.open(root / "out" / "mat_rma.png") as image:
                self.assertEqual((120, 20, 250), image.getpixel((0, 0)))

    def test_partial_unpack_failure_keeps_shared_source(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "mat_orm.tif"
            Image.new("RGB", (8, 8), (250, 120, 20)).save(source)
            converter = ImageConverter()
            original = converter.convert_source

            def convert_channel(source_spec, destination, options):
                if source_spec.source_channel == "G":
                    raise OSError("Disk full")
                return original(source_spec, destination, options)

            options = ConversionOptions(delete_source=True, unpack_packed=True)
            with patch.object(converter, "convert_source", side_effect=convert_channel):
                summary = BatchConversionService(converter).run(BatchRequest(
                    input_path=source, output_root=root / "out", options=options,
                ))
            self.assertEqual(1, summary.failed)
            self.assertTrue(source.exists())

    def test_overwriting_in_place_never_deletes_the_output(self) -> None:
        with TemporaryDirectory() as directory:
            source = Path(directory) / "mat.png"
            Image.new("RGB", (8, 8), "red").save(source)
            summary = BatchConversionService().run(BatchRequest(
                input_path=source, output_root=None,
                options=ConversionOptions(overwrite=True, delete_source=True),
            ))
            self.assertEqual(1, summary.succeeded)
            self.assertTrue(source.exists())


class PersistenceSafetyTests(unittest.TestCase):
    def test_bootstrap_uses_user_directory_and_reports_settings_save_failure(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            legacy, user = root / "installed", root / "user"
            AppSettingsRepository(legacy / "app_settings.json").save(AppSettings(input_path="legacy"))
            with (
                patch.object(bootstrap, "_runtime_root", return_value=legacy),
                patch.object(bootstrap, "_resource_root", return_value=legacy),
                patch.object(bootstrap, "_user_data_root", return_value=user),
                patch.object(bootstrap, "QApplication") as app_type,
                patch.object(bootstrap, "MainWindow") as window_type,
                patch.object(bootstrap, "ConversionController"),
            ):
                app_type.return_value.exec.return_value = 0
                window = window_type.return_value
                window.build_app_settings.return_value = AppSettings(input_path="updated")
                self.assertEqual(0, bootstrap.run_gui())
                self.assertEqual("legacy", window.apply_app_settings.call_args.args[0].input_path)
                window.set_preset_repository.call_args.args[0].save_preset("Artist", ConversionOptions())
                self.assertTrue((user / "conversion_presets.json").is_file())
                self.assertFalse((legacy / "conversion_presets.json").exists())
                save_settings = app_type.return_value.aboutToQuit.connect.call_args.args[0]
                save_settings()
                self.assertEqual("updated", AppSettingsRepository(user / "app_settings.json").load().input_path)
                self.assertEqual("legacy", AppSettingsRepository(legacy / "app_settings.json").load().input_path)
                with patch("image_converter.services.atomic_io.os.replace", side_effect=PermissionError("Read only")):
                    save_settings()
                window.show_error.assert_called_once()
                self.assertIn(str(user / "app_settings.json"), window.show_error.call_args.args[1])

    def test_failed_replace_preserves_saved_scene_and_removes_temporary_file(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "scene.texturegraph"
            repository = NodeGraphProjectRepository()
            repository.save(NodeGraphProject(name="Original"), path)
            original = path.read_bytes()
            with patch("image_converter.services.atomic_io.os.replace", side_effect=OSError("Disk full")):
                with self.assertRaises(OSError):
                    repository.save(NodeGraphProject(name="Replacement"), path)
            self.assertEqual(original, path.read_bytes())
            self.assertEqual([path], list(path.parent.iterdir()))

    def test_serialization_failure_preserves_saved_scene(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "scene.texturegraph"
            repository = NodeGraphProjectRepository()
            repository.save(NodeGraphProject(name="Original"), path)
            original = path.read_bytes()
            node = create_graph_node(NodeType.COLOR, properties={"invalid": object()})
            with self.assertRaises(TypeError):
                repository.save(NodeGraphProject(graph=NodeGraph(nodes=[node])), path)
            self.assertEqual(original, path.read_bytes())
            self.assertEqual([path], list(path.parent.iterdir()))

    def test_unknown_node_is_rejected_instead_of_reinterpreted(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "unknown.texturegraph"
            write_json_atomic(path, {"version": 1, "graph": {"nodes": [{"id": "n", "type": "future_node"}]}})
            with self.assertRaisesRegex(ValueError, "Unsupported node type"):
                NodeGraphProjectRepository().load(path)

    def test_future_project_version_is_rejected(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "future.texturegraph"
            write_json_atomic(path, {"version": 999, "graph": {"nodes": []}})
            with self.assertRaisesRegex(ValueError, "Unsupported graph project version"):
                NodeGraphProjectRepository().load(path)

    def test_legacy_versionless_project_still_loads(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.texturegraph"
            write_json_atomic(path, {"name": "Legacy", "graph": {"nodes": []}})
            self.assertEqual("Legacy", NodeGraphProjectRepository().load(path).name)

    def test_settings_write_failure_is_not_silenced(self) -> None:
        with TemporaryDirectory() as directory:
            repository = AppSettingsRepository(Path(directory) / "settings.json")
            with patch("image_converter.services.atomic_io.os.replace", side_effect=PermissionError("Read only")):
                with self.assertRaises(PermissionError):
                    repository.save(AppSettings())

    def test_migration_preserves_legacy_files_and_existing_user_preferences(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            legacy, user = root / "installed", root / "user"
            AppSettingsRepository(legacy / "app_settings.json").save(AppSettings(input_path="legacy"))
            PresetRepository(legacy / "conversion_presets.json").save_preset("Artist", ConversionOptions())
            original = (legacy / "app_settings.json").read_bytes()
            self.assertEqual((), migrate_legacy_user_data(legacy, user))
            self.assertEqual(original, (legacy / "app_settings.json").read_bytes())
            self.assertEqual("legacy", AppSettingsRepository(user / "app_settings.json").load().input_path)
            self.assertIn("Artist", [preset.name for preset in PresetRepository(user / "conversion_presets.json").load_presets()])
            AppSettingsRepository(user / "app_settings.json").save(AppSettings(input_path="current"))
            self.assertEqual((), migrate_legacy_user_data(legacy, user))
            self.assertEqual("current", AppSettingsRepository(user / "app_settings.json").load().input_path)

    def test_migration_reports_write_failure_without_removing_source(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            legacy, user = root / "installed", root / "user"
            write_json_atomic(legacy / "app_settings.json", {})
            with patch("image_converter.services.atomic_io.os.replace", side_effect=PermissionError("Read only")):
                errors = migrate_legacy_user_data(legacy, user)
            self.assertEqual(1, len(errors))
            self.assertTrue((legacy / "app_settings.json").is_file())
            self.assertFalse((user / "app_settings.json").exists())


class CliResultTests(unittest.TestCase):
    def test_corrupt_image_returns_failure(self) -> None:
        with TemporaryDirectory() as directory, redirect_stdout(io.StringIO()):
            source = Path(directory) / "broken.png"
            source.touch()
            self.assertEqual(1, run_cli([str(source), "--out", str(Path(directory) / "out")]))

    def test_success_returns_zero_and_invalid_request_returns_two(self) -> None:
        with TemporaryDirectory() as directory, redirect_stdout(io.StringIO()):
            source = Path(directory) / "valid.tif"
            Image.new("RGB", (8, 8), "red").save(source)
            self.assertEqual(0, run_cli([str(source)]))
            self.assertEqual(2, run_cli([str(Path(directory) / "missing.tif")]))

    def test_pack_failure_returns_failure(self) -> None:
        with TemporaryDirectory() as directory, redirect_stdout(io.StringIO()):
            source = Path(directory) / "mat_orm.png"
            Image.new("RGB", (8, 8), "red").save(source)
            with patch("image_converter.services.conversion.execute_channel_pack_job", side_effect=OSError("Disk full")):
                self.assertEqual(1, run_cli([str(source), "--pack-layout", "rma", "--pack-only"]))


class ResizePreviewTests(unittest.TestCase):
    def test_preview_preserves_canvas_layout_for_every_mode(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "source.png"
            image = Image.new("RGBA", (512, 256), "red")
            image.paste("lime", (192, 64, 320, 192))
            image.save(path)
            texture = create_graph_node(NodeType.TEXTURE_INPUT, properties={"path": str(path)})
            for mode in ("crop", "pad", "fit", "fill", "stretch"):
                with self.subTest(mode=mode):
                    resize = create_graph_node(NodeType.RESIZE_CANVAS, properties={
                        "width": 256, "height": 256, "resize_mode": mode, "filter": "nearest",
                    })
                    graph = NodeGraph(nodes=[texture, resize], connections=[
                        GraphConnection("link", texture.node_id, "image", resize.node_id, "image"),
                    ])
                    full, _ = NodeGraphExecutor().render_display_node(graph, resize, max_side=256)
                    cache = NodeGraphPreviewCache(interactive_preview=True)
                    draft, _ = NodeGraphExecutor().render_display_node(graph, resize, max_side=64, preview_cache=cache)
                    expected = full.resize((64, 64), Image.Resampling.NEAREST)
                    # Downsampling the input uses Lanczos; ignore its narrow edge transition.
                    for point in ((0, 0), (32, 32), (8, 32), (56, 32), (32, 8), (32, 56)):
                        self.assertEqual(expected.getpixel(point), draft.getpixel(point))
                    self.assertIsNone(cache.get_image("texture-preview", (texture.node_id, (512, 256))))
                    self.assertIsNotNone(cache.get_image("texture-preview", (texture.node_id, (128, 64))))

    def test_pad_preview_preserves_no_upscale_rule(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "source.png"
            Image.new("RGBA", (64, 32), "red").save(path)
            texture = create_graph_node(NodeType.TEXTURE_INPUT, properties={"path": str(path)})
            resize = create_graph_node(NodeType.RESIZE_CANVAS, properties={
                "width": 256, "height": 128, "resize_mode": "pad", "filter": "nearest",
            })
            graph = NodeGraph([texture, resize], [GraphConnection("link", texture.node_id, "image", resize.node_id, "image")])
            full, _ = NodeGraphExecutor().render_display_node(graph, resize, max_side=256)
            draft, _ = NodeGraphExecutor().render_display_node(graph, resize, max_side=64)
            expected = full.resize(draft.size, Image.Resampling.NEAREST)
            self.assertEqual(expected.tobytes(), draft.tobytes())


class ProjectCacheIsolationTests(unittest.TestCase):
    def test_failed_project_load_preserves_active_scene_and_cache(self) -> None:
        app = QApplication.instance() or QApplication([])
        workspace = GraphWorkspace()
        try:
            project, cache = workspace.project, workspace._preview_cache
            with TemporaryDirectory() as directory:
                path = Path(directory) / "unsupported.texturegraph"
                write_json_atomic(path, {"version": 999})
                self.assertFalse(workspace.load_project(path))
            self.assertIs(project, workspace.project)
            self.assertIs(cache, workspace._preview_cache)
        finally:
            workspace.shutdown_background_jobs(wait_ms=1000)
            workspace.close()
            workspace.deleteLater()
            app.processEvents()

    def test_canceled_old_scene_cannot_overwrite_new_scene_cache(self) -> None:
        app = QApplication.instance() or QApplication([])
        workspace = GraphWorkspace()
        loaded, release = Event(), Event()
        thread = None
        try:
            with TemporaryDirectory() as directory:
                root = Path(directory)
                for name in ("red", "blue"):
                    Image.new("RGBA", (8, 8), name).save(root / f"{name}.png")
                old = create_graph_node(NodeType.TEXTURE_INPUT, properties={"path": str(root / "red.png")})
                new = create_graph_node(NodeType.TEXTURE_INPUT, properties={"path": str(root / "blue.png")})
                new.node_id = old.node_id
                old_cache = workspace._preview_cache
                old_cache.max_side = 512
                worker = GraphPreviewWorker(1, NodeGraphProject(graph=NodeGraph([old])), old, preview_cache=old_cache)
                workspace._preview_active_worker = worker
                original = NodeGraphExecutor._load_texture_preview_image

                def delayed_load(node):
                    image = original(node)
                    if node.properties["path"] == old.properties["path"]:
                        loaded.set()
                        if not release.wait(5):
                            raise TimeoutError("Old worker did not resume")
                    return image

                with patch.object(NodeGraphExecutor, "_load_texture_preview_image", staticmethod(delayed_load)):
                    thread = Thread(target=worker.run)
                    thread.start()
                    self.assertTrue(loaded.wait(5))
                    workspace._reset_preview_for_project_change()
                    new_cache = workspace._preview_cache
                    self.assertIsNot(old_cache, new_cache)
                    self.assertEqual(512, new_cache.max_side)
                    self.assertTrue(new_cache.interactive_preview)
                    first, _ = NodeGraphExecutor().render_display_node(NodeGraph([new]), new, preview_cache=new_cache)
                    release.set()
                    thread.join(5)
                    self.assertFalse(thread.is_alive())
                old_image = old_cache.get_image("texture-preview", (old.node_id, (8, 8)))
                self.assertIsNotNone(old_image)
                self.assertEqual((255, 0, 0, 255), old_image.getpixel((0, 0)))
                second, _ = NodeGraphExecutor().render_display_node(NodeGraph([new]), new, preview_cache=new_cache)
                self.assertEqual((0, 0, 255, 255), first.getpixel((0, 0)))
                self.assertEqual(first.getpixel((0, 0)), second.getpixel((0, 0)))
        finally:
            release.set()
            if thread is not None:
                thread.join(5)
            workspace.shutdown_background_jobs(wait_ms=1000)
            workspace.close()
            workspace.deleteLater()
            app.processEvents()


if __name__ == "__main__":
    unittest.main()
