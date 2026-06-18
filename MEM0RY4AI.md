# mem0ry4ai — agent guide

You have a **persistent memory** of this user's durable, hard-won knowledge — gotchas, decisions,
infrastructure facts, reusable commands, and the user's preferences — kept across sessions in a
markdown+git store. Reach it through these MCP tools:

- **`memory_search(query, scope?, type?, limit?)`** — hybrid keyword+semantic search. **Call this
  BEFORE answering** anything where past context could matter (a tool/host/decision/convention you
  might have seen before). Recalling beats guessing.
- **`memory_get(id, lines?)`** — load one record; its body is shown with 1-based line numbers. Cite a
  precise line with a **fragment ref**: `memory_get("<id>:5")` or `"<id>:5-9"` returns just those lines.
- **`memory_list(scope?, type?, status?)`** — browse, newest first.
- **`memory_resume(scope?)`** — a "where was I?" briefing for a scope: latest status + open todos +
  recent knowledge. Good at the start of work on a project.
- **`memory_add(type, scope, summary, body)`** — save durable knowledge (secrets are auto-redacted).
  May be disabled (`MEM_MCP_WRITE=0`).
- **`memory_note(scope, summary, body, type?)`** — jot a **working** (scratch) note: NOT injected, hidden
  from default search/list, so it won't pollute recall. For findings you're not yet sure are durable.
- **`memory_promote(id)`** — promote a working note to a durable memory (working → active).

There is **no edit and no delete tool** — to revise a record's content, *supersede* the old one and *add*
a new one (`mem.py supersede <id> --by <new>`, or the web UI `/memories`). The store is markdown+git, so
nothing is ever lost. (`memory_promote` only flips a working note's status; it does not edit content.)

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

## Working memory (scratch)
For in-progress, still-uncertain findings during a task, use `memory_note` instead of `memory_add` —
working notes stay out of recall and out of session-start injection, so the durable store stays clean
while you think out loud. Review them with `memory_list status="working"`, then `memory_promote` the few
that earn a durable place (leave or supersede the rest).

## Scope
`global` if it applies across projects; otherwise `project:<slug>` (the project folder name).

## Multi-agent: write agent-neutral
This store is shared across agents (Claude Code, Gemini/Antigravity, Cursor, …) — they all read and
write the same records. So:
- Write every memory **agent-neutrally**: second person ("you, the agent" / "do X"), never first-person
  tied to one model ("I, Claude…"). A memory phrased around one assistant misreads to the others.
- Keep model-specific tool names or limits as **examples**, not as the rule itself.
- Before `memory_add`, run `memory_search` first to avoid duplicate records.
