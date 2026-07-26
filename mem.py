#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""mem0ry4ai — memory CLI (markdown + git as the source of truth, stdlib-only).

Commands:
  mem.py add --type gotcha --scope project:my-app --summary "..." [--body "..." | stdin]
  mem.py list [--scope global|project:<slug>] [--type ...] [--status active|superseded|all] [--json]
  mem.py search "query" [--since 2026-05-01] [--until ...]   # FTS5 ranked (bm25); substring fallback
  mem.py supersede <id> [--by <new-id>]
  mem.py propose ...               # queue a candidate for human review (NOT written to the store)
  mem.py audit                     # report secret-like patterns in the store (never modifies)
  mem.py reindex                   # rebuild the derived FTS5 index from markdown

Storage format: see store/FORMAT.md. No external dependencies.
"""
import argparse
import contextlib
import datetime
import functools
import hashlib
import math
import os
import re
import shutil
import sqlite3
import struct
import subprocess
import sys
import threading
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)


def _local_env_path():
    """Where per-machine settings live. Follows MEM_DATA_DIR when it is set, and only falls back to
    the code directory otherwise.

    It used to be pinned next to the code unconditionally, which made "point MEM_DATA_DIR at a copy
    so you can test safely" a lie: the store was isolated but the settings were not, so any test
    that exercised the settings page rewrote the real config. That happened here — an audit run
    against throwaway stores silently clamped three live settings to its test values and dropped a
    fourth entirely, and it went unnoticed until a dashboard light turned red."""
    d = os.environ.get("MEM_DATA_DIR")
    if d:
        return os.path.join(os.path.abspath(os.path.expanduser(d)), ".mem-local.env")
    return os.path.join(ROOT, ".mem-local.env")


LOCAL_ENV = _local_env_path()


def load_local_env():
    """Ingest .mem-local.env (KEY=VALUE lines, next to the code) into os.environ via setdefault.

    The web UI persists power-user settings here; loading it at import means a setting changed in the
    UI is honored EVERYWHERE that imports mem — the CLI and the MCP server — not just the web server.
    setdefault => a real shell export still wins. Gitignored, never synced. Runs before _data_dir() so
    a MEM_DATA_DIR set in the file is respected too."""
    try:
        with open(LOCAL_ENV, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("export "):
                    line = line[7:].strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except OSError:
        pass


load_local_env()
import redact  # noqa: E402

# Suppress the console window a child process (rg) would pop on Windows when this module
# runs under a detached, console-less server. The flag only exists on Windows.
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def _data_dir():
    """Where store/ and staging/ live. Default = next to the code (git-clone install).

    MEM_DATA_DIR overrides. When the code runs from a Claude Code plugin install,
    data defaults to ~/.mem0ry4ai instead — plugin updates replace the plugin dir,
    and memories must survive that.
    """
    d = os.environ.get("MEM_DATA_DIR")
    if d:
        return os.path.abspath(os.path.expanduser(d))
    if f"{os.sep}.claude{os.sep}plugins{os.sep}" in ROOT + os.sep:
        return os.path.join(os.path.expanduser("~"), ".mem0ry4ai")
    return ROOT


DATA = _data_dir()
STORE = os.path.join(DATA, "store")
GLOBAL_FILE = os.path.join(STORE, "global.md")
PROJ_DIR = os.path.join(STORE, "projects")

TYPES = ["gotcha", "fact", "decision", "command", "procedural", "preference", "todo", "status", "profile"]


def access_db_path():
    return os.path.join(STORE, ".access.db")


def log_access(rec_id, action):
    """Log an access event (inject, get, search) for a record ID to .access.db."""
    path = access_db_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        con = sqlite3.connect(path, timeout=5.0)
        try:
            con.execute("CREATE TABLE IF NOT EXISTS access (id TEXT, action TEXT, ts TEXT)")
            con.execute("INSERT INTO access (id, action, ts) VALUES (?, ?, ?)", (rec_id, action, now_ts()))
            con.commit()
        finally:
            con.close()
    except Exception:
        pass


def get_last_accessed():
    """Return a dict of {rec_id: last_accessed_timestamp_str}."""
    path = access_db_path()
    if not os.path.exists(path):
        return {}
    try:
        con = sqlite3.connect(path, timeout=5.0)
        try:
            rows = con.execute("SELECT id, MAX(ts) FROM access GROUP BY id").fetchall()
        finally:
            con.close()
        return {row[0]: row[1] for row in rows}
    except Exception:
        return {}

START_RE = re.compile(r"^<!-- mem:start id=(?P<id>[0-9a-z-]+) -->\s*$")
END_MARK = "<!-- mem:end -->"
META_RE = re.compile(r"^- (?P<k>[a-z-]+):\s*(?P<v>.*)$")
TITLE_RE = re.compile(r"^###\s+(?P<t>.+)$")


def now_ts():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def gen_id():
    stamp = datetime.date.today().strftime("%Y%m%d")
    h = hashlib.sha1(os.urandom(8)).hexdigest()[:6]
    return f"{stamp}-{h}"


def session_marker_path():
    return os.path.join(DATA, "staging", ".session")


def current_session():
    """Best-effort current session id, to stamp `session:` provenance on writes (which conversation
    produced a memory). MEM_SESSION_ID wins; else the marker the SessionStart hook writes, but only if
    recent — a stale marker from a session that ended long ago must not claim new writes. None if unknown."""
    import json
    sid = (os.environ.get("MEM_SESSION_ID") or "").strip()
    if sid:
        return sid
    try:
        with open(session_marker_path(), encoding="utf-8") as f:
            m = json.load(f)
        ts = (m.get("ts") or "")[:19]
        if ts:
            age = (datetime.datetime.now() - datetime.datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")).total_seconds()
            if age > 16 * 3600:   # marker older than 16h -> the session is almost certainly over
                return None
        return (m.get("session_id") or "").strip() or None
    except Exception:
        return None


def scope_file(scope):
    """Map a scope to its storage file. Raises ValueError on a bad/unknown scope — NEVER sys.exit,
    so a bad scope from the long-lived MCP server (or the web UI) can't kill the process."""
    if scope == "global":
        return GLOBAL_FILE
    if scope.startswith("project:"):
        slug = scope.split(":", 1)[1].strip()
        if not slug or "/" in slug or "\\" in slug or ".." in slug or os.path.isabs(slug):
            raise ValueError(f"invalid scope: {scope}")
        return os.path.join(PROJ_DIR, f"{slug}.md")
    raise ValueError(f"unknown scope: {scope} (use 'global' or 'project:<slug>')")


def scope_label(scope):
    return "global" if scope == "global" else scope.split(":", 1)[1]


def ensure_header(path, scope):
    """Create the scope file with a header if it does not exist yet."""
    if os.path.exists(path):
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    title = "Global memories" if scope == "global" else f"Memories — {scope_label(scope)}"
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# {title}\n\n_mem0ry4ai store. Format: see `store/FORMAT.md`._\n\n")


def store_files():
    files = []
    if os.path.exists(GLOBAL_FILE):
        files.append(GLOBAL_FILE)
    if os.path.isdir(PROJ_DIR):
        for name in sorted(os.listdir(PROJ_DIR)):
            if name.endswith(".md"):
                files.append(os.path.join(PROJ_DIR, name))
    return files


def parse_file(path):
    """Return records as dicts: id, meta{}, title, body, start/end line indices."""
    with open(path, "r", encoding="utf-8") as f:
        lines = f.readlines()
    records, i, n = [], 0, len(lines)
    while i < n:
        m = START_RE.match(lines[i].rstrip("\n"))
        if not m:
            i += 1
            continue
        rec = {"id": m.group("id"), "meta": {}, "title": "", "body": "",
               "start": i, "end": None, "file": path}
        j, body_lines, seen_blank = i + 1, [], False
        while j < n and lines[j].rstrip("\n") != END_MARK:
            line = lines[j].rstrip("\n")
            tm = TITLE_RE.match(line)
            mm = META_RE.match(line)
            if tm and not rec["title"]:
                rec["title"] = tm.group("t").strip()
            elif mm and not seen_blank:
                rec["meta"][mm.group("k")] = mm.group("v").strip()
            elif line == "" and not seen_blank and rec["meta"]:
                seen_blank = True
            elif seen_blank:
                body_lines.append(line)
            j += 1
        rec["end"] = j
        rec["body"] = "\n".join(body_lines).strip()
        records.append(rec)
        i = j + 1
    return records


def all_records():
    out = []
    for path in store_files():
        out.extend(parse_file(path))
    return out


def _sanitize_summary(summary):
    """A summary is a single header-line field (`### … · … · <summary>`). A line break in it would push
    the trailing text down into the record's meta block on re-parse — the self-escalation vector (e.g. a
    summary of "x\n- priority: critical\n- protected: true"). Fold every line break to a space so a
    caller-supplied summary can never synthesize meta lines."""
    return (summary or "").replace("\r\n", " ").replace("\r", " ").replace("\n", " ").strip()


def _neutralize_body(body):
    """Neutralize the record DELIMITERS in caller-supplied body text so it can never forge a new record.
    Only the two structural markers are dangerous: a body line that matches START_RE or equals END_MARK
    lets the parser end this record early and begin an attacker-controlled one (with a forged
    `priority: critical` / `protected: true` meta) that SessionStart then injects into every session.
    Legit '###'/'- key:' body lines are inert — parse_file only reads meta before the meta/body blank
    split — so they are left untouched. A single backslash breaks the exact match while keeping the line
    human-readable; an already-neutralized line no longer matches, so re-rendering (edit / re-scope /
    consolidation merge) is idempotent. Line splitting mirrors what parse_file sees after the file is read
    back with universal newlines (\\r\\n and \\r become \\n)."""
    out = []
    for line in body.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        out.append("\\" + line if (START_RE.match(line) or line == END_MARK) else line)
    return "\n".join(out)


def render_record(rid, rtype, scope, summary, body, confidence, source, created, updated, status,
                  priority=None, files=None, protected=None, session=None, tier=None):
    summary = _sanitize_summary(summary)
    body = _neutralize_body(body)
    lines = [
        f"<!-- mem:start id={rid} -->",
        f"### {rtype} · {scope_label(scope)} · {summary}",
        f"- type: {rtype}",
        f"- scope: {scope}",
        f"- created: {created}",
        f"- updated: {updated}",
        f"- status: {status}",
    ]
    if priority:
        lines.append(f"- priority: {priority}")
    if files:
        lines.append(f"- files: {files}")
    if protected:
        lines.append(f"- protected: {protected}")
    if tier and tier != "open":
        lines.append(f"- tier: {tier}")
    if session:
        lines.append(f"- session: {session}")
    lines += [
        f"- confidence: {confidence}",
        f"- source: {source}",
        "",
        body.strip(),
        END_MARK,
    ]
    return "\n".join(lines) + "\n"


# ----- egress: what a memory is allowed to say, and to whom -----
# The tier model (open / redacted / private, resolved at one choke point) is taken from mnema
# by MerlijnW70 — https://github.com/MerlijnW70/mnema (MIT OR Apache-2.0), where it guards a local
# model against a cloud one. Adapted here: every agent context we feed is remote, so the
# destination collapses to "the user" vs "a model".
# Redaction strips things that LOOK like secrets (regex, keyword-driven). It cannot know that a
# client's name, a salary or a home address must not be handed to a model — nothing about those
# strings is suspicious. A tier is the user's own lever for that, and unlike a pattern it never
# has false negatives: it is stated, not inferred.
#
# The single choke point below is the point of the design. Every surface that puts memory into a
# model's context -- the SessionStart injection, MCP tools, MCP resources and prompts -- resolves
# what to emit HERE, so "a private memory never reaches an agent" is a property of one function
# that can be read in full, not a convention repeated across four files and eventually forgotten.

MAX_BODY = 65536        # one record's body, on every write path

TIERS = ("open", "redacted", "private")

# Where the text is going. LOCAL is the user: the CLI in their terminal, the web UI on their
# machine, `mem.py get`. AGENT is any model context -- for us that is always a remote model, so
# there is no third case to reason about.
DEST_LOCAL, DEST_AGENT = "local", "agent"


def record_tier(rec):
    t = (rec["meta"].get("tier") or "open").strip().lower()
    return t if t in TIERS else "open"


def emit_for(rec, dest=DEST_LOCAL):
    """What this record may show at `dest`: (summary, body) with body None when withheld,
    or (None, None) when the record must not appear at all.

    open      -> everything, everywhere.
    redacted  -> the summary travels, the body stays on this machine. For a memory whose gist an
                 agent needs ("the X credentials live in Y") but whose detail it does not.
    private   -> never leaves for a model. Still fully searchable and readable by the user.
    """
    tier = record_tier(rec)
    if dest != DEST_AGENT or tier == "open":
        return record_summary(rec), rec.get("body", "")
    if tier == "redacted":
        return record_summary(rec), None
    return None, None


def visible_for(records, dest=DEST_LOCAL):
    """The records that may appear at `dest` at all (drops `private` for an agent)."""
    return [r for r in records if emit_for(r, dest)[0] is not None]


# ----- write layer (shared by the CLI and the web UI) -----

def render_from_meta(rid, meta, summary, body):
    """Full record block from a parsed meta dict + summary + body, preserving EVERY meta field
    (web edit/re-scope). The parser is order-independent, so field order here is just canonical.
    `updated` is bumped to now (these are mutating operations)."""
    summary = _sanitize_summary(summary)
    body = _neutralize_body(body)
    lines = [f"<!-- mem:start id={rid} -->",
             f"### {meta.get('type', 'fact')} · {scope_label(meta.get('scope', 'global'))} · {summary}",
             f"- type: {meta.get('type', 'fact')}",
             f"- scope: {meta.get('scope', 'global')}",
             f"- created: {meta.get('created') or now_ts()}",
             f"- updated: {now_ts()}",
             f"- status: {meta.get('status', 'active')}"]
    for k in ("superseded-by", "invalidated", "invalid-reason", "priority", "files", "related-to", "blocked-by", "protected", "tier", "session"):
        if meta.get(k):
            lines.append(f"- {k}: {meta[k]}")
    lines += [f"- confidence: {meta.get('confidence', '1.0')}",
              f"- source: {meta.get('source', 'web')}", "", body.strip(), END_MARK]
    return "\n".join(lines) + "\n"


def _find_record_lines(rec_id):
    """(path, readlines-with-\\n, parsed-record) for rec_id, or (None, None, None)."""
    for path in store_files():
        for r in parse_file(path):
            if r["id"] == rec_id:
                with open(path, "r", encoding="utf-8") as f:
                    return path, f.readlines(), r
    return None, None, None


_lock_state = threading.local()


def _pid_alive(pid):
    """Best-effort liveness probe: True if the process is (or might be) alive, False only when we are
    confident it is dead. Used so a CRASHED holder's lock can be stolen but a slow-but-ALIVE holder's
    cannot. Never uses os.kill(pid, 0) on Windows (there sig 0 would terminate the process)."""
    if pid <= 0:
        return True   # unknown holder -> assume alive (never steal on a missing/empty pid)
    if os.name == "nt":
        try:
            import ctypes
            h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if h:
                ctypes.windll.kernel32.CloseHandle(h)
                return True
            return False
        except Exception:
            return True
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return True   # e.g. EPERM -> the process exists


@contextlib.contextmanager
def _locked(timeout=30.0):
    """Serialize ALL store writers (CLI, web UI, MCP, multiple agents). Cross-platform + stdlib: an
    atomic O_EXCL create is the mutex. REENTRANT per thread, so a locked function may call another.
    A stale lock is stolen ONLY when its recorded holder PID is dead (liveness probe) — never from a
    slow-but-alive writer — and the steal is atomic (rename) so two waiters can't both steal. Raises
    after `timeout` instead of hanging forever. Prevents interleaved appends and lost read-modify-writes."""
    if getattr(_lock_state, "depth", 0) > 0:   # already held by THIS thread -> reentrant no-op
        _lock_state.depth += 1
        try:
            yield
        finally:
            _lock_state.depth -= 1
        return
    lockpath = os.path.join(DATA, "staging", ".write.lock")
    os.makedirs(os.path.dirname(lockpath), exist_ok=True)
    deadline = time.time() + timeout
    while True:
        try:
            fd = os.open(lockpath, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, str(os.getpid()).encode())
            finally:
                os.close(fd)
            break
        except FileExistsError:
            try:
                holder = int(open(lockpath, encoding="utf-8").read().strip() or "0")
            except (OSError, ValueError):
                holder = 0
            if holder and not _pid_alive(holder):
                tmp = f"{lockpath}.stale.{os.getpid()}"
                try:                       # atomic steal: only one waiter wins the rename
                    os.rename(lockpath, tmp)
                    os.unlink(tmp)
                except OSError:
                    pass
                continue
            if time.time() > deadline:
                raise TimeoutError(f"could not acquire store write lock within {timeout}s: {lockpath}")
            time.sleep(0.05)
    _lock_state.depth = 1
    try:
        yield
    finally:
        _lock_state.depth = 0
        try:
            os.unlink(lockpath)
        except OSError:
            pass


def _locked_write(fn):
    """Decorator: run a store-mutating function under the write lock (reentrant-safe)."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with _locked():
            return fn(*args, **kwargs)
    return wrapper


def _rewrite_block(rec_id, new_block):
    """Replace a record's block with new_block, or delete it (new_block=None). Returns bool."""
    with _locked():
        # locate + read UNDER the lock, so a concurrent append between read and write is not lost
        path, lines, r = _find_record_lines(rec_id)
        if not path:
            return False
        start, end = r["start"], r["end"]
        if new_block is None:
            if start > 0 and lines[start - 1].strip() == "":   # drop the blank line above too
                start -= 1
            del lines[start:end + 1]
        else:
            lines[start:end + 1] = [ln + "\n" for ln in new_block.rstrip("\n").split("\n")]
        with open(path, "w", encoding="utf-8") as f:
            f.writelines(lines)
        return True


def _supersede_prior_status(scope, new_id):
    """A new `status` retires the one it continues: the most recent live status for `scope`.
    Returns (retired_ids, skipped_protected_ids, still_live_count).

    A status answers "where is this project now", so a stack of live ones makes the injection show
    several and leaves the agent guessing which is current. Every write path goes through
    `add_memory`, so this runs here rather than in each caller: an invariant held in one place is an
    invariant, held in three it is a convention.

    Deliberately retires only ONE, not every live status for the scope. Measured on a real store,
    13 of 29 scopes had already accumulated several (one had 19), and some are legitimately
    concurrent — a release state and an open-PR state are both current. Retiring the lot on an
    unrelated write would be a large, surprising edit triggered by an ordinary `add`; retiring the
    one it directly continues is the claim actually being made. Older ones are reported, not touched.

    A `protected: true` status is left alone (and reported): the flag exists precisely to stop an
    automatic rewrite. MEM_STATUS_UNIQUE=0 disables the whole behaviour."""
    if os.environ.get("MEM_STATUS_UNIQUE", "1") == "0":
        return [], [], 0
    # Position is the tie-breaker, not just `created`: timestamps are second-resolution, so several
    # statuses written in the same second compare equal and the "most recent" would be arbitrary.
    # A scope maps to one file and records are appended, so a later position IS a later write.
    live = [(n, r) for n, r in enumerate(all_records())
            if r["id"] != new_id and r["meta"].get("type") == "status"
            and r["meta"].get("scope") == scope and r["meta"].get("status", "active") == "active"]
    if not live:
        return [], [], 0
    live.sort(key=lambda nr: ((nr[1]["meta"].get("created") or ""), nr[0]), reverse=True)
    target = live[0][1]
    try:
        supersede_memory(target["id"], by=new_id, reason="superseded by a newer status")
        return [target["id"]], [], len(live) - 1
    except ValueError:
        return [], [target["id"]], len(live) - 1   # protected — the user asked for it to stay put


def add_memory(rtype, scope, summary, body, confidence="1.0", source="web", redact_secrets=True,
               status="active", protected=None, session=None, on_supersede=None,
               priority=None, files=None, tier=None, supersedes=None):
    """Append a fresh record; returns its id. Redacts secrets like every write path.
    status="working" makes it a scratch note: not injected at SessionStart, hidden from default
    search/list — until promote_memory() flips it to active.
    A new active `status` retires the previous one for its scope (see `_supersede_prior_status`);
    `on_supersede(done, skipped)` is called with what it did, for callers that report it.

    `supersedes` is the id this memory REVISES: it is retired in the same locked section, with the
    usual trail (never a delete). This exists because the agent that just got corrected knows what
    it is correcting; asking it beats inferring the link later from similarity. Raises if the id
    does not exist or is protected — a revision that silently fails to retire the old record leaves
    two contradictory memories live, which is the exact failure it is meant to prevent."""
    if rtype not in TYPES:
        raise ValueError(f"invalid type: {rtype}")
    if status not in ("active", "working"):
        raise ValueError(f"invalid status for add: {status} (use 'active' or 'working')")
    body = (body or "").strip()
    if not body:
        raise ValueError("empty body")
    if len(body) > MAX_BODY:   # sanity cap (defense against an agent writing an unbounded body via MCP)
        raise ValueError("body too large (max 64 KiB)")
    if redact_secrets and redact.enabled():
        body = redact.redact(body)[0]
        summary = redact.redact(summary)[0]
    rid = gen_id()
    path = scope_file(scope)
    if session is None:
        session = current_session()   # provenance: stamp the session that produced this memory
    rec = render_record(rid, rtype, scope, summary, body, confidence, source, now_ts(), now_ts(), status,
                        priority=priority, files=files, protected=protected, session=session,
                        tier=tier)
    with _locked():
        # Validate BEFORE the append, under the lock: a bad `supersedes` must abort the whole write.
        # Appending first and failing after leaves the new record live AND the old one un-retired --
        # two contradictory memories, which is precisely what the argument exists to prevent.
        if supersedes:
            target = get_record(supersedes)
            if not target:
                raise ValueError(f"supersedes: no record {supersedes}")
            if target["meta"].get("status", "active") != "active":
                raise ValueError(f"supersedes: {supersedes} is already "
                                 f"{target['meta'].get('status')}")
            if target["meta"].get("protected", "").strip().lower() in ("true", "yes", "1"):
                raise ValueError(f"supersedes: {supersedes} is protected")
        ensure_header(path, scope)
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n" + rec)
        if supersedes:
            # Same lock as the append: the revision and the retirement land together or not at all.
            supersede_memory(supersedes, by=rid, reason=f"revised by {rid}")
        # Under the same (reentrant) lock, so no concurrent writer can leave two live statuses.
        if rtype == "status" and status == "active":
            done, skipped, older = _supersede_prior_status(scope, rid)
            if on_supersede and (done or skipped or older):
                on_supersede(done, skipped, older)
    return rid


@_locked_write
def update_memory(rec_id, rtype=None, scope=None, summary=None, body=None, confidence=None,
                  redact_secrets=True, bypass_protected=False):
    """Edit a record's type/scope/summary/body/confidence in place, preserving all other meta.
    Redacts secrets in any field being changed — like add_memory, so no write path bypasses it.
    Locked across the whole read-modify-write (lock is reentrant, so the inner _rewrite_block is a
    no-op) so a concurrent writer can't slip a change in between the read and the rewrite (lost update)."""
    path, lines, r = _find_record_lines(rec_id)
    if not path:
        return False
    if not bypass_protected:
        if r["meta"].get("protected", "").strip().lower() in ("true", "yes", "1"):
            raise ValueError(f"cannot update protected memory: {rec_id}")
    m = dict(r["meta"])
    if rtype is not None:
        m["type"] = rtype
    if scope is not None:
        m["scope"] = scope
    if confidence is not None:
        m["confidence"] = confidence
    summ = summary if summary is not None else record_summary(r)
    bod = body if body is not None else r["body"]
    # The same cap add_memory enforces. It lives here rather than in a route handler so the two
    # write paths agree by construction: a profile edited through the web UI was unbounded while
    # the identical body was refused on create, and the profile is injected in FULL every session.
    if body is not None and len(bod) > MAX_BODY:
        raise ValueError("body too large (max 64 KiB)")
    if redact_secrets and redact.enabled():   # only the fields the caller is changing
        if body is not None:
            bod = redact.redact(bod)[0]
        if summary is not None:
            summ = redact.redact(summ)[0]
    return _rewrite_block(rec_id, render_from_meta(r["id"], m, summ, bod))


@_locked_write
def rescope_memory(rec_id, new_scope):
    """Move a record to a different scope file (validates the scope first)."""
    path, lines, r = _find_record_lines(rec_id)
    if not path:
        return False
    if r["meta"].get("scope", "") == new_scope:
        return True
    m = dict(r["meta"])
    m["scope"] = new_scope
    block = render_from_meta(r["id"], m, record_summary(r), r["body"])
    newpath = scope_file(new_scope)        # validate BEFORE deleting from the old file
    _rewrite_block(rec_id, None)
    ensure_header(newpath, new_scope)
    with open(newpath, "a", encoding="utf-8") as f:
        f.write("\n" + block)
    return True


@_locked_write
def delete_memory(rec_id, bypass_protected=False):
    path, lines, r = _find_record_lines(rec_id)
    if not path:
        return False
    if not bypass_protected:
        if r["meta"].get("protected", "").strip().lower() in ("true", "yes", "1"):
            raise ValueError(f"cannot delete protected memory: {rec_id}")
    # Drop the edges pointing at it first. A relation chip that resolves to nothing looks exactly
    # like a live one in the UI, and no amount of staring at the surviving record explains it.
    # Supersede deliberately does NOT do this: a superseded record still exists and its links are
    # still meaningful history. A deleted one is gone, so pointing at it is just wrong.
    for other in all_records():
        if other["id"] == rec_id:
            continue
        for key in ("related-to", "blocked-by"):
            if rec_id in _list_meta(other, key):
                try:
                    _edit_list_meta(other["id"], key, remove=[rec_id])
                except ValueError:
                    pass
    return _rewrite_block(rec_id, None)


@_locked_write
def supersede_memory(rec_id, by="", reason="", bypass_protected=False):
    """Mark a record superseded (bi-temporal: keep it + WHEN it stopped being valid + WHY).
    Line-level so all other meta is untouched. Returns the relpath written, or None if not found."""
    for path in store_files():
        for r in parse_file(path):
            if r["id"] != rec_id:
                continue
            if not bypass_protected:
                if r["meta"].get("protected", "").strip().lower() in ("true", "yes", "1"):
                    raise ValueError(f"cannot supersede protected memory: {rec_id}")
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            inserts = []
            if by:
                inserts.append(f"- superseded-by: {by}\n")
            inserts.append(f"- invalidated: {now_ts()}\n")
            if reason:
                inserts.append(f"- invalid-reason: {reason}\n")
            new_block = []
            for k in range(r["start"], r["end"] + 1):
                line = lines[k]
                mm = META_RE.match(line.rstrip("\n"))
                if mm and mm.group("k") in ("superseded-by", "invalidated", "invalid-reason"):
                    continue  # drop old copies; re-added after status
                if mm and mm.group("k") == "status":
                    line = "- status: superseded\n"
                elif mm and mm.group("k") == "updated":
                    line = f"- updated: {now_ts()}\n"
                new_block.append(line)
                if mm and mm.group("k") == "status":
                    new_block.extend(inserts)
            lines[r["start"]:r["end"] + 1] = new_block
            with open(path, "w", encoding="utf-8") as f:
                f.writelines(lines)
            return os.path.relpath(path, DATA)
    return None


@_locked_write
def promote_memory(rec_id):
    """Promote a working note to a durable memory (status working -> active). Line-level, like
    supersede. Returns the relpath written, None if the id is not found, or False if the record
    exists but is not a working note."""
    for path in store_files():
        for r in parse_file(path):
            if r["id"] != rec_id:
                continue
            if r["meta"].get("status") != "working":
                return False
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            new_block = []
            for k in range(r["start"], r["end"] + 1):
                line = lines[k]
                mm = META_RE.match(line.rstrip("\n"))
                if mm and mm.group("k") == "status":
                    line = "- status: active\n"
                elif mm and mm.group("k") == "updated":
                    line = f"- updated: {now_ts()}\n"
                new_block.append(line)
            lines[r["start"]:r["end"] + 1] = new_block
            with open(path, "w", encoding="utf-8") as f:
                f.writelines(lines)
            return os.path.relpath(path, DATA)
    return None


# ----- fragment references (@id:line) -----

def parse_ref(ref):
    """Parse a fragment reference into (id, (start,end)|None). Accepts 'id', 'id:5', 'id:5-9', and a
    leading '@'. Line numbers are 1-based, inclusive."""
    ref = (ref or "").strip().lstrip("@")
    rng = None
    if ":" in ref:
        ref, _, ln = ref.partition(":")
        ln = ln.strip()
        if ln:
            a, _, b = ln.partition("-")
            try:
                rng = (int(a), int(b)) if b else (int(a), int(a))
            except ValueError:
                raise ValueError(f"invalid line range in reference: {ln!r}")
    return ref.strip(), rng


def get_record(rec_id):
    """The parsed record with this id (any status), or None."""
    return next((r for r in all_records() if r["id"] == rec_id), None)


def number_body(body, rng=None):
    """Body with 1-based line numbers; if rng=(start,end), only those lines. Lets a caller SEE line
    numbers (to form an @id:line ref) and retrieve a precise fragment."""
    blines = (body or "").rstrip("\n").split("\n")
    if not blines:
        return ""
    lo, hi = (1, len(blines))
    if rng:
        lo, hi = max(1, rng[0]), min(len(blines), rng[1])
    if lo > hi:
        return f"(no such lines; record has {len(blines)})"
    width = len(str(hi))
    return "\n".join(f"{i:>{width}}: {blines[i - 1]}" for i in range(lo, hi + 1))


# ----- commands -----

def cmd_add(a):
    if a.type not in TYPES:
        sys.exit(f"invalid type: {a.type} (choose from {', '.join(TYPES)})")
    body = a.body
    if body is None:
        if not sys.stdin.isatty():
            body = sys.stdin.read()
        else:
            sys.exit("missing body: pass --body \"...\" or pipe it on stdin")
    body = (body or "").strip()
    if not body:
        sys.exit("empty body")
    summary = a.summary
    if not a.no_redact and redact.enabled():
        body, f1 = redact.redact(body)
        summary, f2 = redact.redact(summary)
        if f1 or f2:
            print(f"redacted secrets: {redact.describe(f1 + f2)} (use --no-redact to keep them)")
    path = scope_file(a.scope)
    files = ", ".join(x.strip() for x in (a.files or "").split(",") if x.strip()) or None
    # duplicate guard: warn (never block) if a very similar memory of the same type already exists,
    # so we supersede instead of silently piling up near-duplicates. Computed before the write so it
    # never matches the new record against itself.
    dup_warn = []
    if (not a.no_dup_check and os.environ.get("MEM_DUP_CHECK", "1") != "0"
            and not getattr(a, "supersedes", None)):   # already told us what it revises
        dup_warn = find_duplicates(a.type, summary, body, files)
    inj = redact.scan_injection(summary + "\n" + body)
    status = "working" if getattr(a, "working", False) else "active"
    retired = []
    # One write path for every caller (CLI, MCP, web): store invariants such as "at most one live
    # status per scope" live inside add_memory, so no surface can forget to apply them.
    rid = add_memory(
        a.type, a.scope, summary, body, a.confidence, a.source,
        redact_secrets=False,   # cmd_add already redacted above, and reported what it removed
        status=status,
        priority="critical" if a.critical and status == "active" else None,
        files=files,
        protected="true" if getattr(a, "protected", False) else None,
        tier=getattr(a, "tier", None),
        supersedes=getattr(a, "supersedes", None),
        session=(getattr(a, "session", None) or current_session()),
        on_supersede=lambda done, skipped, older: retired.extend(
            [("retired", i) for i in done] + [("protected", i) for i in skipped]
            + ([("older", str(older))] if older else [])))
    tag = "  [working]" if status == "working" else ("  [CRITICAL]" if a.critical else "")
    print(f"added {rid}  [{a.type} · {a.scope}]{tag}  -> {os.path.relpath(path, DATA)}")
    if getattr(a, "supersedes", None):
        print(f"  retired {a.supersedes} (revised by {rid}; superseded, not deleted)")
    sys.stdout.flush()   # so the stdout 'added' line lands before any stderr warning below
    for kind, old in retired:
        if kind == "retired":
            print(f"  retired the previous status for {a.scope}: {old} (superseded by {rid})")
        elif kind == "older":
            print(f"  note: {old} older status{'es' if old != '1' else ''} for {a.scope} still live — "
                  f"`mem.py list --scope {a.scope} --type status` to review", file=sys.stderr)
        else:
            print(f"  note: {old} is a protected status for {a.scope} and stays live — "
                  f"two live statuses now, resolve by hand", file=sys.stderr)
    sys.stdout.flush()
    if dup_warn:
        print("note: a very similar memory of this type already exists — supersede instead of duplicating?",
              file=sys.stderr)
        for s, did, dscope, dsum in dup_warn:
            print(f"   {round(s * 100):>3}%  {did}  [{a.type}·{dscope}]  {dsum[:78]}", file=sys.stderr)
        print(f"   -> mem.py supersede <old-id> --by {rid}   (mute: --no-dup-check / MEM_DUP_CHECK=0)",
              file=sys.stderr)
    if inj:
        print(f"warning: this memory contains prompt-injection-like phrasing ({', '.join(inj)}).",
              file=sys.stderr)
        print("   it gets injected into the agent's context every session — review it if it wasn't "
              "written deliberately (mute: MEM_SCAN_INJECTION=0).", file=sys.stderr)


def _match_filters(rec, scope, rtype, status, since=None, until=None):
    if scope and rec["meta"].get("scope") != scope:
        return False
    if rtype and rec["meta"].get("type") != rtype:
        return False
    if status != "all" and rec["meta"].get("status", "active") != status:
        return False
    created = (rec["meta"].get("created") or "")[:19]
    if since and created < since:
        return False
    if until and created > until:
        return False
    return True


def _norm_date(s, end=False):
    """'2026-05-01' / '2026-05-01 14:30[:00]' -> full timestamp for string comparison."""
    if not s:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return s + (" 23:59:59" if end else " 00:00:00")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2})?", s):
        s = s.replace("T", " ")
        return s if len(s) == 19 else s + (":59" if end else ":00")
    sys.exit(f"invalid date: {s} (use YYYY-MM-DD or 'YYYY-MM-DD HH:MM')")


def record_summary(rec):
    """Extract the summary from the '### type · scope · summary' title."""
    parts = [p.strip() for p in rec["title"].split("·")]
    return parts[2] if len(parts) >= 3 else rec["title"]


def cmd_list(a):
    since, until = _norm_date(a.since), _norm_date(a.until, end=True)
    recs = [r for r in all_records() if _match_filters(r, a.scope, a.type, a.status, since, until)]
    dest = getattr(a, "dest", None) or DEST_LOCAL
    if dest == DEST_AGENT:
        recs = visible_for(recs, dest)   # private never leaves for a model
    suppressed = []
    if getattr(a, "dedup", False):
        # Unranked list: keep the newest of each cluster — a rewrite is usually the better copy.
        recs, suppressed = dedup_near(recs, prefer="newest")
    if getattr(a, "json", False):
        import json
        access_times = get_last_accessed()
        out = [{
            "id": r["id"], "type": r["meta"].get("type"), "scope": r["meta"].get("scope"),
            "summary": record_summary(r), "status": r["meta"].get("status", "active"),
            "confidence": r["meta"].get("confidence"), "source": r["meta"].get("source"),
            "created": r["meta"].get("created"), "updated": r["meta"].get("updated"),
            "last_accessed": access_times.get(r["id"]),
            "superseded_by": r["meta"].get("superseded-by"),
            "priority": r["meta"].get("priority"),
            "related_to": r["meta"].get("related-to"),
            "blocked_by": r["meta"].get("blocked-by"),
            "files": r["meta"].get("files"),
            "session": r["meta"].get("session"),
            "tier": record_tier(r),
            "invalidated": r["meta"].get("invalidated"),
            "invalid_reason": r["meta"].get("invalid-reason"),
            "body": emit_for(r, dest)[1] or "",
        } for r in recs]
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return
    if not recs:
        print("(no memories)")
        return
    for r in recs:
        st = r["meta"].get("status", "active")
        flag = "" if st == "active" else f"  ({st})"
        print(f"{r['id']}  [{r['meta'].get('type','?')} · {r['meta'].get('scope','?')}]{flag}")
        print(f"    {r['title'] or r['body'][:80]}")
    print(f"\n{len(recs)} memories" + (f"  ({len(suppressed)} near-duplicates hidden)" if suppressed else ""))


# ----- derived FTS5 index: ranked search, regenerable from markdown -----

def index_path():
    return os.path.join(STORE, ".index.db")


def index_stale():
    idx = index_path()
    if not os.path.exists(idx):
        return True
    imt = os.path.getmtime(idx)
    return any(os.path.getmtime(f) > imt for f in store_files())


def build_index():
    """(Re)build the FTS5 index from markdown. Returns False if FTS5 is unavailable."""
    idx = index_path()
    tmp = idx + ".build." + str(os.getpid())   # per-process temp: concurrent builders don't collide
    os.makedirs(os.path.dirname(idx), exist_ok=True)   # fresh/empty store: store/ may not exist yet
    if os.path.exists(tmp):
        os.remove(tmp)
    con = sqlite3.connect(tmp)
    try:
        con.execute("CREATE VIRTUAL TABLE mem USING fts5(id UNINDEXED, summary, body)")
    except sqlite3.OperationalError:
        con.close()
        os.remove(tmp)
        return False
    for r in all_records():
        files = r["meta"].get("files", "")
        body = r["body"] + (("\nfiles: " + files) if files else "")  # make file paths searchable
        con.execute("INSERT INTO mem (id, summary, body) VALUES (?, ?, ?)",
                    (r["id"], record_summary(r), body))
    con.commit()
    con.close()
    os.replace(tmp, idx)
    try:
        os.chmod(idx, 0o666)
    except OSError:
        pass
    return True


def fts_search(query):
    """Record ids ordered by relevance (bm25). None if FTS5 is unavailable."""
    if index_stale() and not build_index():
        return None
    terms = re.findall(r"\w+", query, flags=re.UNICODE)
    if not terms:
        return []
    match = " OR ".join(f'"{t}"*' for t in terms)
    con = sqlite3.connect(index_path())
    try:
        rows = con.execute(
            "SELECT id, bm25(mem) FROM mem WHERE mem MATCH ?", (match,)
        ).fetchall()
    except sqlite3.OperationalError:
        return None
    finally:
        con.close()
    if not rows:
        return []
    # recency nudge: among matches, newer ranks slightly higher. bm25() is negative-better;
    # subtract a small recency bonus so recents win near-ties WITHOUT overriding a clearly
    # stronger keyword match (those differ by many bm25 units). Weight tunable via env.
    w = float(os.environ.get("MEM_RECENCY_WEIGHT", "1.5"))

    def _ord(rid_created):
        try:
            return datetime.datetime.strptime((rid_created or "")[:10], "%Y-%m-%d").toordinal()
        except ValueError:
            return None
    created = {r["id"]: _ord(r["meta"].get("created")) for r in all_records()}
    ords = [created[i] for i, _ in rows if created.get(i)]
    lo, hi = (min(ords), max(ords)) if ords else (0, 0)
    span = (hi - lo) or 1

    def score(rid, bm):
        o = created.get(rid)
        rec01 = ((o - lo) / span) if o else 0.0   # 0 = oldest, 1 = newest
        return bm - w * rec01
    return [rid for rid, _ in sorted(rows, key=lambda r: score(r[0], r[1]))]


def _print_hits(hits):
    if not hits:
        print("(no results)")
        return
    for r in hits:
        st = r["meta"].get("status", "active")
        flag = "" if st == "active" else f"  ({st})"
        print(f"{r['id']}  [{r['meta'].get('type','?')} · {r['meta'].get('scope','?')}]{flag}")
        print(f"    {r['title']}")
        if r["body"]:
            print(f"    {r['body'].splitlines()[0][:100]}")
    print(f"\n{len(hits)} results")


def _print_mode(mode, n):
    """One dim banner (to stderr, so piped stdout stays clean) naming the path that ran."""
    label = {
        "hybrid": "hybrid (FTS + semantic)",
        "off": "keyword (FTS) — semantic disabled (--no-semantic)",
        "no-vectors": "keyword (FTS) — no vectors; run `mem.py embed`",
        "embedder-offline": "keyword (FTS) — embedder offline; start Ollama",
        "substring": "substring (FTS5 unavailable)",
    }.get(mode, "keyword (FTS)")
    print(f"# search mode: {label}  ·  {n} hit{'' if n == 1 else 's'}", file=sys.stderr)


def cmd_search(a):
    since, until = _norm_date(a.since), _norm_date(a.until, end=True)
    ids, mode = hybrid_search(a.query, allow_semantic=not getattr(a, "no_semantic", False))
    if ids is not None:
        by_id = {r["id"]: r for r in all_records()}
        hits = [by_id[i] for i in ids
                if i in by_id and by_id[i]["meta"].get("status") != "working"  # scratch notes aren't recall
                and _match_filters(by_id[i], a.scope, a.type, "all", since, until)]
        dropped = []
        if getattr(a, "dedup", False):
            hits, dropped = dedup_near(hits)   # ranked order: the most relevant of a cluster survives
        _print_mode(mode, len(hits))
        _print_hits(hits)
        if dropped:
            # Never silently: this is the user's own store, and a hidden hit reads as a lost memory.
            print(f"\n({len(dropped)} near-duplicate{'s' if len(dropped) > 1 else ''} suppressed: "
                  + ", ".join(f"{r['id']}~{tid}" for r, tid in dropped[:5])
                  + (", ..." if len(dropped) > 5 else "") + " — drop --dedup to see them)")
        return
    # fallback: substring scan (ripgrep when available) if FTS5 is missing
    q = a.query
    if shutil.which("rg"):
        try:
            out = subprocess.run(["rg", "-l", "-i", q, STORE], capture_output=True, text=True,
                                 creationflags=_NO_WINDOW).stdout
            cand_files = set(out.split())
        except Exception:
            cand_files = set(store_files())
    else:
        cand_files = set(store_files())
    ql = q.lower()
    hits = []
    for path in (cand_files or store_files()):
        for r in parse_file(path):
            blob = f"{r['title']}\n{r['body']}\n{r['meta'].get('scope','')}".lower()
            if ql in blob and r["meta"].get("status") != "working" \
                    and _match_filters(r, a.scope, a.type, "all", since, until):
                hits.append(r)
    _print_mode("substring", len(hits))
    _print_hits(hits)


# ----- optional semantic layer: embeddings in a SEPARATE derived db (.embed.db) -----
# Kept apart from .index.db so rebuilding the FTS index never wipes the (slower) vectors.
# The embedder is a retrieval helper only — it never decides what is a memory and never writes.

def embed_path():
    return os.path.join(STORE, ".embed.db")


def _rec_text(r):
    files = r["meta"].get("files", "")
    return f"{record_summary(r)}\n{r['body']}" + (f"\nfiles: {files}" if files else "")


def _rec_hash(r):
    return hashlib.sha1(_rec_text(r).encode("utf-8")).hexdigest()[:12]


def embed_index(force=False):
    """(Re)build the derived embeddings db. Incremental: only (re)embeds changed records.
    Returns (embedded, total) or None if no embedder is available."""
    import llm
    if not llm.embedder_up():
        return None
    os.makedirs(os.path.dirname(embed_path()), exist_ok=True)   # fresh/empty store: store/ may not exist yet
    # autocommit (isolation_level=None): each row is its own short write, so we don't hold one write
    # transaction open across the per-record Ollama HTTP calls and block a concurrent embed run.
    con = sqlite3.connect(embed_path(), isolation_level=None)
    con.execute("CREATE TABLE IF NOT EXISTS emb (id TEXT PRIMARY KEY, hash TEXT, vec BLOB)")
    have = {row[0]: row[1] for row in con.execute("SELECT id, hash FROM emb")}
    recs = [r for r in all_records() if r["meta"].get("status", "active") == "active"]
    ids = {r["id"] for r in recs}
    n = 0
    for r in recs:
        h = _rec_hash(r)
        if not force and have.get(r["id"]) == h:
            continue
        v = llm.embed(_rec_text(r))
        if v is None:
            continue
        con.execute("INSERT OR REPLACE INTO emb (id, hash, vec) VALUES (?, ?, ?)",
                    (r["id"], h, struct.pack("<" + f"{len(v)}f", *v)))
        n += 1
    for gone in set(have) - ids:   # drop removed / superseded
        con.execute("DELETE FROM emb WHERE id = ?", (gone,))
    con.commit()
    con.close()
    try:
        os.chmod(embed_path(), 0o666)
    except OSError:
        pass
    return (n, len(recs))


def load_embeddings():
    """id -> vector, from the derived .embed.db ({} if none)."""
    p = embed_path()
    if not os.path.exists(p):
        return {}
    con = sqlite3.connect(p)
    try:
        out = {row[0]: list(struct.unpack("<" + f"{len(row[1]) // 4}f", row[1]))
               for row in con.execute("SELECT id, vec FROM emb")}
    except sqlite3.OperationalError:
        out = {}
    con.close()
    return out


def _float_env(key, default):
    """float(os.environ[key]) that never raises and never returns nan/inf — a typo in
    .mem-local.env must not take search down or poison a ranking comparison."""
    try:
        v = float(os.environ.get(key, default))
        return v if v == v and v not in (float("inf"), float("-inf")) else float(default)
    except (TypeError, ValueError):
        return float(default)


def _cosine(a, b):
    if len(a) != len(b):
        return 0.0
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return sum(x * y for x, y in zip(a, b)) / (na * nb) if na and nb else 0.0


def find_duplicates(rec_type, summary, body, files=None, k=3, embed_timeout=None):
    """Existing ACTIVE memories of the SAME type most similar to a candidate, as
    (score, id, scope, summary) sorted high-first. Semantic (cosine over .embed.db) when an
    embedder is up; otherwise a lexical Jaccard fallback on the summary. Empty if nothing crosses
    the bar. A guard against silently duplicating a memory — it warns at write time, never blocks.
    Tunable: MEM_DUP_THRESHOLD (cosine, default 0.62), MEM_DUP_JACCARD (lexical, default 0.5).

    `embed_timeout` bounds the embedder call, for callers on a latency-sensitive write path (the
    MCP tool): a slow Ollama must not make saving a memory feel broken, because an agent that
    finds writing slow simply stops writing. On timeout this degrades to the lexical fallback
    rather than failing — a weaker warning beats no warning."""
    cands = [r for r in all_records()
             if r["meta"].get("status", "active") == "active"
             and r["meta"].get("type") == rec_type]
    if not cands:
        return []
    text = f"{summary}\n{body}" + (f"\nfiles: {files}" if files else "")
    hits = []
    emb = load_embeddings()
    qv = None
    if emb:
        import llm
        try:
            qv = llm.embed(text, timeout=embed_timeout) if embed_timeout else llm.embed(text)
        except Exception:
            qv = None   # any embedder trouble -> lexical fallback below, never a failed write
    if qv is not None:
        thresh = float(os.environ.get("MEM_DUP_THRESHOLD", "0.62"))
        for r in cands:
            v = emb.get(r["id"])
            if not v:
                continue
            s = _cosine(qv, v)
            if s >= thresh:
                hits.append((s, r))
    else:
        # no embedder: token-overlap (Jaccard) on the summary catches near-identical wording
        toks = lambda s: set(re.findall(r"\w+", (s or "").lower()))
        qs = toks(summary)
        jac = float(os.environ.get("MEM_DUP_JACCARD", "0.5"))
        if qs:
            for r in cands:
                rs = toks(record_summary(r))
                if rs and len(qs & rs) / len(qs | rs) >= jac:
                    hits.append((len(qs & rs) / len(qs | rs), r))
    hits.sort(key=lambda x: -x[0])
    return [(s, r["id"], r["meta"].get("scope", ""), record_summary(r)) for s, r in hits[:k]]


def _tokens(text):
    return set(re.findall(r"\w+", (text or "").lower(), flags=re.UNICODE))


def _jaccard(a, b):
    """Token-set Jaccard of two token sets, in [0,1]. Two empty sets are not similar (0.0):
    a record with no tokens says nothing, so it must not swallow another one."""
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def dedup_near(records, threshold=None, prefer=None):
    """Suppress near-duplicate records from an ORDERED list. Returns (kept, suppressed).

    OPT-IN, and deliberately NOT on the recall or injection path. Measured on a real 799-record
    store: the highest token-Jaccard between two same-(type, scope) records is 0.36, so a
    suppression threshold high enough to be safe never fires at all; and by embedding cosine, the
    pairs that do clear 0.79-0.90 are stale statuses ("step 2" vs "step 3", "engine chosen" vs
    "deployed live") and adjacent-but-distinct facts — not copies. Hiding those at read time would
    lose information, and `consolidate` already proposes the same pairs for review at a lower bar
    (MEM_DUP_THRESHOLD 0.62). Left here for the case it does fit: a bulk import that lands genuine
    copies, where `list --dedup` gives a clean view without touching the store.

    Similarity is token Jaccard over summary + body, and only records of the SAME type and scope
    are ever compared — a `todo` is never suppressed by a `gotcha` that happens to share wording,
    and the comparison stays near-linear instead of O(n^2) over the whole store.

    `prefer=None` keeps the first of each cluster in the order given (use it when the list is
    ranked by relevance); `prefer='newest'` keeps the most recently created (use it for
    unranked lists, where a rewrite is usually the better copy). Input order is preserved.
    Threshold: MEM_RECALL_DEDUP (default 0.8); 0 disables suppression entirely."""
    if threshold is None:
        try:
            threshold = float(os.environ.get("MEM_RECALL_DEDUP", "0.8"))
        except (TypeError, ValueError):
            threshold = 0.8
    if threshold <= 0 or len(records) < 2:
        return list(records), []

    order = records
    if prefer == "newest":
        order = sorted(records, key=lambda r: (r["meta"].get("created") or ""), reverse=True)

    toks = {}
    for r in records:
        toks[r["id"]] = _tokens(f"{record_summary(r)}\n{r.get('body', '')}")

    kept_ids, suppressed = set(), []
    kept_by_bucket = {}
    for r in order:
        bucket = (r["meta"].get("type"), r["meta"].get("scope"))
        mine = toks[r["id"]]
        twin = next((k for k in kept_by_bucket.get(bucket, [])
                     if _jaccard(mine, toks[k["id"]]) >= threshold), None)
        if twin is None:
            kept_ids.add(r["id"])
            kept_by_bucket.setdefault(bucket, []).append(r)
        else:
            suppressed.append((r, twin["id"]))
    return [r for r in records if r["id"] in kept_ids], suppressed


def _project_dir(scope):
    """Where a scope's code lives: <repo-root>/<slug> for project:<slug>. None for global.

    The store sits inside the monorepo (or beside it), so a sibling directory named after the slug
    is the convention every memory in this store already follows. Returns None rather than guessing
    when that directory does not exist — a wrong root would report drift for every memory at once."""
    if not scope or not scope.startswith("project:"):
        return None
    slug = scope.split(":", 1)[1].strip()
    if not slug:
        return None
    root = os.path.dirname(os.path.abspath(DATA))
    d = os.path.join(root, slug)
    return d if os.path.isdir(d) else None


def check_drift(records=None, since_commits=None):
    """Which memories may have gone stale because the code they describe moved on.

    This is the honest, cheap half of a hard problem. It does NOT read the code or judge whether a
    memory is still true — nothing here understands the change. It reports a *reason to re-read*:
    a memory anchored to files (`files:`) whose files have since been deleted, or committed to many
    times since the memory was written. Deletion is a strong signal; churn is a weak one, which is
    why the threshold is tunable and the output is a report, never an edit.

    Returns [(record, [(path, verdict, detail), ...])] for records with at least one finding.
    Verdicts: 'missing' (the file is gone), 'churn' (>= threshold commits since), 'moved' (the path
    is gone but a file with that basename exists elsewhere in the project).
    Only records carrying `files:` can be checked at all — see `mem.py drift` for the coverage line.
    """
    if since_commits is None:
        try:
            since_commits = int(os.environ.get("MEM_DRIFT_COMMITS", "10"))
        except (TypeError, ValueError):
            since_commits = 10
    if records is None:
        records = [r for r in all_records() if r["meta"].get("status", "active") == "active"]
    out = []
    for r in records:
        raw = (r["meta"].get("files") or "").strip()
        if not raw:
            continue
        proj = _project_dir(r["meta"].get("scope", ""))
        if not proj:
            continue
        created = (r["meta"].get("created") or "")[:19]
        findings = []
        for rel in [x.strip() for x in raw.split(",") if x.strip()]:
            full = os.path.join(proj, rel)
            if not os.path.exists(full):
                base = os.path.basename(rel)
                alt = None
                for dirpath, dirnames, filenames in os.walk(proj):
                    dirnames[:] = [d for d in dirnames if d not in (".git", "node_modules", "__pycache__")]
                    if base in filenames:
                        alt = os.path.relpath(os.path.join(dirpath, base), proj)
                        break
                if alt and alt != rel:
                    findings.append((rel, "moved", f"now at {alt}"))
                else:
                    findings.append((rel, "missing", "no such file in the project"))
                continue
            if not created:
                continue
            res = subprocess.run(["git", "-C", proj, "log", "--since", created, "--oneline", "--", rel],
                                 capture_output=True, text=True, creationflags=_NO_WINDOW)
            if res.returncode != 0:
                continue          # not a git repo, or path outside it — no churn signal available
            n = len([ln for ln in res.stdout.splitlines() if ln.strip()])
            if n >= since_commits:
                findings.append((rel, "churn", f"{n} commits since {created[:10]}"))
        if findings:
            out.append((r, findings))
    return out


@_locked_write
def _set_files(rec_id, files):
    """In-place meta edit of the `files:` anchor. Line-level like _set_priority and _set_tier, so
    nothing else about the record is rewritten."""
    for path in store_files():
        for r in parse_file(path):
            if r["id"] != rec_id:
                continue
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            new_block = []
            for k in range(r["start"], r["end"] + 1):
                line = lines[k]
                mm = META_RE.match(line.rstrip("\n"))
                if mm and mm.group("k") == "files":
                    continue   # drop the old anchor; re-added below unless clearing
                if mm and mm.group("k") == "updated":
                    line = f"- updated: {now_ts()}\n"
                new_block.append(line)
                if files and mm and mm.group("k") == "status":
                    new_block.append(f"- files: {files}\n")
            lines[r["start"]:r["end"] + 1] = new_block
            with open(path, "w", encoding="utf-8") as f:
                f.writelines(lines)
            return os.path.relpath(path, DATA)
    return None


def cmd_anchor(a):
    """Point a memory at the files it is about, or clear the anchor.

    Exists because `drift` could report a stale anchor and nothing could fix one: neither the CLI
    nor the web edit form touched `files:`, so the only repair was hand-editing markdown. A report
    you cannot act on is worse than no report."""
    rec = get_record(a.id)
    if not rec:
        sys.exit(f"id not found: {a.id}")
    if a.files.strip() == "-":
        rel = _set_files(a.id, None)
        print(f"{a.id} — anchor cleared  in {rel}")
        return
    paths = [x.strip() for x in a.files.split(",") if x.strip()]
    proj = _project_dir(rec["meta"].get("scope", ""))
    missing = [p for p in paths if proj and not os.path.exists(os.path.join(proj, p))]
    rel = _set_files(a.id, ", ".join(paths))
    print(f"{a.id} -> files: {', '.join(paths)}  in {rel}")
    if missing:
        # Not refused: a path can legitimately not exist yet. But said out loud, because an anchor
        # that never resolves makes `drift` cry wolf forever.
        print(f"  warning: not present in {os.path.basename(proj)}: {', '.join(missing)}"
              f" — drift will keep reporting this memory", file=sys.stderr)


def cmd_drift(a):
    recs = [r for r in all_records() if r["meta"].get("status", "active") == "active"]
    if a.scope:
        recs = [r for r in recs if r["meta"].get("scope") == a.scope]
    anchored = [r for r in recs if (r["meta"].get("files") or "").strip()]
    hits = check_drift(anchored, since_commits=a.commits)
    print(f"# {len(anchored)} of {len(recs)} active memories are anchored to files "
          f"({round(100 * len(anchored) / max(len(recs), 1))}%)"
          + ("" if anchored else " — nothing to check; add `files:` when a memory is about code"))
    if not hits:
        if anchored:
            print("no drift signals: every anchored file is present and quiet")
        return
    print(f"{len(hits)} memor{'y' if len(hits) == 1 else 'ies'} worth re-reading:\n")
    for r, findings in hits:
        print(f"{r['id']}  [{r['meta'].get('type')} · {r['meta'].get('scope')}]")
        print(f"    {record_summary(r)[:100]}")
        for rel, verdict, detail in findings:
            print(f"    {verdict:8} {rel} — {detail}")
        print()
    print("Nothing was changed. These are files that moved on, not memories proven wrong —")
    print("read them, then supersede the ones that no longer hold.")


def hybrid_search(query, allow_semantic=True):
    """Keyword (FTS5 + recency) fused with semantic similarity via Reciprocal Rank Fusion (k=60),
    plus a light summary phrase-match rerank, when an embedder is available.
    Returns (ranked_ids, mode); ids is None if FTS5 is missing (caller does the substring fallback).
    mode is one of: 'hybrid', 'keyword', 'off', 'no-vectors', 'embedder-offline', 'substring'
    — so the caller can tell the user which path actually ran."""
    fts = fts_search(query)
    if fts is None:
        return None, "substring"
    if not allow_semantic:
        return fts, "off"
    emb = load_embeddings()
    if not emb:
        return fts, "no-vectors"
    import llm
    qvec = llm.embed(query)
    if qvec is None:
        return fts, "embedder-offline"
    sims = {i: _cosine(qvec, v) for i, v in emb.items()}
    # Reciprocal Rank Fusion (k=60): fuse the keyword ranking (fts, already recency-nudged) with the
    # semantic ranking by RANK position, not raw score. Robust to the scale mismatch between bm25 and
    # cosine — neither signal can swamp the other (the old 0.5*cosine + 0.5*linear-rank blend was
    # sensitive to cosine's compressed range). Tunable via MEM_RRF_K.
    K = float(os.environ.get("MEM_RRF_K", "60"))
    # Per-retriever weights: the two rankings do not deserve equal votes once the embedder is good.
    # Measured with tools/bench_recall.py (40 paraphrased queries) on bge-m3 — see the sweep in the
    # commit that introduced this. Tunable rather than hardcoded because the right value depends on
    # the embedder: with a weak one, weighting the dense signal up just amplifies noise.
    W_DENSE = _float_env("MEM_RRF_W_DENSE", "1.0")
    W_KEYWORD = _float_env("MEM_RRF_W_KEYWORD", "1.0")
    kw_rank = {i: n for n, i in enumerate(fts)}
    sem_sorted = [i for i, _ in sorted(sims.items(), key=lambda x: -x[1])]
    sem_rank = {i: n for n, i in enumerate(sem_sorted)}
    cand = set(fts) | set(sem_sorted[:25])
    unit = 1.0 / (K + 1.0)   # the max contribution of one list (rank 0) — boosts are scaled to it
    ql = query.strip().lower()
    qterms = set(re.findall(r"\w+", ql, flags=re.UNICODE))
    by = {r["id"]: r for r in all_records()}

    def score(i):
        s = 0.0
        if i in kw_rank:
            s += W_KEYWORD / (K + kw_rank[i] + 1)
        if i in sem_rank:
            s += W_DENSE / (K + sem_rank[i] + 1)
        # light rerank: a query hit in the SUMMARY is a strong precision signal the fusion misses.
        # Scaled to `unit` so it nudges near-ties without overriding a clearly better-ranked pair.
        r = by.get(i)
        if r and qterms:
            summ = (record_summary(r) or "").lower()
            if ql and ql in summ:
                s += 2.0 * unit                                  # exact query phrase in summary (~rank-1 in both)
            elif qterms <= set(re.findall(r"\w+", summ, flags=re.UNICODE)):
                s += 1.0 * unit                                  # all query terms present in summary
        return s

    return sorted(cand, key=score, reverse=True), "hybrid"


def cmd_embed(a):
    r = embed_index(force=a.force)
    if r is None:
        print("embedder unavailable — start Ollama and `ollama pull bge-m3` (or set MEM_EMBED_MODEL).")
        sys.exit(1)
    print(f"embeddings up to date: {r[0]} (re)embedded of {r[1]} active -> {os.path.relpath(embed_path(), DATA)}")


def cmd_reindex(a):
    if build_index():
        print(f"FTS5 index rebuilt: {len(all_records())} records -> {os.path.relpath(index_path(), DATA)}")
    else:
        print("FTS5 unavailable in this sqlite — search falls back to substring scan.")
    r = embed_index()
    if r is not None:
        print(f"embeddings refreshed: {r[0]} (re)embedded of {r[1]} active.")


def cmd_supersede(a):
    rel = supersede_memory(a.id, a.by or "", getattr(a, "reason", None) or "")
    if rel is None:
        sys.exit(f"id not found: {a.id}")
    extra = f" (replaced by {a.by})" if a.by else ""
    extra += f" — {a.reason}" if getattr(a, "reason", None) else ""
    print(f"superseded {a.id}{extra}  in {rel}")


def cmd_promote(a):
    rec = get_record(a.id)
    if not rec:
        sys.exit(f"id not found: {a.id}")
    st = rec["meta"].get("status", "active")
    if st != "working":
        sys.exit(f"{a.id} is '{st}', not a working note — nothing to promote")
    rel = promote_memory(a.id)
    print(f"promoted {a.id} (working -> active)  in {rel}")


def cmd_get(a):
    rid, rng = parse_ref(a.ref)
    rec = get_record(rid)
    if not rec:
        sys.exit(f"no record {rid}")
    m = rec["meta"]
    st = m.get("status", "active")
    suffix = "" if st == "active" else f" ({st})"
    print(f"[{rec['id']}] {m.get('type', '?')} · {m.get('scope', '?')}{suffix}")
    print(record_summary(rec))
    print()
    print(number_body(rec.get("body", ""), rng))


@_locked_write
def _set_priority(rec_id, priority):
    """In-place meta edit: set (pin) or remove (unpin) the priority field of a record."""
    for path in store_files():
        for r in parse_file(path):
            if r["id"] != rec_id:
                continue
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            new_block = []
            for k in range(r["start"], r["end"] + 1):
                line = lines[k]
                mm = META_RE.match(line.rstrip("\n"))
                if mm and mm.group("k") == "priority":
                    continue  # drop the existing priority line; re-added below if pinning
                if mm and mm.group("k") == "updated":
                    line = f"- updated: {now_ts()}\n"
                new_block.append(line)
                if priority and mm and mm.group("k") == "status":
                    new_block.append(f"- priority: {priority}\n")
            lines[r["start"]:r["end"] + 1] = new_block
            with open(path, "w", encoding="utf-8") as f:
                f.writelines(lines)
            return os.path.relpath(path, DATA)
    sys.exit(f"id not found: {rec_id}")


@_locked_write
def _set_tier(rec_id, tier):
    """In-place meta edit: set or clear a record's egress tier. Line-level, like _set_priority, so
    nothing else about the record is rewritten (and `protected` does not apply — classifying a
    memory as private is exactly the kind of correction protection must never stand in the way of)."""
    for path in store_files():
        for r in parse_file(path):
            if r["id"] != rec_id:
                continue
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            new_block = []
            for k in range(r["start"], r["end"] + 1):
                line = lines[k]
                mm = META_RE.match(line.rstrip("\n"))
                if mm and mm.group("k") == "tier":
                    continue   # drop the existing tier line; re-added below unless clearing
                if mm and mm.group("k") == "updated":
                    line = f"- updated: {now_ts()}\n"
                new_block.append(line)
                if tier and tier != "open" and mm and mm.group("k") == "status":
                    new_block.append(f"- tier: {tier}\n")
            lines[r["start"]:r["end"] + 1] = new_block
            with open(path, "w", encoding="utf-8") as f:
                f.writelines(lines)
            return os.path.relpath(path, DATA)
    return None


def cmd_tier(a):
    if a.tier not in TIERS:
        sys.exit(f"invalid tier: {a.tier} (choose from {', '.join(TIERS)})")
    rel = _set_tier(a.id, a.tier)
    if rel is None:
        sys.exit(f"id not found: {a.id}")
    note = {"open": "reaches any agent",
            "redacted": "summary reaches an agent, body stays local",
            "private": "never reaches an agent; still searchable by you"}[a.tier]
    print(f"{a.id} -> tier: {a.tier} ({note})  in {rel}")


def cmd_pin(a):
    rel = _set_priority(a.id, "critical")
    print(f"pinned {a.id} -> priority: critical (ALWAYS injected, first, at SessionStart)  in {rel}")


def cmd_unpin(a):
    rel = _set_priority(a.id, None)
    print(f"unpinned {a.id} (normal priority)  in {rel}")


# ----- relations: related-to (any record) + blocked-by (todos) -----

def _list_meta(rec, key):
    """The comma-separated id list stored in meta[key] (e.g. related-to, blocked-by)."""
    return [x.strip() for x in (rec["meta"].get(key, "") or "").split(",") if x.strip()]


@_locked_write
def _edit_list_meta(rec_id, key, add=(), remove=()):
    """In-place add/remove of ids in a comma-separated meta list field. Drops the line
    when the list becomes empty. Returns (relpath, new_list)."""
    for path in store_files():
        for r in parse_file(path):
            if r["id"] != rec_id:
                continue
            cur = _list_meta(r, key)
            for i in add:
                if i and i != rec_id and i not in cur:
                    cur.append(i)
            cur = [i for i in cur if i not in set(remove)]
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            new_block = []
            for k in range(r["start"], r["end"] + 1):
                line = lines[k]
                mm = META_RE.match(line.rstrip("\n"))
                if mm and mm.group("k") == key:
                    continue  # drop existing; re-added after status if non-empty
                if mm and mm.group("k") == "updated":
                    line = f"- updated: {now_ts()}\n"
                new_block.append(line)
                if cur and mm and mm.group("k") == "status":
                    new_block.append(f"- {key}: {', '.join(cur)}\n")
            lines[r["start"]:r["end"] + 1] = new_block
            with open(path, "w", encoding="utf-8") as f:
                f.writelines(lines)
            return os.path.relpath(path, DATA), cur
    # ValueError, not sys.exit: this is library code. SystemExit derives from BaseException, so it
    # sails past `except Exception` in the web server and kills the thread serving the request —
    # the client sees a dropped connection instead of an error. The CLI turns it into an exit below.
    raise ValueError(f"id not found: {rec_id}")


def _warn_unknown(ids):
    have = {r["id"] for r in all_records()}
    missing = [i for i in ids if i not in have]
    if missing:
        print(f"warning: unknown id(s): {', '.join(missing)}")


def cmd_link(a):
    _warn_unknown(a.others)
    try:
        rel, cur = _edit_list_meta(a.id, "related-to", add=a.others)
    except ValueError as e:
        sys.exit(str(e))
    print(f"linked {a.id} related-to: {', '.join(cur) or '(none)'}  in {rel}")


def cmd_unlink(a):
    try:
        rel, cur = _edit_list_meta(a.id, "related-to", remove=a.others)
    except ValueError as e:
        sys.exit(str(e))
    print(f"unlinked {a.id} related-to: {', '.join(cur) or '(none)'}  in {rel}")


def cmd_block(a):
    _warn_unknown(a.blockers)
    try:
        rel, cur = _edit_list_meta(a.todo, "blocked-by", add=a.blockers)
    except ValueError as e:
        sys.exit(str(e))
    print(f"{a.todo} blocked-by: {', '.join(cur) or '(none)'}  in {rel}")


def cmd_unblock(a):
    remove = a.blockers
    if not remove:  # no ids given -> clear all blockers
        cur0 = next((_list_meta(r, "blocked-by") for r in all_records() if r["id"] == a.todo), [])
        remove = cur0
    try:
        rel, cur = _edit_list_meta(a.todo, "blocked-by", remove=remove)
    except ValueError as e:
        sys.exit(str(e))
    print(f"{a.todo} blocked-by: {', '.join(cur) or '(none)'}  in {rel}")


def _open_blockers(rec, by_id):
    """Blockers of a todo that are still OPEN (active todos). Resolved = superseded/missing."""
    out = []
    for bid in _list_meta(rec, "blocked-by"):
        b = by_id.get(bid)
        if b and b["meta"].get("status", "active") == "active" and b["meta"].get("type") == "todo":
            out.append(bid)
    return out


def cmd_ready(a):
    """Active todos with no OPEN blocker = what you can tackle right now."""
    recs = all_records()
    by_id = {r["id"]: r for r in recs}
    todos = [r for r in recs
             if r["meta"].get("type") == "todo"
             and r["meta"].get("status", "active") == "active"
             and (not a.scope or r["meta"].get("scope") == a.scope)]
    ready, blocked = [], []
    for r in todos:
        ob = _open_blockers(r, by_id)
        (blocked if ob else ready).append((r, ob))
    ready.sort(key=lambda t: t[0]["meta"].get("created", ""), reverse=True)
    if not todos:
        print("(no active todos)")
        return
    print(f"READY ({len(ready)}):")
    for r, _ in ready:
        print(f"  {r['id']}  [{r['meta'].get('scope','?')}]  {record_summary(r)}")
    if blocked:
        print(f"\nBLOCKED ({len(blocked)}):")
        for r, ob in blocked:
            print(f"  {r['id']}  [{r['meta'].get('scope','?')}]  {record_summary(r)}  <- {', '.join(ob)}")


def cmd_resume(a):
    """"Where was I?" — a compact briefing for a scope: latest status + ready/blocked todos + recent
    knowledge. With no --scope, a one-line-per-project overview. Mirrors what SessionStart injects."""
    recs = all_records()
    by_id = {r["id"]: r for r in recs}
    active = [r for r in recs if r["meta"].get("status", "active") == "active"]

    def latest_status(scope):
        ss = [r for r in active if r["meta"].get("type") == "status" and r["meta"].get("scope") == scope]
        ss.sort(key=lambda r: r["meta"].get("created", ""), reverse=True)
        return ss[0] if ss else None

    if not a.scope:
        scopes = sorted({r["meta"].get("scope", "global") for r in active
                         if r["meta"].get("scope", "global") != "global"})
        print("RESUME — overview  (use --scope project:<slug> for detail)\n")
        for sc in scopes:
            st = latest_status(sc)
            todos = [r for r in active if r["meta"].get("type") == "todo" and r["meta"].get("scope") == sc]
            ready = sum(1 for r in todos if not _open_blockers(r, by_id))
            line = f"  {sc}"
            if st:
                line += f"  —  {record_summary(st)[:68]}"
            if todos:
                line += f"   [{ready}/{len(todos)} todo ready]"
            print(line)
        return

    sc = a.scope
    print(f"RESUME — {sc}\n")
    st = latest_status(sc)
    if st:
        print(f"STATUS ({st['meta'].get('created', '')[:10]}):  {record_summary(st)}")
    else:
        print("STATUS: (none recorded)")
    todos = [r for r in active if r["meta"].get("type") == "todo" and r["meta"].get("scope") == sc]
    ready = sorted([r for r in todos if not _open_blockers(r, by_id)],
                   key=lambda r: r["meta"].get("created", ""), reverse=True)
    blocked = [(r, _open_blockers(r, by_id)) for r in todos if _open_blockers(r, by_id)]
    print(f"\nREADY TODOS ({len(ready)}):")
    for r in ready:
        print(f"  {r['id']}  {record_summary(r)}")
    if blocked:
        print(f"\nBLOCKED ({len(blocked)}):")
        for r, ob in blocked:
            print(f"  {r['id']}  {record_summary(r)}  <- {', '.join(ob)}")
    recent = sorted([r for r in active if r["meta"].get("scope") == sc
                     and r["meta"].get("type") not in ("status", "todo")],
                    key=lambda r: r["meta"].get("created", ""), reverse=True)
    if recent:
        print(f"\nRECENT ({min(5, len(recent))} of {len(recent)}):")
        for r in recent[:5]:
            print(f"  [{r['meta'].get('type', '?')}]  {record_summary(r)[:80]}")


def cmd_serve(a):
    """Start the pure-Python web UI (no PHP). Cross-platform; stdlib only."""
    import mem_web
    mem_web.serve(host=a.host, port=a.port)


def cmd_mcp(a):
    """Start the MCP server (stdio JSON-RPC) so any MCP runtime can pull memory. stdlib only."""
    import mcp
    mcp.serve_stdio()


def cmd_audit(a):
    """Report records with secret-like OR prompt-injection-like patterns. Never modifies anything."""
    recs = [r for r in all_records() if _match_filters(r, a.scope, None, "all")]
    findings = []
    for r in recs:
        text = record_summary(r) + "\n" + r["body"]
        sec = redact.scan(text)
        inj = redact.scan_injection(text)
        if sec or inj:
            findings.append((r, sec, inj))
    if not findings:
        print(f"audit clean: no secret-like or injection-like patterns in {len(recs)} records")
        return
    for r, sec, inj in findings:
        st = r["meta"].get("status", "active")
        flag = "" if st == "active" else f"  ({st})"
        labels = ([f"secret:{l}" for l in sec] + [f"injection:{l}" for l in inj])
        print(f"{r['id']}  [{r['meta'].get('type','?')} · {r['meta'].get('scope','?')}]{flag}  -> {', '.join(labels)}")
        print(f"    {record_summary(r)[:100]}")
    n_sec = sum(1 for _, s, _ in findings if s)
    n_inj = sum(1 for _, _, i in findings if i)
    print(f"\n{len(findings)} of {len(recs)} records flagged "
          f"({n_sec} secret-like, {n_inj} injection-like).")
    print("Nothing was modified — review them (edit in the web UI, or supersede + re-add).")
    sys.exit(1)


def cmd_sessions(a):
    """Search PAST CONVERSATIONS (raw transcripts, zero LLM cost) — distinct from `search`, which
    searches the distilled memory store. Backed by sessions.py (a derived FTS5 index)."""
    import sessions
    if a.reindex:
        res = sessions.build_index(DATA, rebuild=True)
        if res is None:
            sys.exit("FTS5 unavailable — cannot index sessions")
        print(f"indexed {res[1]} messages across {res[0]} session(s)")
        return
    if a.list:
        rows = sessions.list_sessions(DATA, project=a.project, limit=a.limit)
        if rows is None:
            sys.exit("FTS5 unavailable — cannot list sessions")
        if not rows:
            print("(no captured sessions yet)")
            return
        for s in rows:
            print(f"{s['start'][:10]}  {(s['project'] or '?'):22.22}  {s['nmsg']:>4} msg  [{s['session_id'][:8]}]")
            if s["first"]:
                print(f"    {s['first']}")
        print(f"\n{len(rows)} session(s)")
        return
    if not a.query:
        sys.exit("pass a query to search, or --list / --reindex")
    hits = sessions.search(a.query, DATA, project=a.project, limit=a.limit)
    if hits is None:
        sys.exit("FTS5 unavailable — cannot search sessions")
    if not hits:
        print("(no past messages match)")
        return
    for h in hits:
        print(f"{h['ts'][:16].replace('T', ' ')}  {h['project'] or '?'}  · {h['role']}  [{h['session_id'][:8]}]")
        print(f"    {h['snippet']}")
    print(f"\n{len(hits)} message(s) — raw transcript search, zero LLM cost")


def queue_path():
    return os.path.join(DATA, "staging", "queue.jsonl")


def cmd_propose(a):
    """Queue a candidate for human review (web UI), NOT directly into the store.

    Used by low-trust writers (e.g. batch LLM extraction) — a human approves or rejects.
    """
    import json
    if a.type not in TYPES:
        sys.exit(f"invalid type: {a.type} (choose from {', '.join(TYPES)})")
    body = a.body
    if body is None:
        body = sys.stdin.read() if not sys.stdin.isatty() else ""
    body = (body or "").strip()
    if not body:
        sys.exit("empty body: pass --body \"...\" or pipe it on stdin")
    summary = a.summary.strip()
    if not a.no_redact and redact.enabled():
        body, f1 = redact.redact(body)
        summary, f2 = redact.redact(summary)
        if f1 or f2:
            print(f"redacted secrets: {redact.describe(f1 + f2)} (use --no-redact to keep them)")
    try:
        conf = float(a.confidence)
    except ValueError:
        conf = 0.8
    rec = {
        "qid": gen_id(),
        "type": a.type, "scope": a.scope,
        "summary": summary, "body": body,
        "confidence": conf, "source": a.source,
        "transcript": None,
        "extracted_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "status": "pending",
    }
    os.makedirs(os.path.dirname(queue_path()), exist_ok=True)
    with open(queue_path(), "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(f"proposed {rec['qid']}  [{a.type} · {a.scope}]  -> review queue (web UI)")


def cmd_log_access(a):
    if not a.ids:
        sys.exit("error: missing --ids (comma-separated list of IDs)")
    ids = [i.strip() for i in a.ids.split(",") if i.strip()]
    action = a.action or "inject"
    for rid in ids:
        log_access(rid, action)
    print(f"logged {action} access for {len(ids)} record(s)")


def find_all_clusters(allow_semantic=True):
    """Group all active memories of the same type into connected duplicate components.

    Returns a list of list of records: [[recA1, recA2, ...], [recB1, ...]].
    """
    recs = all_records()
    active_recs = [r for r in recs if r["meta"].get("status", "active") == "active"]
    
    # Group by type
    by_type = {}
    for r in active_recs:
        by_type.setdefault(r["meta"].get("type"), []).append(r)
        
    # Similarity logic
    emb = load_embeddings() if allow_semantic else {}
    thresh = float(os.environ.get("MEM_DUP_THRESHOLD", "0.62"))
    jac_thresh = float(os.environ.get("MEM_DUP_JACCARD", "0.5"))
    
    def get_tokens(s):
        return set(re.findall(r"\w+", (s or "").lower()))

    # Build adjacency graph
    clusters = []
    
    for rtype, type_recs in by_type.items():
        if len(type_recs) < 2:
            continue
            
        # Build adjacency list
        adj = {r["id"]: [] for r in type_recs}
        
        # Cache token sets for Jaccard — over summary AND body, so two memories with near-identical
        # bodies but different one-line summaries are still caught (summary-only missed those).
        tokens = {r["id"]: get_tokens(record_summary(r) + " " + (r.get("body") or "")) for r in type_recs}
        
        for i in range(len(type_recs)):
            for j in range(i + 1, len(type_recs)):
                r1 = type_recs[i]
                r2 = type_recs[j]
                
                # Check semantic first if embeddings exist
                has_edge = False
                v1, v2 = emb.get(r1["id"]), emb.get(r2["id"])
                if v1 and v2:
                    s = _cosine(v1, v2)
                    if s >= thresh:
                        has_edge = True
                
                if not has_edge:
                    # Jaccard fallback
                    t1, t2 = tokens[r1["id"]], tokens[r2["id"]]
                    if t1 and t2:
                        union_len = len(t1 | t2)
                        if union_len > 0 and len(t1 & t2) / union_len >= jac_thresh:
                            has_edge = True
                            
                if has_edge:
                    adj[r1["id"]].append(r2["id"])
                    adj[r2["id"]].append(r1["id"])
                    
        # Find connected components (DFS)
        visited = set()
        for r in type_recs:
            rid = r["id"]
            if rid not in visited:
                component = []
                queue = [rid]
                visited.add(rid)
                while queue:
                    curr = queue.pop(0)
                    component.append(curr)
                    for neighbor in adj[curr]:
                        if neighbor not in visited:
                            visited.add(neighbor)
                            queue.append(neighbor)
                if len(component) > 1:
                    # Map IDs back to record dicts
                    rec_map = {rc["id"]: rc for rc in type_recs}
                    clusters.append([rec_map[cid] for cid in component])
                    
    return clusters


def llm_merge(records):
    import llm
    # Format memories text
    memories_text = ""
    for r in records:
        memories_text += f"ID: {r['id']}\nType: {r['meta'].get('type')}\nSummary: {record_summary(r)}\nBody:\n{r['body']}\n\n"
    
    schema = {
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            "body": {"type": "string"}
        },
        "required": ["summary", "body"]
    }
    system = (
        "You are a memory consolidator. Combine the given similar memories into a single, cohesive, "
        "and clear memory record. The output must be an optimal merge of all information. "
        "Preserve all technical facts, configurations, and commands. "
        "Ensure the body is concise (1-4 sentences) and the summary is a short, single sentence. "
        "Respond with valid JSON matching the schema."
    )
    prompt = f"Combine these memories:\n\n{memories_text}"
    try:
        res = llm.generate_json(system, prompt, schema)
        if res and "summary" in res and "body" in res:
            return res["summary"].strip(), res["body"].strip()
    except Exception:
        pass
    return None


def cmd_consolidate(a):
    # Check if git tree is clean
    res = subprocess.run(["git", "status", "--porcelain"], cwd=DATA, capture_output=True, text=True, creationflags=_NO_WINDOW)
    if res.returncode != 0:
        sys.exit("error: not a git repository or git not available")
    if res.stdout.strip():
        sys.exit("error: Git working tree has uncommitted changes. Please commit or stash them first.")
        
    clusters = find_all_clusters(allow_semantic=not getattr(a, "no_semantic", False))
    if not clusters:
        print("No duplicate memories found. Memory store is clean!")
        return
        
    print(f"Found {len(clusters)} duplicate cluster(s) to consolidate.")
    
    # Save original branch
    res = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=DATA, capture_output=True, text=True, creationflags=_NO_WINDOW)
    original_branch = res.stdout.strip()
    
    # Switch to mem-consolidation
    res = subprocess.run(["git", "checkout", "-B", "mem-consolidation"], cwd=DATA, capture_output=True, text=True, creationflags=_NO_WINDOW)
    if res.returncode != 0:
        sys.exit(f"error: failed to switch to mem-consolidation branch: {res.stderr}")
        
    # Process consolidation
    access_times = get_last_accessed()
    
    def master_key(r):
        prio = 0 if r["meta"].get("priority") == "critical" else 1
        try:
            conf = -float(r["meta"].get("confidence", "1.0"))
        except ValueError:
            conf = -1.0
        last_acc = access_times.get(r["id"], "")
        created = r["meta"].get("created", "")
        return (prio, conf, -len(last_acc), last_acc, created)
        
    llm_enabled = a.llm
    if llm_enabled:
        import llm
        if not llm.ollama_up():
            print("Warning: Ollama is down. Falling back to deterministic merge.")
            llm_enabled = False
            
    consolidated_count = 0
    
    try:
        for cluster in clusters:
            # Sort to find master
            cluster.sort(key=master_key)
            master = cluster[0]
            duplicates = cluster[1:]
            
            summary, body = None, None
            if llm_enabled:
                merged = llm_merge(cluster)
                if merged:
                    summary, body = merged
            
            if not summary or not body:
                # Deterministic fallback
                summary = record_summary(master)
                body = master["body"]
                seen = {body.lower()}
                for r in duplicates:
                    b = r["body"].strip()
                    if b.lower() not in seen:
                        body += "\n\n" + b
                        seen.add(b.lower())
            
            # Update master (bypass protection)
            update_memory(master["id"], summary=summary, body=body, bypass_protected=True)
            
            # Supersede duplicates (bypass protection)
            for r in duplicates:
                supersede_memory(r["id"], by=master["id"], reason=f"consolidated into {master['id']}", bypass_protected=True)
                
            consolidated_count += 1
            print(f"Consolidated: {master['id']} (kept) <- " + ", ".join(r["id"] for r in duplicates) + " (superseded)")
            
        # Commit changes. -c commit.gpgsign=false: consolidate runs git non-interactively (no TTY), so if
        # the user has commit.gpgsign=true globally and no local override, a signing commit would hang
        # forever waiting for a GPG passphrase — force-skip signing for this internal checkpoint commit.
        subprocess.run(["git", "-c", "commit.gpgsign=false", "commit", "-am", f"Propose memory consolidation ({consolidated_count} clusters)"], cwd=DATA, capture_output=True, text=True, creationflags=_NO_WINDOW)
        
    finally:
        # Restore original branch
        subprocess.run(["git", "checkout", original_branch], cwd=DATA, capture_output=True, text=True, creationflags=_NO_WINDOW)
        
    print("\nConsolidation branch 'mem-consolidation' successfully updated!")
    print("To review the proposed changes:")
    print("  git diff HEAD..mem-consolidation")
    print("To accept and merge:")
    print("  git merge mem-consolidation")
    print("To discard:")
    print("  git branch -D mem-consolidation")


def main():
    p = argparse.ArgumentParser(prog="mem.py", description="mem0ry4ai — local memory (markdown+git)")
    sub = p.add_subparsers(dest="cmd", required=True)

    pa = sub.add_parser("add", help="add a memory")
    pa.add_argument("--type", required=True, help=f"one of: {', '.join(TYPES)}")
    pa.add_argument("--scope", required=True, help="global or project:<slug>")
    pa.add_argument("--summary", required=True, help="one-line summary")
    pa.add_argument("--body", help="body (or pipe it on stdin)")
    pa.add_argument("--confidence", default="1.0")
    pa.add_argument("--source", default="manual")
    pa.add_argument("--files", help="comma-separated file paths this memory relates to")
    pa.add_argument("--no-redact", action="store_true", help="keep secret values verbatim (redacted by default)")
    pa.add_argument("--no-dup-check", action="store_true",
                    help="skip the semantic warning about a similar existing memory")
    pa.add_argument("--critical", action="store_true",
                    help="critical action rule: ALWAYS injected, first, regardless of the budget")
    pa.add_argument("--working", action="store_true",
                    help="scratch note: status=working — NOT injected, hidden from default search/list "
                         "until `mem.py promote <id>`")
    pa.add_argument("--supersedes", metavar="ID",
                    help="id of the memory this one revises — retired in the same write (never deleted)")
    pa.add_argument("--tier", choices=TIERS,
                    help="egress class: open (default) | redacted (summary only reaches an agent) "
                         "| private (never reaches an agent; still yours to search and read)")
    pa.add_argument("--protected", action="store_true",
                    help="protect this memory from being modified or deleted by agents")
    pa.add_argument("--session", help="session id provenance (default: auto-stamped from the current session)")
    pa.set_defaults(func=cmd_add)

    pl = sub.add_parser("list", help="list memories")
    pl.add_argument("--scope")
    pl.add_argument("--type")
    pl.add_argument("--status", default="active", help="active|superseded|all")
    pl.add_argument("--since", help="created on/after (YYYY-MM-DD or 'YYYY-MM-DD HH:MM')")
    pl.add_argument("--until", help="created on/before (YYYY-MM-DD or 'YYYY-MM-DD HH:MM')")
    pl.add_argument("--json", action="store_true", help="JSON output (for tooling/tests)")
    pl.add_argument("--dest", choices=(DEST_LOCAL, DEST_AGENT), default=DEST_LOCAL,
                    help="agent: apply the egress tiers (drop private, summary-only for redacted) "
                         "— what any surface feeding a model must pass")
    pl.add_argument("--dedup", action="store_true",
                    help="drop near-duplicates (same type+scope), keeping the newest of each cluster")
    pl.set_defaults(func=cmd_list)

    ps = sub.add_parser("search", help="search memories (FTS5 ranked)")
    ps.add_argument("query")
    ps.add_argument("--scope")
    ps.add_argument("--type")
    ps.add_argument("--since", help="created on/after (YYYY-MM-DD or 'YYYY-MM-DD HH:MM')")
    ps.add_argument("--until", help="created on/before (YYYY-MM-DD or 'YYYY-MM-DD HH:MM')")
    ps.add_argument("--no-semantic", action="store_true",
                    help="keyword-only (skip the semantic embedder even if it is available)")
    ps.add_argument("--dedup", action="store_true",
                    help="suppress near-duplicate hits (opt-in: see `dedup_near` for why not by default)")
    ps.set_defaults(func=cmd_search)

    pp = sub.add_parser("supersede", help="mark a memory as superseded (records when + why; never deletes)")
    pp.add_argument("id")
    pp.add_argument("--by", help="id of the record that replaces it")
    pp.add_argument("--reason", help="why it is no longer valid (kept for the audit trail)")
    pp.set_defaults(func=cmd_supersede)

    ppr = sub.add_parser("promote", help="promote a working note to a durable memory (working -> active)")
    ppr.add_argument("id")
    ppr.set_defaults(func=cmd_promote)

    pg = sub.add_parser("get", help="show one memory, body line-numbered; ref 'id', 'id:5' or 'id:5-9'")
    pg.add_argument("ref", help="record id, optionally with a line range: <id>[:<line>[-<line>]] (@ ok)")
    pg.set_defaults(func=cmd_get)

    pr = sub.add_parser("propose", help="queue a candidate for human review (NOT written to the store)")
    pr.add_argument("--type", required=True, help=f"one of: {', '.join(TYPES)}")
    pr.add_argument("--scope", required=True, help="global or project:<slug>")
    pr.add_argument("--summary", required=True, help="one-line summary")
    pr.add_argument("--body", help="body (or pipe it on stdin)")
    pr.add_argument("--confidence", default="0.8")
    pr.add_argument("--source", default="claude:live")
    pr.add_argument("--no-redact", action="store_true", help="keep secret values verbatim (redacted by default)")
    pr.set_defaults(func=cmd_propose)

    pu = sub.add_parser("audit", help="report secret-like or injection-like patterns in the store (read-only)")
    pu.add_argument("--scope")
    pu.set_defaults(func=cmd_audit)

    pan = sub.add_parser("anchor", help="set or clear the files a memory is about (feeds `drift`)")
    pan.add_argument("id")
    pan.add_argument("files", help="comma-separated paths relative to the project, or '-' to clear")
    pan.set_defaults(func=cmd_anchor)

    pd = sub.add_parser("drift", help="memories whose anchored files moved on (report only, changes nothing)")
    pd.add_argument("--scope")
    pd.add_argument("--commits", type=int, default=None,
                    help="commits since the memory was written that count as churn (default 10, MEM_DRIFT_COMMITS)")
    pd.set_defaults(func=cmd_drift)

    pt = sub.add_parser("tier", help="set a memory's egress class (what may reach a model)")
    pt.add_argument("id")
    pt.add_argument("tier", choices=TIERS)
    pt.set_defaults(func=cmd_tier)

    pn = sub.add_parser("pin", help="mark a memory as a critical rule (always injected, first)")
    pn.add_argument("id")
    pn.set_defaults(func=cmd_pin)

    pf = sub.add_parser("unpin", help="remove the critical-rule mark")
    pf.add_argument("id")
    pf.set_defaults(func=cmd_unpin)

    pk = sub.add_parser("link", help="link a memory to related ones (related-to)")
    pk.add_argument("id")
    pk.add_argument("others", nargs="+", help="id(s) of related memories")
    pk.set_defaults(func=cmd_link)

    puk = sub.add_parser("unlink", help="remove related-to link(s)")
    puk.add_argument("id")
    puk.add_argument("others", nargs="+", help="id(s) to unlink")
    puk.set_defaults(func=cmd_unlink)

    pb = sub.add_parser("block", help="mark a todo as blocked by other work (blocked-by)")
    pb.add_argument("todo", help="the blocked todo id")
    pb.add_argument("blockers", nargs="+", help="id(s) that must be done first")
    pb.set_defaults(func=cmd_block)

    pub = sub.add_parser("unblock", help="remove blocker(s) from a todo (no id = clear all)")
    pub.add_argument("todo")
    pub.add_argument("blockers", nargs="*", help="id(s) to remove; empty = clear all")
    pub.set_defaults(func=cmd_unblock)

    prd = sub.add_parser("ready", help="active todos with no open blocker (what to tackle now)")
    prd.add_argument("--scope")
    prd.set_defaults(func=cmd_ready)

    prs = sub.add_parser("resume", help="\"where was I?\" briefing: status + ready todos + recent")
    prs.add_argument("--scope", help="project:<slug> (omit for a cross-project overview)")
    prs.set_defaults(func=cmd_resume)

    psv = sub.add_parser("serve", help="start the web UI (pure Python, no PHP)")
    psv.add_argument("--port", type=int, help="port (default: MEM_WEB_PORT or 8841)")
    psv.add_argument("--host", default="127.0.0.1")
    psv.set_defaults(func=cmd_serve)

    pmc = sub.add_parser("mcp", help="start the MCP server (stdio) — pull memory from any MCP runtime (Claude/Gemini/Cursor/OpenCode)")
    pmc.set_defaults(func=cmd_mcp)

    pss = sub.add_parser("sessions",
                         help="full-text search your PAST CONVERSATIONS (raw transcripts; zero LLM cost)")
    pss.add_argument("query", nargs="?", help="text to find in past conversation messages")
    pss.add_argument("--project", help="limit to sessions whose working-dir folder has this name")
    pss.add_argument("--limit", type=int, default=20)
    pss.add_argument("--list", action="store_true", help="list recent sessions instead of searching")
    pss.add_argument("--reindex", action="store_true", help="rebuild the session index from scratch")
    pss.set_defaults(func=cmd_sessions)

    px = sub.add_parser("reindex", help="rebuild the derived FTS5 index (+ embeddings if Ollama is up)")
    px.set_defaults(func=cmd_reindex)

    pe = sub.add_parser("embed", help="build/refresh the optional semantic index (needs Ollama + embed model)")
    pe.add_argument("--force", action="store_true", help="re-embed everything, not just changed records")
    pe.set_defaults(func=cmd_embed)

    pla = sub.add_parser("log-access", help="log an access event for a memory")
    pla.add_argument("--ids", required=True, help="comma-separated list of memory IDs")
    pla.add_argument("--action", default="inject", help="inject|get|search")
    pla.set_defaults(func=cmd_log_access)

    pco = sub.add_parser("consolidate", help="consolidate duplicate memories on a branch")
    pco.add_argument("--llm", action="store_true", help="use local LLM for merging instead of deterministic fallback")
    pco.add_argument("--no-semantic", action="store_true", help="use lexical matching instead of embeddings")
    pco.set_defaults(func=cmd_consolidate)

    a = p.parse_args()
    try:
        a.func(a)
    except ValueError as e:   # invalid input (e.g. bad scope) -> clean message, not a traceback
        sys.exit(str(e))


if __name__ == "__main__":
    main()
