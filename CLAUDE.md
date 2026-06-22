# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

# SciComm Agent · Project Brief (always follow)

## What this is

An agent that turns a research paper into public-facing content for [news / wechat / xhs],
with source provenance and overstatement flags. It plugs into the Turing Planet MCP platform.

## Directory contract

- `/api`      single source of truth for business logic (the whole pipeline)
- `/mcp`      a THIN wrapper over `/api` exposing MCP tools; NO business logic
- `agent.yaml`  manifest: name/version/owner, tools, routing intents, model_requirements, config_schema, health
- `/config`   config.example.yaml; models/keys configurable; NEVER hardcode real tokens
- `/tests`    tests + faithfulness regression
- `Dockerfile` + deploy manifests

## Pipeline (4 steps)

fetch+extract  ->  claim ledger  ->  per-platform draft  ->  faithfulness check

## Hard rules (faithfulness first)

1. Any number/causation/magnitude/"first"/"proves" statement in a draft MUST map to a source in the claim ledger, or it may not be written.
2. Every claim must keep its qualifier (species, sample, "preliminary", correlation-not-causation).
3. Drafting and checking use DIFFERENT models and DIFFERENT prompts. No grading your own work.
4. NEVER auto-publish. Always return draft + provenance + overstatement flags for a human.

## Engineering conventions

- Python. Declare models by ROLE (extractor=cheap, reviewer=strong); read names from config, never hardcode.
- Keep `/api` and `/mcp` strictly separate; `/api` must not import `/mcp`.
- Style differs by platform (structure), controlled by `api/styles/*.md`.
- Language / audience / liveliness / variety are PARAMETERS, not separate files.

## How to collaborate

- ONE step at a time; let me confirm before continuing.
- Remind me to git commit after each working step.
