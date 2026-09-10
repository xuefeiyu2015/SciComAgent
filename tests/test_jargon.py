"""Tests for api.jargon — the pure jargon detector.

The detector is the single source of jargon truth: the glossary pass uses it to
decide what to look up, and the post-draft check uses it to decide what leaked.
Those two consumers want different tiers, so the tier split is what most of
these tests pin.

No model, no network — every function here is pure.
"""

from __future__ import annotations

from api.jargon import find_jargon, scan_draft
from api.schema import Platform, PlatformOutput


def _terms(text: str) -> set[str]:
    return {hit.term for hit in find_jargon(text)}


def _banned(text: str) -> set[str]:
    return {hit.term for hit in find_jargon(text) if hit.banned}


# --- banned tier: lexicon + notation ------------------------------------------

def test_metric_name_is_banned():
    """A metric score is the exact defect that reached the operator's drafts."""
    hits = [h for h in find_jargon("它取得了 28.4 BLEU，比最佳结果高 2 分。") if h.banned]

    assert len(hits) == 1
    assert hits[0].term == "BLEU"
    assert hits[0].category == "metric"


def test_big_o_notation_is_banned():
    assert "O(n²·d)" in _banned("每层的复杂度是 O(n²·d)，很长的序列会很贵。")


def test_bare_big_o_is_banned():
    assert "O(n)" in _banned("循环层需要 O(n) 的顺序操作。")


def test_model_internal_term_is_banned():
    hits = [h for h in find_jargon("large d_k pushes softmax into tiny gradients") if h.banned]

    assert {h.term for h in hits} == {"d_k", "softmax"}


def test_symbols_and_mechanism_names_land_in_different_categories():
    """`d_k` is a symbol, `softmax` a mechanism — the gloss for each differs."""
    hits = {h.term: h.category for h in find_jargon("d_k and softmax")}

    assert hits["d_k"] == "notation"
    assert hits["softmax"] == "internal"


def test_benchmark_name_is_banned():
    assert "WSJ" in _banned("即使仅使用 4 万句 WSJ 训练集，它也优于其他系统。")


def test_lexicon_matching_is_case_sensitive_for_acronyms():
    """'blue' the colour must not be mistaken for the BLEU metric."""
    assert _banned("天空是 blue 的") == set()


def test_metric_inside_a_longer_word_is_not_a_hit():
    """Substring matches would flag innocent prose; require a token boundary."""
    assert _banned("The BLEUPRINT project") == set()


# --- nominated tier: lookup targets that are not defects ----------------------

def test_unknown_acronym_is_nominated_but_not_banned():
    """A lay reader can't read 'LSTM' either — but explaining it is allowed."""
    hits = [h for h in find_jargon("LSTM 曾经是主流。") if h.term == "LSTM"]

    assert len(hits) == 1
    assert hits[0].banned is False
    assert hits[0].category == "acronym"


def test_nominated_and_banned_tiers_coexist():
    text = "LSTM 的 BLEU 分数"

    assert _banned(text) == {"BLEU"}
    assert _terms(text) == {"BLEU", "LSTM"}


def test_short_capital_run_is_not_an_acronym():
    """Single capitals and ordinary Title Case are not jargon."""
    assert _terms("A Transformer model") == set()


# --- offsets ------------------------------------------------------------------

def test_offsets_slice_back_to_the_original_text():
    """The board paints spans with these offsets, exactly like FlagSpan."""
    text = "它取得了 28.4 BLEU，训练用了 O(n) 的操作。"

    for hit in find_jargon(text):
        assert text[hit.start : hit.end] == hit.term


def test_every_occurrence_is_reported():
    hits = [h for h in find_jargon("BLEU 上升，BLEU 又上升") if h.term == "BLEU"]

    assert len(hits) == 2
    assert hits[0].start < hits[1].start


def test_hits_are_ordered_by_position():
    hits = find_jargon("softmax 之后是 BLEU，然后是 O(n)")

    assert [h.start for h in hits] == sorted(h.start for h in hits)


# --- clean prose --------------------------------------------------------------

def test_plain_language_yields_nothing():
    """The rewrite draft.md asks for must not trip the detector."""
    text = "翻译质量明显超过当时最好的系统，处理很长的文本时计算成本会上升。"

    assert find_jargon(text) == []


def test_empty_text_is_safe():
    assert find_jargon("") == []


# --- draft scanning -----------------------------------------------------------

def test_scan_draft_covers_body_cover_copy_and_titles():
    draft = PlatformOutput(
        platform=Platform.xhs,
        title_options=["干净的标题", "28.4 BLEU 的突破"],
        cover_copy="softmax 改变一切",
        body="它用了 O(n) 的操作。",
    )

    fields = {hit.field for hit in scan_draft(draft) if hit.banned}

    assert fields == {"title:1", "cover_copy", "body"}


def test_scan_draft_offsets_are_relative_to_their_own_field():
    draft = PlatformOutput(
        platform=Platform.xhs,
        title_options=[],
        cover_copy="",
        body="BLEU",
    )

    hit = scan_draft(draft)[0]

    assert (hit.field, hit.start, hit.end) == ("body", 0, 4)


def test_clean_draft_scans_clean():
    draft = PlatformOutput(
        platform=Platform.news,
        title_options=["翻译质量明显提升"],
        cover_copy="",
        body="研究者让模型一次看见整句话。",
    )

    assert scan_draft(draft) == []
