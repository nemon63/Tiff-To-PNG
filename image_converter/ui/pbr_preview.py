from __future__ import annotations

from PyQt6.QtGui import QShowEvent
from PyQt6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from image_converter.application.pbr_preview import (
    PbrPreviewController,
    PbrPreviewRequest,
)
from image_converter.domain.models import QueueItem
from image_converter.services.pbr_preview import (
    PBR_GEOMETRY_PLANE,
    PBR_GEOMETRY_SPHERE,
    PBR_NORMAL_AUTO,
    PBR_NORMAL_DIRECTX,
    PBR_NORMAL_OPENGL,
    PBR_SOLO_AO,
    PBR_SOLO_BASECOLOR,
    PBR_SOLO_BEAUTY,
    PBR_SOLO_METALLIC,
    PBR_SOLO_NORMAL,
    PBR_SOLO_ROUGHNESS,
    PbrMaterialData,
    PbrPreviewSettings,
    PbrTextureSource,
    pbr_sources_from_items,
)
from image_converter.ui.pbr_gl_widget import PbrOpenGLWidget


class PbrPreviewPanel(QWidget):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._controller = PbrPreviewController(self)
        self._controller.ready.connect(self._on_material_ready)
        self._controller.failed.connect(self._on_material_failed)
        self._sources: tuple[PbrTextureSource, ...] = ()
        self._source_revision: tuple[tuple[str, int, int], ...] = ()
        self._generation = 0
        self._material: PbrMaterialData | None = None
        self._build_ui()

    def _build_ui(self) -> None:
        self.setObjectName("SectionPanel")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        header = QHBoxLayout()
        title = QLabel("Live PBR Preview · GPU")
        title.setObjectName("PanelTitle")
        header.addWidget(title)
        header.addStretch(1)
        self.refresh_button = QPushButton("Reload Maps")
        self.refresh_button.clicked.connect(self._request_material_load)
        header.addWidget(self.refresh_button)
        layout.addLayout(header)

        controls = QHBoxLayout()
        controls.setSpacing(6)
        self.geometry_combo = QComboBox()
        self.geometry_combo.addItem("Sphere", PBR_GEOMETRY_SPHERE)
        self.geometry_combo.addItem("Plane", PBR_GEOMETRY_PLANE)
        self.geometry_combo.setToolTip("Preview geometry")
        self.geometry_combo.currentIndexChanged.connect(self._apply_gpu_settings)
        controls.addWidget(self.geometry_combo)

        self.solo_combo = QComboBox()
        for label, value in (
            ("Beauty", PBR_SOLO_BEAUTY),
            ("Base Color", PBR_SOLO_BASECOLOR),
            ("Normal", PBR_SOLO_NORMAL),
            ("Roughness", PBR_SOLO_ROUGHNESS),
            ("Metallic", PBR_SOLO_METALLIC),
            ("AO", PBR_SOLO_AO),
        ):
            self.solo_combo.addItem(label, value)
        self.solo_combo.setToolTip("Beauty shading or an individual material map")
        self.solo_combo.currentIndexChanged.connect(self._apply_gpu_settings)
        controls.addWidget(self.solo_combo)

        self.normal_combo = QComboBox()
        self.normal_combo.addItem("Normal: Auto", PBR_NORMAL_AUTO)
        self.normal_combo.addItem("Normal: OpenGL", PBR_NORMAL_OPENGL)
        self.normal_combo.addItem("Normal: DirectX", PBR_NORMAL_DIRECTX)
        self.normal_combo.setToolTip(
            "Auto reads DX/GL from filename. DirectX flips Green in the shader."
        )
        self.normal_combo.currentIndexChanged.connect(self._apply_gpu_settings)
        controls.addWidget(self.normal_combo)
        layout.addLayout(controls)

        self.gl_preview = PbrOpenGLWidget(self)
        self.gl_preview.setToolTip(
            "ЛКМ: вращение. СКМ: перемещение. Ctrl+ЛКМ: свет. Колесо: масштаб."
        )
        self.gl_preview.initialization_failed.connect(self._on_gl_failed)
        layout.addWidget(self.gl_preview, 1)

        navigation_hint = QLabel(
            "ЛКМ: сфера · СКМ: сдвиг · Ctrl+ЛКМ: свет · колесо: зум"
        )
        navigation_hint.setObjectName("SummaryText")
        layout.addWidget(navigation_hint)

        self.set_label = QLabel("Выберите Texture Set в очереди.")
        self.set_label.setObjectName("PreviewFileName")
        self.set_label.setWordWrap(True)
        layout.addWidget(self.set_label)

        self.meta_label = QLabel(
            "Поддерживаются отдельные карты, ORM/RMA/MRA и Unity Mask Maps."
        )
        self.meta_label.setObjectName("PreviewMetaText")
        self.meta_label.setWordWrap(True)
        layout.addWidget(self.meta_label)
        self._apply_gpu_settings()

    def set_texture_set(self, items: tuple[QueueItem, ...] | list[QueueItem]) -> None:
        sources = pbr_sources_from_items(tuple(items))
        revision = self._revision_for_sources(sources)
        if sources == self._sources and revision == self._source_revision:
            return
        self._sources = sources
        self._source_revision = revision
        self._material = None
        self._controller.invalidate()
        self.gl_preview.set_material(None)
        self.refresh_button.setEnabled(bool(sources))
        if not sources:
            self.set_label.setText("Texture Set не выбран")
            self.meta_label.setText("Добавьте и выберите распознанные PBR-карты.")
            return

        parent_name = sources[0].path.parent.name or str(sources[0].path.parent)
        self.set_label.setText(f"Texture Set: {parent_name}")
        self.set_label.setToolTip("\n".join(str(source.path) for source in sources))
        self.meta_label.setText(f"Карт в наборе: {len(sources)} · загрузка на GPU...")
        if self.isVisible():
            self._request_material_load()

    def current_sources(self) -> tuple[PbrTextureSource, ...]:
        return self._sources

    @staticmethod
    def _revision_for_sources(
        sources: tuple[PbrTextureSource, ...],
    ) -> tuple[tuple[str, int, int], ...]:
        revisions: list[tuple[str, int, int]] = []
        for source in sources:
            try:
                stat = source.path.stat()
                revisions.append((str(source.path), stat.st_size, stat.st_mtime_ns))
            except OSError:
                revisions.append((str(source.path), -1, -1))
        return tuple(revisions)

    def _settings(self) -> PbrPreviewSettings:
        return PbrPreviewSettings(
            geometry=str(self.geometry_combo.currentData() or PBR_GEOMETRY_SPHERE),
            solo=str(self.solo_combo.currentData() or PBR_SOLO_BEAUTY),
            normal_convention=str(
                self.normal_combo.currentData() or PBR_NORMAL_AUTO
            ),
            light_rotation=round(self.gl_preview.light_rotation),
        )

    def _apply_gpu_settings(self, *_args: object) -> None:
        if not hasattr(self, "gl_preview"):
            return
        settings = self._settings()
        self.gl_preview.set_geometry(settings.geometry)
        self.gl_preview.set_solo(settings.solo)
        self.gl_preview.set_normal_convention(settings.normal_convention)
        if self._material is not None:
            self._update_summary()

    def _request_material_load(self, *_args: object) -> None:
        if not self._sources:
            return
        self.meta_label.setText("Чтение и подготовка карт...")
        self._generation = self._controller.request(
            PbrPreviewRequest(self._sources, max_dimension=2048)
        )
        self.refresh_button.setEnabled(False)

    def _on_material_ready(self, generation: int, result: object) -> None:
        if (
            generation != self._generation
            or generation != self._controller.generation
            or not isinstance(result, PbrMaterialData)
        ):
            return
        self._material = result
        self.gl_preview.set_material(result)
        self._update_summary()
        self.refresh_button.setEnabled(True)

    def _update_summary(self) -> None:
        if self._material is None:
            return
        width, height = self._material.size
        normal_setting = str(self.normal_combo.currentData() or PBR_NORMAL_AUTO)
        directx = normal_setting == PBR_NORMAL_DIRECTX or (
            normal_setting == PBR_NORMAL_AUTO and self._material.normal_is_directx
        )
        normal_label = "DirectX → OpenGL" if directx else "OpenGL"
        used = ", ".join(self._material.used_labels) or "fallback values"
        summary = f"GPU · {width}×{height} · {normal_label} · maps: {used}"
        self.meta_label.setText(summary)
        self.meta_label.setToolTip(summary)

    def _on_material_failed(self, generation: int, message: str) -> None:
        if generation != self._generation or generation != self._controller.generation:
            return
        self.meta_label.setText(f"PBR preview error: {message}")
        self.refresh_button.setEnabled(True)

    def _on_gl_failed(self, message: str) -> None:
        self.meta_label.setText(f"OpenGL initialization error: {message}")

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        if self._sources and self._material is None:
            self._request_material_load()

    def background_job_running(self) -> bool:
        return self._controller.is_running

    def shutdown_background_jobs(self, *, wait_ms: int = 0) -> bool:
        return self._controller.shutdown(wait_ms=wait_ms)
