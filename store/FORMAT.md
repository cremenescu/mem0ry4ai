# mem0ry4ai storage format

The source of truth is plain markdown under `store/`. Each **memory** is a block delimited
by HTML comments (machine-parseable, invisible when the markdown is rendered).

## Files
- `store/global.md` — cross-project memories (preferences, infrastructure facts, global gotchas).
- `store/projects/<slug>.md` — memories scoped to one project (`<slug>` = the project folder name).

## Record structure

```
<!-- mem:start id=20260610-a1b2c3 -->
### gotcha · my-project · Apache strips the Authorization header under FastCGI
- type: gotcha
- scope: project:my-project
- created: 2026-06-10 14:30:00
- updated: 2026-06-10 14:30:00
- status: active
- confidence: 0.9
- source: claude:live

PHP never sees $_SERVER['HTTP_AUTHORIZATION'] under default FastCGI config. Fall back to
REDIRECT_HTTP_AUTHORIZATION, then getallheaders(). Without this every authenticated request fails.
<!-- mem:end -->
```

### Fields
- `id` (in the start comment) — `YYYYMMDD-<hex6>`, generated at creation, immutable.
- title `### {type} · {scope-label} · {summary}` — for humans; machines read the meta lines below it.
- `type` ∈ `gotcha` | `fact` | `decision` | `command` | `procedural` | `preference` | `todo` | `status`
  (`procedural` = a reusable multi-step workflow/runbook, distinct from a single `command`)
- `scope` = `global` or `project:<slug>`
- `created` / `updated` — `YYYY-MM-DD HH:MM:SS`
- `status` ∈ `active` | `superseded`
- `priority: critical` — optional; a critical action rule: ALWAYS injected, first, with its body,
  regardless of the injection budget (`mem.py pin <id>` / `unpin`, or `add --critical`)
- `related-to: <id>, <id>` — optional; links to related memories (a gotcha ↔ the decision that
  caused it, a status ↔ its todos). Shown both ways in the web UI. (`mem.py link <id> <other>...`)
- `blocked-by: <id>, <id>` — optional, on `todo`s; work that must be done first. `mem.py ready`
  lists todos with no OPEN blocker (a blocker is open while it is still an active todo).
- `files: a.py, b.php` — optional; file paths this memory relates to (`add --files`), made
  searchable and shown as chips in the web UI
- `superseded-by: <id>` — present only when `status: superseded` (records are never deleted —
  history is preserved, plus git)
- `invalidated: <ts>` + `invalid-reason: <text>` — written on supersede (bi-temporal): WHEN it
  stopped being valid / when you learned, and WHY. `created` is the valid-from. (`supersede --reason`)
- `confidence` — `0.0`..`1.0` (manual = `1.0`; LLM-extracted candidates carry the model's score)
- `source` — `manual` | `claude:live` | `llm:session:<id>` | `web` | ...
- body = free markdown after the first blank line, up to `<!-- mem:end -->`

## Rules
1. **Append-mostly**: new records are appended to the end of their scope file.
2. **Supersede, never delete**: when a fact stops being true, mark the old record
   `status: superseded` + `superseded-by`, and add a new one. Git keeps everything.
3. **The index is derived**: `store/.index.db` (SQLite FTS5) is rebuilt from these `.md` files.
   Never treat the DB as the source of truth — `rm` it and it regenerates.
4. Manual editing is allowed and encouraged — it is just markdown. Run `mem.py reindex` after
   (or let the next search rebuild the index automatically).
