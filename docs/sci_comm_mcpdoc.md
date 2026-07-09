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
| [`extract_ledger`](#tool-extract_ledger) | Cheap provenance preview: paper → claim ledger only (no drafting) |
| [`check_draft`](#tool-check_draft) | Re-check a (human-edited) draft against its claim ledger |
| [`health`](#tool-health) | Report configured model roles, search sources, and key presence |

A typical human-in-the-loop flow: **`extract_ledger`** to inspect/approve the
facts → draft (via `generate`, or edit by hand) → **`check_draft`** to re-verify.

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
exposed as `mcp__scicomm-agent__generate`, `…__extract_ledger`,
`…__check_draft`, and `…__health`.

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

### Parameters

| Name          | Type                        | Required | Default                    | Notes |
|---------------|-----------------------------|----------|----------------------------|-------|
| `source`      | string                      | ✅       | —                          | PDF link / DOI / web URL of the paper |
| `source_type` | `doi` \| `url` \| `pdf`     | ✅       | —                          | How to interpret `source` |
| `platforms`   | list of `news`/`wechat`/`xhs` | ❌     | `[news, wechat, xhs]`      | Target platforms to draft for |
| `language`    | `zh` \| `en`                | ❌       | `zh`                       | Output language |
| `audience`    | string                      | ❌       | `general_public`           | Intended reader |
| `liveliness`  | int 1–5                     | ❌       | `3`                        | Tone liveliness |
| `background`  | bool                        | ❌       | `true`                     | Gather external background materials (web / arXiv / scholarly APIs) as **framing context** for the drafts. Never a source of facts; failure degrades gracefully |

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
  "status": "ok | needs_review | no_claims | failed"
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
- **`notices`** — non-draft messages (e.g. why fetch failed).
- **`status`** — coarse outcome (see below).

### Status values

| Status         | Meaning |
|----------------|---------|
| `ok`           | Full pipeline succeeded |
| `needs_review` | Produced, but has flags / needs a human |
| `no_claims`    | Nothing could be sourced → nothing may be written (rule #1) |
| `failed`       | Fetch/pipeline failure — see the notice |

### Notice codes

| Code          | Meaning / action |
|---------------|------------------|
| `ok`          | Full text obtained |
| `need_pdf`    | Source exists but access is blocked (paywall) → provide a PDF link |
| `too_short`   | Reachable but too little text (stub / scanned PDF) → provide a PDF link |
| `not_a_paper` | Content is not a research paper → check the link |
| `fetch_error` | Network failure / unreachable link |
| `draft_error` | One platform's draft crashed (pipeline-internal) |
| `background_error` | Background search was skipped; drafts are produced without it (pipeline-internal) |

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
   structure controlled by `api/styles/{news,wechat,xhs}.md`.
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
