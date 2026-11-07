"""mcp_server/ — THIN wrapper over /api exposing MCP tools.

Directory contract (see CLAUDE.md): NO business logic here. This package
only adapts api/ functions into MCP tools. It may import from /api;
/api must never import from /mcp_server.

Named `mcp_server` (not `mcp`) so it does not shadow the PyPI `mcp` SDK
this wrapper imports (`from mcp.server.fastmcp import FastMCP`).
"""
