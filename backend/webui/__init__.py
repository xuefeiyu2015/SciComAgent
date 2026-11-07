"""Local review board — a THIN HTTP window onto /api.

Mirrors the /mcp_server contract: no business logic and no model calls live
here, only request/response marshalling. /api never imports this package.
"""
