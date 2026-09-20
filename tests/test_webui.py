"""Tests for the /webui HTTP wrapper — /api is stubbed, no model calls.

The wrapper holds no business logic, so what is tested here is exactly the
wrapper's job: refusing input /api should never see, never leaking a secret,
never letting a filename escape the outputs directory, and never turning a
pipeline failure into a 500.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from api.schema import (
    AgentOutput,
    Claim,
    ConfidenceLevel,
    ImageAsset,
    ImageKind,
    JobProgress,
    JobState,
    OverreachFlag,
    Platform,
    PlatformOutput,
    Status,
)
from webui import app as webui

_PDF = b"%PDF-1.7\n" + b"x" * 100

_DRAFT = PlatformOutput(
    platform=Platform.news,
    title_options=["温和的标题"],
    body="研究团队做了实验。该疗法治愈了癌症。",
)
_LEDGER = [
    Claim(
        id="c1",
        claim="该疗法在小鼠中将肿瘤体积缩小了23%",
        source_evidence="key_numbers",
        qualifier="mice, n=12",
        confidence=ConfidenceLevel.high,
    )
]
_RESULT = AgentOutput(
    status=Status.needs_review,
    platform_outputs=[_DRAFT],
    claim_ledger=_LEDGER,
    overreach_flags=[
        OverreachFlag(text="该疗法治愈了癌症。", reason="丢失限定词", platform=Platform.news)
    ],
)

_GENERATE = {"source": "https://arxiv.org/abs/1706.03762", "source_type": "url"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A test client whose writes land in tmp_path and whose /api is stubbed."""
    monkeypatch.setattr(webui, "_UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setattr(webui, "_REVIEW_DIR", tmp_path / "reviews")
    monkeypatch.setattr(webui.settings, "drafter_reviewer_distinct", lambda: True)
    with TestClient(webui.app) as test_client:
        yield test_client


# --- upload -------------------------------------------------------------------

def test_upload_accepts_a_pdf_and_returns_a_pdf_source(client, tmp_path):
    resp = client.post("/api/upload", content=_PDF, headers={"X-Filename": "paper.pdf"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["source_type"] == "pdf"
    assert (tmp_path / "uploads").exists()
    assert body["filename"] == "paper.pdf"


def test_upload_rejects_something_that_is_not_a_pdf(client):
    resp = client.post("/api/upload", content=b"<html>nope</html>", headers={"X-Filename": "x.pdf"})

    assert resp.status_code == 415
    assert "error" in resp.json()


def test_upload_rejects_an_oversize_file(client, monkeypatch):
    monkeypatch.setattr(webui, "_MAX_UPLOAD_BYTES", 50)

    resp = client.post("/api/upload", content=_PDF, headers={"X-Filename": "big.pdf"})

    assert resp.status_code == 413


# --- generate -----------------------------------------------------------------

def test_generate_starts_a_job_and_returns_its_session_id(client, monkeypatch):
    monkeypatch.setattr(webui.jobs, "start", lambda inp: "j_test_1")

    resp = client.post("/api/generate", json=_GENERATE)

    assert resp.status_code == 200
    assert resp.json()["session_id"] == "j_test_1"


def test_generate_is_blocked_when_drafter_and_reviewer_are_the_same(client, monkeypatch):
    monkeypatch.setattr(webui.settings, "drafter_reviewer_distinct", lambda: False)
    started = []
    monkeypatch.setattr(webui.jobs, "start", lambda inp: started.append(inp) or "j")

    resp = client.post("/api/generate", json=_GENERATE)

    assert resp.status_code == 409
    assert started == []          # rule #3: the run must never begin
    assert "error" in resp.json()


def test_generate_rejects_an_out_of_range_dial(client, monkeypatch):
    monkeypatch.setattr(webui.jobs, "start", lambda inp: "j")

    resp = client.post("/api/generate", json={**_GENERATE, "liveliness": 9})

    assert resp.status_code == 400


# --- job polling --------------------------------------------------------------

def test_job_status_is_passed_through(client, monkeypatch):
    monkeypatch.setattr(
        webui.jobs,
        "status",
        lambda sid: JobProgress(session_id=sid, state=JobState.running, stage="draft:news"),
    )

    body = client.get("/api/job/j_test_1/status").json()

    assert body["state"] == "running"
    assert body["stage"] == "draft:news"


def test_job_result_carries_flag_spans_for_the_board(client, monkeypatch):
    monkeypatch.setattr(webui.jobs, "result", lambda sid: _RESULT)

    body = client.get("/api/job/j_test_1/result").json()

    span = body["spans"]["news"]["spans"][0]
    assert _DRAFT.body[span["start"] : span["end"]] == "该疗法治愈了癌症。"
    assert body["spans"]["news"]["unlocated"] == []


def test_a_lost_session_is_reported_not_a_server_error(client, monkeypatch):
    monkeypatch.setattr(webui.jobs, "result", lambda sid: None)

    resp = client.get("/api/job/j_gone/result")

    assert resp.status_code == 404


# --- save ---------------------------------------------------------------------

def test_save_writes_one_clean_text_file_per_platform(client, tmp_path):
    resp = client.post(
        "/api/save",
        json={"filename": "attention", "result": json.loads(_RESULT.model_dump_json())},
    )

    assert resp.status_code == 200
    written = tmp_path / "reviews" / "attention.news.txt"
    assert written.exists()
    text = written.read_text(encoding="utf-8")
    assert "该疗法治愈了癌症。" in text     # the reviewed prose
    assert "依据清单" not in text          # no provenance
    assert "丢失限定词" not in text         # no flags


def test_saved_text_carries_no_ledger_citations(client, tmp_path):
    cited = json.loads(_RESULT.model_dump_json())
    cited["platform_outputs"][0]["body"] = "肿瘤体积缩小了23% (c1)。"

    client.post("/api/save", json={"filename": "cited", "result": cited})

    text = (tmp_path / "reviews" / "cited.news.txt").read_text(encoding="utf-8")
    assert "c1" not in text
    assert "肿瘤体积缩小了23%。" in text


def test_save_refuses_a_filename_that_escapes_the_reviews_directory(client, tmp_path):
    resp = client.post(
        "/api/save",
        json={"filename": "../../etc/passwd", "result": json.loads(_RESULT.model_dump_json())},
    )

    assert resp.status_code == 400
    assert not (tmp_path.parent / "etc").exists()


# --- settings -----------------------------------------------------------------

def test_settings_never_returns_a_key_value(client, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-do-not-leak")

    resp = client.get("/api/settings")

    assert resp.status_code == 200
    assert "sk-do-not-leak" not in resp.text
    assert resp.json()["keys"]["ANTHROPIC_API_KEY"] is True


def test_there_is_no_route_that_writes_a_key(client):
    """Keys come from .env only. A live write path the UI stopped using would
    still be reachable by anything on localhost."""
    resp = client.post("/api/settings/keys", json={"keys": {"PATH": "/tmp/evil"}})

    assert resp.status_code in (404, 405)


def test_providers_offers_only_what_a_key_can_run(client, monkeypatch):
    from api import providers as providers_module

    monkeypatch.setenv("GOOGLE_API_KEY", "k")
    for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    providers_module.forget_models()
    monkeypatch.setattr(providers_module, "_fetch_json", lambda *a, **k: {
        "models": [{"name": "models/gemini-flash-latest", "displayName": "Gemini Flash Latest",
                    "supportedGenerationMethods": ["generateContent"]}]
    })

    rows = {p["id"]: p for p in client.get("/api/providers").json()["providers"]}
    providers_module.forget_models()

    assert rows["google_genai"]["available"] is True
    assert [m["id"] for m in rows["google_genai"]["models"]] == ["gemini-flash-latest"]
    assert rows["openai"]["available"] is False
    assert rows["openai"]["models"] == []


def test_providers_never_returns_a_key_value(client, monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "sk-do-not-leak")

    assert "sk-do-not-leak" not in client.get("/api/providers").text


# --- review actions -----------------------------------------------------------

def test_revise_returns_the_rewritten_sentence(client, monkeypatch):
    monkeypatch.setattr(webui, "revise_sentence", lambda *a, **k: "在小鼠中（n=12），肿瘤体积缩小了23%。")

    resp = client.post(
        "/api/revise",
        json={
            "sentence": "该疗法治愈了癌症。",
            "instruction": "丢失限定词",
            "ledger": [json.loads(_LEDGER[0].model_dump_json())],
            "platform": "news",
            "language": "zh",
        },
    )

    assert resp.status_code == 200
    assert resp.json()["sentence"] == "在小鼠中（n=12），肿瘤体积缩小了23%。"


def test_a_provider_failure_during_revise_is_a_message_not_a_crash(client, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("provider is on fire")

    monkeypatch.setattr(webui, "revise_sentence", boom)

    resp = client.post(
        "/api/revise",
        json={
            "sentence": "s",
            "instruction": "r",
            "ledger": [],
            "platform": "news",
            "language": "zh",
        },
    )

    assert resp.status_code == 502
    assert "provider is on fire" in resp.json()["error"]


def test_recheck_returns_the_reviewers_flags(client, monkeypatch):
    monkeypatch.setattr(
        webui, "check_faithfulness", lambda draft, ledger, card, language: []
    )

    resp = client.post(
        "/api/recheck",
        json={
            "draft": json.loads(_DRAFT.model_dump_json()),
            "ledger": [json.loads(_LEDGER[0].model_dump_json())],
            "language": "zh",
        },
    )

    assert resp.status_code == 200
    assert resp.json()["flags"] == []


def test_the_learned_voice_never_reaches_the_browser(client, monkeypatch):
    """The voice profile is the operator's own craft — it is not shipped."""
    from api.schema import StyleProfile

    secret = AgentOutput(
        status=Status.needs_review,
        platform_outputs=[_DRAFT],
        claim_ledger=_LEDGER,
        style_profile=StyleProfile(voice="从一个具体场景开场", sources=["favourite-essay.md"]),
    )
    monkeypatch.setattr(webui.jobs, "result", lambda sid: secret)

    resp = client.get("/api/job/j_test_1/result")

    assert "从一个具体场景开场" not in resp.text
    assert "favourite-essay.md" not in resp.text
    assert resp.json()["result"].get("style_profile") is None


def test_revise_refuses_an_empty_instruction(client, monkeypatch):
    """Without a request there is nothing to ask for — do not spend a call."""
    called = []
    monkeypatch.setattr(webui, "revise_sentence", lambda *a, **k: called.append(a) or "x")

    resp = client.post(
        "/api/revise",
        json={"sentence": "s", "instruction": "   ", "ledger": [], "platform": "news"},
    )

    assert resp.status_code == 400
    assert called == []


def test_job_result_marks_sentences_resting_on_hedged_evidence(client, monkeypatch):
    """A clean draft can still need review when its evidence is shaky."""
    from api.schema import ConfidenceLevel

    hedged_claim = Claim(
        id="c9", claim="推测的机制", source_evidence="discussion: may relate to",
        qualifier="推测", confidence=ConfidenceLevel.low,
    )
    draft = PlatformOutput(
        platform=Platform.news,
        body="扎实的一句 (c1)。作者推测机制与T细胞有关 (c9)。",
    )
    clean = AgentOutput(
        status=Status.needs_review,
        platform_outputs=[draft],
        claim_ledger=[_LEDGER[0], hedged_claim],
        overreach_flags=[],
    )
    monkeypatch.setattr(webui.jobs, "result", lambda sid: clean)

    pack = client.get("/api/job/j_test_1/result").json()["spans"]["news"]

    assert pack["flags"] == []
    assert len(pack["hedged"]) == 1
    span = pack["hedged"][0]
    assert draft.body[span["start"] : span["end"]] == "作者推测机制与T细胞有关 (c9)。"
    assert span["claim_ids"] == ["c9"]


def test_the_overview_serves_the_agents_own_manifest(client):
    """The page describes the agent from agent.yaml, never a second copy."""
    body = client.get("/api/agent").json()

    assert body["name"] == "scicomm-agent"
    names = [t["name"] for t in body["tools"]]
    assert names[0] == "generate"
    assert "health" in names


def test_the_board_keeps_its_own_address(client):
    assert client.get("/board").status_code == 200


def test_history_lists_past_runs(client, monkeypatch):
    from api import jobs as jobs_module

    jobs_module.jobs_dir().mkdir(parents=True, exist_ok=True)
    (jobs_module.jobs_dir() / "j_hist_1.json").write_text(
        _RESULT.model_dump_json(), encoding="utf-8"
    )

    runs = client.get("/api/history").json()["runs"]

    assert [r["session_id"] for r in runs] == ["j_hist_1"]
    assert runs[0]["title"] == "温和的标题"
    assert runs[0]["claims"] == 1


def test_history_survives_a_nonsense_limit(client):
    assert client.get("/api/history?limit=banana").status_code == 200


def _converse_body(**kw):
    body = {
        "message": "样本量写进去了吗？",
        "drafts": [json.loads(_DRAFT.model_dump_json())],
        "ledger": [json.loads(_LEDGER[0].model_dump_json())],
        "flags": [],
        "language": "zh",
    }
    body.update(kw)
    return body


def test_converse_answers_a_question(client, monkeypatch):
    from api.converse import AgentReply

    monkeypatch.setattr(
        webui, "converse", lambda *a, **k: AgentReply(kind="answer", message="写了。")
    )

    body = client.post("/api/converse", json=_converse_body()).json()

    assert body["kind"] == "answer"
    assert body["message"] == "写了。"
    assert body["replacement"] == ""


def test_converse_refuses_an_empty_message_before_spending_anything(client, monkeypatch):
    called = []
    monkeypatch.setattr(webui, "converse", lambda *a, **k: called.append(a))

    resp = client.post("/api/converse", json=_converse_body(message="   "))

    assert resp.status_code == 400
    assert called == []


def test_converse_needs_a_draft_to_talk_about(client, monkeypatch):
    called = []
    monkeypatch.setattr(webui, "converse", lambda *a, **k: called.append(a))

    resp = client.post("/api/converse", json=_converse_body(drafts=[]))

    assert resp.status_code == 400
    assert called == []


def test_conversing_knows_which_paper_it_is_talking_about(client, monkeypatch):
    """Without the run's real request, a rerun's dials would be a diff against
    a placeholder — and the agent could not honestly offer one at all."""
    from api.converse import AgentReply
    from api.schema import AgentInput, Language, SourceType

    recorded = AgentInput(
        source="https://example.org/the-paper",
        source_type=SourceType.pdf,
        language=Language.en,
    )
    monkeypatch.setattr(webui.jobs, "read_request", lambda sid: recorded)
    monkeypatch.setattr(webui.jobs, "read_card", lambda sid: {"title": "t"})
    seen = {}

    def spy(message, drafts, ledger, flags, inp, transcript, **kw):
        seen.update(inp=inp, card=kw.get("card"))
        return AgentReply(kind="answer", message="ok")

    monkeypatch.setattr(webui, "converse", spy)
    client.post("/api/converse", json=_converse_body(session_id="j_a_1"))

    assert seen["inp"].source == "https://example.org/the-paper"
    assert seen["inp"].source_type is SourceType.pdf
    assert seen["card"] == {"title": "t"}


def test_conversing_about_a_run_with_no_sidecar_still_works(client, monkeypatch):
    """A run mirrored before the sidecars existed can still be edited."""
    from api.converse import AgentReply

    monkeypatch.setattr(webui.jobs, "read_request", lambda sid: None)
    monkeypatch.setattr(webui.jobs, "read_card", lambda sid: None)
    seen = {}

    def spy(message, drafts, ledger, flags, inp, transcript, **kw):
        seen.update(inp=inp, card=kw.get("card"))
        return AgentReply(kind="answer", message="ok")

    monkeypatch.setattr(webui, "converse", spy)
    resp = client.post("/api/converse", json=_converse_body(session_id="j_old_1"))

    assert resp.status_code == 200
    assert seen["card"] is None


# --- redrafting ---------------------------------------------------------------

def test_redraft_starts_a_new_run_and_returns_its_id(client, monkeypatch):
    seen = {}
    monkeypatch.setattr(
        webui.jobs, "start_redraft",
        lambda sid, changes, restate=False: seen.update(sid=sid, changes=changes) or "j_a_2",
    )

    body = client.post(
        "/api/redraft", json={"session_id": "j_a_1", "changes": {"language": "en"}}
    ).json()

    assert body["session_id"] == "j_a_2"
    assert seen == {"sid": "j_a_1", "changes": {"language": "en"}}


def test_redrafting_a_run_that_is_gone_is_a_404(client, monkeypatch):
    def lost(sid, changes, restate=False):
        raise LookupError("No such job: call generate again.")

    monkeypatch.setattr(webui.jobs, "start_redraft", lost)

    resp = client.post("/api/redraft", json={"session_id": "j_a_1", "changes": {"language": "en"}})

    assert resp.status_code == 404
    assert "generate again" in resp.json()["error"]


def test_redrafting_nothing_is_a_400(client, monkeypatch):
    def nothing(sid, changes, restate=False):
        raise ValueError("nothing to redraft")

    monkeypatch.setattr(webui.jobs, "start_redraft", nothing)

    assert client.post(
        "/api/redraft", json={"session_id": "j_a_1", "changes": {}}
    ).status_code == 400
    assert client.post("/api/redraft", json={"changes": {}}).status_code == 400
    assert client.post(
        "/api/redraft", json={"session_id": "j_a_1", "changes": "en"}
    ).status_code == 400


def test_a_redraft_cannot_grade_its_own_work_either(client, monkeypatch):
    """Same refusal as generate: a redraft is a full draft-and-check chain."""
    called = []
    monkeypatch.setattr(webui.jobs, "start_redraft", lambda *a, **k: called.append(a))
    monkeypatch.setattr(webui.settings, "drafter_reviewer_distinct", lambda: False)

    resp = client.post(
        "/api/redraft", json={"session_id": "j_a_1", "changes": {"language": "en"}}
    )

    assert resp.status_code == 409
    assert called == []


def test_a_provider_failure_while_conversing_is_a_message_not_a_crash(client, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("provider is on fire")

    monkeypatch.setattr(webui, "converse", boom)

    resp = client.post("/api/converse", json=_converse_body())

    assert resp.status_code == 502
    assert "provider is on fire" in resp.json()["error"]


# --- images (#34) -------------------------------------------------------------

_COVER = ImageAsset(
    kind=ImageKind.cover,
    path="outputs/images/sess1/cover.png",
    alt="封面配图",
    generated=True,
    prompt="一张关于神经元的插画",
    model="illustrator",
)
_EXPLAINER = ImageAsset(
    kind=ImageKind.explainer,
    claim_id="c1",
    path="outputs/images/sess1/c1.png",
    alt="c1 的说明卡",
    generated=False,
)

_BOARD_JS = Path(webui.__file__).resolve().parent / "static" / "board.js"


@pytest.fixture
def images_client(tmp_path, monkeypatch):
    """The live `/images` mount, pointed at a throwaway `outputs/images/` tree.

    The mount is built at IMPORT time, so rebinding `webui._IMAGES_DIR` would
    not reach it. Redirecting the already-constructed `StaticFiles` is what
    keeps these tests on the real route and the real class — the containment
    under test is Starlette's, and a hand-built stand-in would not test it.
    """
    mount = next(r for r in webui.routes if getattr(r, "name", "") == "images")
    root = tmp_path / "outputs" / "images"
    root.mkdir(parents=True)
    monkeypatch.setattr(mount.app, "directory", str(root))
    monkeypatch.setattr(mount.app, "all_directories", [str(root)])
    with TestClient(webui.app) as test_client:
        yield test_client, root


def test_the_images_mount_serves_a_file_under_outputs_images(images_client):
    client, root = images_client
    (root / "sess1").mkdir()
    (root / "sess1" / "cover.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")

    resp = client.get("/images/sess1/cover.png")

    assert resp.status_code == 200
    assert resp.content == b"\x89PNG\r\n\x1a\nfake"
    # The mount reuses _RevalidatingStatic rather than a second static class.
    assert resp.headers["cache-control"] == "no-cache"


def test_the_images_mount_refuses_a_path_escape(images_client, tmp_path):
    """Containment is Starlette's, not a hand-rolled filter in /webui.

    `StaticFiles` resolves every request path against its own `directory` and
    refuses anything that lands outside it — the same mechanism that already
    protects `/static`. On top of that the server binds to 127.0.0.1 only
    (`_HOST`), so there is no remote caller to attempt this in the first place.
    """
    client, root = images_client
    secret = tmp_path / "outputs" / "secret.txt"
    secret.write_text("token", encoding="utf-8")
    assert secret.exists()          # the escape target really is there to find

    # Percent-encoded on purpose: a plain "/images/../secret.txt" is collapsed
    # to "/secret.txt" by the client before it is ever sent, so it would never
    # reach the mount. These arrive at the mount with the escape intact, which
    # is what a hand-crafted request would do.
    for attempt in (
        "/images/%2e%2e/secret.txt",
        "/images/..%2Fsecret.txt",
        "/images/sess1/%2e%2e/%2e%2e/secret.txt",
        f"/images/{secret}",                     # an absolute path as the segment
    ):
        resp = client.get(attempt)
        assert resp.request.url.raw_path.startswith(b"/images/"), attempt
        assert resp.status_code != 200, attempt
        assert b"token" not in resp.content, attempt


def test_the_images_directory_exists_at_import(tmp_path):
    """`StaticFiles` raises at CONSTRUCTION, and the mount is built at import.

    So the `mkdir` has to be at module scope too — a fresh clone with no
    `outputs/` tree at all must still start. This pins both halves: that the
    construction really does fail without the directory, and that importing
    `webui.app` has already created the real one.
    """
    missing = tmp_path / "outputs" / "images"
    with pytest.raises(RuntimeError):
        webui._RevalidatingStatic(directory=str(missing))

    missing.mkdir(parents=True, exist_ok=True)      # what webui/app.py does at import
    webui._RevalidatingStatic(directory=str(missing))    # now it constructs

    assert webui._IMAGES_DIR == webui._REPO_ROOT / "outputs" / "images"
    assert webui._IMAGES_DIR.is_dir()


def test_with_spans_leaves_a_no_images_result_exactly_as_api_produced_it():
    """A result with no images must reach the board unchanged (regression guard).

    Mirrors the byte-identical empty-images guards #32 added for
    `render_markdown`/`render_text`. Pinned against `model_dump` itself rather
    than a frozen literal, so it keeps holding as the schema grows.
    """
    assert _RESULT.images == []
    expected = _RESULT.model_dump(mode="json")
    expected.pop("style_profile", None)

    payload = webui._with_spans(_RESULT)["result"]

    assert payload == expected
    assert payload["images"] == []


def test_with_spans_never_rewrites_an_images_path():
    """`ImageAsset.path` stays repo-relative, exactly as /api produced it (#53).

    The browser URL is derived in `board.js` at render time; MCP and Markdown
    consumers read the same payload and depend on the repo-relative form.
    """
    out = _RESULT.model_copy(update={"images": [_COVER, _EXPLAINER]})

    payload = webui._with_spans(out)["result"]

    assert payload["images"] == [
        _COVER.model_dump(mode="json"),
        _EXPLAINER.model_dump(mode="json"),
    ]
    assert payload["images"][0]["path"] == "outputs/images/sess1/cover.png"
    assert payload["images"][1]["path"] == "outputs/images/sess1/c1.png"
    assert out.images[0].path == "outputs/images/sess1/cover.png"   # not mutated in place


def test_a_missing_image_file_is_a_404_not_a_server_error(images_client):
    """The slot 404s; `board.js` swaps in its placeholder via `img.onerror`.

    What matters for the rest of the board is that the miss stays local: a 404
    here, a placeholder there, and `renderBoard()` carries on.
    """
    client, _ = images_client

    resp = client.get("/images/sess1/gone.png")

    assert resp.status_code == 404


def test_the_board_guards_both_image_call_sites():
    """A result with no images must render exactly as it did before #34.

    There is no JS test harness in this suite, so the guard is pinned at the
    source: both call sites sit behind a truthiness check, so an absent cover
    or explainer appends nothing at all.
    """
    source = _BOARD_JS.read_text(encoding="utf-8")

    assert "if (coverAsset) wrap.append(imageFigure(coverAsset, { badge: true }));" in source
    assert "if (explainer) node.append(imageFigure(explainer, { badge: false }));" in source


def test_every_new_image_string_is_in_both_locales():
    """No user-facing image string is hardcoded in board.js (#34 criterion)."""
    strings = json.loads(
        (Path(webui.__file__).resolve().parent / "i18n.json").read_text(encoding="utf-8")
    )
    for locale in ("zh", "en"):
        assert strings[locale]["board.images.generated"]
        assert strings[locale]["board.images.missing"]

    source = _BOARD_JS.read_text(encoding="utf-8")
    for literal in (
        strings["zh"]["board.images.generated"], strings["en"]["board.images.generated"],
        strings["zh"]["board.images.missing"], strings["en"]["board.images.missing"],
    ):
        assert literal not in source
    assert "t('board.images.generated')" in source
    assert "t('board.images.missing')" in source
