"""Tests for api.manifest — reading agent.yaml. Pure, no model calls.

The front page describes the agent from its manifest rather than from a
hand-written copy, so this pins the shape that page depends on: the tools are
listed in declaration order, and a tool's parameters keep whether they are
required and what they default to.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

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


def _enum_members(schema: dict, prop: Any, _seen: frozenset[str] = frozenset()) -> set | None:
    """The values a parameter accepts, read off the server's own JSON schema.

    The source of truth is the enum the server emits (`ImageMode`, `Platform`,
    `Language`, ...), never a list written here — a literal in the test is the
    test grading its own homework, which is how #53 went green while wrong.

    Returns None when the server puts no enum on that parameter, so the caller
    can tell "the server allows anything" apart from "the server allows these".
    Walks `$ref` into `$defs`, unwraps `anyOf` (an optional parameter is
    `anyOf: [<enum>, null]`) and `items` (a list parameter such as
    `platforms` is an array of enum members).
    """
    if not isinstance(prop, dict):
        return None

    ref = prop.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/$defs/"):
        name = ref.rsplit("/", 1)[-1]
        if name in _seen:
            return None
        return _enum_members(schema, (schema.get("$defs") or {}).get(name), _seen | {name})

    if isinstance(prop.get("enum"), list):
        return set(prop["enum"])

    if "items" in prop:
        return _enum_members(schema, prop["items"], _seen)

    branches = prop.get("anyOf") or prop.get("oneOf") or []
    members: set = set()
    found = False
    for branch in branches:
        if isinstance(branch, dict) and branch.get("type") == "null":
            continue  # the "omitted" arm of an optional parameter
        got = _enum_members(schema, branch, _seen)
        if got is not None:
            found = True
            members |= got
    return members if found else None


def test_every_tool_declares_the_servers_parameters():
    """Parameter-level fidelity, for EVERY tool, not one.

    The name-set check above goes green while a tool's parameters drift: the
    manifest is what a calling agent reads to discover a dial, so a parameter
    the server accepts and the manifest omits is a feature nobody can find.
    Generalised over all tools on purpose — a check pinned to a single tool
    would not have caught `generate`/`redraft` silently missing `images`.

    Names and required-ness are not the whole contract: the manifest also
    advertises each dial's `allowed` values, and a caller picks from that list.
    So the values are compared against the server's enum too — that half is
    what #56 added, after `illustrate`'s unquoted `off` loaded as the boolean
    False and the front page offered `false` as a legal mode.
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

            served_values = _enum_members(schema, (schema.get("properties") or {})[param.name])
            if not param.allowed:
                if served_values:
                    problems.append(
                        f"{tool.name}: parameter '{param.name}' accepts only "
                        f"{sorted(served_values)} on the server but agent.yaml "
                        f"lists no `allowed` — a caller cannot see the choices"
                    )
            elif served_values is None:
                problems.append(
                    f"{tool.name}: agent.yaml restricts '{param.name}' to "
                    f"{param.allowed} but the server constrains it to no such "
                    f"set — the manifest advertises a rule the server lacks"
                )
            elif set(param.allowed) != served_values:
                problems.append(
                    f"{tool.name}: parameter '{param.name}' allows "
                    f"{param.allowed} in agent.yaml but {sorted(served_values)} "
                    f"on the server (missing {sorted(served_values - set(param.allowed))}, "
                    f"unknown {sorted(map(repr, set(param.allowed) - served_values))})"
                )

    assert not problems, "manifest/server parameter drift:\n  " + "\n  ".join(problems)


# ── the front page, rendered (#56) ──────────────────────────────────────────
#
# The manifest being right is only half of it: what a reader is offered is the
# markup `front.js` builds from it. `webui/app.py:agent` serves exactly
# `load_manifest().model_dump(mode="json")`, so the page's own `toolCard()` is
# run here over that payload and the assertion reads the HTML it produced —
# not the parsed manifest a second time.

_STATIC = Path(__file__).resolve().parent.parent / "webui" / "static"
_NODE = shutil.which("node")
_NO_NODE = "node is not installed; the front.js render harness needs it"

_FRONT_HARNESS_JS = r"""
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const opts = JSON.parse(process.env.FRONT_HARNESS);

/* A DOM only as big as toolCard() needs. */
class Elem {
  constructor(tag) { this.tag = tag; this.className = ''; this.innerHTML = ''; this.children = []; this.dataset = {}; }
  set textContent(v) { this.innerHTML = String(v); }
  append(...nodes) { this.children.push(...nodes); }
  get outerHTML() {
    const cls = this.className ? ' class="' + this.className + '"' : '';
    const kids = this.children.map((c) => c.outerHTML).join('');
    return '<' + this.tag + cls + '>' + this.innerHTML + kids + '</' + this.tag + '>';
  }
}
globalThis.document = {
  createElement: (tag) => new Elem(tag),
  querySelector: () => new Elem('div'),
  querySelectorAll: () => [],
};
globalThis.window = globalThis;
globalThis.localStorage = { getItem: () => null, setItem() {}, removeItem() {} };
globalThis.fetch = async () => { throw new Error('the harness serves no network'); };
/* front.js ends in `start()`, which awaits a fetch that cannot succeed here.
   That rejection is expected and says nothing about toolCard(). */
process.on('unhandledRejection', () => {});

const source = ['i18n.js', 'front.js']
  .map((f) => fs.readFileSync(path.join(opts.static_dir, f), 'utf8'))
  .join('\n');
const epilogue = '\n'
  + 'I18N.strings = ' + fs.readFileSync(opts.i18n_json, 'utf8') + ';\n'
  + 'I18N.lang = ' + JSON.stringify(opts.locale) + ';\n'
  + 'globalThis.__manifest = ' + JSON.stringify(opts.manifest) + ';\n'
  + 'globalThis.__render = (name) => '
  + 'toolCard(globalThis.__manifest.tools.find((t) => t.name === name)).outerHTML;\n';
vm.runInThisContext(source + epilogue, { filename: 'front-harness.js' });
process.stdout.write(globalThis.__render(opts.tool));
"""


def _render_tool_card(tool_name: str, locale: str = "en") -> str:
    """The markup the overview page draws for one tool, from front.js itself."""
    env = {
        **os.environ,
        "FRONT_HARNESS": json.dumps({
            "static_dir": str(_STATIC),
            "i18n_json": str(_STATIC.parent / "i18n.json"),
            "locale": locale,
            "manifest": load_manifest().model_dump(mode="json"),  # what /api/agent serves
            "tool": tool_name,
        }),
    }
    proc = subprocess.run(
        [_NODE, "-e", _FRONT_HARNESS_JS],
        capture_output=True, text=True, env=env, timeout=60,
    )
    assert proc.returncode == 0, f"the front.js harness failed:\n{proc.stderr}"
    return proc.stdout


@pytest.mark.skipif(_NODE is None, reason=_NO_NODE)
def test_the_front_page_offers_illustrate_the_three_image_modes():
    """`off · cover · all` — the modes the server accepts, as a reader sees them.

    The bug this pins rendered `false · cover · all`: a value the server would
    reject, offered on the front page, because bare `off` is a YAML 1.1
    boolean.
    """
    markup = _render_tool_card("illustrate")
    images_row = next(
        row for row in markup.split('<div class="param">')
        if row.startswith('<span class="param-name">images')
    )

    assert "<b>off</b> · <b>cover</b> · <b>all</b>" in images_row
    assert "<b>false</b>" not in images_row.lower()   # `force`'s real default is elsewhere
