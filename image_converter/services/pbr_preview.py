from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps

from image_converter.domain.models import ChannelPackLayout, QueueItem, TextureMapType
from image_converter.services.image_loading import copy_first_frame_preserving_alpha
from image_converter.services.material_validation import (
    NORMAL_ORIENTATION_DIRECTX,
    detect_normal_map_orientation,
)
from image_converter.services.pipeline import RESAMPLING_LANCZOS


PBR_GEOMETRY_SPHERE = "sphere"
PBR_GEOMETRY_PLANE = "plane"

PBR_SOLO_BEAUTY = "beauty"
PBR_SOLO_BASECOLOR = "basecolor"
PBR_SOLO_NORMAL = "normal"
PBR_SOLO_ROUGHNESS = "roughness"
PBR_SOLO_METALLIC = "metallic"
PBR_SOLO_AO = "ao"

PBR_NORMAL_AUTO = "auto"
PBR_NORMAL_OPENGL = "opengl"
PBR_NORMAL_DIRECTX = "directx"


@dataclass(slots=True, frozen=True)
class PbrTextureSource:
    path: Path
    map_type: TextureMapType
    packed_layout: ChannelPackLayout | None = None


@dataclass(slots=True, frozen=True)
class PbrPreviewSettings:
    geometry: str = PBR_GEOMETRY_SPHERE
    solo: str = PBR_SOLO_BEAUTY
    normal_convention: str = PBR_NORMAL_AUTO
    light_rotation: int = -35


@dataclass(slots=True, frozen=True)
class PbrMaterialData:
    """Images prepared once on the CPU and uploaded once to the GPU."""

    basecolor: Image.Image
    normal: Image.Image
    properties: Image.Image
    emissive: Image.Image
    normal_is_directx: bool
    used_labels: tuple[str, ...]

    @property
    def size(self) -> tuple[int, int]:
        return self.basecolor.size


def pbr_sources_from_items(
    items: list[QueueItem] | tuple[QueueItem, ...],
) -> tuple[PbrTextureSource, ...]:
    return tuple(
        PbrTextureSource(
            path=item.path,
            map_type=item.effective_map_type,
            packed_layout=item.effective_packed_layout,
        )
        for item in items
        if item.metadata is not None
    )


class PbrPreviewService:
    """Decode separate and packed material maps without doing any pixel shading."""

    def load(
        self,
        sources: tuple[PbrTextureSource, ...],
        *,
        max_dimension: int = 2048,
    ) -> PbrMaterialData:
        if not sources:
            raise ValueError("Texture Set не содержит доступных изображений.")

        target_size = self._target_size(sources, max_dimension)
        images: dict[TextureMapType, Image.Image] = {}
        packed_images: list[tuple[ChannelPackLayout, Image.Image]] = []
        normal_path: Path | None = None
        used_labels: list[str] = []

        for source in sorted(sources, key=lambda value: value.path.name.casefold()):
            image = self._load_image(source.path, target_size)
            if source.packed_layout is not None:
                packed_images.append((source.packed_layout, image.convert("RGBA")))
                used_labels.append(source.packed_layout.label)
                continue
            if source.map_type is TextureMapType.UNKNOWN or source.map_type in images:
                continue
            images[source.map_type] = image
            used_labels.append(source.map_type.label)
            if source.map_type is TextureMapType.NORMAL:
                normal_path = source.path

        scalar_channels: dict[TextureMapType, Image.Image] = {}
        for layout, packed in packed_images:
            red, green, blue, alpha = packed.split()
            if layout is ChannelPackLayout.ORM:
                scalar_channels.update(
                    {
                        TextureMapType.AO: red,
                        TextureMapType.ROUGHNESS: green,
                        TextureMapType.METALLIC: blue,
                    }
                )
            elif layout is ChannelPackLayout.RMA:
                scalar_channels.update(
                    {
                        TextureMapType.ROUGHNESS: red,
                        TextureMapType.METALLIC: green,
                        TextureMapType.AO: blue,
                    }
                )
            elif layout is ChannelPackLayout.MRA:
                scalar_channels.update(
                    {
                        TextureMapType.METALLIC: red,
                        TextureMapType.ROUGHNESS: green,
                        TextureMapType.AO: blue,
                    }
                )
            elif layout is ChannelPackLayout.UNITY_URP:
                scalar_channels[TextureMapType.METALLIC] = red
                scalar_channels[TextureMapType.ROUGHNESS] = ImageOps.invert(alpha)
            elif layout is ChannelPackLayout.UNITY_HDRP:
                scalar_channels[TextureMapType.METALLIC] = red
                scalar_channels[TextureMapType.AO] = green
                scalar_channels[TextureMapType.ROUGHNESS] = ImageOps.invert(alpha)

        base_image = images.get(TextureMapType.BASECOLOR)
        if base_image is None:
            base_image = Image.new("RGBA", target_size, (160, 160, 160, 255))
        else:
            base_image = base_image.convert("RGBA")

        normal_image = images.get(TextureMapType.NORMAL)
        if normal_image is None:
            normal_image = Image.new("RGB", target_size, (128, 128, 255))
        else:
            normal_image = normal_image.convert("RGB")

        for map_type in (
            TextureMapType.ROUGHNESS,
            TextureMapType.METALLIC,
            TextureMapType.AO,
            TextureMapType.OPACITY,
        ):
            source_image = images.get(map_type)
            if source_image is not None:
                scalar_channels[map_type] = source_image.convert("L")

        smoothness = images.get(TextureMapType.SMOOTHNESS)
        if smoothness is not None and TextureMapType.ROUGHNESS not in images:
            scalar_channels[TextureMapType.ROUGHNESS] = ImageOps.invert(
                smoothness.convert("L")
            )

        opacity = scalar_channels.get(TextureMapType.OPACITY)
        if opacity is None:
            opacity = Image.new("L", target_size, 255)

        roughness = scalar_channels.get(TextureMapType.ROUGHNESS)
        metallic = scalar_channels.get(TextureMapType.METALLIC)
        ao = scalar_channels.get(TextureMapType.AO)
        if roughness is None:
            roughness = Image.new("L", target_size, 128)
        if metallic is None:
            metallic = Image.new("L", target_size, 0)
        if ao is None:
            ao = Image.new("L", target_size, 255)

        emissive_image = images.get(TextureMapType.EMISSIVE)
        if emissive_image is None:
            emissive_image = Image.new("RGB", target_size, (0, 0, 0))
        else:
            emissive_image = emissive_image.convert("RGB")

        detected_orientation = (
            detect_normal_map_orientation(normal_path)
            if normal_path is not None
            else None
        )
        return PbrMaterialData(
            basecolor=base_image,
            normal=normal_image,
            properties=Image.merge("RGBA", (roughness, metallic, ao, opacity)),
            emissive=emissive_image,
            normal_is_directx=detected_orientation == NORMAL_ORIENTATION_DIRECTX,
            used_labels=tuple(dict.fromkeys(used_labels)),
        )

    @staticmethod
    def _target_size(
        sources: tuple[PbrTextureSource, ...],
        max_dimension: int,
    ) -> tuple[int, int]:
        preferred = next(
            (
                source
                for source in sources
                if source.map_type is TextureMapType.BASECOLOR
                and source.packed_layout is None
            ),
            sources[0],
        )
        with Image.open(preferred.path) as image:
            width, height = image.size
        maximum = max(64, min(int(max_dimension), 4096))
        scale = min(1.0, maximum / max(width, height, 1))
        return max(1, round(width * scale)), max(1, round(height * scale))

    @staticmethod
    def _load_image(path: Path, size: tuple[int, int]) -> Image.Image:
        with Image.open(path) as source:
            image = copy_first_frame_preserving_alpha(source)
        if image.size != size:
            image = image.resize(size, RESAMPLING_LANCZOS)
        return image
