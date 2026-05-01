from __future__ import annotations

from PyQt6.QtGui import QColor, QImage

from image_converter.domain.models import (
    PreviewChannel,
    QueueItem,
    QueueStatus,
    TextureColorSpace,
    TextureMapType,
)
from image_converter.services.colorspace import item_warning_count

QUEUE_HEADERS = ("Имя", "Карта", "Размер", "Разрешение", "Статус", "Выходной путь")
AUTO_MAP_TYPE_DATA = "__auto__"
CURRENT_PRESET_DATA = "__current_preset__"


def _extract_local_paths(event) -> list[str]:
    mime_data = event.mimeData()
    if not mime_data.hasUrls():
        return []

    paths: list[str] = []
    for url in mime_data.urls():
        if url.isLocalFile():
            local_path = url.toLocalFile()
            if local_path:
                paths.append(local_path)
    return paths


def _status_text(status: QueueStatus) -> str:
    mapping = {
        QueueStatus.PENDING: "Ожидает",
        QueueStatus.READY: "Готово к запуску",
        QueueStatus.RUNNING: "В обработке",
        QueueStatus.DONE: "Успех",
        QueueStatus.SKIPPED: "Пропущено",
        QueueStatus.ERROR: "Ошибка",
    }
    return mapping[status]


def _queue_status_display(item: QueueItem) -> str:
    base_text = _status_text(item.status)
    warning_count = item_warning_count(item)
    if item.status in (QueueStatus.READY, QueueStatus.PENDING) and warning_count:
        return f"{base_text} ({warning_count} предупрежд.)"
    return base_text


def _preview_channel_label(channel: PreviewChannel) -> str:
    mapping = {
        PreviewChannel.COMPOSITE: "RGB",
        PreviewChannel.RED: "R",
        PreviewChannel.GREEN: "G",
        PreviewChannel.BLUE: "B",
        PreviewChannel.ALPHA: "A",
        PreviewChannel.LUMA: "Luma",
    }
    return mapping[channel]


def _map_type_label(map_type: TextureMapType) -> str:
    return map_type.label


def _colorspace_label(colorspace: TextureColorSpace) -> str:
    return colorspace.label


def _qimage_from_pil(image) -> QImage:
    rgba_image = image.convert("RGBA")
    raw_data = rgba_image.tobytes("raw", "RGBA")
    qimage = QImage(
        raw_data,
        rgba_image.width,
        rgba_image.height,
        rgba_image.width * 4,
        QImage.Format.Format_RGBA8888,
    )
    return qimage.copy()


def _status_colors(item: QueueItem) -> tuple[QColor, QColor]:
    status = item.status
    if status is QueueStatus.RUNNING:
        return QColor("#203A56"), QColor("#D7E9FF")
    if status is QueueStatus.DONE:
        return QColor("#1F3D2A"), QColor("#CFF0D8")
    if status is QueueStatus.SKIPPED:
        return QColor("#2B3138"), QColor("#BAC4CF")
    if status is QueueStatus.ERROR:
        return QColor("#4A2429"), QColor("#FFD7D7")
    if item_warning_count(item):
        return QColor("#3D321C"), QColor("#FFE5A4")
    return QColor("#171D23"), QColor("#D8DEE6")
