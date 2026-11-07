# tests/faithfulness/test_ledger_filter.py
# DETERMINISTIC test of the ledger's code guardrail: a claim with no
# source_evidence must be dropped ("no receipt, no claim"). We replace the
# module-level `invoke_json` with a fake that returns canned data and stub out
# `get_model`, so no real model runs and the project's own parser + filter do
# the work.
#
# api/ledger.py builds claims via `get_model("extractor")` + `invoke_json(...)`,
# expects a {"claims": [...]} object, drops entries with empty `source_evidence`,
# and returns Claim objects with re-assigned ids (c1, c2, ...).

import api.ledger as ledger
from api.schema import Language


# c1 is properly sourced; c2 has an empty source; c3 has none at all.
_CANNED = {
    "claims": [
        {"id": "c1", "claim": "A", "source_evidence": "p3, line 2",
         "qualifier": "in mice", "confidence": "high"},
        {"id": "c2", "claim": "B", "source_evidence": "",
         "qualifier": "", "confidence": "low"},
        {"id": "c3", "claim": "C", "qualifier": "preliminary"},
    ]
}


def test_unsourced_claims_are_dropped(monkeypatch):
    # No real model is constructed or called.
    monkeypatch.setattr(ledger, "get_model", lambda *a, **k: object())
    monkeypatch.setattr(ledger, "invoke_json", lambda _model, _messages: _CANNED)

    result = ledger.build_ledger({"title": "anything"}, Language.en)

    ids = [c.id for c in result]
    assert ids == ["c1"]                       # only the sourced claim survives
    assert all(c.source_evidence for c in result)
