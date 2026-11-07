"""api/ — single source of truth for business logic (the whole pipeline).

Directory contract (see CLAUDE.md):
- This package owns ALL business logic.
- /mcp is a THIN wrapper over this package; /api must NOT import /mcp.
"""
