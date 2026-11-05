# scicomagent-app

[SciComAgent](https://github.com/xuefeiyu2015/SciComAgent) (by xuefeiyu2015) split into two deployable halves, hosted by the
图灵星球 platform:

| Folder | What | Hosted on | Address |
| --- | --- | --- | --- |
| `frontend/` | The pages: overview (`index.html`) and review board (`board.html`). Plain HTML/JS/CSS, no build step | Vercel | https://scicom-app.apps.turingplanet.ai |
| `backend/` | The Python agent: the board's API (`webui/app.py`) plus everything under `api/`. The original project, moved as-is | Railway | https://scicom-api.agents.turingplanet.ai |

Both come from this one repo through two entries in
[`agent-registry/deployments.yaml`](https://github.com/turingplanet/agent-registry/blob/main/deployments.yaml):

```yaml
  - slug: scicom-api
    repo: enochhz/scicomagent-app
    host: railway
    root: backend
    gateway: false
  - slug: scicom-app
    repo: enochhz/scicomagent-app
    host: vercel
    root: frontend
```

## How the halves talk

- `frontend/static/config.js` holds the backend's address. The pages call it directly (`fetch` with credentials), and
  generated images load from its `/images/`.
- The backend allows that one origin (CORS, `SCICOM_FRONTEND_ORIGINS`) and keeps every `/api` and `/images` route behind
  an **access key** (`backend/webui/access.py`). The key is entered once per browser and kept as an HttpOnly cookie for
  30 days; both addresses are under turingplanet.ai, so the cookie is same-site. Without a key configured, a hosted
  backend stays locked.

## Run locally

```bash
cd backend
uv sync
uv run python -m webui.app      # http://127.0.0.1:8080 , board at /board
```

Locally the backend also serves `../frontend`, so it is still one app on one port, with no access key asked.
Tests: `uv run pytest -q --ignore=tests/faithfulness` (the faithfulness suite calls real models).

## Backend settings (Railway → the scicom-api service → Variables)

| Variable | What |
| --- | --- |
| `SCICOM_ACCESS_KEY` | **Required.** The access key people type to use the app. Changing it signs everyone out |
| `GOOGLE_API_KEY` / `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` | At least one, for the providers your roles use |
| `EXTRACTOR_PROVIDER` `EXTRACTOR_MODEL` · `DRAFTER_PROVIDER` `DRAFTER_MODEL` · `REVIEWER_PROVIDER` `REVIEWER_MODEL` | The three required roles (reviewer must differ from drafter). Optional roles and `IMAGE_MODEL`: see `backend/config/config.example.yaml` |
| `SCICOM_FRONTEND_ORIGINS` | Optional; comma-separated. Default `https://scicom-app.apps.turingplanet.ai` |

`PORT` and `HOST` are set by the platform. Model choices made in the board's Settings panel are written to the
container's disk and are lost on the next deploy; set them as variables to keep them.

## Keeping up with the original project

`upstream` points at xuefeiyu2015/SciComAgent. Git follows the moved files, so most changes merge cleanly:

```bash
git fetch upstream && git merge upstream/main
```

New files upstream land at the repository root; move them into `backend/` (or `frontend/` for `webui/static/`).
