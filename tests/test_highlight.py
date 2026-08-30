"""Tests for api.highlight.locate_flags — pure text matching, no model/network.

The board paints a flagged sentence red by slicing the draft at the offsets this
module returns, so the contract under test is: every span slices back to real
draft text, and any flag that cannot be located is REPORTED rather than dropped.
"""

from __future__ import annotations

from api.highlight import locate_flags, locate_hedged, locate_text
from api.schema import (
    Claim,
    ConfidenceLevel,
    OverreachFlag,
    Platform,
    PlatformOutput,
)


def _draft(body: str = "", cover_copy: str = "", titles: list[str] | None = None) -> PlatformOutput:
    return PlatformOutput(
        platform=Platform.news,
        body=body,
        cover_copy=cover_copy,
        title_options=titles or [],
    )


def _flag(text: str) -> OverreachFlag:
    return OverreachFlag(text=text, reason="overstates the ledger")


def test_exact_quote_in_body_is_located():
    body = "研究团队做了实验。该疗法治愈了癌症。样本量为12只小鼠。"
    spans, unlocated = locate_flags(_draft(body), [_flag("该疗法治愈了癌症。")])

    assert unlocated == []
    assert len(spans) == 1
    span = spans[0]
    assert span.field == "body"
    assert span.flag_index == 0
    assert body[span.start : span.end] == "该疗法治愈了癌症。"


def test_whitespace_drift_still_locates_the_original_text():
    body = "Background.  The therapy reduced\n  tumor volume in mice. End."
    quote = "The therapy reduced tumor volume in mice."

    spans, unlocated = locate_flags(_draft(body), [_flag(quote)])

    assert unlocated == []
    assert body[spans[0].start : spans[0].end] == "The therapy reduced\n  tumor volume in mice."


def test_quote_that_is_absent_is_reported_not_dropped():
    spans, unlocated = locate_flags(_draft("A wholly unrelated body."), [_flag("Not here.")])

    assert spans == []
    assert unlocated == [0]


def test_empty_quote_is_unlocated():
    spans, unlocated = locate_flags(_draft("Some body text."), [_flag("")])

    assert spans == []
    assert unlocated == [0]


def test_second_flag_overlapping_the_first_is_demoted():
    body = "该疗法治愈了癌症。"
    flags = [_flag("该疗法治愈了癌症。"), _flag("治愈了癌症")]

    spans, unlocated = locate_flags(_draft(body), flags)

    assert [s.flag_index for s in spans] == [0]
    assert unlocated == [1]


def test_repeated_sentence_gives_each_flag_its_own_occurrence():
    body = "它有效。别的内容。它有效。"
    flags = [_flag("它有效。"), _flag("它有效。")]

    spans, unlocated = locate_flags(_draft(body), flags)

    assert unlocated == []
    assert [s.start for s in spans] == sorted(s.start for s in spans)
    assert len({s.start for s in spans}) == 2


def test_cover_copy_and_titles_are_searched_too():
    draft = _draft(body="无关正文。", cover_copy="彻底治愈癌症！", titles=["温和的标题", "史上首次证明"])
    flags = [_flag("彻底治愈癌症！"), _flag("史上首次证明")]

    spans, unlocated = locate_flags(draft, flags)

    assert unlocated == []
    by_flag = {s.flag_index: s for s in spans}
    assert by_flag[0].field == "cover_copy"
    assert draft.cover_copy[by_flag[0].start : by_flag[0].end] == "彻底治愈癌症！"
    assert by_flag[1].field == "title:1"
    assert draft.title_options[1][by_flag[1].start : by_flag[1].end] == "史上首次证明"


# --- hedged evidence ----------------------------------------------------------

def _claim(cid: str, confidence: ConfidenceLevel) -> Claim:
    return Claim(id=cid, claim="c", source_evidence="e", qualifier="q", confidence=confidence)


_HEDGED_LEDGER = [
    _claim("c1", ConfidenceLevel.high),
    _claim("c2", ConfidenceLevel.medium),
    _claim("c3", ConfidenceLevel.low),
]


def test_marks_the_sentence_resting_on_a_hedged_claim():
    body = "第一句很扎实 (c1)。第二句就没那么确定了 (c2)。第三句无关。"
    spans = locate_hedged(_draft(body), _HEDGED_LEDGER)

    assert len(spans) == 1
    assert body[spans[0].start : spans[0].end] == "第二句就没那么确定了 (c2)。"
    assert spans[0].claim_ids == ["c2"]


def test_a_solid_claim_is_not_marked():
    spans = locate_hedged(_draft("只有扎实的依据 (c1)。"), _HEDGED_LEDGER)

    assert spans == []


def test_two_hedged_citations_in_one_sentence_give_one_span():
    body = "这句同时靠两条不确定的依据 (c2, c3)。"
    spans = locate_hedged(_draft(body), _HEDGED_LEDGER)

    assert len(spans) == 1
    assert spans[0].claim_ids == ["c2", "c3"]


def test_a_decimal_point_does_not_end_a_sentence():
    body = "The effect was 0.5 percentage points, which is small (c3). Next."
    spans = locate_hedged(_draft(body), _HEDGED_LEDGER)

    assert body[spans[0].start : spans[0].end] == (
        "The effect was 0.5 percentage points, which is small (c3)."
    )


def test_an_unknown_id_is_not_treated_as_hedged():
    spans = locate_hedged(_draft("引用了不存在的条目 (c9)。"), _HEDGED_LEDGER)

    assert spans == []


def test_hedged_citations_in_cover_and_titles_are_found():
    draft = _draft(body="正文扎实 (c1)。", cover_copy="封面不太确定 (c2)。", titles=["标题也不确定 (c3)"])
    spans = locate_hedged(draft, _HEDGED_LEDGER)

    assert {s.field for s in spans} == {"cover_copy", "title:0"}


# --- locating an arbitrary passage --------------------------------------------

def test_locate_text_finds_an_exact_passage():
    body = "第一句。该疗法治愈了癌症。第三句。"
    span = locate_text(_draft(body), "该疗法治愈了癌症。")

    assert span is not None
    assert body[span.start : span.end] == "该疗法治愈了癌症。"
    assert span.field == "body"


def test_locate_text_tolerates_whitespace_drift():
    body = "Background.  The therapy reduced\n  tumor volume. End."
    span = locate_text(_draft(body), "The therapy reduced tumor volume.")

    assert body[span.start : span.end] == "The therapy reduced\n  tumor volume."


def test_locate_text_searches_cover_and_titles_too():
    draft = _draft(body="正文。", cover_copy="封面文案。", titles=["标题一"])

    assert locate_text(draft, "封面文案。").field == "cover_copy"
    assert locate_text(draft, "标题一").field == "title:0"


def test_locate_text_returns_none_when_it_is_not_there():
    """The caller must be able to say 'I could not find that' instead of guessing."""
    assert locate_text(_draft("完全无关的正文。"), "根本不存在的句子") is None


def test_locate_text_refuses_an_empty_quote():
    assert locate_text(_draft("正文。"), "   ") is None
