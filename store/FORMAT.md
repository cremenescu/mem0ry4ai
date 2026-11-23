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
- `created` / `updated` — `YYYY-MM-DD HH:MM:SS`. `updated` is also the age the session-start list
  reads for a `status` record: one not touched for `MEM_INJECT_STATUS_DAYS` (60) is no longer
  listed there, though it stays active and searchable. `mem.py stale` lists those, and
  `mem.py stale --keep <id>` moves `updated` to now and changes nothing else.
- `status` ∈ `active` | `superseded` | `working` (`working` = a scratch note or a draft: not
  injected, hidden from default search/list until promoted — `add --working`, `mem.py promote <id>`)
- `priority: critical` — optional; a critical action rule: ALWAYS injected, first, with its body,
  regardless of the injection budget (`mem.py pin <id>` / `unpin`, or `add --critical`)
- `mirrored-in: <file>` — optional, on a critical rule whose full text ALSO lives in an instruction file
  the agent always loads (e.g. `CLAUDE.md`). The SessionStart hook then names the rule instead of
  repeating its body; MCP clients, which do not load that file, still get it in full.
  (`mem.py mirror <id> CLAUDE.md`, `mem.py mirror <id> -` to clear)
- `related-to: <id>, <id>` — optional; links to related memories (a gotcha ↔ the decision that
  caused it, a status ↔ its todos). Shown both ways in the web UI. (`mem.py link <id> <other>...`)
- `blocked-by: <id>, <id>` — optional, on `todo`s; work that must be done first. `mem.py ready`
  lists todos with no OPEN blocker (a blocker is open while it is still an active todo).
- `distinct-from: <id>, <id>` — optional; memories that read alike but were judged NOT to be
  duplicates of this one (the Duplicates page's "Not duplicates", or `mem.py dups --distinct`).
  Read both ways, and through a later revision of either memory; such a pair is not offered as
  duplicates again. Writing it leaves `updated` alone.
- `files: a.py, b.php` — optional; file paths this memory relates to (`add --files`), made
  searchable and shown as chips in the web UI. The first time a session touches one of them, the
  memory is put next to that tool call's result (`MEM_FILE_RECALL`).
- `when: <moment>` — optional; makes the record a **digest**: a page of preferences distilled by
  theme, given whole at its moment instead of being listed as one more title. Clauses are
  separated by `;`: `always` (every session start) · `paths=<glob>, <glob>` (the first time a
  session touches a matching file; the glob is matched against the path from the repo root, so
  `*.css` and `*/design/*` both work) · `commands=<regex>` (the first time a shell command
  matches; a literal `;` is written `\x3b`). Given only while `status: active`: a draft stays
  `working` until the user promotes it. (`add --when`, `mem.py digest <id> --when ...`)
- `digest-of: <id>, <id>` — optional, on a digest: the memories it distils, which stay the source.
  `mem.py digest` reports the ones retired or rewritten since the digest was written, and the
  preferences no digest cites. (`add --digest-of`, `mem.py digest <id> --of ...`)
- `superseded-by: <id>` — present only when `status: superseded` (records are never deleted —
  history is preserved, plus git)
- `invalidated: <ts>` + `invalid-reason: <text>` — written on supersede (bi-temporal): WHEN it
  stopped being valid / when you learned, and WHY. `created` is the valid-from. (`supersede --reason`)
  A copy retired in favour of the memory that says the same thing carries
  `invalid-reason: duplicate of <id>` (`mem.py dups --keep <id> --retire <id>`). Such a
  retirement can be taken back (`mem.py dups --undo <id>`, or Undo on the Duplicates page): the
  copy is `active` again and these two lines go, with `superseded-by`.
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
