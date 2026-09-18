"""Asset storage and manifest for the image feature.

One module owns ``outputs/images/<session_id>/`` and its manifest
(``images.json``), so nothing else in the feature ever builds an image path by
hand. It mirrors the shape of ``api.jobs``'s disk mirror: a per-session
directory, a JSON sidecar, and best-effort reads that degrade to an empty
result rather than raising on anything short of an attacker-shaped id.

Generating or drawing images is out of scope here (#28, #26) — this module
only decides WHERE an asset lives and records WHAT was written, in the shape
``api.schema.ImageAsset`` already defines (#23).

/api owns this because it is business logic; mcp_server only wraps it.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from api.jobs import _is_safe_session_id
from api.schema import ImageAsset, ImageKind

_log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_IMAGES_DIR = _REPO_ROOT / "outputs" / "images"

_MANIFEST_NAME = "images.json"


def _require_safe(value: str, *, what: str) -> None:
    """Reject anything `api.jobs._is_safe_session_id` would reject.

    Both `session_id` and `claim_id` are attacker-shaped inputs in principle —
    a claim id ultimately becomes a filename too — so both halves of the path
    are checked with the SAME rule `api.jobs` already uses for its mirrors,
    rather than a second, possibly-looser one invented here.
    """
    if not _is_safe_session_id(value):
        raise ValueError(f"unsafe {what}: {value!r}")


def image_dir(session_id: str) -> Path:
    """The session's image directory, created on demand."""
    _require_safe(session_id, what="session_id")
    path = _IMAGES_DIR / session_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def image_path(session_id: str, kind: ImageKind, claim_id: str = "") -> Path:
    """The only place an image filename is built.

    The cover is always ``cover.png``. An explainer is ``<claim_id>.png`` —
    `Claim.id` already carries its own ``c`` prefix (`api.ledger` assigns
    ``id=f"c{n}"``, e.g. ``c17``), so the filename is ``c17.png``, never
    ``cc17.png``.

    `claim_id` is checked for every `kind`, not just `explainer` — it is
    unused by the cover filename, but an attacker-shaped value passed
    alongside `kind=cover` must still raise rather than be silently ignored.
    An explainer additionally requires a non-empty `claim_id`; a cover does
    not, since it never appears in that filename.
    """
    _require_safe(session_id, what="session_id")
    if kind is ImageKind.explainer:
        _require_safe(claim_id, what="claim_id")  # non-empty and safe
        name = f"{claim_id}.png"
    else:
        if claim_id:  # optional for a cover, but must be safe if given at all
            _require_safe(claim_id, what="claim_id")
        name = "cover.png"
    return image_dir(session_id) / name


def _manifest_path(session_id: str) -> Path:
    _require_safe(session_id, what="session_id")
    return image_dir(session_id) / _MANIFEST_NAME


def write_manifest(session_id: str, images: list[ImageAsset]) -> None:
    """Write the session's manifest, replacing whatever was there."""
    path = _manifest_path(session_id)
    payload = [asset.model_dump(mode="json") for asset in images]
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def read_manifest(session_id: str) -> list[ImageAsset]:
    """The session's manifest, or ``[]`` when there is none yet or it is unreadable.

    A missing ``images.json`` (no images generated yet) and a corrupt one
    (partial write, hand-edited, from an incompatible version) are both
    reported the same way — an empty list rather than a raised exception —
    because neither is a caller error. Only an unsafe `session_id` raises.
    """
    path = _manifest_path(session_id)
    try:
        if not path.exists():
            return []
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            return []
        return [ImageAsset.model_validate(item) for item in raw]
    except Exception as err:  # corrupt/non-JSON/schema-mismatched manifest
        _log.debug("session %s: unreadable image manifest (%s)", session_id, err)
        return []


__all__ = [
    "image_dir",
    "image_path",
    "write_manifest",
    "read_manifest",
]
