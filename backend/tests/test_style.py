"""Tests for api.style.load_style_profile — models are stubbed, no network/keys.

Distillation quality lives in the two prompts; here we verify the wiring
(stylist role with extractor fallback, then the reviewer audit), the
folder/char guardrails, parse normalization, content-caching, and the
faithfulness boundary: the profile carries voice, never facts.

Every test points EXAMPLES_DIR at a tmp_path, so none of them read the
operator's real api/styles/examples/ folder.
"""

from __future__ import annotations

import json

import pytest
from langchain_core.messages import AIMessage

from api import style
from api.schema import StyleProfile
from api.style import (
    MAX_CHARS_PER_FILE,
    MAX_FILES,
    MAX_ITEM_CHARS,
    MAX_ITEMS,
    _parse_profile,
    load_style_profile,
)

_PROFILE_JSON = json.dumps(
    {
        "voice": "a curious peer thinking out loud",
        "rhythm": "long build-up, then a short landing",
        "openings": ["open inside a concrete physical scene"],
        "vocabulary": ["plain register, one technical term at a time"],
        "devices": ["an analogy carried through and paid off at the end"],
        "avoid": ["hype", "throat-clearing"],
    }
)

_CLEAN_AUDIT = json.dumps({"content_bearing": []})


class _StubModel:
    """Returns queued replies; records every message list it was given."""

    def __init__(self, *replies):
        self._replies = list(replies)
        self.seen: list = []

    def invoke(self, messages):
        self.seen.append(messages)
        return AIMessage(content=self._replies.pop(0))


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """Point the loader at an empty tmp folder and drop the distill cache."""
    monkeypatch.setattr(style, "EXAMPLES_DIR", tmp_path)
    style.clear_cache()
    yield
    style.clear_cache()


def _stub_models(monkeypatch, *, profile=_PROFILE_JSON, audit=_CLEAN_AUDIT):
    """Wire stylist -> `profile` and reviewer -> `audit`; record the roles."""
    models = {"stylist": _StubModel(profile), "reviewer": _StubModel(audit)}
    roles: list[tuple[str, str | None]] = []

    def fake_get_model(role, temperature=0.0, fallback=None):
        roles.append((role, fallback))
        return models[role]

    monkeypatch.setattr(style, "get_model", fake_get_model)
    return models, roles


def _write(tmp_path, name, text="An article.\n\nWith paragraphs."):
    (tmp_path / name).write_text(text, encoding="utf-8")


# --- empty folder -> default behavior -----------------------------------------

def test_empty_folder_returns_none_without_calling_a_model(monkeypatch):
    monkeypatch.setattr(
        style, "get_model",
        lambda *a, **k: pytest.fail("no model may be called for an empty folder"),
    )
    assert load_style_profile() is None


def test_scaffolding_does_not_count_as_an_example(monkeypatch, tmp_path):
    # README.md / .gitkeep are tracked in the real folder; an operator who has
    # dropped nothing in must still get the default drafting behavior.
    _write(tmp_path, "README.md", "drop example articles here")
    (tmp_path / ".gitkeep").write_text("")
    monkeypatch.setattr(
        style, "get_model",
        lambda *a, **k: pytest.fail("scaffolding must not be distilled"),
    )
    assert load_style_profile() is None


def test_non_article_suffixes_ignored(monkeypatch, tmp_path):
    _write(tmp_path, "notes.pdf")
    _write(tmp_path, "cover.png")
    monkeypatch.setattr(
        style, "get_model",
        lambda *a, **k: pytest.fail("only .md/.txt are examples"),
    )
    assert load_style_profile() is None


def test_disabled_setting_skips_distillation(monkeypatch, tmp_path):
    _write(tmp_path, "a.md")
    monkeypatch.setattr(style, "resolve_setting", lambda *a, **k: "false")
    monkeypatch.setattr(
        style, "get_model",
        lambda *a, **k: pytest.fail("style.enabled=false must not distill"),
    )
    assert load_style_profile() is None


# --- populated folder -> a full profile ---------------------------------------

def test_populated_folder_returns_full_profile(monkeypatch, tmp_path):
    _write(tmp_path, "a.md")
    _write(tmp_path, "b.txt")
    models, roles = _stub_models(monkeypatch)

    profile = load_style_profile()

    assert isinstance(profile, StyleProfile)
    assert profile.voice == "a curious peer thinking out loud"
    assert profile.rhythm == "long build-up, then a short landing"
    assert profile.openings == ["open inside a concrete physical scene"]
    assert profile.devices == ["an analogy carried through and paid off at the end"]
    assert profile.avoid == ["hype", "throat-clearing"]
    assert profile.sources == ["a.md", "b.txt"]  # audit trail, sorted

    # stylist (cheap, falls back to extractor) distills; reviewer audits.
    # Different models + different prompts — no grading your own work.
    assert roles == [("stylist", "extractor"), ("reviewer", None)]
    assert models["stylist"].seen[0][0].content == style._prompt()
    assert models["reviewer"].seen[0][0].content == style._audit_prompt()
    assert style._prompt() != style._audit_prompt()


def test_filenames_are_withheld_from_the_model(monkeypatch, tmp_path):
    # a filename usually names the subject; the model reads craft, not topic
    _write(tmp_path, "why-the-gut-microbiome-matters.md")
    models, _ = _stub_models(monkeypatch)

    load_style_profile()

    payload = models["stylist"].seen[0][1].content
    assert "gut-microbiome" not in payload
    assert "EXAMPLE 1" in payload


def test_returned_profile_is_a_copy(monkeypatch, tmp_path):
    _write(tmp_path, "a.md")
    _stub_models(monkeypatch)

    first = load_style_profile()
    first.voice = "mutated"
    assert load_style_profile().voice == "a curious peer thinking out loud"


# --- guardrails ----------------------------------------------------------------

def test_reads_at_most_max_files(monkeypatch, tmp_path):
    for i in range(MAX_FILES + 4):
        _write(tmp_path, f"{i:02d}.md", f"article {i}")
    _stub_models(monkeypatch)

    profile = load_style_profile()

    assert len(profile.sources) == MAX_FILES
    assert profile.sources == [f"{i:02d}.md" for i in range(MAX_FILES)]


def test_long_article_capped_but_keeps_both_ends(monkeypatch, tmp_path):
    # a head-only cut would hide how a piece closes, which the prompt asks about
    _write(tmp_path, "long.md", "HEAD-MARKER " + ("filler " * 4000) + " TAIL-MARKER")
    models, _ = _stub_models(monkeypatch)

    load_style_profile()

    payload = models["stylist"].seen[0][1].content
    assert "HEAD-MARKER" in payload
    assert "TAIL-MARKER" in payload
    assert style._ELISION.strip() in payload      # the gap is marked, not hidden
    assert len(payload) <= MAX_CHARS_PER_FILE + 32  # + the "### EXAMPLE 1" header


def test_short_article_is_not_excerpted(monkeypatch, tmp_path):
    _write(tmp_path, "short.md", "a whole tiny article")
    models, _ = _stub_models(monkeypatch)

    load_style_profile()

    assert style._ELISION.strip() not in models["stylist"].seen[0][1].content


def test_parse_caps_items_and_drops_blanks():
    profile = _parse_profile(
        {
            "voice": "  warm  ",
            "openings": ["o1", "  ", None, "o2", "o3", "o4", "o5", "o6"],
            "devices": "not-a-list",
            "vocabulary": ["x" * (MAX_ITEM_CHARS + 50)],
        },
        sources=["a.md"],
    )
    assert profile.voice == "warm"
    assert profile.openings == ["o1", "o2", "o3", "o4", "o5"]
    assert len(profile.openings) == MAX_ITEMS
    assert profile.devices == []            # non-list normalized
    assert profile.rhythm == ""             # missing key
    assert len(profile.vocabulary[0]) == MAX_ITEM_CHARS


# --- caching -------------------------------------------------------------------

def test_repeated_loads_distill_once(monkeypatch, tmp_path):
    _write(tmp_path, "a.md")
    _, roles = _stub_models(monkeypatch)

    # three platforms in one run ask for the same profile
    assert load_style_profile() == load_style_profile() == load_style_profile()
    assert roles == [("stylist", "extractor"), ("reviewer", None)]  # one pass only


def test_editing_an_example_redistills(monkeypatch, tmp_path):
    _write(tmp_path, "a.md", "first version")
    models, roles = _stub_models(monkeypatch)
    load_style_profile()

    # cache is keyed on file CONTENT, so an edit must invalidate it
    other = json.dumps({"voice": "second voice"})
    models["stylist"]._replies.append(other)
    models["reviewer"]._replies.append(_CLEAN_AUDIT)
    _write(tmp_path, "a.md", "second version")

    assert load_style_profile().voice == "second voice"
    assert len(roles) == 4  # two full distill+audit passes


# --- faithfulness: voice in, facts out -----------------------------------------

def test_fact_shaped_numbers_are_dropped(monkeypatch, tmp_path):
    """A number that slipped past the distill prompt never reaches the drafter."""
    _write(tmp_path, "a.md")
    leaky = json.dumps(
        {
            "voice": "opens with the 47% figure",
            "rhythm": "cites 1,200 participants",
            "openings": ["the 2019 breakthrough framing", "open in a concrete scene"],
            "vocabulary": ["a sample of 1500 people"],
            "devices": ["an analogy carried through"],
            "avoid": ["hype"],
        }
    )
    _stub_models(monkeypatch, profile=leaky)

    profile = load_style_profile()

    assert profile.voice == ""       # percentage
    assert profile.rhythm == ""      # thousands separator
    assert profile.openings == ["open in a concrete scene"]  # year dropped
    assert profile.vocabulary == []  # large count
    assert profile.devices == ["an analogy carried through"]  # clean craft survives


def test_rhythm_guidance_with_small_numbers_survives(monkeypatch, tmp_path):
    """The number filter must not eat the concrete pacing hints we want."""
    _write(tmp_path, "a.md")
    _stub_models(
        monkeypatch,
        profile=json.dumps({"rhythm": "paragraphs of 100-150 words, then a short one"}),
    )

    assert load_style_profile().rhythm == "paragraphs of 100-150 words, then a short one"


_ECHO_ARTICLE = """
The morning the trial results came back, the lab went quiet. Researchers had
spent a decade arguing about the gut-brain axis, and at the end of it all the
answer arrived on a Tuesday. Nobody in the room said a word for a long moment.
"""


def test_verbatim_echoes_of_the_source_are_dropped(monkeypatch, tmp_path):
    """An entry quoting the article is copying content, not describing craft."""
    _write(tmp_path, "a.md", _ECHO_ARTICLE)
    _stub_models(monkeypatch, profile=json.dumps({
        "voice": "borrows the phrase the lab went quiet",
        "rhythm": "long build-up, then a short landing",
        "openings": ["open on the morning the trial results came back",
                     "open inside a concrete physical scene"],
        "vocabulary": ["returns to the gut-brain axis metaphor"],
        "devices": ["an analogy carried through and paid off at the end"],
    }))

    profile = load_style_profile()

    assert profile.voice == ""                              # quoted the article
    assert profile.openings == ["open inside a concrete physical scene"]
    assert profile.vocabulary == []                         # named the subject
    # craft language survives, including a phrase sharing only stopwords
    assert profile.rhythm == "long build-up, then a short landing"
    assert profile.devices == ["an analogy carried through and paid off at the end"]


def test_echo_check_handles_cjk(monkeypatch, tmp_path):
    # CJK has no spaces, so echoes are compared as character runs
    _write(tmp_path, "cn.md", "那天早上，实验室安静得出奇。研究者们争论了十年的肠脑轴问题。")
    _stub_models(monkeypatch, profile=json.dumps({
        "voice": "回到肠脑轴问题的比喻",
        "rhythm": "先铺陈场景，再点明抽象概念",
    }))

    profile = load_style_profile()

    assert profile.voice == ""                              # echoed the source
    assert profile.rhythm == "先铺陈场景，再点明抽象概念"      # pure craft, kept


def test_common_phrasing_is_not_treated_as_an_echo(monkeypatch, tmp_path):
    """Connective phrases shared with any prose must not trip the check."""
    _write(tmp_path, "a.md", _ECHO_ARTICLE)
    _stub_models(monkeypatch, profile=json.dumps({
        "devices": ["pays the analogy off at the end of it all"],
    }))

    # "at the end of it all" appears verbatim in the article but carries no
    # content words — dropping this would cost real guidance for no safety gain
    assert load_style_profile().devices == ["pays the analogy off at the end of it all"]


def test_audit_drops_content_bearing_entries(monkeypatch, tmp_path):
    """Topic leakage no regex can see is removed by the reviewer audit."""
    _write(tmp_path, "a.md")
    leaky = json.dumps(
        {
            "voice": "speaks as a gut-microbiome researcher to skeptical patients",
            "rhythm": "long build-up, then a short landing",
            "openings": ["open in a concrete scene", "start from the black-hole metaphor"],
            "devices": ["an analogy carried through"],
        }
    )
    audit = json.dumps({"content_bearing": ["voice", "openings.1"]})
    models, _ = _stub_models(monkeypatch, profile=leaky, audit=audit)

    profile = load_style_profile()

    assert profile.voice == ""
    assert profile.openings == ["open in a concrete scene"]
    assert profile.rhythm == "long build-up, then a short landing"  # clean, kept
    assert profile.devices == ["an analogy carried through"]

    # the auditor is addressed with ids and never asked to rewrite anything
    audited = json.loads(models["reviewer"].seen[0][1].content)
    assert set(audited) == {"voice", "rhythm", "openings.0", "openings.1", "devices.0"}


def test_audit_failure_ships_no_profile(monkeypatch, tmp_path):
    """Fail closed: a broken audit must not release an unaudited voice."""
    _write(tmp_path, "a.md")

    def fake_get_model(role, temperature=0.0, fallback=None):
        if role == "reviewer":
            raise ValueError("model role 'reviewer': missing provider")
        return _StubModel(_PROFILE_JSON)

    monkeypatch.setattr(style, "get_model", fake_get_model)

    with pytest.raises(ValueError):
        load_style_profile()


def test_audit_without_a_verdict_list_keeps_the_profile(monkeypatch, tmp_path):
    # a reply carrying no content_bearing list means "nothing flagged"
    _write(tmp_path, "a.md")
    _stub_models(monkeypatch, audit=json.dumps({"notes": "all clean"}))

    assert load_style_profile().voice == "a curious peer thinking out loud"


def test_profile_never_carries_ledger_style_fields(monkeypatch, tmp_path):
    """The schema itself has no room for a fact: voice fields only."""
    _write(tmp_path, "a.md")
    _stub_models(monkeypatch)

    profile = load_style_profile()

    assert set(profile.model_dump()) == {
        "voice", "rhythm", "openings", "vocabulary", "devices", "avoid", "sources",
    }
