#!/usr/bin/env bash
# Teardown for a fleet member repo — the mirror of joining.
#
#   bash scripts/teardown.sh                # interactive (two questions)
#   bash scripts/teardown.sh --unregister   # leave the fleet, keep the repo
#   bash scripts/teardown.sh --delete-repo  # leave the fleet AND delete the repo
#   add --yes to skip confirmation prompts (automation)
#
# What "leave the fleet" does: flips fleet.register to false in your manifest
# (that flip IS your consent — the platform verifies it), pushes, then asks the
# platform to open a registry PR removing your members.yaml entry AND any
# platform-hosting entry. An admin merges it; merging also tears down hosting.
# Nothing is removed until that merge. Your repo and code are untouched.
#
# What "--delete-repo" adds: after unregistering, deletes the GitHub repo
# (needs the delete_repo scope: gh auth refresh -h github.com -s delete_repo).
# IRREVERSIBLE. Your local folder is never touched.
set -euo pipefail

REGISTRAR="https://fleet-services.agents.turingplanet.ai/api/deregister"
REPO=$(gh repo view --json nameWithOwner -q .nameWithOwner 2>/dev/null || true)
[ -n "$REPO" ] || { echo "✗ can't determine the GitHub repo — this folder has no GitHub remote (or gh isn't authed). Nothing to tear down on the platform side."; exit 1; }

DELETE=n; UNREG=n; YES=n
for a in "$@"; do case "$a" in
  --delete-repo) DELETE=y; UNREG=y;;
  --unregister)  UNREG=y;;
  --yes)         YES=y;;
  *) echo "unknown flag: $a"; exit 1;;
esac; done

ask() { # ask "question" -> yes/no in $REPLY_YN
  if [ "$YES" = y ]; then REPLY_YN=y; return; fi
  printf '%s [y/N] ' "$1"; read -r r; case "$r" in y|Y|yes) REPLY_YN=y;; *) REPLY_YN=n;; esac
}

if [ "$DELETE" = n ] && [ "$UNREG" = n ]; then
  ask "1/2 · Delete $REPO from GitHub ENTIRELY (code, issues, PRs — irreversible)?"
  DELETE=$REPLY_YN
  if [ "$DELETE" = y ]; then
    UNREG=y
    echo "    → deleting implies leaving the fleet first."
  else
    ask "2/2 · Leave the fleet (removes membership, /review access, platform hosting)?"
    UNREG=$REPLY_YN
  fi
fi

[ "$UNREG" = y ] || { echo "Nothing to do."; exit 0; }

# --- leave the fleet ---------------------------------------------------------
# The platform verifies your consent flip on the DEFAULT branch — pushed from a
# feature branch it's invisible, the platform refuses, and nothing is removed.
DEF=$(gh repo view --json defaultBranchRef -q .defaultBranchRef.name 2>/dev/null || echo main)
CUR=$(git branch --show-current)
if [ "$CUR" != "$DEF" ]; then
  echo "✗ you're on branch '$CUR', but the platform reads consent from '$DEF'."
  echo "  Switch first, then rerun:  git switch $DEF && git pull"
  exit 1
fi
if [ -f agent.manifest.yaml ] && grep -q '^  register: true' agent.manifest.yaml; then
  echo "• recording leave-intent: fleet.register → false (the platform verifies this)"
  sed -i.bak 's/^  register: true/  register: false/' agent.manifest.yaml && rm -f agent.manifest.yaml.bak
  git add agent.manifest.yaml
  git commit -q -m "fleet: leave — set fleet.register: false" || true
  git push -q
fi

echo "• asking the platform to open the removal PR…"
resp=$(curl -s --max-time 30 -X POST "$REGISTRAR" -H 'Content-Type: application/json' \
  -d "{\"repo\":\"$REPO\"}" || echo '{"status":"unreachable"}')
echo "  registrar says: $resp"
case "$resp" in
  *pr_opened*)   echo "  ✓ removal PR opened — an admin merges it; hosting (if any) tears down on merge." ;;
  *pr_pending*)  echo "  ✓ a removal PR is already awaiting the admin." ;;
  *not_registered*) echo "  ✓ this repo isn't on the fleet roster — nothing to remove." ;;
  *registration_pending*) echo "  → you were never admitted; ask the admin to CLOSE the pending registration PR." ;;
  *still_consented*) echo "  ✗ manifest still says register: true — commit+push the flip, then rerun." ;;
  *) echo "  ✗ unexpected — will also retry automatically on your next push if the manifest stays false." ;;
esac

# --- delete the repo ---------------------------------------------------------
if [ "$DELETE" = y ]; then
  # Deleting implies leaving FIRST — and once the repo is gone you can no longer
  # push the consent flip, so never delete over a failed leave.
  case "$resp" in
    *pr_opened*|*pr_pending*|*not_registered*|*registration_pending*) ;;
    *)
      echo "✗ NOT deleting the repo: leaving the fleet didn't go through (see above)."
      echo "  Fix that, rerun, and deletion will proceed."
      exit 1 ;;
  esac
  echo "• deleting $REPO from GitHub…"
  if gh repo delete "$REPO" --yes; then
    echo "  ✓ deleted. Local folder left in place — remove it yourself if wanted:"
    echo "    rm -rf $(pwd)"
  else
    echo "  ✗ delete failed — likely missing scope. Grant it once, then rerun:"
    echo "    gh auth refresh -h github.com -s delete_repo"
    exit 1
  fi
fi
echo "Done."
