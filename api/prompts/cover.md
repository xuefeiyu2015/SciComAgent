<!-- Prompt: cover-image generation (used by api/imageprompt.py, build_cover_prompt). -->
<!-- This file is the BASE of the prompt text handed straight to an image
     model (see #28) — unlike the other files here it is not a chat-role
     instruction to a text model. build_cover_prompt() appends the
     card-derived subject matter, the audience-language note, the
     liveliness/mood dial and an optional house-mood section after this base,
     each its own paragraph, never by interpolating into this text.
     Unlike every other prompt in this folder, this one's output ships with
     NO faithfulness review pass (CLAUDE.md rule #3 has nothing to check
     here) — the hard constraints below are what make that safe. Do not
     soften them. -->

Generate a decorative cover illustration for a public-facing article about a
research paper. The illustration sets mood and theme for the piece — it is
packaging, not content, and it must never be mistaken for a figure from the
paper or a claim about it.

## Hard constraints — never soften these

This image ships straight to readers with no review pass: there is no second
model checking it against the claim ledger the way every drafted sentence is
checked. That is only safe because the image is constrained to assert
**nothing** — no fact, no number, no data, no likeness. If it can't assert
anything, there's nothing to fabricate and nothing to check.

1. **No text or lettering of any kind** — no words, no letters, no numerals,
   no captions, no logos, no watermark, in any language or writing system.
   A title card, a label on an object, or text-shaped scribbles are all
   violations.
2. **No charts, graphs, axes, or data** — no plots, bar/line/pie charts, data
   tables, coordinate axes, diagrams with numeric labels, or anything that
   reads as a figure from the paper. This is a mood piece, not a result.
3. **No identifiable real people** — no depiction of the paper's authors, any
   named or recognizable public figure, or a photorealistic portrait that
   could be mistaken for a real, identifiable individual. Generic, anonymous
   human silhouettes or stylized figures are fine if the scene calls for one;
   a recognizable face is not.

## What to compose instead

An abstract, editorial, conceptual illustration that evokes the paper's theme
through metaphor, motif, color and composition — never a literal restatement
of a finding. Favor a clean, professional, magazine-cover sensibility: strong
composition, tasteful color, generous negative space. Where the subject
matter below is thin or missing, fall back to a generic, tasteful
science/research-themed abstract scene rather than inventing specifics about
the paper.
