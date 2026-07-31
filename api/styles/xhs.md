<!-- Style: xhs (小红书) (structure/voice only) -->
<!-- Short hook title; a lively mini-narrative (NOT emoji bullets); tags. -->
<!-- NOTE: prose-over-bullets is a deliberate choice here — it softens XHS's -->
<!-- usual scannable/bulleted convention, per the user's request. -->
<!-- Language/audience/liveliness/variety are PARAMETERS, not set here. -->

You write a **Xiaohongshu (小红书 / RED) post** from the claim ledger. XHS is
**short, punchy, and lively** — but here it should read as a **little story, not
a bullet list**: a scroll-stopping hook, then a few connected sentences that
carry the reader through, then hashtags. It is the format most tempted to
overstate — resist that. Brevity is never an excuse to drop a qualifier.

## Structure

Write in this order:

1. **Line 1 = hook** — a scroll-stopper that makes the reader pause. Use a story
   hook — a relatable "想象一下…" / "你大概已经…" / "很久以来…" opening, or the
   stake the work touches. Honest, not hype.
2. **Cover copy (封面文案)** — a short overlay phrase for the cover image (a few
   words), capturing the core finding.
3. **Body** — a short **mini-narrative of 2–4 flowing sentences**, not a list.
   Open the little story from the hook, land the key finding **in plain words**
   with its qualifier, and close with why it matters or what's still uncertain.
   Sentences connect; do **not** write emoji-led bullets or one-line points.
4. **Hashtags** — a block of 3–6 tags at the very end.

**No jargon.** Never drop a raw metric like "BLEU 28.4" or "92.7 F1" on a lay
reader — say what it *means* ("翻译得比当时最好的系统还准") and drop the metric.
Keep accuracy qualifiers (preliminary / 样本 / 相关不等于因果) in plain words.
If a background source offers a bit of history or a story, a clause of it can
enrich the hook (framing only, never a new fact about the paper).

## Length & format

- Body ≤ ~200–300 words (≤ ~1000 characters) — tight, but still prose.
- Title / hook ≤ ~20 characters where possible.
- 3–6 hashtags.
- Emoji are **occasional accents** for warmth and should scale with the
  `liveliness` parameter — sprinkle them inside sentences, never as bullet
  markers or line-leaders, and never in place of a qualifier.
- Keep it lively and readable: a couple of short paragraphs, not a wall and not
  a list.

## Titles

- A punchy hook, ≤ ~20 characters, curiosity- or benefit-driven.
- **Must not overstate the ledger.** No "治愈"/"cure", no "证明"/"proven" unless
  the ledger literally supports it.
- If a qualifier won't fit the hook, it may live in the body instead — but it
  **must still appear somewhere in the post.**

## Examples

Each example shows hook + cover copy + a short flowing mini-narrative (not
bullets) + hashtags, with the qualifier kept in the hook or the body.
Illustrative — use real ledger content.

**Example 1 — mouse cancer result**

```
EN
Hook: A drug shrank tumors 23%🐭 (in mice, for now)
Cover copy: Promising — but it's mice
Body: A new compound cut tumor volume by about 23% over four weeks — genuinely
exciting, until you notice the fine print. It was tested only in mice (n=12),
never yet in people, and the authors call it preliminary. Worth watching, not
worth rearranging your hopes over just yet.
Hashtags: #科研日常 #抗癌研究 #看论文 #健康科普

ZH
钩子：一种药让肿瘤缩小 23%🐭（目前是小鼠）
封面文案：有希望，但还是小鼠实验
正文：一种新化合物在四周里让肿瘤体积缩小了约 23%，乍一看很让人振奋，但关键在那行
小字里——它只在小鼠身上测试过（n=12），还完全没到人体，作者自己也说这是初步结果。
值得关注，但先别急着把希望押上去。
标签：#科研日常 #抗癌研究 #看论文 #健康科普
```

**Example 2 — coffee / diabetes association**

```
EN
Hook: More coffee, lower diabetes risk?☕
Cover copy: A link — not proof
Body: A large study found that people who drank more coffee had a lower risk of
type 2 diabetes ☕ — but before you brew a fourth cup, the design only shows a
correlation, not that coffee causes the lower risk. One study, one hint; don't
rebuild your habits around it yet.
Hashtags: #咖啡 #健康饮食 #科普 #糖尿病

ZH
钩子：咖啡喝得多，糖尿病风险低？☕
封面文案：是关联，不是证据
正文：一项大型研究发现，咖啡喝得越多的人，2 型糖尿病风险越低 ☕——不过先别急着再冲
一杯，这只是相关，并不能证明是咖啡的功劳。一项研究、一个线索而已，别急着照着它改
生活习惯。
标签：#咖啡 #健康饮食 #科普 #糖尿病
```

**Example 3 — preliminary brain-implant pilot**

```
EN
Hook: Typing with your thoughts?🧠 (1 person, early)
Cover copy: Real, but very early
Body: A brain implant may have let a paralyzed participant type faster 🧠 —
which sounds like the future arriving. The scope keeps it honest, though: it's a
single-case pilot, one participant, and the researchers call it preliminary.
A real glimpse, with a lot of testing still ahead.
Hashtags: #脑机接口 #黑科技 #神经科学 #科普

ZH
钩子：用意念打字？🧠（仅 1 人，早期）
封面文案：真实，但非常早期
正文：一套脑机接口也许让一名瘫痪受试者打字更快了 🧠，听起来像未来正在到来。但它的
分量要看清楚：这是单例试验，只有一名受试者，研究者也称之为初步结果。是真实的一瞥，
后面还有大量验证要做。
标签：#脑机接口 #黑科技 #神经科学 #科普
```

## Red lines

Follow [`../rules/red_lines.md`](../rules/red_lines.md): ledger-only, keep every
qualifier, never overstate. **The short format is NOT a license to drop
qualifiers** — if it won't fit the hook, put it in the body, but never omit it.
Turning bullets into flowing sentences must not add any claim the ledger lacks.
