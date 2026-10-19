from __future__ import annotations

import os
from collections import defaultdict
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from image_converter.domain.errors import ValidationError


@dataclass(frozen=True, slots=True)
class PlannedImageWrite:
    destination: Path
    sources: tuple[Path, ...]
    label: str


def path_identity_keys(path: Path) -> frozenset[tuple]:
    """Match normalized paths, symlinks/junctions and existing hard links."""
    keys = {("path", os.path.normcase(str(path.resolve())))}
    try:
        stat = path.stat()
    except FileNotFoundError:
        pass
    else:
        if stat.st_ino:
            keys.add(("file", stat.st_dev, stat.st_ino))
    return frozenset(keys)


def validate_batch_output_paths(
    sources: list[Path], writes: list[PlannedImageWrite], *, overwrite: bool,
) -> None:
    """Reject ambiguous plans before any write, even with overwrite enabled.

    In-place processing is allowed only for an output's own input, after its
    last reader. Callers must also preserve it if an earlier consumer fails.
    """
    identity = lru_cache(maxsize=None)(path_identity_keys)
    inputs = defaultdict(set)
    readers = defaultdict(set)
    outputs = defaultdict(set)
    try:
        for path in sources:
            for key in identity(path):
                inputs[key].add(path)
        for index, write in enumerate(writes):
            for path in write.sources:
                for key in identity(path):
                    readers[key].add(index)
            for key in identity(write.destination):
                outputs[key].add(index)

        issues = []
        duplicate_groups = {tuple(sorted(indices)) for indices in outputs.values() if len(indices) > 1}
        for indices in sorted(duplicate_groups):
            issues.append("Conflicting outputs:\n" + "\n".join(
                f"  {writes[index].destination} <- {writes[index].label}\n"
                + "\n".join(f"    Source: {path}" for path in dict.fromkeys(writes[index].sources))
                for index in indices
            ))
        if overwrite:
            for index, write in enumerate(writes):
                keys = identity(write.destination)
                affected = set().union(*(inputs[key] for key in keys))
                if not affected:
                    continue
                own_input = any(keys & identity(path) for path in write.sources)
                future_readers = any(reader > index for key in keys for reader in readers[key])
                if not own_input or future_readers:
                    issues.append(
                        f"Output would overwrite an input still needed by this batch:\n"
                        f"  {write.destination} <- {write.label}\n"
                        + "\n".join(f"  Input: {path}" for path in sorted(affected))
                    )
    except OSError as exc:
        raise ValidationError(f"Cannot validate batch output paths: {exc}") from exc
    if issues:
        raise ValidationError(
            "Batch export blocked before writing files.\n"
            "Choose a separate output folder or resolve conflicting names.\n\n"
            + "\n\n".join(issues)
        )
