"""Tests for api.density — the pure figure-density detector.

The lesson from the jargon work, applied again: asking the drafter to leave
descriptive statistics out did not hold across three live runs (25 -> 22 -> 29
figures). A prompt requests; only code enforces.

This counts Arabic figures per paragraph, because a run of them is what turns
narrative prose into a methods section. It is deliberately blunt — it cannot
tell which number earns its place, only that a paragraph is reciting.
"""

from __future__ import annotations

from api.density import MAX_FIGURES, count_figures, find_dense
from api.schema import Platform, PlatformOutput

# The paragraph that survived two prompt fixes, verbatim from a live run.
_RECITED = (
    "为了看清这个动态过程，研究人员在两只成年雄性恒河猴身上展开了一场精细的探索。"
    "每隔平均约100轮试验就会悄悄发生轮换。在8到9个实验阶段中，它们每期都能摸透"
    "5到16个完全不同的模板规则。猴子B和猴子S分别只用了15轮和7轮尝试，"
    "并最终在65%和67%的试验里稳稳选中了全场最好的选项。"
)


def test_counts_plain_integers():
    assert count_figures("训练了 15 轮，用了 7 天") == 2


def test_counts_decimals_and_percentages_once_each():
    assert count_figures("准确率 91.3%，提升 2.5 个点") == 2


def test_counts_thousands_separators_as_one_figure():
    assert count_figures("共 9,536 轮试验") == 1


def test_prose_without_figures_counts_zero():
    assert count_figures("翻译质量明显超过当时最好的系统。") == 0


def test_cjk_numerals_are_not_counted():
    """两只猴子 is prose. A blunt detector must not punish ordinary Chinese."""
    assert count_figures("两只猴子在几轮尝试后学会了任务。") == 0


def test_the_recited_paragraph_is_over_the_limit():
    """The regression this detector exists for."""
    assert count_figures(_RECITED) > MAX_FIGURES


def test_a_normal_narrative_paragraph_passes():
    text = (
        "研究团队让猴子在不断变化的颜色中试错，几轮之后它们就摸清了规律，"
        "而大脑里的目标蓝图也随之改写。"
    )

    assert count_figures(text) <= MAX_FIGURES


def test_find_dense_reports_the_offending_paragraph_and_its_field():
    draft = PlatformOutput(
        platform=Platform.xhs,
        title_options=["干净的标题"],
        cover_copy="",
        body=f"开头很干净。\n\n{_RECITED}\n\n结尾也很干净。",
    )

    dense = find_dense(draft)

    assert len(dense) == 1
    assert dense[0].field == "body"
    assert dense[0].index == 1
    assert dense[0].figures > MAX_FIGURES


def test_titles_and_cover_copy_are_not_density_checked():
    """A headline carrying its one number is not reciting a methods section."""
    draft = PlatformOutput(
        platform=Platform.xhs,
        title_options=[_RECITED],
        cover_copy=_RECITED,
        body="干净的正文。",
    )

    assert find_dense(draft) == []


def test_a_clean_draft_reports_nothing():
    draft = PlatformOutput(platform=Platform.news, body="研究者让模型一次看见整句话。")

    assert find_dense(draft) == []


# --- provenance markers are not figures ---------------------------------------

def test_citation_markers_are_not_counted():
    """A reader never sees (c14): the board raises markers into superscripts and
    strips them at publish. Counting them fires the gate on innocent prose."""
    assert count_figures("大脑把输入抽象成通用价值 (c13)，并非临摹细节 (c14)。") == 0


def test_grouped_markers_are_not_counted():
    assert count_figures("局部表征演化为全局表征 (c15, c16)。") == 0


def test_full_width_markers_are_not_counted():
    assert count_figures("完全摒弃循环（c1，c18）。") == 0


def test_real_figures_still_count_alongside_markers():
    text = "最初约 50 毫秒时是局部表征，到 250 毫秒便成为全局表征 (c15, c16)。"

    assert count_figures(text) == 2
