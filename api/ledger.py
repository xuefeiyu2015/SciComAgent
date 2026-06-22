"""Pipeline step 2 — claim ledger.

Build the ledger of source-grounded claims. Each claim records its
qualifier (species, sample, "preliminary", correlation-not-causation)
and a pointer back to the source span.

Hard rules (CLAUDE.md): every number/causation/magnitude/"first"/"proves"
statement a draft may use MUST originate here. Qualifiers are never dropped.

Uses the EXTRACTOR model role (cheap). No business logic yet.

TODO: implement build_ledger().
"""
