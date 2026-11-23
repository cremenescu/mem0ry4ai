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


# What an access row means. PUSHED: the system put the memory in front of the agent, unasked --
# at session start (inject), next to a prompt (prompt), in a project's brief (brief), because a
# tool touched the file it is anchored to (file), or as a digest whose moment came (digest). READ:
# something asked for it. Only a read says the memory was wanted.
PUSHED_ACTIONS = ("inject", "prompt", "brief", "file", "digest")
READ_ACTIONS = ("search", "get")

_ACCESS_READY = False


def _access_con():
    """Open .access.db with its schema in place (once per process).

    `session` came later than the table: without it an injection cannot be tied to the session it
    happened in, so "was what we pushed ever used?" has no answer. An older file gains the column
    here. `prompt_log` records one row per prompt the recall hook looked at -- sizes, counts and the
    outcome, never the text: the prompt is the user's, and the transcript already holds it."""
    global _ACCESS_READY
    path = access_db_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    con = sqlite3.connect(path, timeout=5.0)
    if not _ACCESS_READY:
        con.execute("CREATE TABLE IF NOT EXISTS access (id TEXT, action TEXT, ts TEXT, session TEXT)")
        if "session" not in {row[1] for row in con.execute("PRAGMA table_info(access)")}:
            try:
                con.execute("ALTER TABLE access ADD COLUMN session TEXT")
            except sqlite3.OperationalError:
                pass                       # a concurrent process added it between the two statements
        con.execute("CREATE INDEX IF NOT EXISTS access_session ON access (session, action)")
        con.execute("CREATE TABLE IF NOT EXISTS prompt_log (ts TEXT, session TEXT, chars INTEGER, "
                    "terms INTEGER, candidates INTEGER, injected INTEGER, top_score REAL, "
                    "ms INTEGER, outcome TEXT)")
        con.execute("CREATE TABLE IF NOT EXISTS session_reset (session TEXT PRIMARY KEY, ts TEXT)")
        # Which sessions' prompts used which word -- only words that already occur in the store.
        # See recall.py: a word spread across most sessions is conversation, not a topic.
        con.execute("CREATE TABLE IF NOT EXISTS prompt_vocab (term TEXT, session TEXT, "
                    "PRIMARY KEY (term, session)) WITHOUT ROWID")
        # What a session has already been briefed on -- a project's brief is given once, when the
        # session first touches the project (brief.py). `kind` leaves room for other things that
        # are said once.
        con.execute("CREATE TABLE IF NOT EXISTS session_seen (session TEXT, kind TEXT, key TEXT, "
                    "ts TEXT, PRIMARY KEY (session, kind, key)) WITHOUT ROWID")
        # What agents searched for (see log_search). The one table here that keeps a text.
        con.execute("CREATE TABLE IF NOT EXISTS search_log (ts TEXT, session TEXT, source TEXT, "
                    "query TEXT, mode TEXT, total INTEGER, shown INTEGER)")
        con.commit()
        _ACCESS_READY = True
    return con


def log_access(rec_ids, action, session=None):
    """Log an access event for one id or several (one transaction). Never raises: a memory that
    could not be counted must still be served."""
    ids = [rec_ids] if isinstance(rec_ids, str) else [i for i in rec_ids if i]
    if not ids:
        return
    try:
        con = _access_con()
        try:
            ts = now_ts()
            con.executemany("INSERT INTO access (id, action, ts, session) VALUES (?, ?, ?, ?)",
                            [(i, action, ts, session) for i in ids])
            con.commit()
        finally:
            con.close()
    except Exception:
        pass


def log_search(query, total, shown, mode, source, session=None):
    """One row per search an agent ran: what it asked for, and how much came back.

    The access log says which memories a search SHOWED. Nothing said what was being looked for,
    and that is what shows where the store is asked for something it does not hold: a query whose
    results nobody went on to open. A prompt is never stored here -- it is the user's. A query is
    the agent's own wording, so it is kept: redacted like anything written to the store, cut to
    200 characters, and not at all when MEM_SEARCH_LOG=0. Never raises."""
    if (os.environ.get("MEM_SEARCH_LOG", "1") or "").strip().lower() in ("0", "no", "false", "off"):
        return
    try:
        text = " ".join((query or "").split())
        if redact.enabled():
            text = redact.redact(text)[0]
        con = _access_con()
        try:
            con.execute("INSERT INTO search_log (ts, session, source, query, mode, total, shown) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (now_ts(), session, source, text[:200], mode, int(total), int(shown)))
            con.commit()
        finally:
            con.close()
    except Exception:
        pass


def mark_session_reset(session):
    """The session's context was rebuilt (compaction, /clear): what was pushed before is gone from
    it, so the per-prompt recall may show those memories again."""
    if not session:
        return
    try:
        con = _access_con()
        try:
            con.execute("INSERT OR REPLACE INTO session_reset (session, ts) VALUES (?, ?)",
                        (session, now_ts()))
            con.commit()
        finally:
            con.close()
    except Exception:
        pass


def pushed_this_session(session):
    """Ids already put in front of the agent in this session, since its context was last rebuilt."""
    if not session:
        return set()
    try:
        con = _access_con()
        try:
            row = con.execute("SELECT ts FROM session_reset WHERE session = ?", (session,)).fetchone()
            since = row[0] if row else ""
            marks = ",".join("?" * len(PUSHED_ACTIONS))
            rows = con.execute(f"SELECT DISTINCT id FROM access WHERE session = ? AND ts >= ? "
                               f"AND action IN ({marks})", (session, since) + PUSHED_ACTIONS).fetchall()
        finally:
            con.close()
        return {r[0] for r in rows}
    except Exception:
        return set()


def seen_this_session(session, kind, keys):
    """Which of `keys` this session was already briefed on, since its context was last rebuilt.

    Asked after every tool call, by a process that nearly always has nothing else to do -- so it
    only reads: no schema set-up, and a database that is not there yet stays not there."""
    keys = [k for k in keys if k]
    path = access_db_path()
    if not session or not keys or not os.path.exists(path):
        return set()
    try:
        con = sqlite3.connect(path, timeout=2.0)
        try:
            row = con.execute("SELECT ts FROM session_reset WHERE session = ?", (session,)).fetchone()
            since = row[0] if row else ""
            marks = ",".join("?" * len(keys))
            rows = con.execute(f"SELECT key FROM session_seen WHERE session = ? AND kind = ? "
                               f"AND ts >= ? AND key IN ({marks})",
                               [session, kind, since] + keys).fetchall()
        finally:
            con.close()
        return {r[0] for r in rows}
    except Exception:
        return set()            # a table from before this existed: nothing was briefed yet


def claim_seen(session, kind, key):
    """Mark `key` as given to this session. True only for the call that made the mark.

    The answer decides who speaks. Tool calls of one turn run in parallel, each with its own hook
    process, and two of them touching the same project must not both brief it: the insert is the
    arbiter. A mark older than the session's last context reset is taken over instead of obeyed --
    what it stood for is no longer in the context."""
    if not session or not key:
        return False
    try:
        con = _access_con()
        try:
            row = con.execute("SELECT ts FROM session_reset WHERE session = ?", (session,)).fetchone()
            since = row[0] if row else ""
            cur = con.execute(
                "INSERT INTO session_seen (session, kind, key, ts) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (session, kind, key) DO UPDATE SET ts = excluded.ts "
                "WHERE session_seen.ts < ?", (session, kind, key, now_ts(), since))
            con.commit()
            return cur.rowcount > 0
        finally:
            con.close()
    except Exception:
        return False


def get_last_accessed():
    """{rec_id: when it was last READ} -- searched for or opened, not merely injected.

    Injection orders memories by this, so counting an injection as an access made the order feed
    itself: whatever was shown last session looked "recently used" this session and was shown
    again, ahead of every older memory nobody had been offered. Being pushed is not being wanted."""
    path = access_db_path()
    if not os.path.exists(path):
        return {}
    try:
        con = sqlite3.connect(path, timeout=5.0)
        try:
            marks = ",".join("?" * len(READ_ACTIONS))
            rows = con.execute(f"SELECT id, MAX(ts) FROM access WHERE action IN ({marks}) GROUP BY id",
                               READ_ACTIONS).fetchall()
        finally:
            con.close()
        return {row[0]: row[1] for row in rows}
    except Exception:
        return {}


def opened_counts():
    """{rec_id: how many times an agent opened the memory in full} -- the `get` rows of the access
    log. Being listed by a search is not counted: a title that was shown is not a text that was
    read. Never raises."""
    path = access_db_path()
    if not os.path.exists(path):
        return {}
    try:
        con = sqlite3.connect(path, timeout=5.0)
        try:
            return dict(con.execute("SELECT id, count(*) FROM access WHERE action = 'get' GROUP BY id"))
        finally:
            con.close()
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


def git_identity(who):
    """Commit identity passed inline, never taken from the machine's global git config.

    Every store commit in this project captures its output, so a machine with no `user.email` --
    any fresh container, VM or CI runner -- does not fail loudly. It fails silently, and the store
    quietly stops accumulating history while everything else keeps working perfectly. Two of the
    four commit paths shipped without an inline identity and it was only found by installing into
    a clean container.

    Signing is force-disabled for the same class of reason: `commit.gpgsign=true` with no local
    override hangs an unattended commit on a passphrase prompt that nobody is there to answer.
    """
    return ["-c", "commit.gpgsign=false",
            "-c", f"user.name=mem0ry4ai {who}",
            "-c", f"user.email={who}@mem0ry4ai.local"]


_STORE_REPO = None


def ensure_store_repo():
    """Make DATA a git repository, unless it already is one or already lives inside one.

    git is not decoration here: the project's first claim is that markdown plus git IS the source
    of truth, and supersede-never-delete only means something if the history exists to look back
    at. But `git init` lived solely in the SessionEnd hook, so any install that never runs that
    hook -- a container, a VM, a server, anyone using only the CLI or only the web UI -- had no
    repository at all. Nothing reported it, because every commit path swallows its output; the
    store simply had no history and looked fine.

    The `rev-parse` check is the part that must not be skipped. In a git-clone install the store
    sits INSIDE the project's own clone, and initialising a nested repository there would detach
    the store from the history it already had -- turning a doc bug into data loss.
    """
    global _STORE_REPO
    if _STORE_REPO is not None:
        return _STORE_REPO
    try:
        if os.path.isdir(os.path.join(DATA, ".git")):
            _STORE_REPO = True
            return True
        r = subprocess.run(["git", "-C", DATA, "rev-parse", "--is-inside-work-tree"],
                           capture_output=True, text=True, creationflags=_NO_WINDOW)
        if r.returncode == 0 and r.stdout.strip() == "true":
            _STORE_REPO = True             # already tracked by an enclosing repo; leave it alone
            return True
        subprocess.run(["git", "init", "-q", DATA], capture_output=True, timeout=10,
                       creationflags=_NO_WINDOW)
        gi = os.path.join(DATA, ".gitignore")
        if not os.path.exists(gi):
            with open(gi, "w", encoding="utf-8") as f:
                # One glob for every database under store/, the ones that exist and the ones
                # added later: derived indexes and local logs. A name missing from a list here
                # is a binary the checkpoint commits into the store's history at every
                # session end.
                f.write("staging/\nstore/.*.db*\n.web-server.pid\n.web-server.log\n")
        _STORE_REPO = os.path.isdir(os.path.join(DATA, ".git"))
    except Exception:
        _STORE_REPO = False                # no git on this box: everything else still works
    return _STORE_REPO


def ensure_header(path, scope):
    """Create the scope file with a header if it does not exist yet."""
    # Every write goes through here, which makes it the one place that can guarantee the store is
    # version-controlled before anything lands in it.
    ensure_store_repo()
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
                  priority=None, files=None, protected=None, session=None, tier=None,
                  digest_of=None, when=None):
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
    if digest_of:
        lines.append(f"- digest-of: {digest_of}")
    if when:
        lines.append(f"- when: {when}")
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
    for k in ("superseded-by", "invalidated", "invalid-reason", "priority", "mirrored-in", "files",
              "digest-of", "when", "related-to", "blocked-by", "distinct-from", "protected", "tier",
              "session"):
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


def _body_start(lines, r):
    """Index of the first line of record `r` that is no longer meta: the blank line that ends its
    meta block, the boundary `parse_file` draws. Past it a line is text, whatever it looks like.

    The line-level editors below walk a record's lines looking for `- key: value`. A bullet in a
    body can read exactly like that (`- status: draft`, a record quoted as an example), and an
    editor that took it for meta would rewrite or drop a line of the memory's own text."""
    seen = False
    for k in range(r["start"], r["end"] + 1):
        text = lines[k].rstrip("\n")
        if META_RE.match(text):
            seen = True
        elif text == "" and seen:
            return k
    return r["end"] + 1


def _meta_slot(lines, r, body_at):
    """Index of the line of record `r` before which an editor writes its own lines: the one after
    the record's `- status:` line.

    A record written or edited by hand may carry no such line (the parser reads it as active). An
    editor that waits for it writes nothing and still returns the path it "wrote": the pin, the
    tier or the retirement is reported and never happens. On such a record the lines go at the end
    of the meta block, after its last line -- and under the title on a record that has no meta
    line at all. `body_at` is where that block ends (see `_body_start`): a `- status: ...` bullet
    in the memory's own text is not the line looked for."""
    slot, titled = r["start"] + 1, False
    for k in range(r["start"] + 1, min(body_at, r["end"])):
        text = lines[k].rstrip("\n")
        mm = META_RE.match(text)
        if mm and mm.group("k") == "status":
            return k + 1
        if mm:
            slot = k + 1
        elif not titled and TITLE_RE.match(text):
            slot, titled = k + 1, True
    return slot


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


def _supersede_prior_status(scope, new_id, named=False):
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

    A status written with `supersedes=` has said which one it continues, and `add_memory` has
    retired that one by name. Run after that, the rule would take the next most recent as well:
    two retired by one write, the second named by nobody. So with `named` nothing is retired here
    and the statuses still live are only counted.

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
    if named:   # the one it continues is retired already, by name
        return [], [], len(live)
    live.sort(key=lambda nr: ((nr[1]["meta"].get("created") or ""), nr[0]), reverse=True)
    target = live[0][1]
    try:
        supersede_memory(target["id"], by=new_id, reason="superseded by a newer status")
        return [target["id"]], [], len(live) - 1
    except ValueError:
        return [], [target["id"]], len(live) - 1   # protected — the user asked for it to stay put


def add_memory(rtype, scope, summary, body, confidence="1.0", source="web", redact_secrets=True,
               status="active", protected=None, session=None, on_supersede=None,
               priority=None, files=None, tier=None, supersedes=None, digest_of=None, when=None):
    """Append a fresh record; returns its id. Redacts secrets like every write path.
    status="working" makes it a scratch note: not injected at SessionStart, hidden from default
    search/list — until promote_memory() flips it to active.
    A new active `status` retires the previous one for its scope (see `_supersede_prior_status`),
    unless `supersedes` names the live status of that scope it continues: then that one alone is
    retired. `on_supersede(done, skipped, older)` is called with what was done and how many
    statuses of the scope are still live, for callers that report it.

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
    if files is None:
        files = derive_files(scope, summary, body) or None
    else:
        files = normalize_anchors(scope, files) or None
    if session is None:
        session = current_session()   # provenance: stamp the session that produced this memory
    if when:
        when = " ".join(str(when).split())
        parse_when(when)              # a clause it does not know, or a bad regex: refuse the write
    if digest_of:
        digest_of = ", ".join(i.strip() for i in str(digest_of).split(",") if i.strip())
    rec = render_record(rid, rtype, scope, summary, body, confidence, source, now_ts(), now_ts(), status,
                        priority=priority, files=files, protected=protected, session=session,
                        tier=tier, digest_of=digest_of or None, when=when or None)
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
            # Carry the graph forward. A revision continues the memory's role, so its relations are
            # about the successor too — and dropping them is silent: the edge stays visible on the
            # retired record, which reads as "still linked" while the live one has no link at all.
            # Caught the first time this argument was used in anger: a todo was revised minutes
            # after being linked to the gotcha that corrected it, and the new todo came out isolated.
            # Inherited rather than moved, because the old record's history should stay readable.
            inherited = []
            for key in ("related-to", "blocked-by"):
                vals = _list_meta(target, key)
                if vals:
                    _edit_list_meta(rid, key, add=vals)
                    inherited += vals
            # Same lock as the append: the revision and the retirement land together or not at all.
            supersede_memory(supersedes, by=rid, reason=f"revised by {rid}")
        # Under the same (reentrant) lock, so no concurrent writer can leave two live statuses.
        if rtype == "status" and status == "active":
            # A status that named the live status of its own scope has retired it just above. A
            # revision of anything else leaves the rule as it is: the previous status of the
            # scope is still live, and still the one this status continues.
            named = bool(supersedes) and (target["meta"].get("type") == "status"
                                          and target["meta"].get("scope") == scope)
            done, skipped, older = _supersede_prior_status(scope, rid, named=named)
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
        for key in ("related-to", "blocked-by", "distinct-from"):
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
            if "status" not in r["meta"]:
                inserts.append("- status: superseded\n")   # there is no line to rewrite: write one
            if by:
                inserts.append(f"- superseded-by: {by}\n")
            inserts.append(f"- invalidated: {now_ts()}\n")
            if reason:
                inserts.append(f"- invalid-reason: {reason}\n")
            new_block, body_at = [], _body_start(lines, r)
            slot = _meta_slot(lines, r, body_at)
            for k in range(r["start"], r["end"] + 1):
                line = lines[k]
                if k == slot:
                    new_block.extend(inserts)
                mm = META_RE.match(line.rstrip("\n")) if k < body_at else None
                if mm and mm.group("k") in ("superseded-by", "invalidated", "invalid-reason"):
                    continue  # drop old copies; rewritten at the slot
                if mm and mm.group("k") == "status":
                    line = "- status: superseded\n"
                elif mm and mm.group("k") == "updated":
                    line = f"- updated: {now_ts()}\n"
                new_block.append(line)
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
            new_block, body_at = [], _body_start(lines, r)
            for k in range(r["start"], r["end"] + 1):
                line = lines[k]
                mm = META_RE.match(line.rstrip("\n")) if k < body_at else None
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


@_locked_write
def _edit_meta_lines(rec_id, edit):
    """One pass over a record's meta lines, in place: `edit(key, value)` returns the value the line
    should have, or None to drop the line. A line whose value does not change is left byte for
    byte, and nothing past the meta block is looked at (see `_body_start`). Returns the relpath
    written, or None when the id is not in the store."""
    for path in store_files():
        for r in parse_file(path):
            if r["id"] != rec_id:
                continue
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            new_block, body_at = [], _body_start(lines, r)
            for k in range(r["start"], r["end"] + 1):
                line = lines[k]
                mm = META_RE.match(line.rstrip("\n")) if k < body_at else None
                if mm:
                    value = edit(mm.group("k"), mm.group("v").strip())
                    if value is None:
                        continue
                    if value != mm.group("v").strip():
                        line = f"- {mm.group('k')}: {value}\n"
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

def summary_note(summary):
    """A nudge, never a refusal. The summary is the ONE line that search results, the SessionStart
    injection and the per-prompt recall show; the median had drifted to 185 characters, which is a
    paragraph, and every surface that lists memories fits fewer of them because of it."""
    try:
        limit = int(os.environ.get("MEM_SUMMARY_WARN", "120"))
    except (TypeError, ValueError):
        limit = 120
    n = len(summary or "")
    if limit <= 0 or n <= limit:
        return ""
    return (f"note: the summary is {n} chars. It is the one line search results and the injection "
            f"show — lead with the searchable fact, leave detail to the body "
            f"(aim for <= {limit}; mute: MEM_SUMMARY_WARN=0).")


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
        digest_of=getattr(a, "digest_of", None), when=getattr(a, "when", None),
        session=(getattr(a, "session", None) or current_session()),
        on_supersede=lambda done, skipped, older: retired.extend(
            [("retired", i) for i in done] + [("protected", i) for i in skipped]
            + ([("older", str(older))] if older else [])))
    tag = "  [working]" if status == "working" else ("  [CRITICAL]" if a.critical else "")
    print(f"added {rid}  [{a.type} · {a.scope}]{tag}  -> {os.path.relpath(path, DATA)}")
    if getattr(a, "supersedes", None):
        print(f"  retired {a.supersedes} (revised by {rid}; superseded, not deleted)")
        new = get_record(rid)
        carried = (_list_meta(new, "related-to") + _list_meta(new, "blocked-by")) if new else []
        if carried:
            print(f"  carried its relations forward: {', '.join(carried)}")
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
    if summary_note(summary):
        print(summary_note(summary), file=sys.stderr)
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


def _match_anchor(rec, path):
    """Whether a record's `files:` anchor covers `path`.

    Matching is on whole path components, never raw substrings: 'session_start.py' finds
    'hooks/session_start.py' but not 'not_session_start.py'. A trailing slash means directory --
    'hooks/' finds everything anchored under it. That is the one place a prefix is honoured, and it
    is honoured only because the trailing slash makes the intent unambiguous; a bare 'hooks' still
    means the file named hooks, not the folder."""
    if not path:
        return True
    raw = path.strip()
    anchors = [x.strip() for x in (rec["meta"].get("files") or "").split(",") if x.strip()]
    if raw.endswith("/"):
        pref = raw.strip("/") + "/"
        return any(f == pref[:-1] or f.startswith(pref) for f in anchors)
    want = raw.strip("/")
    for f in anchors:
        if f == want or f.endswith("/" + want) or want.endswith("/" + f):
            return True
    return False


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
    recs = [r for r in all_records() if _match_filters(r, a.scope, a.type, a.status, since, until)
            and _match_anchor(r, getattr(a, "files", None))]
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
            "mirrored_in": r["meta"].get("mirrored-in"),
            "when": r["meta"].get("when"), "digest_of": r["meta"].get("digest-of"),
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
    # A plain table beside the full-text one: the scope of each record and the files it is
    # anchored to. brief.py asks for these after every tool call, and the store cannot answer at
    # that rate -- parsing 3,300 records takes 200 ms.
    con.execute("CREATE TABLE meta (id TEXT PRIMARY KEY, scope TEXT, status TEXT, files TEXT, "
                "trig TEXT)")
    for r in all_records():
        files = r["meta"].get("files", "")
        body = r["body"] + (("\nfiles: " + files) if files else "")  # make file paths searchable
        con.execute("INSERT INTO mem (id, summary, body) VALUES (?, ?, ?)",
                    (r["id"], record_summary(r), body))
        con.execute("INSERT OR REPLACE INTO meta (id, scope, status, files, trig) VALUES (?, ?, ?, ?, ?)",
                    (r["id"], r["meta"].get("scope", ""), r["meta"].get("status", "active"), files,
                     r["meta"].get("when", "")))
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
                and _match_filters(by_id[i], a.scope, a.type, "all", since, until)
                and _match_anchor(by_id[i], getattr(a, "files", None))]
        dropped = []
        if getattr(a, "dedup", False):
            hits, dropped = dedup_near(hits)   # ranked order: the most relevant of a cluster survives
        _print_mode(mode, len(hits))
        # Terms are OR-ed, so an ordinary query matches most of the store: 1166 "hits" was a normal
        # answer, and the caller piped it through `head`. Bounded here instead, which also makes
        # "what was shown" a fact that can be logged -- the MCP search always logged its hits, the
        # CLI never did, and three reads in four go through the CLI.
        limit = getattr(a, "limit", 10)
        total = len(hits)
        if limit and limit > 0:
            hits = hits[:limit]
        _print_hits(hits)
        if total > len(hits):
            print(f"(showing the top {len(hits)} of {total} — `--limit N` for more, `--limit 0` for all)")
        log_access([r["id"] for r in hits], "search", current_session())
        log_search(a.query, total, len(hits), mode, "cli", current_session())
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
    log_search(q, len(hits), len(hits), "substring", "cli", current_session())


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
    if force:
        # --force is how a change of embedding model is applied; the vectors kept aside for an undo
        # (see `_vectors_aside`) came from the model being replaced, and one put back later would
        # sit among the new ones looking current, since nothing but the text's hash is recorded
        con.execute("DROP TABLE IF EXISTS emb_aside")
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
    sim_sync()   # the pairs follow the vectors: the Duplicates page then opens on a table already built
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


# ----- duplicates: memories that say the same thing twice -----
#
# What the Duplicates page and `mem.py dups` read. Likeness comes from the vectors already in
# .embed.db; nothing in this section calls an embedder.

SIM_FLOOR = 0.60   # pairs less alike than this are not kept: nothing asks for them
_SIM_SCHEMA = ("CREATE TABLE IF NOT EXISTS sim (a TEXT, b TEXT, s REAL, PRIMARY KEY (a, b)) WITHOUT ROWID",
               "CREATE INDEX IF NOT EXISTS sim_b ON sim (b)",
               "CREATE TABLE IF NOT EXISTS sim_seen (id TEXT PRIMARY KEY, hash TEXT)")
# math.sumprod (Python 3.12+) is about five times the generator; the result is the same number.
_dot = getattr(math, "sumprod", None) or (lambda a, b: sum(x * y for x, y in zip(a, b)))


def sim_sync(budget=None):
    """Bring the table of alike pairs in step with the vectors in .embed.db.

    Every pair of vectors is compared once and the result kept, so a page that asks "which memories
    say the same thing" reads a small table instead of redoing n*n/2 dot products on every load --
    which is what made the Links page take half a minute and look only at its first 1,500 vectors.
    A vector is compared when it is new or when its text changed (the `hash` stored beside it),
    against every vector compared before it; a vector that is gone takes its pairs with it.

    `budget` is a number of seconds after which the work stops where it is, to be picked up by the
    next call; None finishes. Returns (compared now, still waiting). Never raises: this is derived
    data, and a caller that only wanted to show a page must still get its page."""
    p = embed_path()
    if not os.path.exists(p):
        return (0, 0)
    t0 = time.monotonic()
    done = waiting = 0
    con = None
    try:
        con = sqlite3.connect(p, timeout=5)
        for stmt in _SIM_SCHEMA:
            con.execute(stmt)
        have = dict(con.execute("SELECT id, hash FROM emb"))
        seen = dict(con.execute("SELECT id, hash FROM sim_seen"))
        for i in [i for i in seen if i not in have or have[i] != seen[i]]:
            # gone, or re-embedded because its text changed: what was known about it is about
            # another text, and keeping it would offer two memories for what one of them used to say
            con.execute("DELETE FROM sim WHERE a = ? OR b = ?", (i, i))
            con.execute("DELETE FROM sim_seen WHERE id = ?", (i,))
            del seen[i]
        con.commit()
        todo = [i for i in have if i not in seen]
        waiting = len(todo)
        if not todo:
            return (0, 0)
        vecs = {}
        for i, blob in con.execute("SELECT id, vec FROM emb"):
            v = struct.unpack("<" + f"{len(blob) // 4}f", blob)
            n = math.sqrt(_dot(v, v))
            if n:
                vecs[i] = [x / n for x in v]
        compared = [i for i in seen if i in vecs]
        for i in todo:
            if budget is not None and done and time.monotonic() - t0 > budget:
                break
            vi, rows = vecs.get(i), []
            if vi is not None:
                for j in compared:
                    vj = vecs[j]
                    if len(vj) != len(vi):
                        continue   # two embedding models in one table: not comparable
                    s = _dot(vi, vj)
                    if s >= SIM_FLOOR:
                        rows.append((i, j, s) if i < j else (j, i, s))
                compared.append(i)
            con.executemany("INSERT OR REPLACE INTO sim (a, b, s) VALUES (?, ?, ?)", rows)
            con.execute("INSERT OR REPLACE INTO sim_seen (id, hash) VALUES (?, ?)", (i, have[i]))
            done += 1
            waiting -= 1
            if done % 50 == 0:
                con.commit()   # an interrupted first build keeps what it has done
        con.commit()
    except sqlite3.Error:
        pass
    finally:
        if con is not None:
            con.close()
    return (done, waiting)


def duplicates_threshold():
    """How alike two memories must be to be offered as duplicates (MEM_DUPS_THRESHOLD, 0.80)."""
    return max(SIM_FLOOR, min(_float_env("MEM_DUPS_THRESHOLD", "0.80"), 0.99))


def _apart_pairs(records, raw=None):
    """The pairs someone looked at and said are not duplicates (`distinct-from`, read both ways).

    A retired memory stands for the one that replaced it: if A was judged distinct from B and A was
    later revised into A2, the judgement is about what A says, and A2 says it now.

    `raw`, when given a dict, is filled with the ids each pair is written on -- A and B above, not
    A2 -- which is what taking the judgement back has to edit."""
    by = {r["id"]: r for r in records}

    def current(i):
        walked = set()
        while i in by and i not in walked:
            walked.add(i)
            m = by[i]["meta"]
            nxt = (m.get("superseded-by", "") or "").strip()
            if m.get("status", "active") != "superseded" or not nxt:
                break
            i = nxt
        return i

    out = set()
    for r in by.values():
        for other in _list_meta(r, "distinct-from"):
            a, b = current(r["id"]), current(other)
            if a != b:
                pair = (a, b) if a < b else (b, a)
                out.add(pair)
                if raw is not None:
                    raw.setdefault(pair, set()).update((r["id"], other))
    return out


def duplicate_groups(threshold=None, records=None):
    """Active memories of one type that say the same thing, as groups, the most alike first.

    A group is what a chain of alike pairs connects: five versions of one gotcha come out as one
    group of five, not as ten pairs. Each group is {"sim": its closest pair's likeness, "type": ...,
    "members": [records, newest first]}.

    Left out on purpose: a retired memory (it is settled already), a digest (it repeats its sources
    by design), two memories of different types (a decision and the status that reports it read
    alike and are not copies of each other), and a pair the user marked as distinct."""
    th = duplicates_threshold() if threshold is None else max(SIM_FLOOR, min(float(threshold), 0.99))
    p = embed_path()
    if not os.path.exists(p):
        return []
    try:
        con = sqlite3.connect(p, timeout=5)
        try:
            rows = con.execute("SELECT a, b, s FROM sim WHERE s >= ?", (th,)).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return []
    recs = {r["id"]: r for r in (records if records is not None else all_records())}

    def offered(i):
        r = recs.get(i)
        return bool(r) and r["meta"].get("status", "active") == "active" and not r["meta"].get("when")

    apart = _apart_pairs(recs.values())
    parent, edges = {}, []

    def find(x):
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b, s in rows:
        if not offered(a) or not offered(b):
            continue
        if recs[a]["meta"].get("type") != recs[b]["meta"].get("type"):
            continue
        if (a, b) in apart:
            continue
        edges.append((a, b, s))
        parent[find(a)] = find(b)
    groups = {}
    for a, b, s in edges:
        g = groups.setdefault(find(a), {"sim": 0.0, "ids": set()})
        g["ids"].update((a, b))
        g["sim"] = max(g["sim"], s)
    out = []
    for g in groups.values():
        members = sorted((recs[i] for i in g["ids"]),
                         key=lambda r: (r["meta"].get("created", ""), r["id"]), reverse=True)
        out.append({"sim": g["sim"], "type": members[0]["meta"].get("type", ""), "members": members})
    out.sort(key=lambda g: (-g["sim"], g["members"][0]["id"]))
    return out


@_locked_write
def mark_distinct(ids):
    """Record that these memories are not duplicates of one another, so they are not offered as a
    group again. Returns the number of pairs written.

    Written as `distinct-from` on the memories themselves: the judgement is the user's and belongs
    with the store, in git, not in a cache that a rebuild forgets. `updated` is left alone --
    deciding that two memories differ changes neither of them, and a status must not look
    freshly touched because someone compared it with another."""
    ids = list(dict.fromkeys(i for i in ids if i))
    known = {r["id"] for r in all_records()}
    missing = [i for i in ids if i not in known]
    if missing:
        raise ValueError(f"id not found: {', '.join(missing)}")
    if len(ids) < 2:
        raise ValueError("it takes at least two memories to be distinct from one another")
    n = 0
    for x in range(len(ids)):
        for y in range(x + 1, len(ids)):
            a, b = sorted((ids[x], ids[y]))
            _edit_list_meta(a, "distinct-from", add=[b], touch=False)
            n += 1
    return n


@_locked_write
def unmark_distinct(ids):
    """Take `mark_distinct` back for these memories: they may be offered together again."""
    ids = list(dict.fromkeys(i for i in ids if i))
    n = 0
    for x in ids:
        rec = get_record(x)
        mine = set(_list_meta(rec, "distinct-from")) & (set(ids) - {x}) if rec else set()
        if mine:
            _edit_list_meta(x, "distinct-from", remove=sorted(mine), touch=False)
            n += len(mine)
    return n


@_locked_write
def retire_duplicates(keep_id, other_ids):
    """Retire `other_ids` as copies of `keep_id`. Returns the ids retired.

    Each copy is superseded by the one that stays -- kept in the store, with when and why, so the
    text is one click away and git has the rest. What a copy was attached to moves to the one that
    stays: the memories it was related to, what blocked it and, within one scope, the files it was
    anchored to. Otherwise retiring the copy that happened to carry the anchor would quietly take
    the knowledge away from the file it is about.

    A critical rule is not retired in favour of a memory that is not one: what a copy was attached
    to moves, the pin does not, and the rule would stop being given without anything saying so.
    Keep the rule instead, or unpin it first.

    All or nothing: every id is checked before the first write, so a refusal leaves the store as it
    was instead of a group retired halfway."""
    others = [i for i in dict.fromkeys(other_ids) if i and i != keep_id]
    if not others:
        raise ValueError("nothing to retire")
    keep = get_record(keep_id)
    if not keep:
        raise ValueError(f"id not found: {keep_id}")
    if keep["meta"].get("status", "active") != "active":
        raise ValueError(f"the memory to keep is not active: {keep_id}")
    copies = []
    for i in others:
        r = get_record(i)
        if not r:
            raise ValueError(f"id not found: {i}")
        if r["meta"].get("status", "active") != "active":
            raise ValueError(f"not active, nothing to retire: {i}")
        if r["meta"].get("protected", "").strip().lower() in ("true", "yes", "1"):
            raise ValueError(f"cannot retire protected memory: {i}")
        if r["meta"].get("priority") == "critical" and keep["meta"].get("priority") != "critical":
            raise ValueError(f"cannot retire a critical rule in favour of a memory that is not one: {i}")
        copies.append(r)

    def anchors(r):
        return [f.strip() for f in (r["meta"].get("files", "") or "").split(",") if f.strip()]

    files, grew = anchors(keep), False
    for r in copies:
        for key in ("related-to", "blocked-by"):
            carried = [v for v in _list_meta(r, key) if v != keep_id and v not in others]
            if carried:
                _edit_list_meta(keep_id, key, add=carried)
        if r["meta"].get("scope") == keep["meta"].get("scope"):   # an anchor is a path inside its scope's project
            for f in anchors(r):
                if f not in files:
                    files.append(f)
                    grew = True
    if grew:
        _set_files(keep_id, ", ".join(files))
    if set(_list_meta(keep, "related-to")) & set(others):
        _edit_list_meta(keep_id, "related-to", remove=others)   # a link to its own retired copy says nothing
    for r in copies:
        supersede_memory(r["id"], by=keep_id, reason=f"duplicate of {keep_id}")
    _vectors_aside(others)
    _note_retirement(keep, copies)
    return others


# ----- taking a retirement back -----
#
# A retirement changes more than the copies: the memory that stays may be given links and anchors,
# may lose the link to its own copy, and gets a fresh `updated`. Taking it back exactly means
# knowing which of those were the kept memory's own, and the store cannot say -- a link it already
# had and a link it was given read the same. So each retirement leaves one line in staging/ about
# what it moved. That line is a note for undo, not knowledge: it stays out of git with the rest of
# staging/, and a retirement whose note is gone can still be taken back -- the kept memory then
# simply keeps what it was given.

def dups_journal_path():
    return os.path.join(DATA, "staging", "dups-undo.jsonl")


def _dups_journal():
    """The notes left by `retire_duplicates`, oldest first. A line that does not parse is skipped."""
    import json
    out = []
    try:
        with open(dups_journal_path(), encoding="utf-8") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if isinstance(d, dict) and d.get("keep"):
                    out.append(d)
    except OSError:
        pass
    return out


def _dups_journal_write(entries):
    """Replace the notes with `entries`, atomically. Best-effort: a staging/ that cannot be written
    costs a later undo its exactness, never the operation that is running."""
    import json
    p = dups_journal_path()
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p + ".tmp", "w", encoding="utf-8") as f:
            for e in entries:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
        os.replace(p + ".tmp", p)
        return True
    except OSError:
        return False


def _likeness(ids):
    """How alike the closest two of these memories are, as the table of alike pairs has it; None
    when it holds no pair among them (less alike than SIM_FLOOR, or one of them has no vector)."""
    ids = sorted(set(ids))
    p = embed_path()
    if len(ids) < 2 or not os.path.exists(p):
        return None
    marks = ",".join("?" * len(ids))
    try:
        con = sqlite3.connect(p, timeout=5)
        try:
            row = con.execute(f"SELECT max(s) FROM sim WHERE a IN ({marks}) AND b IN ({marks})",
                              ids + ids).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return None
    return row[0] if row else None


_ASIDE_SCHEMA = "CREATE TABLE IF NOT EXISTS emb_aside (id TEXT PRIMARY KEY, hash TEXT, vec BLOB)"


def _vectors_aside(ids):
    """Keep a copy of these memories' vectors as they are retired.

    The next embed run drops the vector of anything no longer active. A retirement taken back
    after that would return a memory that nothing is compared with until the embedder next runs,
    so the group it belongs to would not come back with it. Never raises: this is derived data."""
    p = embed_path()
    ids = [i for i in ids if i]
    if not ids or not os.path.exists(p):
        return
    try:
        con = sqlite3.connect(p, timeout=5)
        try:
            con.execute(_ASIDE_SCHEMA)
            con.execute("INSERT OR REPLACE INTO emb_aside (id, hash, vec) SELECT id, hash, vec FROM emb "
                        f"WHERE id IN ({','.join('?' * len(ids))})", ids)
            con.commit()
        finally:
            con.close()
    except sqlite3.Error:
        pass


def _vectors_back(records):
    """Give memories that are active again the vectors kept for them -- when the text is still the
    text that was embedded, and no vector has taken their place meanwhile. Never raises."""
    p = embed_path()
    records = [r for r in records if r]
    if not records or not os.path.exists(p):
        return
    try:
        con = sqlite3.connect(p, timeout=5)
        try:
            con.execute(_ASIDE_SCHEMA)
            for r in records:
                row = con.execute("SELECT hash, vec FROM emb_aside WHERE id = ?", (r["id"],)).fetchone()
                if row and row[0] == _rec_hash(r):
                    con.execute("INSERT OR IGNORE INTO emb (id, hash, vec) VALUES (?, ?, ?)", (r["id"], row[0], row[1]))
                con.execute("DELETE FROM emb_aside WHERE id = ?", (r["id"],))
            con.commit()
        finally:
            con.close()
    except sqlite3.Error:
        pass


def _note_retirement(keep, copies):
    """Leave the note `restore_duplicates` reads: what this retirement gave the kept memory, which
    of its links it dropped, and the `updated` stamps it moved. `keep` and `copies` are the records
    as they were before the first write."""
    now = {r["id"]: r for r in all_records()}
    kept = now.get(keep["id"])
    if not kept:
        return
    gone = [r["id"] for r in copies]
    _dups_journal_write(_dups_journal() + [{
        "at": now_ts(), "keep": keep["id"], "copies": gone,
        "moved": {key: [v for v in _list_meta(kept, key) if v not in _list_meta(keep, key)]
                  for key in ("related-to", "blocked-by", "files")},
        "unlinked": [v for v in _list_meta(keep, "related-to") if v in gone],
        "updated": {r["id"]: [r["meta"].get("updated", ""), now[r["id"]]["meta"].get("updated", "")]
                    for r in [keep] + copies if r["id"] in now},
        "sim": _likeness([keep["id"]] + gone)}])


def _retired_into(keep_id, records):
    """The memories retired as duplicates of `keep_id`, as `retire_duplicates` leaves them."""
    why = f"duplicate of {keep_id}"
    return [r for r in records
            if r["meta"].get("status") == "superseded"
            and (r["meta"].get("superseded-by") or "").strip() == keep_id
            and (r["meta"].get("invalid-reason") or "").strip() == why]


@_locked_write
def restore_duplicates(keep_id):
    """Take a retirement back: every memory retired as a duplicate of `keep_id` is active again.
    Returns (the ids brought back, exact).

    The inverse of `retire_duplicates`, for the retirements it made. The copies lose the lines that
    said when, why and in favour of what they were retired. The kept memory gives back what it was
    given -- the links and anchors it did not have before -- and gets back the link to its copy
    if it had one. `updated` returns to what it was on every memory nothing else has touched since,
    so a status that comes back does not come back looking freshly written.

    `exact` is False when no note of the retirement is left (see `dups_journal_path`): the copies
    are active again all the same, and the kept memory keeps what it was given.

    Refused, with the store untouched, when nothing is retired in favour of `keep_id`, and when
    `keep_id` is itself no longer active: its copies were retired as duplicates of what it said,
    and that has been retired or revised since -- which is the first thing to take back."""
    keep = get_record(keep_id)
    if not keep:
        raise ValueError(f"id not found: {keep_id}")
    if keep["meta"].get("status", "active") != "active":
        raise ValueError(f"the memory that was kept is no longer active: {keep_id}")
    copies = _retired_into(keep_id, all_records())
    if not copies:
        raise ValueError(f"nothing is retired as a duplicate of {keep_id}")
    back = [r["id"] for r in copies]
    journal = _dups_journal()
    notes = [e for e in journal if e.get("keep") == keep_id]
    noted = {i for e in notes for i in e.get("copies") or []}
    exact = all(i in noted for i in back)
    # The stamp each memory had before the first of these retirements -- as long as every later
    # one found the stamp the one before it had left.
    stamps = {}
    for e in notes:
        for i, pair in (e.get("updated") or {}).items():
            if isinstance(pair, list) and len(pair) == 2:
                before, after = pair
                if i in stamps and stamps[i][1] == before:
                    before = stamps[i][0]
                stamps[i] = (before, after)

    def stamp_before(r):
        before, after = stamps.get(r["id"], ("", None))
        if r["meta"].get("updated", "") != after:
            return ""          # touched by something else since: that stamp stands
        return before

    for key in ("related-to", "blocked-by"):
        given = [v for e in notes for v in (e.get("moved") or {}).get(key) or []]
        if given:
            _edit_list_meta(keep_id, key, remove=given, touch=False)
    given_files = {v for e in notes for v in (e.get("moved") or {}).get("files") or []}
    if given_files:
        _set_files(keep_id, ", ".join(f for f in _list_meta(keep, "files") if f not in given_files), touch=False)
    relink = [v for e in notes for v in e.get("unlinked") or [] if v in back]
    if relink:
        _edit_list_meta(keep_id, "related-to", add=relink, touch=False)
    old = stamp_before(keep)
    if old and old != keep["meta"].get("updated", ""):
        _edit_meta_lines(keep_id, lambda k, v: old if k == "updated" else v)
    for r in copies:
        stamp = stamp_before(r) or now_ts()
        _edit_meta_lines(r["id"], lambda k, v, stamp=stamp: (
            None if k in ("superseded-by", "invalidated", "invalid-reason")
            else "active" if k == "status" else stamp if k == "updated" else v))
    _vectors_back([get_record(i) for i in back])
    if notes:
        _dups_journal_write([e for e in journal if e.get("keep") != keep_id])
    return back, exact


def resolved_duplicates(records=None):
    """What has been settled about duplicates and can still be taken back.

    Read off the store itself, so the list is the same after a restart or a week later. Two kinds
    of row, retirements first (the latest on top):
      {"kind": "retired", "keep": the memory kept, "members": [its retired copies, newest first],
       "at": when the last of them was retired, "sim": ..., "type": ...}
      {"kind": "distinct", "members": [memories judged not to be duplicates, newest first],
       "ids": [the ids the judgement is written on], "at": "", "sim": ..., "type": ...}
    `sim` is None where it is not known.

    A retirement is listed while the memory that was kept is active -- that is when
    `restore_duplicates` takes it back. A judgement is listed while at least two of the memories
    it is about are active; its `ids` are what `unmark_distinct` wants."""
    recs = list(records if records is not None else all_records())
    by = {r["id"]: r for r in recs}

    def active(i):
        return i in by and by[i]["meta"].get("status", "active") == "active"

    def newest(r):
        return (r["meta"].get("created", ""), r["id"])

    sims = {}
    for e in _dups_journal():
        if isinstance(e.get("sim"), (int, float)):
            sims[e["keep"]] = max(sims.get(e["keep"], 0.0), e["sim"])
    retired = {}
    for r in recs:
        keep_id = (r["meta"].get("superseded-by") or "").strip()
        if keep_id and _retired_into(keep_id, [r]):
            retired.setdefault(keep_id, []).append(r)
    rows = []
    for keep_id, copies in retired.items():
        if not active(keep_id):
            continue
        rows.append({"kind": "retired", "keep": by[keep_id], "members": sorted(copies, key=newest, reverse=True),
                     "at": max(c["meta"].get("invalidated", "") for c in copies),
                     "sim": sims.get(keep_id), "type": by[keep_id]["meta"].get("type", "")})
    rows.sort(key=lambda g: (g["at"], g["keep"]["id"]), reverse=True)

    raw, parent = {}, {}
    apart = [p for p in _apart_pairs(recs, raw) if active(p[0]) and active(p[1])]

    def find(x):
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in apart:
        parent[find(a)] = find(b)
    judged = {}
    for a, b in apart:
        g = judged.setdefault(find(a), {"ids": set(), "raw": set()})
        g["ids"].update((a, b))
        g["raw"].update(raw[(a, b)])
    marks = []
    for g in judged.values():
        members = sorted((by[i] for i in g["ids"]), key=newest, reverse=True)
        marks.append({"kind": "distinct", "members": members, "ids": sorted(g["raw"]), "at": "",
                      "sim": _likeness(g["ids"]), "type": members[0]["meta"].get("type", "")})
    marks.sort(key=lambda g: newest(g["members"][0]), reverse=True)
    return rows + marks


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


def _repo_root():
    """The directory that holds the projects: the parent of the data directory."""
    return os.path.dirname(os.path.abspath(DATA))


def _resolve_anchor(proj, rel):
    """Where an anchor points: (path, where).

    'project'  it exists in the project; `path` is project-relative, an own-name prefix removed
    'outside'  it exists relative to the repo root instead -- a sibling project, or a root file
    'absent'   it resolves nowhere; `path` is the project-relative spelling, for the report

    Sessions started at the monorepo root name files from there -- `myproj/lib/auth.php` on a
    memory scoped to `project:myproj` -- while drift looks inside the project. Read literally,
    every such anchor is "moved ... now at lib/auth.php": 54 of 64 moved verdicts on a real store
    were that and nothing else. The prefix is dropped only when the path does not exist as written,
    so a project that really contains a folder named after itself keeps its anchors."""
    p = rel.strip().strip("/")
    if os.path.exists(os.path.join(proj, p)):
        return p, "project"
    slug = os.path.basename(proj)
    inner = p[len(slug) + 1:] if p.startswith(slug + "/") else ""
    if inner:
        if os.path.exists(os.path.join(proj, inner)):
            return inner, "project"
        return inner, "absent"      # <root>/<slug>/<inner> IS <proj>/<inner>: nowhere else to look
    if os.path.exists(os.path.join(_repo_root(), p)):
        return p, "outside"
    return p, "absent"


def normalize_anchors(scope, files):
    """`files:` as it should be stored: each anchor project-relative, duplicates dropped, order kept.

    The write-side half of `_resolve_anchor`. Only an anchor that resolves INSIDE the project is
    rewritten; one that points at a sibling project, or at a file that does not exist yet, is kept
    exactly as typed -- there is nothing better to say about it."""
    raw = [x.strip() for x in (files or "").split(",") if x.strip()]
    proj = _project_dir(scope)
    if not proj:
        return ", ".join(dict.fromkeys(raw))
    out = []
    for x in raw:
        path, where = _resolve_anchor(proj, x)
        out.append((path + ("/" if x.endswith("/") else "")) if where == "project" else x)
    return ", ".join(dict.fromkeys(out))


_PATHISH = re.compile(r"\b(?:[\w.-]+/)*[\w.-]+\.(?:py|js|ts|tsx|jsx|php|swift|sh|css|html|json|md|yml|yaml|toml|sql|rs|go|rb|c|h|cpp|java|kt)\b")


def derive_files(scope, summary, body, limit=6):
    """Paths mentioned in a memory that actually EXIST in its project, as a comma-separated string.

    The `files:` anchor is what makes `drift` useful, and it was on 5 of 865 records — because it
    could only be filled by hand, and optional metadata that must be typed does not get typed. So
    it is derived instead: pull path-shaped tokens out of the text and keep only the ones that
    resolve to a real file in the project. Existence is the whole filter, and it is a strict one —
    prose about "the config file" yields nothing, while "fixed in hooks/session_start.py" yields
    exactly that. Returns "" when nothing resolves, which is the common and correct outcome."""
    proj = _project_dir(scope)
    if not proj:
        return ""
    seen, out = set(), []
    for m in _PATHISH.finditer(f"{summary}\n{body}"):
        rel = _resolve_anchor(proj, m.group(0))[0]
        if rel in seen:
            continue
        seen.add(rel)
        if os.path.isfile(os.path.join(proj, rel)):
            out.append(rel)
            if len(out) >= limit:
                break
    return ", ".join(out)


def check_drift(records=None, since_commits=None):
    """Which memories may have gone stale because the code they describe moved on.

    This is the honest, cheap half of a hard problem. It does NOT read the code or judge whether a
    memory is still true — nothing here understands the change. It reports a *reason to re-read*:
    a memory anchored to files (`files:`) whose files have since been deleted, or committed to many
    times since the memory was written. Deletion is a strong signal; churn is a weak one, which is
    why the threshold is tunable and the output is a report, never an edit.

    Returns [(record, [(path, verdict, detail), ...])] for records with at least one finding.
    Verdicts: 'missing' (the file is gone), 'churn' (>= threshold commits since the record was last
    touched), 'moved' (the path is gone but a file with that basename exists elsewhere),
    'symbol-gone' (the memory refers to `name()` that no longer exists in the project, and git
    history confirms it once did).
    Only records carrying `files:` can be checked at all — see `mem.py drift` for the coverage line.
    """
    if since_commits is None:
        try:
            since_commits = int(os.environ.get("MEM_DRIFT_COMMITS", "10"))
        except (TypeError, ValueError):
            since_commits = 10
    if records is None:
        records = [r for r in all_records() if r["meta"].get("status", "active") == "active"]

    # Group by project FIRST, so history is walked once per repository rather than once per anchored
    # path. The per-path spelling (`git log -- <path>` inside the loop) is the obvious one and is
    # what shipped, and it was fine at five anchored memories; at two hundred it became ~400
    # subprocess spawns and four seconds on every dashboard load. The work is identical, only the
    # batching changed.
    todo = []
    for r in records:
        raw = (r["meta"].get("files") or "").strip()
        if not raw:
            continue
        proj = _project_dir(r["meta"].get("scope", ""))
        if not proj:
            continue
        # Churn is counted since the record was last TOUCHED, not since it was written. Counting
        # from `created` makes the signal unresolvable: an old memory about an active file is
        # reported forever, because commits only accumulate and nothing the reader does can reduce
        # them. Measuring from `updated` gives the reader an action — re-anchor it (`mem.py anchor
        # <id> <same paths>`) or edit it, which is exactly the "I checked, it still holds" gesture
        # the report otherwise has no way to accept.
        created = ((r["meta"].get("updated") or r["meta"].get("created")) or "")[:19]
        # Every anchor resolved ONCE, to (path, where). A path typed from the monorepo root, or one
        # that lives in a sibling project, is still the file the memory is about; only what
        # resolves nowhere is a finding.
        paths = list(dict.fromkeys(_resolve_anchor(proj, x) for x in raw.split(",") if x.strip()))
        todo.append((proj, r, created, paths))

    out_by_id, order = {}, []
    for proj in dict.fromkeys(t[0] for t in todo):
        group = [t for t in todo if t[0] == proj]
        dates = [t[2] for t in group if t[2]]
        # Only paths that still exist can reach the churn branch below — the rest are reported
        # missing or moved. Keeping them out of the pathspec is not just cheaper: one anchor
        # pointing outside the repo would make the whole batched call fail, and with it the churn
        # signal for every other memory in the project.
        wanted = sorted({p for _, _, _, paths in group for p, where in paths if where == "project"})
        commits = _churn_index(proj, min(dates), wanted) if dates and wanted else {}
        basenames = None                       # built only if some anchored path has gone missing

        # Symbols the memories in this project claim exist. Resolved once for the whole group: one
        # filesystem walk, then a git check only for the ones that turn out to be absent.
        #
        # This is the precise half of drift. Churn says "mem.py had 30 commits", which is true and
        # nearly useless -- the file is 2500 lines and the memory was about one function. A symbol
        # that is gone says exactly what changed underneath the memory.
        sym_gone = {}
        if os.environ.get("MEM_DRIFT_SYMBOLS", "1").strip().lower() not in ("0", "no", "false"):
            claimed = {}
            for _, r, _, _ in group:
                for s in _symbol_refs(r):
                    claimed.setdefault(s, []).append(r["id"])
            missing_syms = set(claimed) - _symbols_present(proj, set(claimed))
            for s in sorted(missing_syms):
                ever, sha = _symbol_ever_existed(proj, s)
                if ever:
                    for rid in claimed[s]:
                        sym_gone.setdefault(rid, []).append((s, sha))
        for _, r, created, paths in group:
            findings = []
            for rel, where in paths:
                if where == "outside":
                    continue               # present, in another project: its history is not ours
                if where == "absent":
                    if basenames is None:
                        basenames = _basename_index(proj)
                    alts = basenames.get(os.path.basename(rel), [])
                    # "moved" is a claim about ONE file. With several candidates, the first one the
                    # walk happened to meet is a guess, and a guess printed as "now at ..." sends
                    # the reader to the wrong file -- lib/map/lib.php was reported as lib/ssl/lib.php.
                    if len(alts) == 1:
                        findings.append((rel, "moved", f"now at {alts[0]}"))
                    elif alts:
                        findings.append((rel, "missing",
                                         f"no such file; {len(alts)} files named "
                                         f"{os.path.basename(rel)} exist, cannot tell which"))
                    else:
                        findings.append((rel, "missing", "no such file in the project"))
                    continue
                if not created or commits is None:
                    continue               # not a git repo, or no date — no churn signal available
                n = sum(1 for stamp in commits.get(rel, ()) if stamp >= created)
                if n >= since_commits:
                    findings.append((rel, "churn", f"{n} commits since {created[:10]}"))
            for sym, sha in sym_gone.get(r["id"], ()):
                findings.append((sym + "()", "symbol-gone",
                                 f"no longer anywhere in the project"
                                 + (f"; last touched in {sha}" if sha else "")))
            if findings:
                out_by_id[r["id"]] = (r, findings)
    # Callers render this list; keep it in the order they passed records in, which the grouping above
    # would otherwise scramble into project order.
    for r in records:
        if r["id"] in out_by_id and r["id"] not in order:
            order.append(r["id"])
    return [out_by_id[i] for i in order]


_SYMBOL_REF = re.compile(r"\b(?:[A-Za-z_]\w*\.)?([A-Za-z_]\w{3,})\(\)")

# CODE only -- deliberately no .md, .json, .yml, .csv. A function that survives in a changelog, a
# README or a memory is still gone from the code, and counting prose as presence makes the check
# unable to fire at all: the store lives INSIDE its own project, so a memory naming a deleted
# function was proving its own claim and the verdict never appeared.
_CODE_EXT = frozenset((".py", ".js", ".ts", ".tsx", ".jsx", ".php", ".swift", ".sh", ".css",
                       ".html", ".sql", ".rs", ".go", ".rb", ".c", ".h", ".cpp", ".java",
                       ".kt", ".pl", ".m", ".mm"))
_WALK_SKIP = frozenset((".git", "node_modules", "__pycache__", ".build", "build", "vendor",
                        "dist", ".venv", ".next", "Pods"))


def _symbol_refs(rec):
    """Function names a memory refers to, spelled `name()` in the prose.

    Empty parens only, and that is the whole trick. Measured on the real store: `name()` yields 66
    distinct symbols across 54 anchored memories and they are all genuine function names, while
    `name(arg)` yields junk -- `COUNT(*)`, `background(.bar)`, `SWAT(=SWDIO)`. A pattern that
    matched arguments too would triple the candidates and halve their quality.
    """
    blob = record_summary(rec) + "\n" + rec.get("body", "")
    return set(_SYMBOL_REF.findall(blob))


def _symbols_present(proj, wanted):
    """Which of `wanted` appear anywhere in the project's source. One walk, early exit."""
    present = set()
    if not wanted:
        return present
    for dirpath, dirnames, filenames in os.walk(proj):
        dirnames[:] = [d for d in dirnames if d not in _WALK_SKIP]
        if os.path.abspath(dirpath).startswith(os.path.abspath(STORE)):
            # Defensive, and currently unreachable: the store holds only markdown, which the
            # extension filter already drops. Kept for the day it holds a snippet of code -- but
            # said plainly rather than left looking load-bearing, because no test can reach it.
            continue
        for name in filenames:
            if os.path.splitext(name)[1] not in _CODE_EXT:
                continue
            try:
                with open(os.path.join(dirpath, name), encoding="utf-8", errors="ignore") as f:
                    text = f.read()
            except OSError:
                continue
            for sym in wanted - present:
                if sym in text:
                    present.add(sym)
        if wanted <= present:
            break
    return present


def _symbol_ever_existed(proj, sym):
    """Did this symbol ever live in this project? `git log -S` over the whole history.

    This check is what makes the verdict trustworthy, and it was added because the first version
    without it was wrong on two of the three cases it produced. `date_default_timezone_get()` is a
    PHP builtin a memory merely mentions -- absent from the project because it was never in it, not
    because anything changed. And a memory scoped to one project can name a function that lives in
    its sibling. "Absent" alone means nothing; "was here and is not any more" is the signal.

    Only ever called for symbols already known to be absent, so the cost is a handful of git
    invocations, not one per candidate.
    """
    # Excluding the store is not a detail: the store lives in this same repo, so without it
    # `git log -S` finds the symbol inside the memory that mentions it and every claim becomes
    # self-confirming. It fired on step_code_drift() -- a function that was only ever PROPOSED in a
    # memory and never written -- which is the opposite of "was here and is gone".
    r = subprocess.run(["git", "-C", proj, "log", "-S", sym, "--oneline", "-1",
                        "--", ".", ":(exclude)store", ":(exclude)*.md"],
                       capture_output=True, text=True, creationflags=_NO_WINDOW)
    return r.returncode == 0 and bool(r.stdout.strip()), r.stdout.strip()[:9]


def _churn_index(proj, since, paths=()):
    """{path relative to `proj`: [commit timestamps]} for one repository, in a single git call.

    `paths` is a pathspec, and passing it is what makes this cheap: without it git walks every
    commit since `since` and prints every file each one touched, which on a repo whose history is
    dominated by unrelated files (this store auto-commits its own markdown) is most of the cost.

    Timestamps are forced to the local timezone (`format-local`) because record timestamps are
    written in local time by `now_ts()`, and comparing those against a commit's own recorded offset
    would silently miscount around a timezone change. Paths come out of git relative to the
    REPOSITORY root, which is not always the project directory — a project nested inside a larger
    repo would otherwise match nothing at all — so the repo-relative prefix is stripped back off.
    """
    if os.path.isdir(os.path.join(proj, ".git")):
        prefix = ""                        # the project IS the repo root: no prefix, no extra call
    else:
        res = subprocess.run(["git", "-C", proj, "rev-parse", "--show-prefix"],
                             capture_output=True, text=True, creationflags=_NO_WINDOW)
        if res.returncode != 0:
            return None                    # not a git repo
        prefix = res.stdout.strip()
    cmd = ["git", "-C", proj, "log", "--since", since, "--name-only",
           "--format=%x01%cd", "--date=format-local:%Y-%m-%d %H:%M:%S"]
    if paths:
        # `:(literal)` so a path containing a glob character is matched as itself, not as a pattern.
        cmd += ["--"] + [":(literal)" + p for p in paths]
    res = subprocess.run(cmd, capture_output=True, text=True, creationflags=_NO_WINDOW)
    if res.returncode != 0:
        return None
    out, stamp = {}, None
    for line in res.stdout.splitlines():
        if line.startswith("\x01"):
            stamp = line[1:].strip()
        elif line.strip() and stamp:
            path = line.strip()
            if prefix:
                if not path.startswith(prefix):
                    continue               # touched a file outside this project directory
                path = path[len(prefix):]
            out.setdefault(path, []).append(stamp)
    return out


def _basename_index(proj):
    """{basename: [every relative path with that name]} — one walk, so N missing anchors cost one
    traversal. Every path, not the first: see the 'moved' verdict in check_drift."""
    found = {}
    for dirpath, dirnames, filenames in os.walk(proj):
        dirnames[:] = [d for d in dirnames if d not in (".git", "node_modules", "__pycache__")]
        for name in filenames:
            found.setdefault(name, []).append(os.path.relpath(os.path.join(dirpath, name), proj))
    return found


@_locked_write
def _set_files(rec_id, files, touch=True):
    """In-place meta edit of the `files:` anchor. Line-level like _set_priority and _set_tier, so
    nothing else about the record is rewritten.

    `touch=False` leaves `updated` alone. Churn is measured from `updated`, so re-anchoring is how a
    reader says "I checked, it still holds" -- a mechanical respelling of the same paths must not
    say that on their behalf."""
    for path in store_files():
        for r in parse_file(path):
            if r["id"] != rec_id:
                continue
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            new_block, body_at = [], _body_start(lines, r)
            slot = _meta_slot(lines, r, body_at)
            for k in range(r["start"], r["end"] + 1):
                line = lines[k]
                if files and k == slot:
                    new_block.append(f"- files: {files}\n")
                mm = META_RE.match(line.rstrip("\n")) if k < body_at else None
                if mm and mm.group("k") == "files":
                    continue   # drop the old anchor; rewritten at the slot unless clearing
                if touch and mm and mm.group("k") == "updated":
                    line = f"- updated: {now_ts()}\n"
                new_block.append(line)
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
    scope = rec["meta"].get("scope", "")
    paths = [p for p in normalize_anchors(scope, a.files).split(", ") if p]
    proj = _project_dir(scope)
    missing = [p for p in paths if proj and _resolve_anchor(proj, p)[1] == "absent"]
    rel = _set_files(a.id, ", ".join(paths))
    print(f"{a.id} -> files: {', '.join(paths)}  in {rel}")
    if missing:
        # Not refused: a path can legitimately not exist yet. But said out loud, because an anchor
        # that never resolves makes `drift` cry wolf forever.
        print(f"  warning: not present in {os.path.basename(proj)}: {', '.join(missing)}"
              f" — drift will keep reporting this memory", file=sys.stderr)


def cmd_backfill_anchors(a):
    """Derive `files:` for records that have none. Report-only unless --write.

    Exists because the anchor was introduced late and 860 records predate it. Deriving on write
    only helps future memories; without this, drift stays a feature that works on 1% of the store."""
    recs = [r for r in all_records()
            if r["meta"].get("status", "active") == "active" and not (r["meta"].get("files") or "").strip()]
    if a.scope:
        recs = [r for r in recs if r["meta"].get("scope") == a.scope]
    found = []
    for r in recs:
        f = derive_files(r["meta"].get("scope", ""), record_summary(r), r.get("body", ""))
        if f:
            found.append((r, f))
    print(f"# {len(recs)} active memories have no anchor; {len(found)} mention a file that exists")
    for r, f in found[:200]:
        print(f"  {r['id']}  [{r['meta'].get('scope')}]  -> {f}")
        print(f"      {record_summary(r)[:92]}")
    if not found:
        return
    if not a.write:
        print("\nReport only. Re-run with --write to set these anchors.")
        return
    n = 0
    for r, f in found:
        if _set_files(r["id"], f):
            n += 1
    print(f"\nanchored {n} memories. `mem.py drift` now covers them.")


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
    log_access(rec["id"], "get", current_session())


@_locked_write
def _set_priority(rec_id, priority):
    """In-place meta edit: set (pin) or remove (unpin) the priority field of a record."""
    for path in store_files():
        for r in parse_file(path):
            if r["id"] != rec_id:
                continue
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            new_block, body_at = [], _body_start(lines, r)
            slot = _meta_slot(lines, r, body_at)
            for k in range(r["start"], r["end"] + 1):
                line = lines[k]
                if priority and k == slot:
                    new_block.append(f"- priority: {priority}\n")
                mm = META_RE.match(line.rstrip("\n")) if k < body_at else None
                if mm and mm.group("k") == "priority":
                    continue  # drop the existing priority line; rewritten at the slot if pinning
                if mm and mm.group("k") == "updated":
                    line = f"- updated: {now_ts()}\n"
                new_block.append(line)
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
            new_block, body_at = [], _body_start(lines, r)
            slot = _meta_slot(lines, r, body_at)
            for k in range(r["start"], r["end"] + 1):
                line = lines[k]
                if tier and tier != "open" and k == slot:
                    new_block.append(f"- tier: {tier}\n")
                mm = META_RE.match(line.rstrip("\n")) if k < body_at else None
                if mm and mm.group("k") == "tier":
                    continue   # drop the existing tier line; rewritten at the slot unless clearing
                if mm and mm.group("k") == "updated":
                    line = f"- updated: {now_ts()}\n"
                new_block.append(line)
            lines[r["start"]:r["end"] + 1] = new_block
            with open(path, "w", encoding="utf-8") as f:
                f.writelines(lines)
            return os.path.relpath(path, DATA)
    return None


@_locked_write
def _set_mirrored(rec_id, where):
    """In-place meta edit: record (or clear) that a rule's full text also lives in an instruction
    file the agent always loads. Line-level, like its siblings above."""
    for path in store_files():
        for r in parse_file(path):
            if r["id"] != rec_id:
                continue
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            new_block, body_at = [], _body_start(lines, r)
            slot = _meta_slot(lines, r, body_at)
            for k in range(r["start"], r["end"] + 1):
                line = lines[k]
                if where and k == slot:
                    new_block.append(f"- mirrored-in: {where}\n")
                mm = META_RE.match(line.rstrip("\n")) if k < body_at else None
                if mm and mm.group("k") == "mirrored-in":
                    continue
                if mm and mm.group("k") == "updated":
                    line = f"- updated: {now_ts()}\n"
                new_block.append(line)
            lines[r["start"]:r["end"] + 1] = new_block
            with open(path, "w", encoding="utf-8") as f:
                f.writelines(lines)
            return os.path.relpath(path, DATA)
    return None


def parse_when(spec):
    """A digest's `when:` line, parsed: {"always": bool, "paths": [glob], "commands": regex or None}.

    Clauses are separated by ';'. `always` -- at every session start. `paths=<glob>, <glob>` -- when
    a tool touches a matching file; the path is matched from the repo root, so `*.css` and
    `*/design/*` both work. `commands=<regex>` -- when a shell command matches.

    Raises ValueError on a clause it does not know or a regex that does not compile. That is for
    the write paths: a digest that silently can never fire is worse than one that is refused."""
    out = {"always": False, "paths": [], "commands": None}
    for clause in (spec or "").split(";"):
        clause = clause.strip()
        if not clause:
            continue
        if clause == "always":
            out["always"] = True
        elif clause.startswith("paths="):
            out["paths"] = [g.strip() for g in clause[6:].split(",") if g.strip()]
        elif clause.startswith("commands="):
            try:
                out["commands"] = re.compile(clause[9:].strip())
            except re.error as e:
                raise ValueError(f"when: the commands= regex does not compile ({e})")
        else:
            raise ValueError(f"when: unknown clause {clause!r} (use always, paths=..., commands=...)")
    return out


@_locked_write
def set_digest(rec_id, when=None, digest_of=None):
    """In-place meta edit: what makes a record a digest. `when` is the moment it is given (see
    parse_when), `digest_of` the memories it distils. None leaves a line alone, "" removes it.
    Returns the relpath written, or None if the id is not found. Raises on a `when` that does not
    parse.

    Only the meta block is touched. A digest's body is a list, and a list item can look exactly
    like a meta line ("- when: the build is green"); the blank line that ends the meta is the
    boundary the parser uses, so it is the one used here."""
    new = {}
    if when is not None:
        when = " ".join(str(when).split())
        if when:
            parse_when(when)
        new["when"] = when
    if digest_of is not None:
        new["digest-of"] = ", ".join(i.strip() for i in str(digest_of).split(",") if i.strip())
    for path in store_files():
        for r in parse_file(path):
            if r["id"] != rec_id:
                continue
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            block, in_meta, seen_meta = [], True, False
            slot = _meta_slot(lines, r, _body_start(lines, r))
            for k in range(r["start"], r["end"] + 1):
                line = lines[k]
                if k == slot:
                    block += [f"- {key}: {val}\n" for key, val in new.items() if val]
                mm = META_RE.match(line.rstrip("\n")) if in_meta else None
                if in_meta and seen_meta and line.strip() == "":
                    in_meta = False
                seen_meta = seen_meta or bool(mm)
                if mm and mm.group("k") in new:
                    continue
                if mm and mm.group("k") == "updated":
                    line = f"- updated: {now_ts()}\n"
                block.append(line)
            lines[r["start"]:r["end"] + 1] = block
            with open(path, "w", encoding="utf-8") as f:
                f.writelines(lines)
            return os.path.relpath(path, DATA)
    return None


def digest_report(records=None):
    """([(digest, sources, {"retired": [...], "changed": [...]})], uncovered) -- what each digest
    needs before it can be trusted again.

    A digest restates other memories, so it goes stale the way any summary does: without a sound.
    Three things can be checked mechanically. A memory it cites was retired. A memory it cites was
    rewritten after the digest was. And a preference exists, in a scope that has digests, that no
    digest cites -- the rule that was added after the page was written."""
    records = all_records() if records is None else records
    by_id = {r["id"]: r for r in records}
    digests = [r for r in records if (r["meta"].get("when") or "").strip()
               and r["meta"].get("status", "active") in ("active", "working")]
    out, cited = [], set()
    for d in digests:
        sources = _list_meta(d, "digest-of")
        cited.update(sources)
        stamp = (d["meta"].get("updated") or d["meta"].get("created") or "")[:19]
        retired = [i for i in sources
                   if i not in by_id or by_id[i]["meta"].get("status", "active") != "active"]
        changed = [i for i in sources if i in by_id and i not in retired
                   and (by_id[i]["meta"].get("updated") or "")[:19] > stamp]
        out.append((d, sources, {"retired": retired, "changed": changed}))
    scopes = {d["meta"].get("scope") for d in digests}
    uncovered = [r["id"] for r in records
                 if r["meta"].get("type") == "preference" and r["meta"].get("scope") in scopes
                 and r["meta"].get("status", "active") == "active" and r["id"] not in cited]
    return out, uncovered


def cmd_digest(a):
    """List the digests with what each needs, or make a record a digest (`<id> --when ...`)."""
    if a.id:
        if a.when is None and a.digest_of is None:
            sys.exit("error: give --when and/or --of (or no id, for the report)")
        try:
            rel = set_digest(a.id, when=a.when, digest_of=a.digest_of)
        except ValueError as e:
            sys.exit(f"error: {e}")
        if not rel:
            sys.exit(f"no record with id {a.id}")
        print(f"digest {a.id} updated  in {rel}")
        return
    rows, uncovered = digest_report()
    if not rows:
        print("(no digests: a digest is a record with a `when:` line -- `mem.py digest <id> --when always`)")
        return
    print(f"DIGESTS ({len(rows)})")
    for d, sources, issues in rows:
        print(f"  {d['id']}  {d['meta'].get('status', 'active'):7s}  {len(sources):3d} sources  "
              f"{record_summary(d)[:60]}\n      when: {d['meta'].get('when')}")
        if issues["retired"]:
            print(f"      {len(issues['retired'])} source(s) retired since: {', '.join(issues['retired'])}")
        if issues["changed"]:
            print(f"      {len(issues['changed'])} source(s) rewritten since: {', '.join(issues['changed'])}")
    print(f"\npreferences no digest cites: {len(uncovered)}"
          + ("" if not uncovered or getattr(a, "verbose", False) else "   (--verbose lists them)"))
    if getattr(a, "verbose", False):
        by_id = {r["id"]: r for r in all_records()}
        for i in uncovered:
            print(f"  {i}  {record_summary(by_id[i])[:90]}")


def stale_statuses(days=None, records=None, scope=None):
    """Active statuses nobody has touched for `days`: [(record, age in days)], oldest first.

    This is the list of what the session-start injection steps over. Getting old retires nothing:
    every record here is active and searchable, and a project's latest status still comes with
    that project's brief. The list exists so that stepping over is not the same as forgetting."""
    import datetime as _dt
    if days is None:
        try:
            days = int(os.environ.get("MEM_INJECT_STATUS_DAYS", "60"))
        except ValueError:
            days = 60
    today = _dt.date.today()
    out = []
    for r in (all_records() if records is None else records):
        m = r["meta"]
        if m.get("type") != "status" or m.get("status", "active") != "active":
            continue
        if scope and m.get("scope") != scope:
            continue
        try:
            stamp = _dt.date.fromisoformat((m.get("updated") or m.get("created") or "")[:10])
        except ValueError:
            continue
        age = (today - stamp).days
        if days <= 0 or age > days:
            out.append((r, age))
    out.sort(key=lambda t: -t[1])
    return out


@_locked_write
def keep_status(rec_id):
    """"It still stands": stamp a record as looked at today, and change nothing else in it.
    Returns the relpath written, or None if the id is not found."""
    for path in store_files():
        for r in parse_file(path):
            if r["id"] != rec_id:
                continue
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            in_meta, seen_meta, stamped, last_meta = True, False, False, None
            for k in range(r["start"], r["end"] + 1):
                mm = META_RE.match(lines[k].rstrip("\n")) if in_meta else None
                if in_meta and seen_meta and lines[k].strip() == "":
                    in_meta = False             # the page below is not ours to touch
                seen_meta = seen_meta or bool(mm)
                if mm:
                    last_meta = k
                if mm and mm.group("k") == "updated":
                    lines[k] = f"- updated: {now_ts()}\n"
                    stamped = True
            if not stamped and last_meta is not None:
                # A record written by hand may carry no such line. Without this the stamp would
                # land nowhere, and the keep would be reported all the same.
                lines.insert(last_meta + 1, f"- updated: {now_ts()}\n")
            with open(path, "w", encoding="utf-8") as f:
                f.writelines(lines)
            return os.path.relpath(path, DATA)
    return None


def cmd_dups(a):
    """List the groups of memories that say the same thing, or settle one."""
    if a.undo:
        try:
            back, exact = restore_duplicates(a.undo)
        except ValueError as e:
            sys.exit(str(e))
        print(f"active again: {', '.join(back)}  (no longer retired as duplicate{'s' if len(back) != 1 else ''} of {a.undo})")
        if not exact:
            print(f"no note was left of what {a.undo} was given at the time: it keeps those links and anchors")
        return
    if a.distinct:
        try:
            n = mark_distinct(a.distinct)
        except ValueError as e:
            sys.exit(str(e))
        print(f"marked as distinct: {', '.join(a.distinct)} ({n} pair{'s' if n != 1 else ''}); "
              "they are not offered together again")
        return
    if a.keep or a.retire:
        if not (a.keep and a.retire):
            sys.exit("give both: --keep ID --retire ID [ID ...]")
        try:
            gone = retire_duplicates(a.keep, a.retire)
        except ValueError as e:
            sys.exit(str(e))
        print(f"kept {a.keep}; retired as its duplicate{'s' if len(gone) != 1 else ''}: {', '.join(gone)}"
              "  (superseded, not deleted)")
        return
    sim_sync()
    groups = duplicate_groups(a.threshold)
    dest = getattr(a, "dest", None) or DEST_LOCAL
    shown = []
    for g in groups:
        members = [(r, emit_for(r, dest)[0]) for r in g["members"]]
        members = [(r, s) for r, s in members if s is not None]   # private never leaves for a model
        if len(members) > 1:
            shown.append((g, members))
    th = duplicates_threshold() if a.threshold is None else max(SIM_FLOOR, min(float(a.threshold), 0.99))
    if getattr(a, "json", False):
        import json
        print(json.dumps([{"sim": round(g["sim"], 4), "type": g["type"],
                           "members": [{"id": r["id"], "scope": r["meta"].get("scope"),
                                        "created": r["meta"].get("created"), "summary": s}
                                       for r, s in members]}
                          for g, members in shown], ensure_ascii=False, indent=1))
        return
    if not shown:
        if not load_embeddings():
            print("(no vectors yet: likeness comes from the semantic index — run `mem.py embed`)")
        else:
            print(f"(no two active memories of one type are at least {th:.0%} alike)")
        return
    print(f"MEMORIES THAT SAY THE SAME THING (same type, at least {th:.0%} alike): {len(shown)} group"
          f"{'s' if len(shown) != 1 else ''}")
    for g, members in shown:
        scopes = list(dict.fromkeys(r["meta"].get("scope", "?") for r, _ in members))
        print(f"\n  {g['sim']:.0%}  {g['type']} · {', '.join(scopes)}")
        for r, s in members:
            print(f"    {r['id']}  {r['meta'].get('created', '')[:10]}  {s[:92]}")
    print("\nNothing is retired by listing.\n"
          "Keep one:      mem.py dups --keep <id> --retire <id> [<id> ...]   (the others are superseded by it,\n"
          "               not deleted; what they were linked or anchored to moves to the one kept)\n"
          "Not the same:  mem.py dups --distinct <id> <id> [<id> ...]        (they are not offered together again)\n"
          "Take it back:  mem.py dups --undo <kept id>                       (its retired copies are active again)")


def cmd_stale(a):
    """List the statuses the session start no longer lists, or mark some as still standing."""
    if a.keep:
        for rid in a.keep:
            rel = keep_status(rid)
            print(f"kept {rid}: stamped today, nothing else changed  in {rel}" if rel else f"no record with id {rid}")
        return
    try:
        days = a.days if a.days is not None else int(os.environ.get("MEM_INJECT_STATUS_DAYS", "60"))
    except ValueError:
        days = 60
    rows = stale_statuses(days, scope=a.scope)
    if not rows:
        print(f"(no active status is older than {days} days)")
        return
    print(f"STATUSES NOT LISTED AT SESSION START (older than {days} days): {len(rows)}")
    by_scope = {}
    for r, age in rows:
        by_scope.setdefault(r["meta"].get("scope", "?"), []).append((r, age))
    for sc in sorted(by_scope, key=lambda s: (s != "global", s)):
        print(f"  {sc}")
        for r, age in by_scope[sc]:
            print(f"    {r['id']}  {age:4d} d  {record_summary(r)[:92]}")
    print("\nNothing here was retired: each is active and searchable, and a project's latest still "
          "comes with\nthat project's brief.\n"
          "Still stands:  mem.py stale --keep <id> [<id> ...]   (stamps it as looked at today)\n"
          "Moved on:      mem.py supersede <id> --reason \"...\"   (retires that one; a new status for a\n"
          "               scope retires only the latest one before it)")


def cmd_mirror(a):
    """Say that a critical rule's full text also lives in an always-loaded instruction file.

    The SessionStart hook then names the rule instead of repeating it. Exists because the hook's
    output is capped per hook, and three of five critical rules on the store this was built for
    were verbatim duplicates of CLAUDE.md -- paid for twice, in the one place where room is scarce.
    It is a statement about ONE reader: agents that do not load that file (anything on MCP) still
    get the full text, since `mcp.py` ignores the field. And it is only as true as the file: remove
    the rule from there and this must be cleared, or the rule shrinks to its summary line."""
    rec = get_record(a.id)
    if not rec:
        sys.exit(f"id not found: {a.id}")
    where = "" if a.where.strip() == "-" else _sanitize_summary(a.where.strip())
    rel = _set_mirrored(a.id, where or None)
    if not where:
        print(f"{a.id} — no longer marked mirrored  in {rel}")
        return
    print(f"{a.id} -> mirrored-in: {where}  in {rel}")
    if rec["meta"].get("priority") != "critical":
        print("  note: only critical rules are injected with their body, so this changes nothing "
              "until the memory is pinned", file=sys.stderr)


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
def _edit_list_meta(rec_id, key, add=(), remove=(), touch=True):
    """In-place add/remove of ids in a comma-separated meta list field. Drops the line
    when the list becomes empty. Returns (relpath, new_list).

    `touch=False` leaves `updated` alone, for a field that says something about the pair rather
    than about this record (`distinct-from`): a status must not look freshly looked-at, and step
    back into the session-start list, because someone compared it with another."""
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
            new_block, body_at = [], _body_start(lines, r)
            slot = _meta_slot(lines, r, body_at)
            for k in range(r["start"], r["end"] + 1):
                line = lines[k]
                if cur and k == slot:
                    new_block.append(f"- {key}: {', '.join(cur)}\n")
                mm = META_RE.match(line.rstrip("\n")) if k < body_at else None
                if mm and mm.group("k") == key:
                    continue  # drop existing; rewritten at the slot if non-empty
                if touch and mm and mm.group("k") == "updated":
                    line = f"- updated: {now_ts()}\n"
                new_block.append(line)
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
    session = (getattr(a, "session", None) or "").strip() or None
    if getattr(a, "reset", False):
        mark_session_reset(session)      # before the rows below, so they count as "since the reset"
    log_access(ids, action, session)
    print(f"logged {action} access for {len(ids)} record(s)")


def find_all_clusters(allow_semantic=True):
    """Group all active memories of the same type into connected duplicate components.

    Returns a list of list of records: [[recA1, recA2, ...], [recB1, ...]].
    """
    recs = all_records()
    active_recs = [r for r in recs if r["meta"].get("status", "active") == "active"]
    
    # Group by (type, scope), not type alone. Scope was missing from the key, so consolidation
    # proposed merging memories from DIFFERENT PROJECTS whenever they read alike — a deploy gotcha
    # from one project folded into the identically-worded one from another, and the merged record
    # would then carry one scope and both projects' facts. Cross-scope look-alikes are still worth
    # knowing about, so they are collected separately and reported rather than merged.
    by_key = {}
    for r in active_recs:
        by_key.setdefault((r["meta"].get("type"), r["meta"].get("scope")), []).append(r)
        
    # Similarity logic
    emb = load_embeddings() if allow_semantic else {}
    thresh = float(os.environ.get("MEM_DUP_THRESHOLD", "0.62"))
    jac_thresh = float(os.environ.get("MEM_DUP_JACCARD", "0.5"))
    
    def get_tokens(s):
        return set(re.findall(r"\w+", (s or "").lower()))

    # Build adjacency graph
    clusters = []
    
    for (rtype, rscope), type_recs in by_key.items():
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


def find_cross_scope(allow_semantic=True, k=20):
    """Same-type memories that look alike but live in DIFFERENT scopes: (score, rec_a, rec_b).

    These are deliberately NOT merged — merging them would fold one project's fact into another's
    and leave the result carrying a single scope. But they are worth seeing: usually one of the two
    is mis-scoped, and the only way to notice is to be shown the pair."""
    active = [r for r in all_records() if r["meta"].get("status", "active") == "active"]
    emb = load_embeddings() if allow_semantic else {}
    thresh = float(os.environ.get("MEM_DUP_THRESHOLD", "0.62"))
    by_type = {}
    for r in active:
        by_type.setdefault(r["meta"].get("type"), []).append(r)
    out = []
    for rs in by_type.values():
        for i in range(len(rs)):
            for j in range(i + 1, len(rs)):
                a_, b_ = rs[i], rs[j]
                if a_["meta"].get("scope") == b_["meta"].get("scope"):
                    continue
                va, vb = emb.get(a_["id"]), emb.get(b_["id"])
                if va and vb:
                    sc = _cosine(va, vb)
                    if sc >= thresh:
                        out.append((sc, a_, b_))
    out.sort(key=lambda x: -x[0])
    return out[:k]


def _print_cross_scope(allow_semantic):
    """Say what was deliberately left alone, so 'nothing to consolidate' is not mistaken for
    'nothing looks duplicated'."""
    try:
        pairs = find_cross_scope(allow_semantic=allow_semantic)
    except Exception:
        return
    if not pairs:
        return
    print(f"\n{len(pairs)} look-alike pair(s) in DIFFERENT scopes — reported, never merged:")
    for sc, a_, b_ in pairs:
        print(f"  {round(sc * 100):>3}%  {a_['id']} [{a_['meta'].get('scope')}]"
              f"  <->  {b_['id']} [{b_['meta'].get('scope')}]")
        print(f"        {record_summary(a_)[:88]}")
    print("  Merging these would fold one project's fact into another. If one is mis-scoped,")
    print("  re-scope it by hand (web UI, or edit the record) and it will cluster normally.")


def cmd_consolidate(a):
    # Check if git tree is clean
    res = subprocess.run(["git", "status", "--porcelain"], cwd=DATA, capture_output=True, text=True, creationflags=_NO_WINDOW)
    if res.returncode != 0:
        sys.exit("error: not a git repository or git not available")
    if res.stdout.strip():
        sys.exit("error: Git working tree has uncommitted changes. Please commit or stash them first.")
        
    clusters = find_all_clusters(allow_semantic=not getattr(a, "no_semantic", False))
    semantic = not getattr(a, "no_semantic", False)
    if not clusters:
        print("No duplicate memories found within a scope.")
        _print_cross_scope(semantic)
        return
        
    print(f"Found {len(clusters)} duplicate cluster(s) to consolidate.")
    _print_cross_scope(semantic)
    
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
            
        # Commit changes. The identity is passed inline (see git_identity): consolidate rewrites
        # records, and on a machine with no global user.email this commit used to fail silently,
        # leaving the store rewritten but unrecorded — the one state from which there is no way
        # back. Report the failure rather than swallowing it.
        ensure_store_repo()
        r = subprocess.run(["git", *git_identity("consolidate"), "commit", "-am",
                            f"Propose memory consolidation ({consolidated_count} clusters)"],
                           cwd=DATA, capture_output=True, text=True, creationflags=_NO_WINDOW)
        if r.returncode != 0 and "nothing to commit" not in (r.stdout + r.stderr):
            print(f"warning: could not commit the consolidation ({(r.stderr or r.stdout).strip().splitlines()[:1]}); "
                  "the store changed but the change is not in git history")
        
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
    pa.add_argument("--when", help="makes it a digest: always | paths=<glob>, <glob> | commands=<regex>")
    pa.add_argument("--digest-of", dest="digest_of", metavar="IDS",
                    help="comma-separated ids of the memories a digest distils")
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
    pl.add_argument("--files", metavar="PATH",
                    help="only memories ANCHORED to this path (the files: field)")
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
    ps.add_argument("--files", metavar="PATH",
                    help="only memories ANCHORED to this path (the files: field, not a text match); trailing slash = directory")
    ps.add_argument("--dedup", action="store_true",
                    help="suppress near-duplicate hits (opt-in: see `dedup_near` for why not by default)")
    ps.add_argument("--limit", type=int, default=10,
                    help="how many ranked hits to show (default 10; 0 = all)")
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

    pb = sub.add_parser("backfill-anchors", help="derive files: for memories that have none (report-only by default)")
    pb.add_argument("--scope")
    pb.add_argument("--write", action="store_true", help="actually set the anchors")
    pb.set_defaults(func=cmd_backfill_anchors)

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

    pmi = sub.add_parser("mirror", help="a critical rule's full text also lives in an instruction file "
                                         "(the hook names it instead of repeating it)")
    pmi.add_argument("id")
    pmi.add_argument("where", help="the file, e.g. CLAUDE.md; '-' clears the mark")
    pmi.set_defaults(func=cmd_mirror)

    pdg = sub.add_parser("digest", help="digests: list them with what each needs, or make a record one")
    pdg.add_argument("id", nargs="?", help="a record to make (or change as) a digest")
    pdg.add_argument("--when", help="always | paths=<glob>, <glob> | commands=<regex>   (';' between clauses)")
    pdg.add_argument("--of", dest="digest_of", metavar="IDS",
                     help="comma-separated ids of the memories it distils")
    pdg.add_argument("--verbose", action="store_true", help="list the preferences no digest cites")
    pdg.set_defaults(func=cmd_digest)

    pst = sub.add_parser("stale", help="statuses too old to be listed at session start (nothing is retired)")
    pst.add_argument("--days", type=int, help="age limit in days (default: MEM_INJECT_STATUS_DAYS, 60)")
    pst.add_argument("--scope", help="only this scope")
    pst.add_argument("--keep", nargs="+", metavar="ID",
                     help="it still stands: stamp these as looked at today, change nothing else")
    pst.set_defaults(func=cmd_stale)

    pdu = sub.add_parser("dups", help="memories that say the same thing twice: list the groups, keep one, "
                                      "or mark them distinct (nothing is deleted)")
    pdu.add_argument("--threshold", type=float, help="how alike, 0.60-0.99 (default: MEM_DUPS_THRESHOLD, 0.80)")
    pdu.add_argument("--json", action="store_true", help="machine-readable output")
    pdu.add_argument("--dest", choices=(DEST_LOCAL, DEST_AGENT), default=DEST_LOCAL,
                     help="who reads the output: 'agent' leaves private memories out")
    pdu.add_argument("--keep", metavar="ID", help="the memory that stays (with --retire)")
    pdu.add_argument("--retire", nargs="+", metavar="ID",
                     help="its copies: superseded by the kept one, not deleted")
    pdu.add_argument("--distinct", nargs="+", metavar="ID",
                     help="these are not duplicates: do not offer them together again")
    pdu.add_argument("--undo", metavar="ID",
                     help="take a retirement back: the copies retired in favour of this memory are active again")
    pdu.set_defaults(func=cmd_dups)

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
    psv.add_argument("--host", default=os.environ.get("MEM_WEB_HOST", "127.0.0.1"),
                     help="address to bind (default: MEM_WEB_HOST or 127.0.0.1 = this machine only). "
                          "Use 0.0.0.0 to accept connections from the network — required in a "
                          "container, where publishing a port to a loopback-bound server produces "
                          "one that looks up and answers nothing")
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
    pla.add_argument("--action", default="inject", help="inject|prompt|brief|file|digest|get|search")
    pla.add_argument("--session", help="the session the access happened in")
    pla.add_argument("--reset", action="store_true",
                     help="the session's context was rebuilt (compaction, /clear) before this")
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
