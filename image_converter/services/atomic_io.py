from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from PIL import Image


def save_png_atomic(
    image: Image.Image, path: Path, *, overwrite: bool, optimize: bool, compress_level: int,
) -> bool:
    """Publish a complete PNG, or return False if a no-overwrite target exists."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w+b", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as stream:
            temporary_path = Path(stream.name)
            image.save(stream, format="PNG", optimize=optimize, compress_level=compress_level)
            stream.flush()
            os.fsync(stream.fileno())
        if overwrite:
            os.replace(temporary_path, path)
        else:
            try:
                # Windows rename never replaces an existing target. On POSIX,
                # link publishes atomically without rename's overwrite behavior.
                if os.name == "nt":
                    os.rename(temporary_path, path)
                else:
                    os.link(temporary_path, path)
            except FileExistsError:
                return False
        return True
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def write_json_atomic(path: Path, payload: Any) -> None:
    """Replace a JSON file only after its complete new contents are on disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as stream:
            temporary_path = Path(stream.name)
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
