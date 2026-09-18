


## Review board (local web UI)

A local page for the human half of the loop: it collects the run parameters,
runs the pipeline, and shows each draft with its flagged sentences marked in
red for you to accept or rewrite, tethered to the claim ledger that backs them.

```
uv run python -m webui.app     # http://127.0.0.1:8080
```

- **The conversation stays open, and it can start work.** Once a draft is on
  screen the agent docks under it instead of disappearing. Four things can come
  back, and all four are proposals you accept:
  - an **answer** — about the draft, the paper, or the background behind it;
  - an **edit** — one passage, whose text always comes from the ledger-bounded
    rewrite path and never from the conversing model, so a chat cannot put an
    unsourced claim into a draft;
  - a **redraft** — "do it in English", "also a Xiaohongshu version", "livelier",
    "write it for clinicians". It arrives as before/after dials to confirm,
    because a redraft is minutes and money. The paper is not fetched again and
    the ledger is reused; only a language change rebuilds it, since the ledger
    is written in the run's language. The original run is untouched and stays in
    History, and the new drafts are checked exactly as a first run's are;
  - a **lookup** — it searches, and shows you what it found with its sources,
    or what it searched for and did not find.

  When a redraft needs to read the paper again and cannot — an old run with no
  stored card, behind a link that is now rate-limiting or down — it does not
  just fail. The ledger keeps each claim's `source_evidence` **verbatim, in the
  paper's own language**, so the agent offers to restate the ledger from that
  and draft without the source. You decide: the provenance is carried over
  rather than read fresh. Evidence is never rewritten, no claim may state a
  number its own evidence does not (checked in code), and anything that fails
  the check keeps its original wording and is named in a notice. The result
  carries a `restated` banner so a reviewer knows what they are looking at.

  The conversation survives a redraft. "Now in English" only makes sense after
  the sentences before it.
- The rail lists **past runs**, newest first — click one to reopen its draft,
  ledger and flags. It reads the mirrors `api/jobs.py` already writes, so
  history survives a restart with no extra storage. Reopening gives you the
  original draft; review decisions are not persisted.
  `uv run python scripts/prune_jobs.py` clears mirrors that carry no draft
  (reports by default, deletes with `--apply`).
- Settings live behind one **⚙** in the rail, with tabs for models, keys and
  search — configuration you touch once should not compete with the manuscript.
- The interface runs in **中文 or English**, switched from the toggle in the
  header; the choice is remembered and also pre-fills the draft language (which
  stays a per-run question, since it is a real `generate` parameter). All UI
  text lives in `webui/i18n.json`.
- **API keys are set only in `.env`.** The sidebar reports which providers have
  a key and never writes one.
- The model dropdowns offer **only what your keys can actually run** — fetched
  from each provider's own catalogue, cached, with a fallback list if that call
  fails.
- `/` opens with the three things it is for, then a diagram of the four agents
  — extractor, researcher, drafter, reviewer — showing what each hands the next
  and, importantly, that the ledger reaches both drafter and reviewer while the
  background materials reach only the drafter.
- `/` is the overview: what the agent does, the four hard rules, the pipeline,
  and every MCP tool with its parameters — that tool list is read from
  `agent.yaml` at request time, so it cannot drift from the manifest the
  platform sees. A tool added to the manifest appears there on its own,
  untranslated until a string is written for it.
- `/board` is the review board itself.

- `/webui` is a THIN Starlette wrapper over `/api`, like `/mcp_server` — no
  business logic lives there.
- It binds loopback only: these routes write `.env`, read local files and spend
  your API budget, so there is no remote surface and no auth.
- The sidebar sets a model per role and stores API keys in `.env`. Saving from
  the sidebar rewrites `config/config.yaml` and drops its comments;
  `config/config.example.yaml` stays the documented reference.
- Drafting will not start while the drafter and the reviewer resolve to the
  same model — a check cannot grade its own work.
- Nothing is ever published. "Complete review" re-audits your edits, then
  offers to save to `outputs/reviews/` — one clean `.txt` per platform,
  carrying your edits with the ledger citations stripped out.
- Claims the extractor was unsure of (`medium` / `low` confidence) are marked
  in amber: the ledger entry, the citations pointing at it, and the whole
  sentence resting on it. The header counts them separately from
  overstatements, because a draft with zero overstatements can still rest
  entirely on shaky evidence.
- Select any passage in a draft and a **Rewrite** button appears: say how you
  want it changed in your own words, refine the proposal as many times as you
  like, then Apply. The claim ledger still bounds the result.
- Every factual sentence cites the ledger entry it rests on. The citation is
  written `(c17)` in the draft, shown as a superscript on the board, and
  written `^c17` in plain text. Click a citation to trace it to its evidence,
  or click a ledger entry to find the sentences resting on it.
