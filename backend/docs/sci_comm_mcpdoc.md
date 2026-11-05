# SciComm Agent — MCP Server

A thin MCP wrapper (`/mcp_server`) that exposes the SciComm pipeline for the
Turing Planet MCP platform. It turns a research paper into public-facing drafts
for **news / WeChat / Xiaohongshu**, with source provenance and overstatement
flags — and **never auto-publishes**.

The server contains **no business logic**: each tool marshals its parameters and
delegates to a function in `/api`, returning the result unchanged. All logic
lives in `/api`.

## Tools at a glance

| Tool | Purpose |
|------|---------|
| [`generate`](#tool-generate) | Full pipeline: paper → multi-platform drafts + provenance + flags |
| [`redraft`](#tool-redraft) | Write an **earlier run's** paper again with different settings |
| [`extract_ledger`](#tool-extract_ledger) | Cheap provenance preview: paper → claim ledger only (no drafting) |
| [`check_draft`](#tool-check_draft) | Re-check a (human-edited) draft against its claim ledger |
| [`render`](#tool-render) | Format a `generate`/`extract_ledger` result as human-readable Markdown |
| [`job_status`](#tool-job_status) | How far a background run has got. Cheap; safe to poll |
| [`job_result`](#tool-job_result) | A background run's output — partial while it is still drafting |
| [`health`](#tool-health) | Report configured model roles, search sources, and key presence |

A typical human-in-the-loop flow: **`extract_ledger`** to inspect/approve the
facts → draft (via `generate`, or edit by hand) → **`check_draft`** to re-verify
→ **`render`** to view/publish.

**The draft is not the end.** `generate` → `redraft` closes the loop: "now in
English", "also for Xiaohongshu", "livelier", "for clinicians" are all the same
paper, and a redraft can itself be redrafted. See
[Long runs and polling](#long-runs) for the `session_id` that makes both work.

---

## Running the server

```bash
# 1. Install deps (uv or pip)
uv sync                      # or: pip install -r requirements.txt

# 2. Configure models + keys (see config/config.example.yaml)
cp config/config.example.yaml config/config.yaml
#    fill in EXTRACTOR_/DRAFTER_/REVIEWER_ provider+model env vars and API key(s)

# 3. Launch (stdio transport)
python -m mcp_server.server
```

The server name registered with the MCP host is `scicomm-agent`, matching
`agent.yaml`. Transport is stdio (FastMCP default).

### Install on an agent / MCP client

Keys are **brought by the operator** via environment (`byo_key: true`); nothing
is stored in the repo. Models are declared by **role**, not name — the reviewer
model **must differ** from the drafter (rule #3: no grading your own work). Pass
the role env vars through your client's `env` config (shown below).

> Run all commands from the repo root so `python -m mcp_server.server` resolves
> the `mcp_server` / `api` packages. To use the project's virtualenv, point the
> command at its interpreter (e.g. `.venv/bin/python`) instead of bare `python`.

#### Claude Code (CLI)

```bash
# From the repo root. --scope project writes .mcp.json (shareable);
# use --scope user to install for yourself across all projects.
claude mcp add scicomm-agent --scope project \
  --env EXTRACTOR_PROVIDER=anthropic \
  --env EXTRACTOR_MODEL=claude-haiku-4-5-20251001 \
  --env DRAFTER_PROVIDER=... \
  --env DRAFTER_MODEL=... \
  --env REVIEWER_PROVIDER=... \
  --env REVIEWER_MODEL=... \
  --env ANTHROPIC_API_KEY=... \
  -- python -m mcp_server.server
```

Then verify and inspect:

```bash
claude mcp list            # should show scicomm-agent
claude mcp get scicomm-agent
```

Inside a session, `/mcp` lists connected servers and their tools; they are
exposed as `mcp__scicomm-agent__generate`, `…__redraft`, `…__extract_ledger`,
`…__check_draft`, `…__render`, `…__job_status`, `…__job_result`, and
`…__health`.

#### Claude Desktop / generic MCP hosts

Add to the host's MCP config (Claude Desktop:
`claude_desktop_config.json`; Cursor / others: their `mcp.json`):

```json
{
  "mcpServers": {
    "scicomm-agent": {
      "command": "python",
      "args": ["-m", "mcp_server.server"],
      "cwd": "/absolute/path/to/SciComAgent",
      "env": {
        "EXTRACTOR_PROVIDER": "anthropic",
        "EXTRACTOR_MODEL":    "claude-haiku-4-5-20251001",
        "DRAFTER_PROVIDER":   "...",
        "DRAFTER_MODEL":      "...",
        "REVIEWER_PROVIDER":  "...",
        "REVIEWER_MODEL":     "...",
        "ANTHROPIC_API_KEY":  "..."
      }
    }
  }
}
```

Set `cwd` to the repo root (or use the venv interpreter as `command`) so the
`mcp_server` / `api` packages import correctly. Restart the host to pick it up.

---

<a id="tool-generate"></a>
## Tool: `generate`

Turn a research paper into multi-platform sci-comm drafts + provenance +
overstatement flags.

A full run is **minutes** of model calls — longer than a tool call can stay
open — so the work starts in the background. If it finishes within
`wait_seconds` you get the complete `AgentOutput` inline (the common case for
fast failures like a paywall); otherwise you get `status="running"` and a
`session_id` to poll. See [Long runs and polling](#long-runs).

### Parameters

| Name          | Type                        | Required | Default                    | Notes |
|---------------|-----------------------------|----------|----------------------------|-------|
| `source`      | string                      | ✅       | —                          | PDF link / DOI / web URL of the paper |
| `source_type` | `doi` \| `url` \| `pdf`     | ✅       | —                          | How to interpret `source` |
| `platforms`   | list of `news`/`wechat`/`xhs` | ❌     | `[news, xhs]`      | Target platforms to draft for. `wechat` is an alias for `xhs`: one shared style card, drafted once, labelled `xhs` |
| `language`    | `zh` \| `en`                | ❌       | `zh`                       | Output language |
| `audience`    | string                      | ❌       | `general_public`           | Intended reader |
| `liveliness`  | int 1–5                     | ❌       | `3`                        | Tone liveliness |
| `length`      | int 1–5                     | ❌       | `3`                        | How long the piece runs, **relative to the platform's own norm**. `3` is that norm; `2` shorter, `1` much shorter; `4`–`5` longer |
| `background`  | bool                        | ❌       | `true`                     | Gather external background materials (web / arXiv / scholarly APIs) as **framing context** for the drafts. Never a source of facts; failure degrades gracefully |
| `wait_seconds`| int                         | ❌       | `10`                       | How long to hold the call open before handing back a `session_id` instead. Clamped to 0–25 — see [Long runs and polling](#long-runs) |

### Return value — `AgentOutput`

```jsonc
{
  "platform_outputs": [
    {
      "platform": "news",
      "title_options": ["…"],
      "cover_copy": "…",
      "body": "…",
      "hashtags": ["…"]
    }
  ],
  "claim_ledger": [
    {
      "id": "c1",
      "claim": "…",
      "source_evidence": "source span / pointer",
      "qualifier": "species, sample, correlation-not-causation, 'preliminary'…",
      "confidence": "high | medium | low"
    }
  ],
  "overreach_flags": [
    { "text": "flagged statement", "reason": "why it overstates", "platform": "xhs" }
  ],
  "background_materials": [
    {
      "snippet": "external context excerpt/summary",
      "source_title": "…",
      "source_url": "https://…",
      "kind": "web | arxiv | semantic_scholar | pubmed | crossref",
      "relation": "why this helps frame the paper's story"
    }
  ],
  "notices": [
    { "code": "ok", "message": "…", "source_url": "…" }
  ],
  "status": "ok | needs_review | no_claims | failed | running"
}
```

- **`platform_outputs`** — one draft per requested platform.
- **`claim_ledger`** — every source-grounded claim, with evidence + qualifier.
  Per rule #1, any number / causation / magnitude / "first" / "proves" statement
  in a draft must map to a ledger entry, or it may not be written.
- **`overreach_flags`** — statements that over-claim vs. the ledger, surfaced for
  a human reviewer.
- **`background_materials`** — external context shown to the drafter for framing
  **only** (never facts, never ledger entries); surfaced as an audit trail so a
  reviewer can see exactly what the drafter was given. Empty when `background`
  is off or nothing useful was found.
- **`notices`** — non-draft messages (e.g. why fetch failed). A finished result
  carries a `done` notice naming what it produced: a caller that has one is
  holding the end of the job, and is the only one who can tell the human so.
- **`status`** — coarse outcome (see below).

### Status values

| Status         | Meaning |
|----------------|---------|
| `ok`           | Full pipeline succeeded |
| `needs_review` | Produced, but has flags / needs a human |
| `no_claims`    | Nothing could be sourced → nothing may be written (rule #1) |
| `failed`       | Fetch/pipeline failure — see the notice |
| `running`      | Still drafting — a handle, or a **partial** result. Never a reviewable draft: a partial can never carry a finished status |

### Notice codes

| Code          | Meaning / action |
|---------------|------------------|
| `ok`          | Full text obtained |
| `need_pdf`    | Source exists but access is blocked (paywall) → provide a PDF link |
| `too_short`   | Reachable but too little text (stub / scanned PDF) → provide a PDF link |
| `not_a_paper` | Content is not a research paper → check the link |
| `rate_limited` | The publisher is throttling us (HTTP 429/503) — **the link is fine** → wait a few minutes, or supply the PDF |
| `can_restate` | The source is out of reach, but this run's ledger still holds the paper's own evidence → a `redraft` can restate it in the new language. **Ask the human first** |
| `restated`   | This ledger was restated from stored evidence rather than re-read from the paper. The evidence is untouched; the wording is not |
| `fetch_error` | Network failure / unreachable link |
| `draft_error` | One platform's draft crashed (pipeline-internal) |
| `background_error` | Background search was skipped; drafts are produced without it (pipeline-internal) |
| `glossary_error` | Term lookup was skipped; drafts fall back to the raw terms (pipeline-internal) |
| `style_error` | The learned writing style was skipped; drafts come back in the default voice (pipeline-internal) |
| `running`    | The run was accepted and is still going → poll `job_status` with the `session_id` |
| `unknown_session` | No job for that `session_id` (expired, or from another instance / before a restart) → call `generate` again |
| `done`        | The run **finished** and this is the whole result → say so, and show it. A complete result looks no different from a partial one to whoever is waiting, and silence reads as still-working |

The tool never crashes: on any exception it returns
`status=failed` with a single `fetch_error` notice. For blocked sources
(`need_pdf` / `too_short`), the notice message is rewritten into an explicit ask
to **retry `generate` with `source_type='pdf'`** and a PDF link.

---

## Example calls

**DOI, default platforms, Chinese output:**

```json
{
  "name": "generate",
  "arguments": {
    "source": "10.1038/s41586-024-00000-0",
    "source_type": "doi"
  }
}
```

**Paywalled source → retry with a PDF:**

```json
// First call returns: status=needs_review/failed with a need_pdf notice.
{
  "name": "generate",
  "arguments": {
    "source": "https://example.com/paper.pdf",
    "source_type": "pdf",
    "platforms": ["xhs"],
    "language": "en",
    "liveliness": 4
  }
}
```

---

<a id="long-runs"></a>
## Long runs and polling

A full run is minutes of model calls; an MCP `tools/call` cannot stay open that
long through the platform gateway. So `generate` and `redraft` both start the
work in the background and return as soon as they can:

- **finished inside `wait_seconds`** → the complete `AgentOutput`, exactly as if
  the call had been synchronous;
- **still going** → `status="running"` plus a `session_id`.

With a `session_id`, poll [`job_status`](#tool-job_status) until it reports
`state="done"`, then call [`job_result`](#tool-job_result) for the content.
Progress is reported by **polling, not `notifications/progress`** — notifications
are dropped or invisible across an HTTP proxy hop; a tool result is not.

The `session_id` is a **tool argument**, not the MCP session id, so it survives
`stateless_http` and a gateway that opens a fresh MCP session per call. It is
also what [`redraft`](#tool-redraft) takes.

### Telling the human it finished

A completed `AgentOutput` used to be **silent**: notices exist for what went
wrong, so a run that went right came back with none — and the caller had a
payload that was complete with no sentence saying so. People sat watching a
page, waiting for work that had ended minutes earlier.

So a finished result now carries a [`done`](#notice-codes) notice naming what it
produced, and `job_status` ends on a message in words rather than the bare
`"done"` it used to share with every other stage line. **Pass it on the moment
you see it.** A complete result looks no different from a partial one to whoever
is waiting, and silence reads as still-working.

### Honesty guarantees

In-process state can always be lost, so:

- finished results are **mirrored to disk**, so a restart does not destroy work
  the caller has not collected yet (best effort — a failed write never sinks a
  completed job);
- every id carries a per-process **instance tag**. An id from another instance,
  or a previous life of this one, comes back as `state="lost"` saying which,
  rather than as a baffling "unknown session".

---

<a id="tool-redraft"></a>
## Tool: `redraft`

Write an **earlier run's** paper again with different settings. Use this instead
of calling `generate` a second time whenever the paper is one this agent has
already drafted: "now in English", "also do a Xiaohongshu version", "make it
livelier", "write it for clinicians".

The source, its claim ledger and its extracted content all come from
`session_id`, so nothing is re-fetched or re-extracted unless it has to be. Pass
**only the settings that change**; anything omitted stays as it was. You cannot
change which paper this is — that is what `generate` is for.

Changing `language` rebuilds the claim ledger, because the ledger is written in
the run's language; every other setting reuses it, which is much faster. Either
way the drafts are checked for faithfulness exactly as a first run's are, the
original run is **left untouched** (it stays in history), and the result comes
back for a human. Never publishes.

### Parameters

| Name           | Type                          | Required | Default | Notes |
|----------------|-------------------------------|----------|---------|-------|
| `session_id`   | string                        | ✅       | —       | The earlier run to redraft, from `generate`. A redraft can itself be redrafted — use the id **it** returns |
| `platforms`    | list of `news`/`wechat`/`xhs` | ❌       | keep    | `wechat` is an alias for `xhs` |
| `language`     | `zh` \| `en`                  | ❌       | keep    | **Rebuilds the ledger** |
| `audience`     | string                        | ❌       | keep    | Intended reader |
| `liveliness`   | int 1–5                       | ❌       | keep    | Tone liveliness |
| `length`       | int 1–5                       | ❌       | keep    | Length relative to the platform's norm |
| `background`   | bool                          | ❌       | keep    | Whether to gather external background materials |
| `from_ledger`  | bool                          | ❌       | `false` | Only meaningful after a redraft came back with a `can_restate` notice. `true` = restate the ledger in the new language from the evidence the first run stored, instead of re-reading a paper that is out of reach. **Ask the human first** — provenance is carried over, not read fresh, and the result is marked `restated` |
| `wait_seconds` | int                           | ❌       | `10`    | As in `generate`. Clamped to 0–25 |

### Return value — `AgentOutput`

Same contract as `generate`, and the same waiting behaviour: a run that finishes
in time comes back complete, otherwise `status="running"` and a **new**
`session_id` to poll. Omitting every dial is an error (`nothing to redraft`);
so is a `session_id` this server cannot account for (`unknown_session` → call
`generate` with the source again).

### Example call

```json
{
  "name": "redraft",
  "arguments": {
    "session_id": "j_a1b2c3_4d5e6f70",
    "language": "en",
    "platforms": ["xhs"]
  }
}
```

---

<a id="tool-extract_ledger"></a>
## Tool: `extract_ledger`

Extract just the **claim ledger** from a paper, without drafting. A cheap
provenance preview — only the `extractor` model runs — so a caller can inspect
and approve the source-grounded facts before spending on a full `generate`.

### Parameters

| Name          | Type                    | Required | Default | Notes |
|---------------|-------------------------|----------|---------|-------|
| `source`      | string                  | ✅       | —       | PDF link / DOI / web URL of the paper |
| `source_type` | `doi` \| `url` \| `pdf` | ✅       | —       | How to interpret `source` |
| `language`    | `zh` \| `en`            | ❌       | `zh`    | Language for the ledger `claim` text |

### Return value — `AgentOutput`

Same contract as `generate`, but `platform_outputs` is empty and only the
provenance fields are populated:

- **`claim_ledger`** — the source-grounded claims (id, claim, source_evidence,
  qualifier, confidence).
- **`status`** — `ok` (ledger ready), `no_claims` (nothing could be sourced →
  nothing may be written, rule #1), or `failed` (fetch failure — see the notice).
- **`notices`** — blocked/short sources yield the same `need_pdf` / `too_short`
  guidance as `generate`.

```json
{
  "name": "extract_ledger",
  "arguments": { "source": "https://arxiv.org/abs/1706.03762", "source_type": "url" }
}
```

---

<a id="tool-check_draft"></a>
## Tool: `check_draft`

Re-run the faithfulness check on a (possibly human-edited) draft against its
claim ledger. Uses the `reviewer` role — a **different, strong model** than the
drafter (rule #3: no grading your own work). Use it to re-verify after editing a
draft by hand.

### Parameters

| Name       | Type                    | Required | Default | Notes |
|------------|-------------------------|----------|---------|-------|
| `draft`    | `PlatformOutput`        | ✅       | —       | Draft to audit: `{platform, title_options, cover_copy, body, hashtags}` |
| `ledger`   | list of `Claim`         | ✅       | —       | Ledger the draft must stay within (e.g. `claim_ledger` from `extract_ledger`) |
| `language` | `zh` \| `en`            | ❌       | `zh`    | Language of the draft & ledger `claim` text |

### Return value — list of `CheckFlag`

An **empty list means the draft is faithful.** Each flag:

```jsonc
[
  {
    "claim_id": "c1",                 // ledger id it maps to; "" if not in the ledger
    "quote": "cures cancer",          // the offending sentence, verbatim
    "issue": "dropped qualifier — 'in mice' removed",
    "suggestion": "restore the species qualifier"
  }
]
```

Flags cover: correlation stated as causation, dropped qualifiers, a minor
finding cast as the main conclusion, and claims not present in the ledger.

```json
{
  "name": "check_draft",
  "arguments": {
    "draft": { "platform": "wechat", "body": "This drug cures cancer." },
    "ledger": [
      { "id": "c1", "claim": "The drug shrank tumors in mice (preliminary).",
        "source_evidence": "…", "qualifier": "in mice; preliminary", "confidence": "low" }
    ]
  }
}
```

---

<a id="tool-render"></a>
## Tool: `render`

Format a `generate` / `extract_ledger` result as human-readable **Markdown**.
Pure and deterministic — no model calls, invents nothing; it only lays out what
the pipeline already produced. Display only, never publishes.

### Parameters

| Name | Type | Required | Default | Meaning |
|------|------|----------|---------|---------|
| `result` | `AgentOutput` | ✅ | — | The result to render (from `generate` / `extract_ledger`) |
| `platform` | `news` \| `wechat` \| `xhs` | — | all | Render only this platform's draft |
| `include_provenance` | bool | — | `true` | `true` = review view (overstatement flags + draft + compact claim ledger + background sources); `false` = publish-ready post only (titles, cover copy, body, hashtags) |

### Return value

A Markdown **string**. Never crashes — on an unexpected error it returns a short
`render failed: …` line instead of raising.

### Example call

```json
{
  "name": "render",
  "arguments": {
    "result": { "...": "the AgentOutput returned by generate" },
    "platform": "wechat",
    "include_provenance": false
  }
}
```

---

<a id="tool-job_status"></a>
## Tool: `job_status`

Check how a background `generate` / `redraft` run is doing. Cheap; safe to poll.
No drafts or ledger come back here — call [`job_result`](#tool-job_result) for
content.

### Parameters

| Name | Type | Required | Default | Meaning |
|------|------|----------|---------|---------|
| `session_id` | string | ✅ | — | The id returned by `generate` or `redraft` |

### Return value — `JobProgress`

```jsonc
{
  "session_id": "j_a1b2c3_4d5e6f70",
  "state": "queued | running | done | failed | lost",
  "kind": "run | redraft",           // what finished: a first pass, or a rewrite
  "stage": "fetch | ledger | background | style | draft:<platform> | done",
  "steps_done": 5,
  "steps_total": 6,                  // 4 + one per platform
  "platforms_ready": ["news"],       // drafts job_result can already return
  "started_at": 1758000000.0,        // unix epoch seconds
  "updated_at": 1758000183.0,
  "elapsed_s": 183.0,
  "message": "the redraft is finished — call `job_result` for it",
  "result_available": true
}
```

- **`state`** — `lost` means the id expired, came from another instance, or
  predates a restart. Not a job that might still appear: call `generate` again.
- **`kind`** — "finished" answers a different question for a first pass than for
  a rewrite. A run produced a draft; a redraft **replaced** one.
- **`message`** — the status line in words. On `state="done"` it says the draft
  (or the redraft) is finished. Pass that on the moment you see it: the human is
  watching and cannot tell a finished run from a slow one.

Never crashes — an unreadable status comes back as `state="lost"` with the
reason in `message`, rather than raising.

---

<a id="tool-job_result"></a>
## Tool: `job_result`

Fetch a background run's output — **partial while it is still running**.

While the run is in flight this returns what already exists (the claim ledger,
plus each platform's draft as it lands) with `status="running"`, so a caller can
read the first draft while the rest are still being written. A partial **never**
carries a finished status, so it cannot be mistaken for a reviewed result.

### Parameters

| Name | Type | Required | Default | Meaning |
|------|------|----------|---------|---------|
| `session_id` | string | ✅ | — | The id returned by `generate` or `redraft` |

### Return value — `AgentOutput`

The same contract as `generate`.

- **still running** → a partial, `status="running"`, with a `running` notice.
- **finished** → the whole result, carrying a [`done`](#notice-codes) notice
  naming what it produced. Say it out loud; see
  [Telling the human it finished](#long-runs).
- **nothing behind that id** → `status="failed"` with an `unknown_session`
  notice saying whether it expired or came from another instance.

```json
{
  "name": "job_result",
  "arguments": { "session_id": "j_a1b2c3_4d5e6f70" }
}
```

---

<a id="tool-health"></a>
## Tool: `health`

Report agent readiness for the MCP host / platform. **Booleans only — never key
values** (`byo_key`). Implements the `/health` probe declared in `agent.yaml`.

### Parameters

None.

### Return value

```jsonc
{
  "roles": {                         // whether each role resolves to a model
    "extractor": true,
    "drafter": true,
    "reviewer": true,
    "researcher": false              // false is OK — it falls back to extractor
  },
  "search_sources": ["arxiv", "semantic_scholar", "pubmed", "crossref", "wikipedia", "ddgs"],
  "optional_keys": {                 // presence only, never the value
    "tavily": false,
    "semantic_scholar": false,
    "ncbi": false
  },
  "byo_key": true
}
```

---

## Pipeline behind the tool

```
fetch + extract  →  claim ledger  →  per-platform draft  →  faithfulness check
```

1. **fetch + extract** — pull the paper text (`extractor`, cheap model).
2. **claim ledger** — bind each claim to source evidence + qualifier (`extractor`).
3. **per-platform draft** — write per platform style (`drafter`, writing model);
   structure controlled by `api/styles/{news,xhs}.md`.
4. **faithfulness check** — a **different** model (`reviewer`, strong) checks the
   drafts against the ledger and emits overstatement flags. Never grades its own
   output (rule #3).

### Faithfulness rules (enforced)

1. Any number / causation / magnitude / "first" / "proves" statement must map to
   a ledger claim, or it is not written.
2. Every claim keeps its qualifier (species, sample, "preliminary",
   correlation-not-causation).
3. Drafting and checking use different models **and** different prompts.
4. **Never auto-publish** — always return draft + provenance + flags for a human.

---

## Contract & boundaries

- `/mcp_server` is a **thin** wrapper; it must not contain business logic and
  imports from `/api` only.
- `/api` is the single source of truth and must **not** import `/mcp_server`.
- The data contract (`AgentInput` / `AgentOutput` and enums) lives in
  `api/schema.py`; the tool signatures mirror `agent.yaml`.
- Health/readiness: the `health` tool (backed by `api.config_loader.capabilities`)
  reports configured roles, search sources, and key presence — see `agent.yaml`.
