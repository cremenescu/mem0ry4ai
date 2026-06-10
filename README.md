# mem0ry4ai

Persistent, local-first memory for coding agents — built for [Claude Code](https://claude.com/claude-code).

**Landing page:** [cremenescu.ro/en/mem0ry4ai](https://cremenescu.ro/en/mem0ry4ai/)

Your agent forgets everything between sessions. mem0ry4ai fixes that: it captures durable
knowledge (gotchas, decisions, facts, commands, preferences, todos, project status), stores it
in **plain markdown versioned by git**, and **injects the relevant slice automatically** at the
start of every session — scoped to the project you are working in.

## Why another memory tool?

We surveyed the landscape first (claude-mem, basic-memory, mem0, Letta/MemGPT, Graphiti,
agentmemory, the official MCP memory server). Recurring failure modes shaped this design:

| Common failure | mem0ry4ai answer |
|---|---|
| Model forgets to call save/recall tools | **Deterministic hooks** inject at SessionStart; the agent is *instructed* to write proactively |
| Vector DB fragility (Chroma/Qdrant = #1 source of real bug reports) | **Markdown + git is the source of truth**; SQLite FTS5 index is *derived and disposable* |
| Memory rots (stale facts, contradictions) | **Supersede, never delete** — old records keep history; git keeps everything |
| Auto-extraction noise (small LLMs are over-confident: ~1.0 confidence on everything — we measured) | **Trust-gated writes**: the in-context agent writes directly; batch LLM extraction goes through a human review queue |
| Tools that vandalize CLAUDE.md / fight native memory | Coexists cleanly — own namespace, never touches your files |
| Nobody remembers "where was I?" on returning to a project | First-class **`todo`** and **`status`** types, pinned in injection and UI |

## Measured impact (author's real setup)

This is not a synthetic benchmark — it is the author's actual monorepo (30 sub-projects), before
and after migrating a monolithic `CLAUDE.md` into mem0ry4ai (217 active memories):

| | Before (one big CLAUDE.md) | After (slim CLAUDE.md + injection) |
|---|---|---|
| Fixed context loaded at **every** session start | 242,956 bytes (1,832 lines) ≈ ~61k tokens | repo root: 29,169 bytes ≈ ~7.3k tokens · sub-project: 19,044 bytes ≈ ~4.8k tokens |
| Reduction | — | **88%** (root) / **92%** (sub-project) |
| Relevance | everything, everywhere (FreeRDP gotchas while editing a weather app) | scoped: global + the current project; `status`/`todo` first |
| SessionStart hook overhead | — | 69 ms |
| Live-update poll (no changes) | — | ~1–4 ms |

At the author's measured pace (34 session starts/day) that is roughly **1.8M tokens/day of
context that no longer gets loaded** — while recall got *better*, because memories are scoped,
ranked-searchable and pinned by relevance instead of buried in a 240KB file.

*Honest caveats: tokens estimated at ~4 chars/token; with prompt caching the billed savings are
smaller than the raw numbers; this is one user's setup, not a controlled study.*

## How it works

```
Claude Code session
  ├─ SessionStart hook ─► injects relevant memories (global + current project;
  │                       from a monorepo root: a capped index of ALL projects)
  ├─ [work] the agent proactively writes durable findings ─► mem.py add
  └─ SessionEnd/PreCompact hook ─► transcript pointer to staging/ + auto-commit of the
                                    store (end-of-session git checkpoint — no manual chore)

store/*.md   ◄── SOURCE OF TRUTH (markdown + git: audit, diff, rollback, supersede)
   ├─► store/.index.db   (FTS5, ranked search — derived, regenerable)
   ├─► web UI            (dashboard, per-project "where was I" page, review queue, live updates)
   └─► mem.py            (CLI: add/list/search/supersede/propose/reindex — stdlib only)
```

## Quick start

Requirements: Python 3.9+, PHP 8+ (for the web UI), git. No other dependencies — no Docker,
no vector database, no API keys.

```bash
git clone https://github.com/cremenescu/mem0ry4ai.git
cd mem0ry4ai

# 1. CLI works immediately
./mem.py add --type gotcha --scope global \
  --summary "openrsync on macOS does not support --chown" \
  --body "Use --rsync-path=\"sudo rsync\" and chown separately over ssh."
./mem.py list
./mem.py search "rsync"          # FTS5 ranked

# 2. Web UI (standalone server, no Apache needed)
./server_web.sh                   # -> http://127.0.0.1:8841/

# 3. Claude Code integration (hooks: inject at start + capture at end)
python3 hooks/install.py --dry-run        # preview what would be written
python3 hooks/install.py --target user    # ~/.claude/settings.json (all projects)
# restart Claude Code (or /clear) to load the hooks
```

The SessionStart hook also auto-starts the web server, so the UI is always up while you work.

### Teach your agent to write memories

Add an instruction like this to your `CLAUDE.md` (this is the behavioral half of the system —
hooks handle recall, the agent handles capture):

> When you discover something durable (a gotcha with a non-obvious cause, an architecture
> decision, an infrastructure fact, a reusable command, a user preference/correction, a change
> of project status), proactively save it without asking:
> `echo "body" | <path>/mem.py add --type <T> --scope <global|project:slug> --summary "..." --source claude:live`
> Check `mem.py search` first to avoid duplicates. Never save ephemeral tasks.

## Memory types

| Type | What it holds |
|---|---|
| `gotcha` | trap + cause + fix ("X breaks because Y, do Z") |
| `decision` | architecture choice + the *why* |
| `fact` | stable infrastructure facts (hosts, paths, ports) |
| `command` | a command you would otherwise look up again |
| `preference` | user style/conventions/corrections |
| `todo` | what remains to be done (supersede when finished) |
| `status` | where the project stands / where you left off |

`todo` + `status` are pinned first in injection and in the per-project web page — they answer
"where was I?" when you return to a project after weeks.

## Web UI

Bilingual (English default, Romanian via the EN/RO switch in the top bar).

![Dashboard — system status, health checks, recent activity, grouped memories](docs/screenshots/dashboard.png)

*Per-project "where was I?" page — status and todos pinned first:*

![Project page](docs/screenshots/project.png)

*The review queue — the over-confident junk candidate (conf 0.95 for "updated the changelog") is exactly why nothing auto-writes:*

![Review queue](docs/screenshots/queue.png)

*"What Claude sees" — the exact SessionStart injection, with its cost:*

![What Claude sees](docs/screenshots/inject.png)

*Git history — the memory timeline with per-commit diffs and commit-from-UI:*

![Git history](docs/screenshots/git-history.png)

*(Screenshots use demo data.)*

- **System dashboard**: counters, health checks (store/staging/index/queue/hooks/git), recent
  activity with source attribution, live updates via cheap polling (4 ms when nothing changed).
- **Per-project page**: status + todos pinned, knowledge grouped by type.
- **Ranked search** (same FTS5 index as the CLI), grouped/sortable/filterable list, bulk
  operations (supersede / re-scope / delete), supersede-chain navigation.
- **"What Claude sees"**: renders the exact SessionStart injection, with its size in bytes/tokens.
- **Review queue**: candidates extracted by the optional local LLM wait here for human approval.
- **Git history**: the store's timeline — commits touching `store/` with colored per-commit
  diffs and a commit-from-the-UI button (store files only).

## Optional: offline extraction with a local LLM

For sessions where the agent could not capture live, `consolidate.py` digests transcripts with a
local model via [Ollama](https://ollama.com) (default `qwen2.5:7b-instruct`) and proposes
candidates into the review queue. Honest finding from our testing: small models are noisy and
over-confident, so **nothing they produce is written without human approval**.

```bash
ollama pull qwen2.5:7b-instruct
python3 consolidate.py --dry-run     # see what it would extract
python3 consolidate.py --write       # queue candidates -> review in the web UI
```

## Configuration

Everything is overridable via environment variables — no config file needed:

| Variable | Default | Purpose |
|---|---|---|
| `MEM_WEB_PORT` | `8841` | web UI port (`server_web.sh`) |
| `MEM_PHP` | `php` from `PATH` | PHP binary (server + conformance test) |
| `MEM_PYTHON` | `python3` from `PATH` | Python binary used by the "What Claude sees" page |
| `OLLAMA_URL` | `http://localhost:11434` | Ollama endpoint for offline extraction |
| `MEM_LLM_MODEL` | `qwen2.5:7b-instruct` | model used by `consolidate.py` |

## Design notes

- **Two parsers, one contract**: the Python engine (`mem.py`) and the PHP web UI read/write the
  same files. `tests/conformance.py` asserts both produce identical output — run it before
  committing parser changes.
- **Concurrency**: atomic writes (tmp + rename), append-mostly files, WAL-free design — the
  store survives multiple sessions because markdown conflicts are rare and git catches the rest.
- **No commit chore**: the SessionEnd hook auto-commits `store/` (authored `mem0ry4ai hook`),
  so every session leaves a git checkpoint behind; the git page's button is for mid-session
  checkpoints with a custom message.
- **Your data is yours**: if you fork this repo, do not commit your personal `store/` upstream.
  The store is meant to be versioned in *your* clone, locally.

## License

GPL-2.0-or-later — see [LICENSE](LICENSE). We are giving back to the community.
