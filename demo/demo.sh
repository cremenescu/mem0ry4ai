#!/usr/bin/env bash
# The scripted demo asciinema records for the README GIF.
# Runs against the throwaway store from seed-demo-store.sh — never a real one.
#   ./demo/seed-demo-store.sh /tmp/mem0ry4ai-demo
#   asciinema rec --headless --window-size 110x30 --overwrite --command "bash demo/demo.sh" demo/demo.cast
#   agg --font-size 18 --theme asciinema demo/demo.cast demo/demo.gif
# 110 columns keeps the longest output line unwrapped; 30 rows fit the whole demo without scrolling.
set -u
REPO="$(cd "$(dirname "$0")/.." && pwd)"
export MEM_DATA_DIR="${MEM_DATA_DIR:-/tmp/mem0ry4ai-demo}"
cd "$REPO"

# so the typed text reads exactly like what a user would run
mem.py() { python3 "$REPO/mem.py" "$@"; }

GREEN=$'\033[1;32m'; DIM=$'\033[2m'; OFF=$'\033[0m'

type_out() {                      # simulate a human typing the command
  printf '%s$%s ' "$GREEN" "$OFF"
  local s="$1" i
  for ((i = 0; i < ${#s}; i++)); do printf '%s' "${s:i:1}"; sleep 0.020; done
  printf '\n'
}
say()  { printf '%s# %s%s\n' "$DIM" "$1" "$OFF"; sleep 0.9; }
run()  { type_out "$1"; sleep 0.30; eval "$1"; }

sleep 0.8
say "your agent saves a lesson the moment it learns it"
run "mem.py add --type gotcha --scope project:api --summary 'N+1 query on the orders list' --body 'The serializer loaded customer per row: 200 orders = 201 queries. Use select_related.'"
sleep 1.6

printf '\n'
say "a later session asks in plain words — and gets the earlier lesson back"
run "mem.py search 'why were requests timing out' --type gotcha"
sleep 2.8

printf '\n'
say "and picks the project up exactly where it stopped"
run "mem.py resume --scope project:api"
sleep 4.0
