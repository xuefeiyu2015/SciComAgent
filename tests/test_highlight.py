"""Tests for api.highlight.locate_flags — pure text matching, no model/network.

The board paints a flagged sentence red by slicing the draft at the offsets this
module returns, so the contract under test is: every span slices back to real
draft text, and any flag that cannot be located is REPORTED rather than dropped.
"""

from __future__ import annotations

from api.highlight import locate_flags
from api.schema import OverreachFlag, Platform, PlatformOutput


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
