"""Tests for api.manifest — reading agent.yaml. Pure, no model calls.

The front page describes the agent from its manifest rather than from a
hand-written copy, so this pins the shape that page depends on: the tools are
listed in declaration order, and a tool's parameters keep whether they are
required and what they default to.
"""

from __future__ import annotations

from api.manifest import load_manifest


def test_reports_the_agents_identity():
    manifest = load_manifest()

    assert manifest.name == "scicomm-agent"
    assert manifest.version
    assert manifest.owner


def test_lists_every_tool_in_declaration_order():
    names = [t.name for t in load_manifest().tools]

    assert names[0] == "generate"
    assert {"generate", "extract_ledger", "check_draft", "render", "health"} <= set(names)


def test_a_tool_keeps_its_description_and_parameters():
    generate = next(t for t in load_manifest().tools if t.name == "generate")

    assert "sci-comm" in generate.description or "drafts" in generate.description
    source_type = next(p for p in generate.parameters if p.name == "source_type")
    assert source_type.required is True
    assert source_type.allowed == ["doi", "url", "pdf"]

    liveliness = next(p for p in generate.parameters if p.name == "liveliness")
    assert liveliness.required is False
    assert liveliness.default == 3


def test_the_manifest_and_the_server_declare_the_same_tools():
    """Two audiences read these separately — a host reads the server, a human
    reads the front page, which is built from the manifest. A tool in one and
    not the other is a tool somebody cannot find."""
    import asyncio

    from mcp_server import server

    declared = {t.name for t in load_manifest().tools}
    registered = {t.name for t in asyncio.run(server.mcp.list_tools())}

    assert declared == registered


def test_a_tool_with_no_parameters_is_not_an_error():
    health = next(t for t in load_manifest().tools if t.name == "health")

    assert health.parameters == []


def test_model_roles_carry_their_tier():
    roles = {r.role: r.tier for r in load_manifest().model_requirements}

    assert roles["reviewer"] == "strong"
    assert roles["extractor"] == "cheap"


def test_the_manifest_is_serialisable_for_the_page():
    payload = load_manifest().model_dump(mode="json")

    assert payload["name"] == "scicomm-agent"
    assert isinstance(payload["tools"], list)


# Parameters that exist on one side only for a legitimate reason — e.g. an
# argument the MCP wrapper adds for transport that the manifest has no business
# describing. Each entry must carry a one-line reason. Empty today: every
# registered tool's signature is exactly what the manifest declares.
PARAMETER_EXEMPTIONS: dict[str, dict[str, str]] = {}


def _registered_tool_schemas() -> dict[str, dict]:
    """The server's own view of every tool: {tool name: JSON input schema}.

    Derived from the running server (the same `server.mcp.list_tools()` the
    tool-name test calls), never from a hand-written list — a list written here
    would be the test seeding its own expected value, and would go stale in
    exactly the way it is supposed to catch.
    """
    import asyncio

    from mcp_server import server

    return {t.name: (t.inputSchema or {}) for t in asyncio.run(server.mcp.list_tools())}


def test_every_tool_declares_the_servers_parameters():
    """Parameter-level fidelity, for EVERY tool, not one.

    The name-set check above goes green while a tool's parameters drift: the
    manifest is what a calling agent reads to discover a dial, so a parameter
    the server accepts and the manifest omits is a feature nobody can find.
    Generalised over all tools on purpose — a check pinned to a single tool
    would not have caught `generate`/`redraft` silently missing `images`.
    """
    schemas = _registered_tool_schemas()
    problems: list[str] = []

    for tool in load_manifest().tools:
        schema = schemas.get(tool.name)
        if schema is None:
            continue  # covered by the tool-name test above

        exempt = PARAMETER_EXEMPTIONS.get(tool.name, {})
        served = {n for n in (schema.get("properties") or {}) if n not in exempt}
        declared = {p.name for p in tool.parameters if p.name not in exempt}

        for name in sorted(served - declared):
            problems.append(
                f"{tool.name}: parameter '{name}' is accepted by the server but "
                f"is not declared in agent.yaml — a caller reading the manifest "
                f"cannot discover it"
            )
        for name in sorted(declared - served):
            problems.append(
                f"{tool.name}: parameter '{name}' is declared in agent.yaml but "
                f"the server does not accept it"
            )

        server_required = set(schema.get("required") or [])
        for param in tool.parameters:
            if param.name in exempt or param.name not in served:
                continue
            is_required = param.name in server_required
            if param.required and not is_required:
                problems.append(
                    f"{tool.name}: parameter '{param.name}' is required in "
                    f"agent.yaml but has a default in the server signature"
                )
            elif not param.required and is_required:
                problems.append(
                    f"{tool.name}: parameter '{param.name}' is optional in "
                    f"agent.yaml but the server has no default for it"
                )

    assert not problems, "manifest/server parameter drift:\n  " + "\n  ".join(problems)
