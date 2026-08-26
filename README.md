


## Review board (local web UI)

A local page for the human half of the loop: it collects the run parameters,
runs the pipeline, and shows each draft with its flagged sentences marked in
red for you to accept or rewrite, tethered to the claim ledger that backs them.

```
uv run python -m webui.app     # http://127.0.0.1:8080
```

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
  in amber, in both the ledger and the citations pointing at them — those are
  the sentences most worth rewriting.
- Select any passage in a draft and a **Rewrite** button appears: say how you
  want it changed in your own words, refine the proposal as many times as you
  like, then Apply. The claim ledger still bounds the result.
- Every factual sentence cites the ledger entry it rests on. The citation is
  written `(c17)` in the draft, shown as a superscript on the board, and
  written `^c17` in plain text. Click a citation to trace it to its evidence,
  or click a ledger entry to find the sentences resting on it.
