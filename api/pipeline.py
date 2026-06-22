"""Pipeline orchestration.

Wires the 4 steps end to end:
    fetch+extract -> claim ledger -> per-platform draft -> faithfulness check

Returns: draft + provenance + overstatement flags (never auto-publishes).
Model names come from config by ROLE; never hardcode. No business logic yet.

TODO: implement run_pipeline().
"""
