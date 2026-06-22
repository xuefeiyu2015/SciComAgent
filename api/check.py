"""Pipeline step 4 — faithfulness check.

Verify every claim in the draft maps to the claim ledger and keeps its
qualifier. Emit overstatement flags. NEVER auto-publish — always return
draft + provenance + flags for a human.

Uses the REVIEWER model role (strong) with a DIFFERENT model + DIFFERENT
prompt than draft.py. No business logic yet.

TODO: implement check().
"""
