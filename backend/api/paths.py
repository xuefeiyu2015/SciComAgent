"""Session/claim id safety — the one place this rule lives.

Both `api.jobs` (mirror paths) and `api.assets` (image paths) build filenames
out of caller-supplied ids. Both need the same guard: an id must be ours, not
a path fragment an attacker could use to escape the directory it is joined
into. This module owns that single predicate so neither caller defines its
own copy that could drift from the other's.

A genuine leaf: standard library only, and it imports nothing from
`api.jobs`, `api.assets`, or `api.visuals`, so either side of that import
graph can depend on it without risk of a cycle.
"""

from __future__ import annotations

__all__ = ["is_safe_session_id"]


def is_safe_session_id(value: str) -> bool:
    """Guard a mirror/asset path: ids are ours, never caller-shaped path fragments."""
    return bool(value) and all(part.isalnum() for part in value.split("_"))
