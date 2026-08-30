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
