"""mcp/ — THIN wrapper over /api exposing MCP tools.

Directory contract (see CLAUDE.md): NO business logic here. This package
only adapts api/ functions into MCP tools. It may import from /api;
/api must never import from /mcp.
"""
