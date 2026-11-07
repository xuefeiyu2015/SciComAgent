"""Read agent.yaml — the agent's own description of itself.

`agent.yaml` is the manifest the Turing Planet platform reads: what this agent
is called, which MCP tools it exposes, what each takes and returns, and which
model roles it needs. The overview page describes the agent FROM this file
rather than from a second copy written by hand, because a hand-written copy
drifts — the manifest changes when a tool changes, and prose does not.

Pure: yaml and pydantic, no models and no network. `/api` owns it because the
manifest is the contract, not a presentation detail; `mcp_server` and `webui`
are both free to serve it.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

_MANIFEST_PATH = Path(__file__).resolve().parent.parent / "agent.yaml"


class ToolParameter(BaseModel):
    """One parameter of an MCP tool, as declared in the manifest."""

    name: str
    required: bool = False
    description: str = ""
    default: Any = None
    allowed: list[Any] = Field(default_factory=list)


class ToolSpec(BaseModel):
    """One MCP tool: what it is for, what it takes, what comes back."""

    name: str
    description: str = ""
    parameters: list[ToolParameter] = Field(default_factory=list)
    output: dict[str, Any] = Field(default_factory=dict)


class ModelRole(BaseModel):
    """A model role the agent needs, and how capable it has to be."""

    role: str
    tier: str = ""


class AgentManifest(BaseModel):
    """The whole manifest, in the shape a page can render directly."""

    name: str = ""
    version: str = ""
    owner: str = ""
    intents: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    tools: list[ToolSpec] = Field(default_factory=list)
    model_requirements: list[ModelRole] = Field(default_factory=list)


@lru_cache(maxsize=1)
def load_manifest() -> AgentManifest:
    """Parse agent.yaml into a structured manifest.

    Cached: the file does not change while the process runs, and both the
    overview page and any health surface may ask for it.
    """
    with _MANIFEST_PATH.open(encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    routing = raw.get("routing") or {}
    return AgentManifest(
        name=str(raw.get("name", "")),
        version=str(raw.get("version", "")),
        owner=str(raw.get("owner", "")),
        intents=[str(i) for i in routing.get("intents", []) or []],
        keywords=[str(k) for k in routing.get("keywords", []) or []],
        tools=[_tool(entry) for entry in raw.get("tools", []) or []],
        model_requirements=[
            ModelRole(role=str(entry.get("role", "")), tier=str(entry.get("tier", "")))
            for entry in raw.get("model_requirements", []) or []
        ],
    )


def _tool(entry: dict[str, Any]) -> ToolSpec:
    """One `tools:` entry, with its parameter mapping flattened to a list.

    Declaration order is preserved: the manifest lists `generate` first because
    that is the tool a reader should meet first, and re-sorting would lose that.
    """
    return ToolSpec(
        name=str(entry.get("name", "")),
        description=str(entry.get("description", "")),
        parameters=[
            _parameter(name, spec)
            for name, spec in (entry.get("parameters") or {}).items()
        ],
        output=entry.get("output") or {},
    )


def _parameter(name: str, spec: Any) -> ToolParameter:
    """One parameter. A bare string in the manifest is read as its description."""
    if not isinstance(spec, dict):
        return ToolParameter(name=name, description=str(spec or ""))
    return ToolParameter(
        name=name,
        required=bool(spec.get("required", False)),
        description=str(spec.get("description", "")),
        default=spec.get("default"),
        allowed=list(spec.get("allowed") or []),
    )


__all__ = ["AgentManifest", "ModelRole", "ToolParameter", "ToolSpec", "load_manifest"]
