# tests/faithfulness/test_traps_live.py
# MODEL-BACKED faithfulness traps. These call the real checker (api.check), so
# they are SKIPPED by default. Enable explicitly:
#
#     RUN_LIVE_TESTS=1 pytest tests/faithfulness/test_traps_live.py -v
#
# Strategy: instead of an end-to-end paper->draft run (flaky, expensive), we
# isolate the faithfulness MECHANISM. Each fixture hands the checker a draft
# with ONE deliberately planted overstatement plus the ledger/card it violates.
# A competent reviewer must flag it. This keeps the signal sharp and the cost low.

import glob
import json
import os
import pathlib

import pytest

from api.check import check_faithfulness
from api.schema import Claim, Language, PlatformOutput

LIVE = os.getenv("RUN_LIVE_TESTS") == "1"
FIXTURES = pathlib.Path(__file__).parent / "fixtures"
CASES = sorted(glob.glob(str(FIXTURES / "*.json")))


@pytest.mark.skipif(not LIVE, reason="set RUN_LIVE_TESTS=1 to run model-backed traps")
@pytest.mark.parametrize("path", CASES, ids=[pathlib.Path(p).stem for p in CASES])
def test_checker_catches_planted_overstatement(path):
    case = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))

    # Build the real data contracts the checker expects.
    draft = PlatformOutput(**case["draft"])
    ledger = [Claim(**c) for c in case["ledger"]]
    language = Language(case.get("language", "zh"))

    # check_faithfulness(drafts, ledger, card, language) -> list[CheckFlag]
    flags = check_faithfulness(draft, ledger, case["card"], language)

    assert flags, f"{case['name']}: checker returned NO flags for a planted " \
                  f"{case['expect_issue']}"

    blob = json.dumps([f.model_dump() for f in flags], ensure_ascii=False).lower()
    assert any(k.lower() in blob for k in case["expect_keywords"]), (
        f"{case['name']}: expected a {case['expect_issue']} flag; got {flags}"
    )
