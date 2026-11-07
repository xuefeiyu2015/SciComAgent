"""Tests for api.explainer.select_claim_ids — pure claim selection for cards.

Covers: scan order/fields, dedup (within a field, across fields, across
platforms), dropping ids absent from the ledger, the three-group ordering
with numeral-detection, and the cap edge cases (0, negative, oversized,
no drafts, empty ledger, determinism).
"""

from __future__ import annotations

from api.explainer import select_claim_ids
from api.schema import (
    AgentOutput,
    Claim,
    ClaimKind,
    Platform,
    PlatformOutput,
)


def _claim(id: str, claim: str, kind: ClaimKind = ClaimKind.finding) -> Claim:
    return Claim(id=id, claim=claim, source_evidence="ev", qualifier="q", kind=kind)


def _draft(
    platform: Platform,
    title_options: list[str] | None = None,
    cover_copy: str = "",
    body: str = "",
    hashtags: list[str] | None = None,
) -> PlatformOutput:
    return PlatformOutput(
        platform=platform,
        title_options=title_options or [],
        cover_copy=cover_copy,
        body=body,
        hashtags=hashtags or [],
    )


def _out(drafts: list[PlatformOutput], ledger: list[Claim]) -> AgentOutput:
    return AgentOutput(platform_outputs=drafts, claim_ledger=ledger)


# --- scan fields / order -----------------------------------------------------

def test_scans_title_options_in_list_order():
    out = _out(
        [_draft(Platform.news, title_options=["t1 (c2)", "t2 (c1)"])],
        [_claim("c1", "no numeral here"), _claim("c2", "also none")],
    )
    assert select_claim_ids(out, cap=10) == ["c2", "c1"]


def test_scans_cover_copy_after_title_options():
    out = _out(
        [_draft(Platform.news, title_options=["t (c1)"], cover_copy="cover (c2)")],
        [_claim("c1", "a"), _claim("c2", "b")],
    )
    assert select_claim_ids(out, cap=10) == ["c1", "c2"]


def test_scans_body_after_cover_copy():
    out = _out(
        [_draft(Platform.news, cover_copy="cover (c1)", body="body (c2)")],
        [_claim("c1", "a"), _claim("c2", "b")],
    )
    assert select_claim_ids(out, cap=10) == ["c1", "c2"]


def test_hashtags_are_never_scanned():
    out = _out(
        [_draft(Platform.news, body="body (c1)", hashtags=["(c2)"])],
        [_claim("c1", "a"), _claim("c2", "b")],
    )
    assert select_claim_ids(out, cap=10) == ["c1"]


# --- dropping / dedup --------------------------------------------------------

def test_id_cited_but_absent_from_ledger_is_dropped():
    out = _out(
        [_draft(Platform.news, body="body (c1) and (c9)")],
        [_claim("c1", "a")],
    )
    assert select_claim_ids(out, cap=10) == ["c1"]


def test_id_repeated_within_one_field_appears_once():
    out = _out(
        [_draft(Platform.news, body="first (c1), again (c1)")],
        [_claim("c1", "a")],
    )
    assert select_claim_ids(out, cap=10) == ["c1"]


def test_id_repeated_across_fields_of_same_platform_appears_once_at_first_citation():
    out = _out(
        [
            _draft(
                Platform.news,
                title_options=["title (c1)"],
                cover_copy="cover (c2)",
                body="body (c1) (c2)",
            )
        ],
        [_claim("c1", "a"), _claim("c2", "b")],
    )
    assert select_claim_ids(out, cap=10) == ["c1", "c2"]


def test_id_repeated_across_two_platforms_appears_once_at_first_citation():
    out = _out(
        [
            _draft(Platform.news, body="news body (c1)"),
            _draft(Platform.xhs, body="xhs body (c1) (c2)"),
        ],
        [_claim("c1", "a"), _claim("c2", "b")],
    )
    assert select_claim_ids(out, cap=10) == ["c1", "c2"]


# --- ordering: numeral-finding, finding, method, ties by first citation -----

def test_finding_with_numeral_before_finding_without():
    out = _out(
        [_draft(Platform.news, body="(c1) (c2)")],
        [
            _claim("c1", "no numbers here", kind=ClaimKind.finding),
            _claim("c2", "grew by 23%", kind=ClaimKind.finding),
        ],
    )
    assert select_claim_ids(out, cap=10) == ["c2", "c1"]


def test_finding_before_method():
    out = _out(
        [_draft(Platform.news, body="(c1) (c2)")],
        [
            _claim("c1", "n=12 sessions", kind=ClaimKind.method),
            _claim("c2", "no digits, just a finding", kind=ClaimKind.finding),
        ],
    )
    assert select_claim_ids(out, cap=10) == ["c2", "c1"]


def test_full_three_group_order():
    out = _out(
        [_draft(Platform.news, body="(c3) (c1) (c4) (c2)")],
        [
            _claim("c3", "method with 12 trials", kind=ClaimKind.method),
            _claim("c1", "finding with no digits", kind=ClaimKind.finding),
            _claim("c4", "another method, no digits", kind=ClaimKind.method),
            _claim("c2", "finding grew 2x", kind=ClaimKind.finding),
        ],
    )
    # order: finding-with-numeral (c2), finding (c1), method (c3, c4 by first citation)
    assert select_claim_ids(out, cap=10) == ["c2", "c1", "c3", "c4"]


def test_ties_within_group_break_by_first_citation():
    out = _out(
        [_draft(Platform.news, body="(c2) (c1)")],
        [
            _claim("c1", "finding grew by 10%", kind=ClaimKind.finding),
            _claim("c2", "finding grew by 20%", kind=ClaimKind.finding),
        ],
    )
    assert select_claim_ids(out, cap=10) == ["c2", "c1"]


def test_cjk_numeral_is_not_treated_as_numeral_bearing():
    # documented consequence: "十倍" (CJK numeral) does not count as a digit.
    out = _out(
        [_draft(Platform.news, body="(c1) (c2)")],
        [
            _claim("c1", "increased tenfold (十倍)", kind=ClaimKind.finding),
            _claim("c2", "increased 10x", kind=ClaimKind.finding),
        ],
    )
    assert select_claim_ids(out, cap=10) == ["c2", "c1"]


# --- cap edge cases -----------------------------------------------------------

def test_cap_zero_returns_empty_list():
    out = _out(
        [_draft(Platform.news, body="(c1)")],
        [_claim("c1", "a")],
    )
    assert select_claim_ids(out, cap=0) == []


def test_negative_cap_treated_as_zero_not_negative_slice():
    out = _out(
        [_draft(Platform.news, body="(c1) (c2) (c3)")],
        [_claim("c1", "a"), _claim("c2", "b"), _claim("c3", "c")],
    )
    # A negative-index slice (xs[:-1]) would silently drop only the last
    # element, returning ["c1", "c2"]. The correct behaviour is [].
    assert select_claim_ids(out, cap=-1) == []
    assert select_claim_ids(out, cap=-100) == []


def test_cap_larger_than_eligible_set_returns_all_unpadded():
    out = _out(
        [_draft(Platform.news, body="(c1) (c2)")],
        [_claim("c1", "a"), _claim("c2", "b")],
    )
    result = select_claim_ids(out, cap=50)
    assert len(result) == 2
    assert set(result) == {"c1", "c2"}


def test_cap_smaller_than_eligible_set_truncates():
    out = _out(
        [_draft(Platform.news, body="(c1) (c2) (c3)")],
        [_claim("c1", "a"), _claim("c2", "b"), _claim("c3", "c")],
    )
    assert len(select_claim_ids(out, cap=2)) == 2


# --- empty inputs --------------------------------------------------------------

def test_no_drafts_returns_empty_list():
    out = _out([], [_claim("c1", "a")])
    assert select_claim_ids(out, cap=10) == []


def test_empty_ledger_returns_empty_list():
    out = _out([_draft(Platform.news, body="(c1)")], [])
    assert select_claim_ids(out, cap=10) == []


def test_output_with_no_markers_anywhere_returns_empty_list():
    out = _out(
        [_draft(Platform.news, title_options=["plain title"], body="plain body")],
        [_claim("c1", "a")],
    )
    assert select_claim_ids(out, cap=10) == []


# --- determinism ---------------------------------------------------------------

def test_deterministic_across_calls():
    out = _out(
        [
            _draft(Platform.news, title_options=["t (c3)"], body="(c1) (c2)"),
            _draft(Platform.xhs, body="(c2) (c4)"),
        ],
        [
            _claim("c1", "finding no digits", kind=ClaimKind.finding),
            _claim("c2", "finding with 5 mice", kind=ClaimKind.finding),
            _claim("c3", "method with 3 sessions", kind=ClaimKind.method),
            _claim("c4", "another method", kind=ClaimKind.method),
        ],
    )
    first = select_claim_ids(out, cap=3)
    second = select_claim_ids(out, cap=3)
    assert first == second
