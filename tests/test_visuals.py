"""Tests for api.visuals.illustrate — the visuals orchestrator (#30).

`illustrate` composes api.claimcard, api.imageprompt, api.imagegen,
api.assets and api.explainer into one call. No network is used anywhere in
this file: `api.visuals.generate_image` and `api.visuals.render_claim_card`
(the names THIS module imported them under) are monkeypatched with stubs
before every test that would otherwise reach a real backend or a real font.

Covers, per the issue's acceptance criteria and testing notes:
- mode=off makes zero backend calls and writes nothing (not even an empty
  manifest)
- cover, and cover+explainers (mode=all)
- per-asset failure isolation: one bad card does not lose the cover or any
  other card
- every distinct failure type maps to a `NoticeCode.image_error` Notice
  whose MESSAGE distinguishes it (font refusal, layout refusal, provider
  config error, provider refusal, provider error, format error)
- idempotence via source_hash, and the `force` override
- the input AgentOutput is never mutated; the return value is a new object
- input notices are preserved, never replaced
- never raises: no drafts, no ledger, a failed status, or an unsafe
  session_id
"""

from __future__ import annotations

import pytest

from api import visuals
from api.assets import image_path, read_manifest, repo_relative
from api.claimcard import CardLayoutRefusedError, FontRefusedError
from api.imagegen import (
    ImageGenConfigError,
    ImageGenFormatError,
    ImageGenProviderError,
    ImageGenRefusedError,
)
from api.schema import (
    AgentOutput,
    Claim,
    ClaimKind,
    ImageAsset,
    ImageKind,
    ImageMode,
    Language,
    Notice,
    NoticeCode,
    Platform,
    PlatformOutput,
    Status,
)
from api.visuals import illustrate

_PNG = b"\x89PNG\r\n\x1a\n" + b"fake-bytes"
_SESSION = "sess1"
_CARD = {"title": "A Paper", "contribution": "A contribution."}


def _claim(id: str, claim: str, kind: ClaimKind = ClaimKind.finding) -> Claim:
    return Claim(id=id, claim=claim, source_evidence="ev", qualifier="preliminary", kind=kind)


def _draft(body: str) -> PlatformOutput:
    return PlatformOutput(platform=Platform.news, title_options=["t"], cover_copy="cc", body=body)


def _out(
    *,
    drafts: list[PlatformOutput] | None = None,
    ledger: list[Claim] | None = None,
    notices: list[Notice] | None = None,
    status: Status = Status.ok,
) -> AgentOutput:
    return AgentOutput(
        platform_outputs=drafts if drafts is not None else [_draft("Found X (c1). Also Y (c2).")],
        claim_ledger=ledger if ledger is not None else [
            _claim("c1", "Result improved by 10 units"),
            _claim("c2", "A qualitative result"),
        ],
        notices=notices or [],
        status=status,
    )


@pytest.fixture(autouse=True)
def _stub_backends(monkeypatch):
    """Default happy-path stubs; individual tests override as needed."""
    monkeypatch.setattr(visuals, "generate_image", lambda prompt: _PNG)
    monkeypatch.setattr(visuals, "render_claim_card", lambda claim: _PNG)


# --- mode=off ----------------------------------------------------------------

def test_off_mode_makes_no_backend_calls_and_writes_nothing(monkeypatch):
    calls = []
    monkeypatch.setattr(visuals, "generate_image", lambda prompt: calls.append("cover") or _PNG)
    monkeypatch.setattr(visuals, "render_claim_card", lambda claim: calls.append("card") or _PNG)

    out = _out()
    result = illustrate(out, _SESSION, ImageMode.off, _CARD, Language.zh, 3)

    assert calls == []
    assert result.images == []
    assert read_manifest(_SESSION) == []  # nothing written, not even an empty manifest


def test_off_mode_returns_new_object(monkeypatch):
    out = _out()
    result = illustrate(out, _SESSION, ImageMode.off, _CARD, Language.zh, 3)
    assert result is not out


# --- cover mode ----------------------------------------------------------------

def test_cover_mode_produces_only_a_cover_asset():
    out = _out()
    result = illustrate(out, _SESSION, ImageMode.cover, _CARD, Language.zh, 3)

    assert len(result.images) == 1
    asset = result.images[0]
    assert asset.kind is ImageKind.cover
    assert asset.claim_id == ""
    assert asset.generated is True
    assert asset.prompt  # the built prompt was recorded
    assert asset.source_hash

    saved_path = image_path(_SESSION, ImageKind.cover)
    assert saved_path.read_bytes() == _PNG
    assert asset.path == repo_relative(saved_path)
    # what the code PRODUCES, not what it was handed: repo-relative, POSIX,
    # never the absolute filesystem path (#53)
    assert asset.path == f"outputs/images/{_SESSION}/cover.png"
    assert not asset.path.startswith("/")
    assert "\\" not in asset.path


def test_cover_mode_writes_no_explainer_even_with_a_ledger():
    out = _out()
    result = illustrate(out, _SESSION, ImageMode.cover, _CARD, Language.zh, 3)
    assert all(a.kind is ImageKind.cover for a in result.images)


# --- mode=all: cover + explainers ---------------------------------------------

def test_all_mode_produces_cover_and_selected_cards():
    out = _out()
    result = illustrate(out, _SESSION, ImageMode.all, _CARD, Language.zh, 3)

    kinds = {a.kind for a in result.images}
    assert kinds == {ImageKind.cover, ImageKind.explainer}

    explainer_ids = {a.claim_id for a in result.images if a.kind is ImageKind.explainer}
    assert explainer_ids == {"c1", "c2"}

    for asset in result.images:
        if asset.kind is ImageKind.explainer:
            assert asset.generated is False
            assert asset.prompt == ""
            path = image_path(_SESSION, ImageKind.explainer, asset.claim_id)
            assert path.read_bytes() == _PNG
            assert asset.path == repo_relative(path)
            assert asset.path == f"outputs/images/{_SESSION}/{asset.claim_id}.png"
            assert not asset.path.startswith("/")
            assert "\\" not in asset.path


def test_all_mode_respects_cap(monkeypatch):
    monkeypatch.setenv("IMAGE_CAP", "1")
    out = _out()
    result = illustrate(out, _SESSION, ImageMode.all, _CARD, Language.zh, 3)
    explainers = [a for a in result.images if a.kind is ImageKind.explainer]
    assert len(explainers) == 1


def test_manifest_written_matches_returned_images():
    out = _out()
    result = illustrate(out, _SESSION, ImageMode.all, _CARD, Language.zh, 3)
    assert read_manifest(_SESSION) == result.images


# --- per-asset failure isolation ----------------------------------------------

def test_one_bad_card_does_not_lose_the_cover_or_other_cards(monkeypatch):
    def _render(claim: Claim) -> bytes:
        if claim.id == "c1":
            raise FontRefusedError("no font")
        return _PNG

    monkeypatch.setattr(visuals, "render_claim_card", _render)

    out = _out()
    result = illustrate(out, _SESSION, ImageMode.all, _CARD, Language.zh, 3)

    kinds_ids = {(a.kind, a.claim_id) for a in result.images}
    assert (ImageKind.cover, "") in kinds_ids
    assert (ImageKind.explainer, "c2") in kinds_ids
    assert (ImageKind.explainer, "c1") not in kinds_ids  # no placeholder asset

    messages = [n.message for n in result.notices if n.code is NoticeCode.image_error]
    assert any("c1" in m and "font refused" in m for m in messages)


def test_cover_failure_does_not_stop_cards(monkeypatch):
    monkeypatch.setattr(
        visuals, "generate_image", lambda prompt: (_ for _ in ()).throw(ImageGenConfigError("no key"))
    )
    out = _out()
    result = illustrate(out, _SESSION, ImageMode.all, _CARD, Language.zh, 3)

    assert not any(a.kind is ImageKind.cover for a in result.images)
    assert {a.claim_id for a in result.images if a.kind is ImageKind.explainer} == {"c1", "c2"}


# --- distinguishable failure messages ------------------------------------------

def _notice_messages(result: AgentOutput) -> list[str]:
    return [n.message for n in result.notices if n.code is NoticeCode.image_error]


@pytest.mark.parametrize(
    "error, expected_fragment",
    [
        (FontRefusedError("bad font"), "font refused"),
        (CardLayoutRefusedError("does not fit"), "layout refused"),
    ],
)
def test_card_failure_messages_are_distinguishable(monkeypatch, error, expected_fragment):
    monkeypatch.setattr(visuals, "render_claim_card", lambda claim: (_ for _ in ()).throw(error))
    out = _out(drafts=[_draft("Found X (c1).")], ledger=[_claim("c1", "A result")])
    result = illustrate(out, _SESSION, ImageMode.all, _CARD, Language.zh, 3)
    assert any(expected_fragment in m for m in _notice_messages(result))


@pytest.mark.parametrize(
    "error, expected_fragment",
    [
        (ImageGenConfigError("no key"), "not configured"),
        (ImageGenRefusedError("blocked"), "refused the request"),
        (ImageGenProviderError("network down"), "provider call failed"),
        (ImageGenFormatError("not a png"), "invalid image data"),
    ],
)
def test_cover_failure_messages_are_distinguishable(monkeypatch, error, expected_fragment):
    monkeypatch.setattr(visuals, "generate_image", lambda prompt: (_ for _ in ()).throw(error))
    out = _out()
    result = illustrate(out, _SESSION, ImageMode.cover, _CARD, Language.zh, 3)
    assert any(expected_fragment in m for m in _notice_messages(result))


def test_all_four_cover_failure_types_yield_distinct_messages(monkeypatch):
    errors = [
        ImageGenConfigError("a"),
        ImageGenRefusedError("b"),
        ImageGenProviderError("c"),
        ImageGenFormatError("d"),
    ]
    messages = set()
    for i, err in enumerate(errors):
        monkeypatch.setattr(visuals, "generate_image", lambda prompt, err=err: (_ for _ in ()).throw(err))
        out = _out()
        result = illustrate(out, f"sess{i}", ImageMode.cover, _CARD, Language.zh, 3)
        messages.update(_notice_messages(result))
    assert len(messages) == 4  # each failure type produced its own distinct text


# --- idempotence / force ------------------------------------------------------

def test_repeat_call_reuses_assets_by_source_hash(monkeypatch):
    calls = {"cover": 0, "card": 0}
    monkeypatch.setattr(visuals, "generate_image", lambda prompt: calls.__setitem__("cover", calls["cover"] + 1) or _PNG)
    monkeypatch.setattr(visuals, "render_claim_card", lambda claim: calls.__setitem__("card", calls["card"] + 1) or _PNG)

    out = _out()
    first = illustrate(out, _SESSION, ImageMode.all, _CARD, Language.zh, 3)
    second = illustrate(out, _SESSION, ImageMode.all, _CARD, Language.zh, 3)

    assert calls == {"cover": 1, "card": 2}  # second call made no new backend calls
    assert first.images == second.images


def test_force_regenerates_despite_matching_hash(monkeypatch):
    calls = {"cover": 0}
    monkeypatch.setattr(visuals, "generate_image", lambda prompt: calls.__setitem__("cover", calls["cover"] + 1) or _PNG)

    out = _out()
    illustrate(out, _SESSION, ImageMode.cover, _CARD, Language.zh, 3)
    illustrate(out, _SESSION, ImageMode.cover, _CARD, Language.zh, 3, force=True)

    assert calls["cover"] == 2


def test_changed_claim_invalidates_the_hash_and_regenerates(monkeypatch):
    calls = {"card": 0}
    monkeypatch.setattr(visuals, "render_claim_card", lambda claim: calls.__setitem__("card", calls["card"] + 1) or _PNG)

    out1 = _out(drafts=[_draft("Found X (c1).")], ledger=[_claim("c1", "Result v1")])
    illustrate(out1, _SESSION, ImageMode.all, _CARD, Language.zh, 3)

    out2 = _out(drafts=[_draft("Found X (c1).")], ledger=[_claim("c1", "Result v2 — changed")])
    illustrate(out2, _SESSION, ImageMode.all, _CARD, Language.zh, 3)

    assert calls["card"] == 2  # different claim text -> different hash -> regenerated


# --- purity: never mutates, notices preserved, platform_outputs untouched ----

def test_does_not_mutate_the_input_agent_output():
    out = _out(notices=[Notice(code=NoticeCode.ok, message="pre-existing")])
    notices_before = list(out.notices)
    images_before = list(out.images)

    result = illustrate(out, _SESSION, ImageMode.all, _CARD, Language.zh, 3)

    assert out.notices == notices_before
    assert out.images == images_before
    assert result is not out


def test_input_notices_are_preserved_and_appended_to():
    pre = Notice(code=NoticeCode.background_error, message="background skipped")
    out = _out(notices=[pre])
    result = illustrate(out, _SESSION, ImageMode.all, _CARD, Language.zh, 3)
    assert pre in result.notices


def test_platform_outputs_unchanged():
    out = _out()
    result = illustrate(out, _SESSION, ImageMode.all, _CARD, Language.zh, 3)
    assert result.platform_outputs == out.platform_outputs


# --- never raises --------------------------------------------------------------

def test_never_raises_with_no_drafts():
    out = _out(drafts=[], ledger=[])
    result = illustrate(out, _SESSION, ImageMode.all, _CARD, Language.zh, 3)
    assert isinstance(result, AgentOutput)
    # no drafts -> no cited ids -> no explainer cards, but the cover still runs
    assert not any(a.kind is ImageKind.explainer for a in result.images)
    assert any(a.kind is ImageKind.cover for a in result.images)


def test_never_raises_with_no_ledger():
    out = _out(drafts=[_draft("no markers here")], ledger=[])
    result = illustrate(out, _SESSION, ImageMode.all, _CARD, Language.zh, 3)
    assert isinstance(result, AgentOutput)
    assert not any(a.kind is ImageKind.explainer for a in result.images)


def test_never_raises_on_failed_status():
    out = _out(status=Status.failed, drafts=[], ledger=[])
    result = illustrate(out, _SESSION, ImageMode.all, _CARD, Language.zh, 3)
    assert isinstance(result, AgentOutput)


def test_never_raises_on_unsafe_session_id():
    out = _out()
    result = illustrate(out, "../escape", ImageMode.all, _CARD, Language.zh, 3)
    assert isinstance(result, AgentOutput)
    # every asset attempt failed the same way (unsafe path), isolated per asset
    assert result.images == []
    assert any(n.code is NoticeCode.image_error for n in result.notices)


def test_never_raises_on_empty_card():
    out = _out()
    result = illustrate(out, _SESSION, ImageMode.cover, {}, Language.zh, 3)
    assert isinstance(result, AgentOutput)
    assert len(result.images) == 1  # sparse card still yields a (thinner) cover


def test_never_raises_when_a_composed_call_crashes_unexpectedly(monkeypatch):
    """A totally unanticipated failure — read_manifest raising something
    other than the ValueError an unsafe id would produce — still must not
    propagate out of illustrate."""
    def _boom(session_id):
        raise RuntimeError("disk exploded")

    monkeypatch.setattr(visuals, "read_manifest", _boom)
    out = _out()
    result = illustrate(out, _SESSION, ImageMode.all, _CARD, Language.zh, 3)
    assert isinstance(result, AgentOutput)
