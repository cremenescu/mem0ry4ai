#!/usr/bin/env bash
# Build a throwaway demo store for the README GIF. Generic sample memories only —
# never record a real store, it would leak whatever you actually saved.
# Usage: ./demo/seed-demo-store.sh /tmp/mem0ry4ai-demo
set -euo pipefail
STORE="${1:-/tmp/mem0ry4ai-demo}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"
rm -rf "$STORE"; mkdir -p "$STORE"
export MEM_DATA_DIR="$STORE"
git init -q "$STORE"
git -C "$STORE" config user.email demo@example.com
git -C "$STORE" config user.name  demo
git -C "$STORE" config commit.gpgsign false

add() { echo "$4" | python3 "$HERE/mem.py" add --type "$1" --scope "$2" --summary "$3" --source demo >/dev/null; }

add gotcha  project:api "connection pool exhausted under load" \
  "The pool defaulted to 5 connections, so bursts queued and timed out. Set pool_size to 20 and add a 2s acquire timeout so a stuck query fails fast instead of stalling every request."
add decision project:api "chose Redis over an in-process cache" \
  "An in-process cache drifts between the API replicas after a deploy. Redis keeps one shared view, and the extra hop costs ~1ms — worth it for correctness."
add fact    project:api "staging runs Postgres 16, prod still on 15" \
  "Migrations that use MERGE work on staging and fail on prod. Target 15 syntax until the prod upgrade lands."
add command global      "rebuild the search index after a bulk import" \
  "Run mem.py reindex — the FTS5 index is derived from the markdown, so it is always safe to regenerate."
add preference global   "no emoji in code or CLI output" \
  "Keep code comments and terminal output plain text."
add status  project:api "auth rewrite done; rate limiting is next" \
  "Token refresh and the session store shipped last week. Rate limiting per API key is the next piece."
add todo    project:api "add a 429 retry-after header" \
  "Clients currently retry immediately and amplify the spike."

# Optional: build semantic vectors so the demo shows hybrid search. Skipped silently
# when no local Ollama is running — the demo still works on keyword search alone.
python3 "$HERE/mem.py" embed >/dev/null 2>&1 || true

git -C "$STORE" add -A >/dev/null
git -C "$STORE" commit -qm "demo store" >/dev/null
echo "demo store ready: $STORE"
