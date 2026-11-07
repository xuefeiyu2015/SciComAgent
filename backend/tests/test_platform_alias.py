"""`wechat` is an alias for `xhs` — one style card, never drafted twice.

The two platforms used to carry different style cards. They now share one
(api/styles/xhs.md, the long-form narrative voice that used to live in
wechat.md), so asking for both must not pay the drafter twice for identical
output. AgentInput normalizes `wechat` -> `xhs` on the way in; everything
downstream only ever sees `xhs`.
"""

from __future__ import annotations

import pytest

from api.draft import _style_card
from api.schema import AgentInput, Platform, SourceType


def _inp(platforms: list[Platform] | None = None) -> AgentInput:
    """Build an AgentInput; None means "leave the field at its default"."""
    kwargs = {} if platforms is None else {"platforms": platforms}
    return AgentInput(source="http://paper", source_type=SourceType.url, **kwargs)


def test_wechat_normalizes_to_xhs():
    assert _inp([Platform.wechat]).platforms == [Platform.xhs]


def test_both_names_collapse_to_one_draft():
    """The whole point: no duplicate drafting spend."""
    assert _inp([Platform.wechat, Platform.xhs]).platforms == [Platform.xhs]
    assert _inp([Platform.xhs, Platform.wechat]).platforms == [Platform.xhs]


def test_order_and_other_platforms_are_preserved():
    got = _inp([Platform.news, Platform.wechat, Platform.xhs]).platforms
    assert got == [Platform.news, Platform.xhs]


def test_default_platforms_have_no_duplicate():
    assert _inp().platforms == [Platform.news, Platform.xhs]


def test_wechat_still_accepted_as_input():
    """Backward compatibility: an existing caller must not get a hard error."""
    AgentInput(source="s", source_type=SourceType.url, platforms=[Platform.wechat])


# --- style cards ------------------------------------------------------------

def test_xhs_card_is_the_long_form_narrative_voice():
    """xhs.md now holds what wechat.md used to say.

    Discriminating traits: the kept card is ONE continuous long-form article
    with no subheads; the retired short-hook card capped the title at ~20
    characters and opened on a scroll-stopper line.
    """
    card = _style_card("xhs")
    assert "one continuous" in card.lower()
    assert "no subheads" in card.lower()
    assert "scroll-stopping hook" not in card.lower()


def test_no_orphaned_wechat_card():
    """wechat.md is gone; asking for it by file name must fail loudly."""
    with pytest.raises(ValueError, match="no style card"):
        _style_card("wechat")
