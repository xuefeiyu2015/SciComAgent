# SciComAgent

The repository has two halves:

| Folder | What | Hosted on | Address |
| --- | --- | --- | --- |
| `frontend/` | The pages: overview (`index.html`) and review board (`board.html`). Plain HTML/JS/CSS, no build step | Vercel | https://scicom-app.apps.turingplanet.ai |
| `backend/` | The Python agent: `api/` (all the logic), the MCP server (`mcp_server/`) and the board's API (`webui/app.py`) | Railway | MCP: https://scicomagent.agents.turingplanet.ai/mcp · board API: https://scicom-api.agents.turingplanet.ai |

The platform files stay at the repository root: `agent.manifest.yaml` (its commands `cd backend` first),
`.copier-answers.yml`, `.github/` and `scripts/`.

## Run locally

```bash
cd backend
uv sync
uv run python -m webui.app      # http://127.0.0.1:8080 , board at /board
```

Locally the backend also serves `../frontend`, so it is one app on one port, with no access key asked.
Every command in the rest of this README runs in `backend/`.
Tests: `uv run pytest -q --ignore=tests/faithfulness` (the faithfulness suite calls real models).

## How the hosted halves talk

- `frontend/static/config.js` holds the board API's address. The pages call it directly (`fetch` with credentials), and
  generated images load from its `/images/`.
- The board API allows that one origin (CORS, `SCICOM_FRONTEND_ORIGINS`) and keeps every `/api` and `/images` route
  behind an **access key** (`backend/webui/access.py`). The key is entered once per browser and kept as an HttpOnly
  cookie for 30 days. Without a key configured, a hosted board API stays locked. A local run (loopback) has no gate.

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
- Under each draft, a **Feedback** box takes a 👍/👎, a 1–5 rating and a
  comment. It is recorded for the evaluation dashboard (below); when telemetry
  is off the box says so instead of pretending it was saved.

## Evaluation dashboard (Langfuse)

How the agent is doing — tokens, cost, latency and errors per subagent, the
quality of each run, and what readers thought of it — lives in
[Langfuse Cloud](https://cloud.langfuse.com). Nothing is stored locally.

**Off unless you turn it on.** Add three lines to `.env` (see `.env.example`):

```
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
LANGFUSE_BASE_URL=https://cloud.langfuse.com   # US region: https://us.cloud.langfuse.com
```

Without both keys every telemetry call is a no-op and the `langfuse` package is
never imported; `telemetry.enabled: false` in `config/config.yaml` (or
`TELEMETRY_ENABLED=false`) switches it off even with keys set. Telemetry can
never fail a run — its own errors are logged and swallowed. The test suite
strips the keys, so `pytest` never posts to your dashboard.

> **Privacy:** with telemetry on, prompts, the paper's text and the drafts are
> sent to Langfuse Cloud. Leave it off for papers you may not share.

### What is recorded

| What | Where it comes from | In Langfuse |
|---|---|---|
| Every LLM call — model, tokens, latency, error, cost | `api/config_loader.get_model()` attaches the Langfuse LangChain handler; the call is named after its **role** (`extractor`, `drafter`, `reviewer`, `researcher`, `stylist`, `image_reviewer`) | Observation (generation), name = role; `metadata.served_by` says which role's config answered when one falls back |
| One run / redraft | `api/jobs._execute()` opens one trace per job | Trace `scicomm.run` / `scicomm.redraft`; **Session = the run's `session_id`**; tags `kind:`, `language:`, `platform:` |
| Run quality | `api/telemetry.compute_quality_scores()` when a job finishes | Session scores: `status`, `claims`, `overreach_flags` (+ `overreach_flags.<platform>`), `jargon_flags`, `density_flags`, `error_notices` |
| Reader feedback | Board **Feedback** box (`POST /api/feedback`) or the MCP `feedback` tool → `api/feedback.record_feedback()` | Session scores: `user_thumb` (boolean — its mean is the approval rate), `user_rating` (1–5), `user_comment` (text); `metadata.channel` = `web` / `mcp`, `metadata.platform` |

Per-platform drafts run in parallel threads; each runs in a copy of the
caller's context so its drafter/reviewer calls still nest under the run's trace.

### Setting it up in Langfuse

1. **Model prices.** Langfuse prices known models itself, but aliases such as
   `gemini-flash-latest` have no price, so their cost shows empty. Add them under
   *Settings → Models → Add model definition* (match pattern e.g.
   `^gemini-flash-latest$`, input/output price per token). Cost is computed at
   ingestion, so set prices before the runs you want costed.
2. **A dashboard.** The built-in *Langfuse Agent Dashboard* counts tool calls,
   which this agent does not make — it will read 0. Create your own
   (*Dashboards → New dashboard → Add Widget*):

   | Widget | View | Metric | Breakdown |
   |---|---|---|---|
   | Tokens per subagent | Observations | Total tokens (sum) | Observation name |
   | Cost per subagent | Observations | Total cost (sum) | Observation name |
   | Latency per subagent | Observations | Latency p50 / p95 | Observation name |
   | Errors per subagent | Observations | Count, level = ERROR | Observation name |
   | Approval rate | Scores | `user_thumb` (avg) | Time |
   | Rating vs. overstatements | Scores | `user_rating`, `overreach_flags` (avg) | Time |

3. **Drill down** from *Sessions*: one row per run, with its traces, every call
   inside them, and all of its scores side by side.

Reading data back programmatically: organisations created after 16 Sep 2026
only get the v2/v3 public APIs (`/api/public/v2/observations`,
`/api/public/v3/scores`); the older `/api/public/traces` and
`/api/public/v2/scores` answer `410`.
