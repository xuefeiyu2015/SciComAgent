"""Pipeline step 3 — per-platform draft.

Generate a platform-specific draft (news / wechat / xhs) from the claim
ledger only. Structure/voice comes from api/styles/*.md; language,
audience, liveliness, variety are PARAMETERS (not separate files).

Uses the DRAFTER model role. Must use a DIFFERENT model + DIFFERENT prompt
than check.py (no grading your own work). No business logic yet.

TODO: implement draft().
"""
