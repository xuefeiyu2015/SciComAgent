"""Smoke runner: build a claim ledger from a saved source card.

Reads a card envelope (as written by the extract smoke run) from outputs/,
runs api.ledger.build_ledger on the inner card using the configured EXTRACTOR
role (Gemini free tier by default), and writes a ledger envelope back to
outputs/ for human inspection. Live call — needs GOOGLE_API_KEY in the env.

Usage:
    python scripts/run_ledger.py [path/to/<name>.card.json]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from api.config_loader import resolve_role
from api.ledger import build_ledger

_DEFAULT_CARD = Path("outputs/attention-is-all-you-need.card.json")


def main() -> None:
    card_path = Path(sys.argv[1]) if len(sys.argv) > 1 else _DEFAULT_CARD
    envelope = json.loads(card_path.read_text(encoding="utf-8"))
    card = envelope.get("card", envelope)  # accept a bare card too

    claims = build_ledger(card)

    provider, model = resolve_role("extractor")
    out = {
        "source": envelope.get("source"),
        "resolved_url": envelope.get("resolved_url"),
        "extractor_model": f"{provider} / {model}",
        "card_title": card.get("title"),
        "claim_count": len(claims),
        "claim_ledger": [c.model_dump() for c in claims],
    }

    out_path = card_path.with_name(card_path.name.replace(".card.json", ".ledger.json"))
    out_path.write_text(
        json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"wrote {out_path}  ({len(claims)} claims)")


if __name__ == "__main__":
    main()
