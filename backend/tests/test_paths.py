"""Direct unit tests for api.paths.is_safe_session_id.

The predicate used to be exercised only indirectly through its callers
(api.jobs's mirror lookups, api.assets's path builders). Now that it lives in
its own leaf module and is the SOLE implementation both of them call, it gets
its own coverage that does not depend on either caller. The "bad" case list
is the same one pinned in tests/test_assets.py's parametrized cases, so this
module's behavior stays provably identical to what api.assets already relies
on.
"""

from __future__ import annotations

import pytest

from api.paths import is_safe_session_id


@pytest.mark.parametrize("bad", ["../escape", "a/b", "a\\b", "..", ""])
def test_rejects_unsafe_ids(bad):
    assert is_safe_session_id(bad) is False


@pytest.mark.parametrize("good", ["sess1", "abc123", "a_b_c", "C17", "20240101_run1"])
def test_accepts_safe_ids(good):
    assert is_safe_session_id(good) is True
