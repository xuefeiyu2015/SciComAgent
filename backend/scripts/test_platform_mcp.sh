#!/usr/bin/env bash
# test_platform_mcp.sh — smoke-test your PLATFORM-HOSTED deployment.
#
# After your deployments.yaml PR merges, this answers "is my agent actually
# live on its public URL?" in one command: it checks the REST API, performs a
# real MCP handshake, and lists the tools Claude would see.
#
# Usage:
#   bash scripts/test_platform_mcp.sh              # URL derived from the repo folder name
#   bash scripts/test_platform_mcp.sh https://my-slug.agents.turingplanet.ai
#
# The default assumes your deployments.yaml slug == your repo name (the usual
# case). Pass the URL explicitly if you picked a different slug.
set -uo pipefail

BASE="${1:-}"
if [ -z "$BASE" ]; then
  slug=$(basename "$(git rev-parse --show-toplevel 2>/dev/null || pwd)")
  BASE="https://${slug}.agents.turingplanet.ai"
fi
BASE="${BASE%/}"
echo "▶ Testing platform deployment at: $BASE"
fail=0

# ── 1. REST surface ─────────────────────────────────────────────────────────
if out=$(curl -fsS --max-time 15 "$BASE/api/health" 2>/dev/null); then
  echo "✅ /api/health → $out"
else
  echo "❌ /api/health unreachable."
  echo "   • Is your repo in the registry's deployments.yaml, and is that PR MERGED?"
  echo "   • First deploy takes a few minutes (build + route); try again shortly."
  echo "   • Custom slug? Re-run with the URL: bash scripts/test_platform_mcp.sh https://<slug>.agents.turingplanet.ai"
  fail=1
fi

# ── 2. MCP handshake (Streamable HTTP) ──────────────────────────────────────
HDRS=$(mktemp); trap 'rm -f "$HDRS"' EXIT
resp=$(curl -fsS --max-time 20 -D "$HDRS" -X POST "$BASE/mcp" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"test_platform_mcp","version":"0.1"}}}' 2>/dev/null)
if echo "$resp" | grep -q '"serverInfo"'; then
  server=$(echo "$resp" | grep -o '"serverInfo":{"name":"[^"]*"' | cut -d'"' -f6)
  echo "✅ /mcp handshake OK — server: ${server:-unknown}"
else
  echo "❌ /mcp handshake failed. Raw response:"
  echo "${resp:-<empty>}" | head -5 | sed 's/^/   /'
  fail=1
fi

# ── 3. Tool listing (what Claude will actually see) ─────────────────────────
SESSION=$(grep -i '^mcp-session-id:' "$HDRS" | tr -d '\r' | awk '{print $2}')
if [ -n "${SESSION:-}" ]; then
  # The session must be initialized before further requests (fire-and-forget).
  curl -fsS --max-time 10 -X POST "$BASE/mcp" \
    -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
    -H "mcp-session-id: $SESSION" \
    -d '{"jsonrpc":"2.0","method":"notifications/initialized"}' >/dev/null 2>&1
  tools=$(curl -fsS --max-time 15 -X POST "$BASE/mcp" \
    -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
    -H "mcp-session-id: $SESSION" \
    -d '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' 2>/dev/null \
    | grep -o '"name":"[^"]*"' | cut -d'"' -f4 | paste -sd ', ' -)
  if [ -n "${tools:-}" ]; then
    echo "✅ tools/list → $tools"
  else
    echo "⚠️  handshake worked but tools/list returned nothing (not fatal — check server logs)"
  fi
fi

# ── verdict ─────────────────────────────────────────────────────────────────
echo
if [ "$fail" -eq 0 ]; then
  echo "🎉 Deployment is LIVE. Connect Claude to it:"
  echo "   claude mcp add --transport http $(basename "$BASE" .agents.turingplanet.ai | sed 's|https://||') $BASE/mcp"
else
  echo "✗ Deployment not (fully) live — see messages above."
fi
exit "$fail"
