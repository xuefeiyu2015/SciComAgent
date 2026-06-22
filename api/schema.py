"""Input/output schemas (pydantic) for the pipeline.

Single source of truth for the data contracts shared across api/ steps and
exposed (read-only) through the /mcp wrapper. Placed under /api because
schemas are business-logic artifacts.

Planned models (no fields yet):
- Claim          : one ledger entry (text, qualifier, source span/pointer)
- ClaimLedger    : collection of Claim
- DraftRequest   : platform + parameters (language/audience/liveliness/variety)
- Draft          : generated content for one platform
- OverstatementFlag : a flagged unsupported/over-claimed statement
- PipelineResult : draft + provenance + flags

TODO: define models.
"""
