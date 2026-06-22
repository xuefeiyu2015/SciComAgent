"""Shared helpers for reading JSON out of LLM responses.

LangChain chat models return message content that may be a plain string or a
list of content blocks, and models often wrap their JSON in prose or markdown
fences. These helpers normalize that so each pipeline step can parse robustly
without duplicating the logic.
"""

from __future__ import annotations

from typing import Any


def as_text(raw: Any) -> str:
    """Coerce message content (string or list of content blocks) to text."""
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list):
        parts = [
            block.get("text", "") if isinstance(block, dict) else str(block)
            for block in raw
        ]
        return "".join(parts)
    return str(raw)


def json_object_slice(text: str) -> str:
    """Return the outermost JSON object substring, tolerating fences/prose."""
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("no JSON object found in model response")
    return text[start : end + 1]
