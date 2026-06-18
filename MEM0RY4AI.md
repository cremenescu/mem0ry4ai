# mem0ry4ai — agent guide

You have a **persistent memory** of this user's durable, hard-won knowledge — gotchas, decisions,
infrastructure facts, reusable commands, and the user's preferences — kept across sessions in a
markdown+git store. Reach it through these MCP tools:

- **`memory_search(query, scope?, type?, limit?)`** — hybrid keyword+semantic search. **Call this
  BEFORE answering** anything where past context could matter (a tool/host/decision/convention you
  might have seen before). Recalling beats guessing.
- **`memory_get(id)`** — load one record in full by its id.
- **`memory_list(scope?, type?, status?)`** — browse, newest first.
- **`memory_resume(scope?)`** — a "where was I?" briefing for a scope: latest status + open todos +
  recent knowledge. Good at the start of work on a project.
- **`memory_add(type, scope, summary, body)`** — save durable knowledge (secrets are auto-redacted).
  May be disabled (`MEM_MCP_WRITE=0`).

These five are the **only** memory tools exposed over MCP. There is **no update and no delete tool** — to
revise a record, *supersede* the old one and *add* a new one (`mem.py supersede <id> --by <new>` on the
CLI, or the web UI `/memories` page). The store is markdown+git, so nothing is ever lost.

## When to save (memory_add)
Save only what you'd want to know **next session** — durable and generalizable:
- **gotcha** — a non-obvious trap + cause + fix.
- **decision** — an architecture/direction choice + WHY.
- **fact** — a stable fact (IP, path, port, version, deploy target, name).
- **command** — a reusable command/sequence.
- **preference** — how the user wants you to work (style, conventions, what NOT to do); corrections always.
- **procedural / todo / status** — a reusable procedure / next steps / where a project stands.

Do **not** save ephemeral steps ("did X today"), thanks, speculation, or anything true only this session.
Search first to avoid duplicates — overlapping memories should be merged, not piled up.

## Scope
`global` if it applies across projects; otherwise `project:<slug>` (the project folder name).

## Multi-agent: write agent-neutral
This store is shared across agents (Claude Code, Gemini/Antigravity, Cursor, …) — they all read and
write the same records. So:
- Write every memory **agent-neutrally**: second person ("you, the agent" / "do X"), never first-person
  tied to one model ("I, Claude…"). A memory phrased around one assistant misreads to the others.
- Keep model-specific tool names or limits as **examples**, not as the rule itself.
- Before `memory_add`, run `memory_search` first to avoid duplicate records.
