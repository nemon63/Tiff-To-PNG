from __future__ import annotations

import json
from pathlib import Path

from image_converter.services.atomic_io import write_json_atomic


def migrate_legacy_user_data(legacy_root: Path, user_root: Path) -> tuple[str, ...]:
    """Copy legacy preferences once; never remove or overwrite either user's copy."""
    errors: list[str] = []
    for filename in ("app_settings.json", "conversion_presets.json"):
        source = legacy_root / filename
        destination = user_root / filename
        if destination.exists() or not source.is_file():
            continue
        try:
            payload = json.loads(source.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("Expected a JSON object.")
            write_json_atomic(destination, payload)
        except (OSError, ValueError) as exc:
            errors.append(f"Could not migrate {source} to {destination}: {exc}")
    return tuple(errors)
