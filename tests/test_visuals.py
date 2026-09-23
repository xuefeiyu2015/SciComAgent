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
- the cover lettering check and its retry loop (#67), with a detector stub
  that is CONTENT-ADDRESSED: `_detector` below reads the bytes it is handed
  and answers from committed fixtures — "lettering" for `lettered.png`,
  "clean" for `clean.png`. It takes no "say yes this time" parameter, so
  production code that forgot to pass the new attempt's bytes through, or
  passed a placeholder, or checked a cached copy, makes these tests fail
  rather than pass.
"""

from __future__ import annotations

from pathlib import Path

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
from api.lettering import LetteringCheckConfigError, LetteringCheckProviderError
from api.schema import (
    AgentOutput,
    Claim,
    ClaimKind,
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

# Real PNGs, committed under tests/fixtures/cover/ — open them and one has
# the word LETTERING on it, the other has no glyph anywhere (#67 AC-14).
_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "cover"
_LETTERED_PNG = (_FIXTURES / "lettered.png").read_bytes()
_CLEAN_PNG = (_FIXTURES / "clean.png").read_bytes()


def _detector(image_bytes: bytes) -> bool:
    """The verdict comes from the BYTES, never from a flag (#67 AC-16).

    There is deliberately no "say yes this time" parameter: a test scripts
    what `generate_image` RETURNS, and the answer follows from that. Bytes
    that no stub produced are an error rather than a guess, so production
    code that hands the detector a placeholder, an empty buffer or a stale
    cached copy cannot quietly pass.
    """
    if image_bytes == _LETTERED_PNG:
        return True
    if image_bytes in (_CLEAN_PNG, _PNG):
        return False
    raise AssertionError(
        "the detector was handed bytes that no stub produced: "
        f"{image_bytes[:16]!r} ({len(image_bytes)} bytes)"
    )


def _scripted_generator(sequence, calls):
    """A `generate_image` stub returning `sequence` in order, counting calls."""

    def _generate(prompt):
        calls.append(prompt)
        return sequence[min(len(calls) - 1, len(sequence) - 1)]

    return _generate


def _counted_detector(calls):
    """`_detector`, wrapped so a test can count the calls it made."""

    def _check(image_bytes):
        calls.append(image_bytes)
        return _detector(image_bytes)

    return _check


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
    """Default happy-path stubs; individual tests override as needed.

    The detector is stubbed with the same content-addressed `_detector` the
    retry tests use — not with `lambda _: False`. No test in this file may
    reach a real model or a real key.
    """
    monkeypatch.setattr(visuals, "generate_image", lambda prompt: _PNG)
    monkeypatch.setattr(visuals, "render_claim_card", lambda claim: _PNG)
    monkeypatch.setattr(visuals, "contains_lettering", _detector)


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


# --- #67: the cover lettering check and its retry loop ------------------------
#
# Every test below scripts what `generate_image` RETURNS and lets `_detector`
# read those bytes. None of them tells the detector what to answer, and none
# asserts on a flag the production code set: the assertions are counted stub
# calls, the bytes on disk, and the notices.

def _cover_notices(result: AgentOutput) -> list[str]:
    return [n.message for n in result.notices if n.code is NoticeCode.image_error]


def test_a_clean_cover_is_checked_once_with_the_bytes_that_were_generated(monkeypatch):
    """AC-18, first direction: delete the call site and this test fails."""
    image_calls: list[str] = []
    detector_calls: list[bytes] = []
    monkeypatch.setattr(
        visuals, "generate_image", _scripted_generator([_CLEAN_PNG], image_calls)
    )
    monkeypatch.setattr(visuals, "contains_lettering", _counted_detector(detector_calls))

    out = _out()
    result = illustrate(out, _SESSION, ImageMode.cover, _CARD, Language.zh, 3)

    assert len(image_calls) == 1
    assert detector_calls == [_CLEAN_PNG]  # the real bytes, not a placeholder
    assert image_path(_SESSION, ImageKind.cover).read_bytes() == _CLEAN_PNG
    assert _cover_notices(result) == []
    assert result.images[0].source_hash  # clean: reusable next time


def test_a_lettered_first_attempt_is_regenerated(monkeypatch):
    """AC-5/AC-17/AC-18, second direction: ignore the detector's result and
    this test fails, because the lettered first attempt would be what ships."""
    image_calls: list[str] = []
    detector_calls: list[bytes] = []
    monkeypatch.setattr(
        visuals,
        "generate_image",
        _scripted_generator([_LETTERED_PNG, _CLEAN_PNG], image_calls),
    )
    monkeypatch.setattr(visuals, "contains_lettering", _counted_detector(detector_calls))

    out = _out()
    result = illustrate(out, _SESSION, ImageMode.cover, _CARD, Language.zh, 3)

    assert len(image_calls) == 2
    assert len(detector_calls) == 2
    assert detector_calls == [_LETTERED_PNG, _CLEAN_PNG]  # each attempt, in order
    # the SECOND attempt's bytes are what is on disk
    assert image_path(_SESSION, ImageKind.cover).read_bytes() == _CLEAN_PNG
    assert _cover_notices(result) == []
    assert len(result.images) == 1


def test_the_budget_is_exhausted_and_then_the_run_continues(monkeypatch):
    """AC-6/AC-7: exactly `cover_attempts` image calls, no more, no fewer."""
    image_calls: list[str] = []
    detector_calls: list[bytes] = []
    monkeypatch.setattr(
        visuals, "generate_image", _scripted_generator([_LETTERED_PNG], image_calls)
    )
    monkeypatch.setattr(visuals, "contains_lettering", _counted_detector(detector_calls))

    out = _out()
    result = illustrate(out, _SESSION, ImageMode.cover, _CARD, Language.zh, 3)

    assert len(image_calls) == 3  # the default budget
    assert len(detector_calls) == 3
    # the last attempt still ships
    assert image_path(_SESSION, ImageKind.cover).read_bytes() == _LETTERED_PNG
    assert len(result.images) == 1
    assert result.images[0].kind is ImageKind.cover

    flagged = [m for m in _cover_notices(result) if "may contain lettering" in m]
    assert len(flagged) == 1
    assert "3" in flagged[0]


@pytest.mark.parametrize(
    "configured, expected",
    [("1", 1), ("2", 2), ("5", 5), ("0", 1), ("-4", 1), ("", 3), ("abc", 3)],
)
def test_the_attempt_budget_is_read_from_config(monkeypatch, configured, expected):
    monkeypatch.setenv("IMAGE_COVER_ATTEMPTS", configured)
    image_calls: list[str] = []
    monkeypatch.setattr(
        visuals, "generate_image", _scripted_generator([_LETTERED_PNG], image_calls)
    )

    out = _out()
    result = illustrate(out, _SESSION, ImageMode.cover, _CARD, Language.zh, 3)

    assert len(image_calls) == expected
    assert visuals._resolve_cover_attempts() == expected
    assert any(f"{expected} attempt" in m for m in _cover_notices(result))


def test_the_budget_is_read_from_the_config_file_too(monkeypatch, isolated_config):
    from api import config_loader

    isolated_config.write_text("images:\n  cover_attempts: 2\n", encoding="utf-8")
    config_loader.reload_config()

    image_calls: list[str] = []
    monkeypatch.setattr(
        visuals, "generate_image", _scripted_generator([_LETTERED_PNG], image_calls)
    )
    illustrate(_out(), _SESSION, ImageMode.cover, _CARD, Language.zh, 3)

    assert len(image_calls) == 2


def test_a_flagged_cover_records_no_source_hash_and_is_regenerated(monkeypatch):
    """AC-8: a cover that shipped flagged must never be reused, so its notice
    can never be dropped by a later run that keeps the file."""
    image_calls: list[str] = []
    monkeypatch.setattr(
        visuals, "generate_image", _scripted_generator([_LETTERED_PNG], image_calls)
    )

    out = _out()
    first = illustrate(out, _SESSION, ImageMode.cover, _CARD, Language.zh, 3)
    assert first.images[0].source_hash == ""

    second = illustrate(out, _SESSION, ImageMode.cover, _CARD, Language.zh, 3)
    assert len(image_calls) == 6  # three more attempts, not a reuse
    assert any("may contain lettering" in m for m in _cover_notices(second))


def test_a_clean_cover_is_reused_with_no_image_and_no_detector_call(monkeypatch):
    """AC-8, the other side: the check does not re-run on a reused cover."""
    image_calls: list[str] = []
    detector_calls: list[bytes] = []
    monkeypatch.setattr(
        visuals, "generate_image", _scripted_generator([_CLEAN_PNG], image_calls)
    )
    monkeypatch.setattr(visuals, "contains_lettering", _counted_detector(detector_calls))

    out = _out()
    illustrate(out, _SESSION, ImageMode.cover, _CARD, Language.zh, 3)
    assert (len(image_calls), len(detector_calls)) == (1, 1)

    second = illustrate(out, _SESSION, ImageMode.cover, _CARD, Language.zh, 3)
    assert (len(image_calls), len(detector_calls)) == (1, 1)  # zero new calls
    assert len(second.images) == 1
    assert _cover_notices(second) == []


@pytest.mark.parametrize(
    "error",
    [
        LetteringCheckConfigError("no image_reviewer model configured"),
        LetteringCheckProviderError("503 unavailable"),
    ],
)
def test_a_detector_failure_keeps_the_cover_and_says_it_was_not_checked(
    monkeypatch, error
):
    """AC-9: fail OPEN on the asset, never silent on the notice. And never
    reported as "image backend not configured", which would skip the cover."""
    image_calls: list[str] = []
    monkeypatch.setattr(
        visuals, "generate_image", _scripted_generator([_CLEAN_PNG], image_calls)
    )
    monkeypatch.setattr(
        visuals,
        "contains_lettering",
        lambda image_bytes: (_ for _ in ()).throw(error),
    )

    out = _out()
    result = illustrate(out, _SESSION, ImageMode.cover, _CARD, Language.zh, 3)

    assert len(result.images) == 1
    assert result.images[0].kind is ImageKind.cover
    assert image_path(_SESSION, ImageKind.cover).read_bytes() == _CLEAN_PNG
    assert result.images[0].source_hash == ""  # unchecked: not reusable either

    messages = _cover_notices(result)
    assert len(messages) == 1
    assert "not checked" in messages[0]
    assert "not configured" not in messages[0]
    assert "skipped" not in messages[0]
    assert len(image_calls) == 1  # a detector failure is not a reason to redraw


def test_a_detector_failure_is_not_mistaken_for_an_image_backend_failure(monkeypatch):
    """The `except LetteringCheckError` clause must precede `except
    ImageGenError`: LetteringCheckError IS an ImageGenError, so the wrong
    order would drop the cover instead of keeping it."""
    monkeypatch.setattr(
        visuals,
        "contains_lettering",
        lambda image_bytes: (_ for _ in ()).throw(LetteringCheckConfigError("nope")),
    )
    result = illustrate(_out(), _SESSION, ImageMode.cover, _CARD, Language.zh, 3)

    assert [a.kind for a in result.images] == [ImageKind.cover]
    assert not any("image generation failed" in m for m in _cover_notices(result))


def test_no_api_key_value_appears_in_a_lettering_notice(monkeypatch):
    """AC-12, in the shape tests/test_imagegen.py uses."""
    monkeypatch.setenv("GOOGLE_API_KEY", "sk-super-secret-key-value")
    monkeypatch.setattr(
        visuals,
        "contains_lettering",
        lambda image_bytes: (_ for _ in ()).throw(
            LetteringCheckProviderError("401: key *** rejected")
        ),
    )
    result = illustrate(_out(), _SESSION, ImageMode.cover, _CARD, Language.zh, 3)

    for message in _cover_notices(result):
        assert "sk-super-secret-key-value" not in message


# --- #67: per-asset isolation, proven three ways (AC-10) ----------------------

def _explainer_ids(result: AgentOutput) -> set[str]:
    return {a.claim_id for a in result.images if a.kind is ImageKind.explainer}


def test_isolation_when_the_detector_always_says_lettering(monkeypatch):
    monkeypatch.setattr(visuals, "generate_image", lambda prompt: _LETTERED_PNG)

    result = illustrate(_out(), _SESSION, ImageMode.all, _CARD, Language.zh, 3)

    assert _explainer_ids(result) == {"c1", "c2"}
    messages = _cover_notices(result)
    assert len(messages) == 1  # the cover's own, and nothing else
    assert "may contain lettering" in messages[0]
    assert not any("c1" in m or "c2" in m for m in messages)


def test_isolation_when_the_detector_always_raises(monkeypatch):
    monkeypatch.setattr(
        visuals,
        "contains_lettering",
        lambda image_bytes: (_ for _ in ()).throw(LetteringCheckProviderError("down")),
    )

    result = illustrate(_out(), _SESSION, ImageMode.all, _CARD, Language.zh, 3)

    assert _explainer_ids(result) == {"c1", "c2"}
    assert any(a.kind is ImageKind.cover for a in result.images)
    messages = _cover_notices(result)
    assert len(messages) == 1
    assert "not checked" in messages[0]
    assert not any("c1" in m or "c2" in m for m in messages)


def test_isolation_when_generate_image_raises_on_every_attempt(monkeypatch):
    monkeypatch.setattr(
        visuals,
        "generate_image",
        lambda prompt: (_ for _ in ()).throw(ImageGenProviderError("network down")),
    )

    result = illustrate(_out(), _SESSION, ImageMode.all, _CARD, Language.zh, 3)

    assert not any(a.kind is ImageKind.cover for a in result.images)  # no cover at all
    assert _explainer_ids(result) == {"c1", "c2"}
    messages = _cover_notices(result)
    assert len(messages) == 1
    assert "provider call failed" in messages[0]
    assert not any("c1" in m or "c2" in m for m in messages)


# --- #67: end to end, stubbing only the network boundary (AC-19) -------------

def test_end_to_end_stubs_only_the_network_boundary(monkeypatch):
    """Real `illustrate`, real `api.assets`, real manifest read/write, real
    `api.claimcard`, and the REAL `api.lettering.contains_lettering` — the
    only two stubs are `api.imagegen.generate_image` and the chat model the
    detector resolves from config. Nothing in between is stubbed, so the
    base64 data URL the detector builds, the prompt it loads from
    api/prompts/lettering.md and its yes/no parse all really run.
    """
    import base64

    from api import lettering

    image_calls: list[str] = []
    monkeypatch.setattr(
        visuals,
        "generate_image",
        _scripted_generator([_LETTERED_PNG, _CLEAN_PNG], image_calls),
    )
    monkeypatch.setattr(visuals, "contains_lettering", lettering.contains_lettering)

    seen: list[bytes] = []

    class _Reply:
        def __init__(self, content):
            self.content = content

    class _ChatModel:
        """Answers from the image it was actually handed — content-addressed
        at the network boundary, so a payload that is not the new attempt's
        bytes produces the wrong verdict and fails this test."""

        def invoke(self, messages):
            url = messages[-1].content[-1]["image_url"]["url"]
            payload = base64.b64decode(url.split(",", 1)[1])
            seen.append(payload)
            return _Reply("yes" if payload == _LETTERED_PNG else "no")

    monkeypatch.setattr(
        lettering, "get_model", lambda role, temperature=0.0, fallback=None: _ChatModel()
    )

    out = _out()
    result = illustrate(out, _SESSION, ImageMode.all, _CARD, Language.zh, 3)

    assert seen == [_LETTERED_PNG, _CLEAN_PNG]
    assert len(image_calls) == 2
    assert _cover_notices(result) == []

    saved = image_path(_SESSION, ImageKind.cover)
    assert saved.read_bytes() == _CLEAN_PNG  # the clean retry, on disk

    manifest = read_manifest(_SESSION)
    assert manifest == result.images
    cover = next(a for a in manifest if a.kind is ImageKind.cover)
    assert cover.path == repo_relative(saved)
    assert cover.source_hash  # clean, so reusable
    for asset in manifest:
        if asset.kind is ImageKind.explainer:
            # rendered by the real api.claimcard, written by the real api.assets
            assert image_path(_SESSION, ImageKind.explainer, asset.claim_id).read_bytes()[:8] == (
                b"\x89PNG\r\n\x1a\n"
            )


# --- #67: the guarantee #27's deferral of #39 rests on (AC-4) ----------------

@pytest.mark.parametrize(
    "scripted, detector_error, expect_clean",
    [
        ([_CLEAN_PNG], None, True),                    # passed the check
        ([_LETTERED_PNG, _CLEAN_PNG], None, True),     # passed it on the retry
        ([_LETTERED_PNG], None, False),                # budget exhausted
        ([_CLEAN_PNG], LetteringCheckProviderError("down"), False),   # not checked
        ([_CLEAN_PNG], LetteringCheckConfigError("no model"), False),  # not checked
    ],
)
def test_every_cover_either_passed_the_check_or_carries_a_notice(
    monkeypatch, scripted, detector_error, expect_clean
):
    """The invariant, asserted over the whole returned AgentOutput rather than
    one branch at a time: no cover reaches the operator that has neither
    passed the lettering check nor gained a notice saying it may contain
    lettering or was not checked."""
    image_calls: list[str] = []
    monkeypatch.setattr(visuals, "generate_image", _scripted_generator(scripted, image_calls))
    if detector_error is not None:
        monkeypatch.setattr(
            visuals,
            "contains_lettering",
            lambda image_bytes: (_ for _ in ()).throw(detector_error),
        )

    result = illustrate(_out(), _SESSION, ImageMode.all, _CARD, Language.zh, 3)

    covers = [a for a in result.images if a.kind is ImageKind.cover]
    assert len(covers) == 1  # the cover always ships
    warned = [
        m
        for m in _cover_notices(result)
        if "may contain lettering" in m or "not checked" in m
    ]
    assert (warned == []) is expect_clean
