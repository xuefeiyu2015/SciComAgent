"""Language labels shared across pipeline steps.

A single place to turn the `Language` enum into a human-readable name for
prompts, so ledger, draft and check stay consistent (and adding a language is
one edit). Language is a PARAMETER threaded through the pipeline, never a
separate file (see CLAUDE.md).
"""

from __future__ import annotations

from api.schema import Language

LANGUAGE_NAMES = {Language.zh: "Chinese (中文)", Language.en: "English"}


def language_label(language: Language) -> str:
    """Human-readable name for a language, for use in prompts."""
    return LANGUAGE_NAMES.get(language, language.value)
