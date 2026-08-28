"""Tests for webui/i18n.json — the interface's text in both languages.

A key present in one language and missing from the other does not fail loudly:
it renders the raw key, like `board.complete`, to whoever chose that language.
Nothing else catches that, so it is checked here.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_STRINGS = json.loads((_ROOT / "webui" / "i18n.json").read_text(encoding="utf-8"))
_PAGES = ["index.html", "front.html"]


def test_both_languages_are_present():
    assert set(_STRINGS) == {"zh", "en"}


def test_the_two_languages_carry_identical_keys():
    zh, en = set(_STRINGS["zh"]), set(_STRINGS["en"])

    assert zh - en == set(), f"missing from en: {sorted(zh - en)}"
    assert en - zh == set(), f"missing from zh: {sorted(en - zh)}"


@pytest.mark.parametrize("language", ["zh", "en"])
def test_no_string_is_empty(language):
    blank = [key for key, value in _STRINGS[language].items() if not str(value).strip()]

    assert blank == [], f"{language} has empty strings: {blank}"


@pytest.mark.parametrize("page", _PAGES)
def test_every_key_used_in_the_markup_exists(page):
    """A data-i18n pointing at nothing shows the key to the user."""
    html = (_ROOT / "webui" / "static" / page).read_text(encoding="utf-8")
    used = set(re.findall(r'data-i18n(?:-\w+)?="([^"]+)"', html))

    missing = sorted(used - set(_STRINGS["zh"]))
    assert missing == [], f"{page} uses keys that are not defined: {missing}"


def test_placeholders_match_across_languages():
    """`{count}` in one language and `{n}` in the other silently renders raw."""
    for key, zh_value in _STRINGS["zh"].items():
        zh_vars = set(re.findall(r"\{(\w+)\}", str(zh_value)))
        en_vars = set(re.findall(r"\{(\w+)\}", str(_STRINGS["en"][key])))
        assert zh_vars == en_vars, f"{key}: zh has {zh_vars}, en has {en_vars}"


def test_every_translated_tool_still_exists_in_the_manifest():
    """A renamed or removed tool would leave its translation pointing at nothing."""
    from api.manifest import load_manifest

    declared = {tool.name for tool in load_manifest().tools}
    translated = {
        key.split(".")[1] for key in _STRINGS["zh"] if key.startswith("tool.")
    }

    assert translated <= declared, f"translations for tools that do not exist: {sorted(translated - declared)}"
