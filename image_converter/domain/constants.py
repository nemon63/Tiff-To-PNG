from __future__ import annotations

SUPPORTED_SOURCE_EXTENSIONS = frozenset(
    {
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
    "Поддерживаемые изображения (*.tif *.tiff *.tga *.jpg *.jpeg *.bmp *.gif *.webp *.psd);;"
    "Все файлы (*)"
)
