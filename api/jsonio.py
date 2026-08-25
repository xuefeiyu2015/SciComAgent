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

It also retries TRANSIENT provider failures (503/429/…) with exponential
backoff. A saturated model would otherwise sink a whole platform draft after
the expensive extract + ledger + background steps had already succeeded.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from typing import Any

# Provider hiccups worth waiting out: overloaded/rate-limited/gateway errors.
# Status codes are matched on word boundaries so a "1500 tokens" message in an
# otherwise permanent error is not mistaken for a 500.
_TRANSIENT_STATUS = re.compile(r"\b(429|500|502|503|504)\b")
_TRANSIENT_WORDS = (
    "unavailable",
    "resource_exhausted",
    "overloaded",
    "rate limit",
    "ratelimit",
    "too many requests",
    "timeout",
    "timed out",
    "temporarily",
    "try again later",
)

# Extra attempts after the first when the provider is transiently unhappy, and
# the base for the exponential backoff between them (2s, 4s, 8s, 16s, 32s).
#
# Sized from a real outage: a saturated gemini-flash-latest answered 34 requests
# with 503 against 20 successes, and the provider SDK's own internal retries had
# already been exhausted underneath us each time. A short budget just converts a
# temporary outage into a lost draft. Waiting is cheap now that runs are async.
_TRANSIENT_RETRIES = 5
_BACKOFF_BASE_S = 2.0


def is_transient(err: Exception) -> bool:
    """Whether an exception looks like a temporary provider failure."""
    text = str(err).lower()
    return bool(_TRANSIENT_STATUS.search(text)) or any(
        word in text for word in _TRANSIENT_WORDS
    )


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


def _invoke_with_backoff(
    model: Any,
    messages: Any,
    *,
    retries: int,
    sleep: Callable[[float], None],
) -> Any:
    """Call ``model.invoke``, waiting out transient provider failures.

    A permanent error (bad request, auth, unknown model) is re-raised on the
    first attempt — retrying it only burns wall-clock time.
    """
    for attempt in range(retries + 1):
        try:
            return model.invoke(messages)
        except Exception as err:
            if attempt == retries or not is_transient(err):
                raise
            sleep(_BACKOFF_BASE_S * (2**attempt))
    raise AssertionError("unreachable")  # pragma: no cover


def invoke_json(
    model: Any,
    messages: Any,
    *,
    retries: int = 2,
    transient_retries: int = _TRANSIENT_RETRIES,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Invoke a chat model and parse one JSON object from its reply.

    Two independent backstops:

    - **Transient provider failures** (503 UNAVAILABLE, 429, gateway errors)
      are retried with exponential backoff before the response is even read.
      A permanent error (400, auth, unknown model) is raised immediately.
    - **Empty or unparseable content** — the failure mode where a provider
      returns an empty candidate (silently surfaced as ``content=""``) or
      truncates the JSON — is retried by re-invoking the model.

    Args:
        model: a LangChain chat model (anything with ``.invoke(messages)``).
        messages: the message list to send.
        retries: extra attempts after the first, so total tries = retries + 1.
        transient_retries: extra attempts after the first when the provider
            fails transiently.
        sleep: injection point for the backoff wait (tests pass a no-op).

    Returns:
        The parsed JSON object as a dict.

    Raises:
        ValueError: if no JSON object could be parsed within the attempts.
    """
    attempts = retries + 1
    last_err: Exception | None = None
    for _ in range(attempts):
        response = _invoke_with_backoff(
            model, messages, retries=transient_retries, sleep=sleep
        )
        text = as_text(response.content)
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
