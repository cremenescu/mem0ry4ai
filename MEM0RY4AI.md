# mem0ry4ai — agent guide

You have a **persistent memory** of this user's durable, hard-won knowledge — gotchas, decisions,
infrastructure facts, reusable commands, and the user's preferences — kept across sessions in a
markdown+git store. Reach it through these MCP tools:

- **`memory_search(query?, files?, scope?, type?, limit?)`** — hybrid keyword+semantic search. **Call
  this BEFORE answering** anything where past context could matter (a tool/host/decision/convention
  you might have seen before). Recalling beats guessing. `files="hooks/session_start.py"` (or
  `"hooks/"` for a directory) asks *what do I already know about this file?* — worth a call **before
  editing a file you have not touched this session**. Either argument works alone.
- **`memory_get(id, lines?)`** — load one record, its body shown with 1-based line numbers;
  `"<id>:5-9"` returns just those lines.
- **`memory_list(scope?, type?, status?)`** — browse, newest first.
- **`memory_resume(scope?)`** — a "where was I?" briefing for a scope: latest status + open todos +
  recent knowledge. Good at the start of work on a project.
- **`memory_add(type, scope, summary, body, supersedes?)`** — save durable knowledge (secrets are
  auto-redacted; prompt-injection-like phrasing is flagged back to you, since a memory is re-injected
  every session). May be disabled (`MEM_MCP_WRITE=0`).
- **`memory_note`**, **`memory_promote`**, **`session_search`** — scratch notes and past
  transcripts, at the end.

## Reading a memory
A memory records what was true when it was written. It reads just as confident a year later, after
the file was renamed or the flag removed. So if it names a file, function, flag, host or version,
check that it still exists before relying on it — and when one turns out wrong, supersede it rather
than work around it.

There is **no edit and no delete tool** — to revise a record, write the new one with
`supersedes=<old id>`: the old one is retired, never deleted (the store is markdown+git, so nothing is
ever lost).

## When to save (memory_add)
Save only what you'd want to know **next session** — durable and generalizable:
- **gotcha** — a non-obvious trap + cause + fix.
- **decision** — an architecture/direction choice + WHY.
- **fact** — a stable fact (IP, path, port, version, deploy target, name).
- **command** — a reusable command/sequence.
- **preference** — how the user wants you to work (style, conventions, what NOT to do); corrections always.
  Also an approach the user explicitly confirmed when it was not the obvious choice and it worked —
  what went right is as reusable as what went wrong. Not routine steps, and not a polite "ok".
- **procedural / todo / status** — a reusable procedure / next steps / where a project stands.

Do **not** save ephemeral steps ("did X today"), thanks, speculation, or anything true only this session.
Run `memory_search` first — overlapping memories should be merged, not piled up.

**Scope**: `global` if it applies across projects; otherwise `project:<slug>` (the project folder name).

**Write agent-neutrally.** The store is shared across agents (Claude Code, Gemini/Antigravity, Cursor, …):
second person ("you, the agent" / "do X"), never first person tied to one model ("I, Claude…") — a
memory phrased around one assistant misreads to the others. Model-specific tool names or limits are
**examples**, not the rule itself.

## Scratch notes and past transcripts
- **`memory_note(scope, summary, body, type?)`** — a **working** note for a finding you're not yet
  sure is durable: NOT injected, hidden from default search/list. Review with
  `memory_list status="working"`, then **`memory_promote(id)`** the few that earn a durable place.
- **`session_search(query, project?, limit?)`** — the raw messages of earlier sessions, NOT the curated
  store: for "what did we actually discuss/decide weeks ago?" when `memory_search` comes up short.
