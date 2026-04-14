from __future__ import annotations

SUPPORTED_SOURCE_EXTENSIONS = frozenset(
    {
        ".dds",
        ".png",
        ".tif",
        ".tiff",
        ".tga",
        ".jpg",
        ".jpeg",
        ".bmp",
        ".gif",
        ".webp",
        ".psd",
    }
)

FILE_DIALOG_FILTER = (
    "Поддерживаемые изображения (*.dds *.png *.tif *.tiff *.tga *.jpg *.jpeg *.bmp *.gif *.webp *.psd);;"
    "Все файлы (*)"
)
