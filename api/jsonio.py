"""Shared helpers for reading JSON out of LLM responses.

LangChain chat models return message content that may be a plain string or a
list of content blocks, and models often wrap their JSON in prose or markdown
fences. These helpers normalize that so each pipeline step can parse robustly
without duplicating the logic.

``invoke_json`` is the entry point each pipeline step uses: it calls the model
and parses one JSON object, retrying on empty / unparseable output. The retry
matters because some providers (e.g. Gemini with "thinking" enabled) can return
an empty candidate that LangChain surfaces *silently* as ``content=""`` — the
client's own network retries never cover that case.
"""

from __future__ import annotations

import json
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


def invoke_json(model: Any, messages: Any, *, retries: int = 2) -> dict[str, Any]:
    """Invoke a chat model and parse one JSON object from its reply.

    Retries on an empty or unparseable response — the failure mode where a
    provider returns an empty candidate (silently surfaced as ``content=""``)
    or truncates the JSON. A truncated ``{...}`` fails ``json.loads`` and is
    retried too. Network errors from ``model.invoke`` are already retried by the
    provider client and propagate unchanged.

    Args:
        model: a LangChain chat model (anything with ``.invoke(messages)``).
        messages: the message list to send.
        retries: extra attempts after the first, so total tries = retries + 1.

    Returns:
        The parsed JSON object as a dict.

    Raises:
        ValueError: if no JSON object could be parsed within the attempts.
    """
    attempts = retries + 1
    last_err: Exception | None = None
    for _ in range(attempts):
        text = as_text(model.invoke(messages).content)
        if not text.strip():
            last_err = ValueError("model returned empty content")
            continue
        try:
            data = json.loads(json_object_slice(text))
        except ValueError as err:  # json_object_slice + JSONDecodeError
            last_err = err
            continue
        if isinstance(data, dict):
            return data
        last_err = ValueError("model response JSON was not an object")
    raise ValueError(
        f"model did not return a JSON object after {attempts} attempts: {last_err}"
    )
