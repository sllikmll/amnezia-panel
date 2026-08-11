"""Release/version helpers for Amnezia Panel self-update flow."""
from __future__ import annotations

import re
from typing import Iterable, Tuple


_VERSION_RE = re.compile(r"\d+(?:\.\d+){0,3}")


def normalize_version(value: str | None) -> Tuple[int, ...]:
    """Return comparable numeric version tuple.

    Accepts values like ``v1.2.3``, ``1.2.3-beta`` or GitHub tag names.
    Missing parts are padded to three components so ``1.2`` equals ``1.2.0``.
    """
    if not value:
        return (0, 0, 0)
    match = _VERSION_RE.search(str(value).strip())
    if not match:
        return (0, 0, 0)
    parts = [int(p) for p in match.group(0).split(".")]
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


def is_newer_version(remote: str | None, current: str | None) -> bool:
    return normalize_version(remote) > normalize_version(current)


def best_release_tag(tags: Iterable[str], current: str | None) -> str | None:
    """Pick the highest tag newer than current, if any."""
    candidates = [t for t in tags if is_newer_version(t, current)]
    if not candidates:
        return None
    return sorted(candidates, key=normalize_version)[-1]
