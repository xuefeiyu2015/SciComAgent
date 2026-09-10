"""Figure density — pure functions, no model and no network.

The companion to api.jargon, and it exists for the same reason. A draft can be
faithful and jargon-free and still read like a methods section, because the
ledger hands the drafter every descriptive statistic the paper reported: how
many subjects, how many sessions, how many trials, how long each block ran.

Asking the drafter to leave those out did not work. Across three live runs of
the same paper the figure count went 25 -> 22 -> 29 while the instruction sat
in the prompt the whole time. A prompt requests; only code enforces. So this
counts, and api.pipeline sends the draft back.

Only Arabic figures count. `两只猴子` is ordinary Chinese prose and a detector
that punished it would be unusable in the language the drafts are written in;
a run of `100`, `8`, `16`, `65%` is what actually reads as a table.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from api.markers import MARKER_RE
from api.schema import PlatformOutput

# Figures a single paragraph may carry before it stops being narrative. Three
# is already generous for the story shape the xhs card asks for; the paragraph
# this detector was built from carried nine.
MAX_FIGURES = 3

# One figure = one run of digits, with optional decimal/thousands separators and
# an optional percent sign. `9,536` and `91.3%` are each one figure, not three.
_FIGURE = re.compile(r"\d+(?:[.,]\d+)*%?")


@dataclass(frozen=True)
class DenseParagraph:
    """One paragraph carrying more figures than it can hold."""

    field: str
    index: int
    figures: int
    excerpt: str


def count_figures(text: str) -> int:
    """Count the Arabic figures in a passage.

    Provenance markers are stripped first. `(c14)` and `(c15, c16)` are not
    figures a reader ever sees — the board raises them into superscripts and
    they are removed at publish — so counting them fires this gate on prose
    that is doing nothing wrong.

    Args:
        text: any prose.

    Returns:
        The number of distinct figure tokens. CJK numerals are ignored.
    """
    return len(_FIGURE.findall(MARKER_RE.sub("", text or "")))


def find_dense(draft: PlatformOutput) -> list[DenseParagraph]:
    """Find the body paragraphs that recite rather than narrate.

    Only the body is checked. A headline carrying its one number is doing its
    job, so titles and cover copy are exempt — the failure mode this guards
    against is a paragraph stacking a study's descriptive statistics.

    Args:
        draft: one platform's generated content.

    Returns:
        One DenseParagraph per offending paragraph, in document order.
    """
    dense: list[DenseParagraph] = []
    for index, paragraph in enumerate((draft.body or "").split("\n\n")):
        figures = count_figures(paragraph)
        if figures > MAX_FIGURES:
            dense.append(
                DenseParagraph(
                    field="body",
                    index=index,
                    figures=figures,
                    excerpt=paragraph.strip()[:60],
                )
            )
    return dense
