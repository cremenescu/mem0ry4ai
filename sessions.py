#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""session_search — full-text search over PAST CONVERSATION TRANSCRIPTS.

Durable memory (mem.py) stores distilled knowledge; this searches the raw conversation history
instead — "what did we actually discuss weeks ago?". Zero LLM cost: a derived SQLite FTS5 index
over the Claude Code transcript .jsonl files that capture.py already records pointers to in
staging/sessions.jsonl. Returns real messages, never summarized (same idea as Hermes Agent's
session_search).

The index (store/.sessions.db) is derived + gitignored; rebuilt INCREMENTALLY on demand — only
sessions whose transcript changed since last run are re-read. Self-contained: every function takes
the data_dir explicitly, so this module never imports mem (no circular import).

Security note: this index is still a data-at-rest copy of your conversation history in ONE file.
Secrets are stripped before indexing (redact.redact, the same policy mem.py uses on its writes) and
the db is created owner-only (0600), but the plaintext of your dialogue lives there — keep it out of
anything world-readable/served. Text is capped per message so a huge pasted blob can't make search hang.
"""
import json
import os
import re
import sqlite3

import redact  # same-dir module; strips secrets before they land in the session index

MAX_MSG_CHARS = 12000  # cap per message: bounds FTS5 snippet() cost + index size on huge pasted blobs


def _db_path(data_dir):
    return os.path.join(data_dir, "store", ".sessions.db")


def _staging(data_dir):
    return os.path.join(data_dir, "staging", "sessions.jsonl")


# Injected/harness noise that isn't real dialogue — stripped before indexing so a search for
# "wireguard" doesn't match the memory block or a hook dump echoed inside a turn.
_NOISE = re.compile(
    r"<system-reminder>.*?</system-reminder>"
    r"|<local-command-[a-z-]+>.*?</local-command-[a-z-]+>"
    r"|<command-[a-z-]+>.*?</command-[a-z-]+>",
    re.S | re.I)


def _clean(text):
    return _NOISE.sub(" ", text or "").strip()


def _iter_pointers(data_dir):
    """Latest pointer record per session_id from staging/sessions.jsonl (last write wins)."""
    path = _staging(data_dir)
    latest = {}
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            sid, tp = rec.get("session_id"), rec.get("transcript_path")
            if sid and tp:
                latest[sid] = rec
    return list(latest.values())


def _extract_messages(path, session_ts):
    """Yield (role, text, ts) for user/assistant messages that carry real dialogue text."""
    out = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:   # one bad byte must not kill the index
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if d.get("type") not in ("user", "assistant") or d.get("isSidechain"):
                    continue
                msg = d.get("message")
                if not isinstance(msg, dict):
                    continue
                content = msg.get("content")
                if isinstance(content, str):          # user turns: content is the plain text
                    text = content
                elif isinstance(content, list):       # assistant turns: list of blocks; keep text blocks
                    text = "\n".join(b.get("text", "") for b in content
                                     if isinstance(b, dict) and b.get("type") == "text")
                else:
                    continue                          # tool_use / tool_result / other -> not dialogue
                text = _clean(text)
                if not text:
                    continue
                if redact.enabled():                      # keep live secrets out of the at-rest index
                    text = redact.redact(text)[0]
                if len(text) > MAX_MSG_CHARS:             # bound snippet() cost; be honest it was cut
                    text = text[:MAX_MSG_CHARS] + " …[truncated]"
                out.append((msg.get("role") or d.get("type"), text, d.get("timestamp") or session_ts or ""))
    except OSError:
        return []
    return out


def _connect(data_dir, rebuild=False):
    """Open the session index, creating the schema. Returns a connection, or None if FTS5 is absent."""
    db = _db_path(data_dir)
    os.makedirs(os.path.dirname(db), exist_ok=True)
    if rebuild and os.path.exists(db):
        os.remove(db)
    con = sqlite3.connect(db)
    try:
        # default unicode61 tokenizer: fine for space-delimited scripts (EN/RO). It does NOT segment
        # CJK, so a search for a Chinese/Japanese word inside a space-free run silently finds nothing —
        # acceptable for this store's languages; switch to tokenize='trigram' if CJK recall is needed.
        con.execute("CREATE VIRTUAL TABLE IF NOT EXISTS msgs USING fts5("
                    "session_id UNINDEXED, ts UNINDEXED, cwd UNINDEXED, project UNINDEXED, "
                    "role UNINDEXED, text)")
    except sqlite3.OperationalError:                  # FTS5 not compiled into this Python's sqlite
        con.close()
        if os.path.exists(db):
            os.remove(db)
        return None
    con.execute("CREATE TABLE IF NOT EXISTS indexed "
                "(session_id TEXT PRIMARY KEY, path TEXT, mtime REAL, nmsg INTEGER)")
    return con


def build_index(data_dir, rebuild=False):
    """Incrementally (re)build the FTS5 session index. Returns (n_sessions, n_messages) touched this
    run, or None if FTS5 is unavailable."""
    con = _connect(data_dir, rebuild=rebuild)
    if con is None:
        return None
    try:
        done = {row[0]: (row[1], row[2])
                for row in con.execute("SELECT session_id, path, mtime FROM indexed")}
        n_sessions = n_msgs = 0
        for rec in _iter_pointers(data_dir):
            sid, path = rec.get("session_id"), rec.get("transcript_path")
            if not sid or not path or not os.path.exists(path):
                continue
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            prev = done.get(sid)
            if prev and prev[0] == path and abs((prev[1] or 0) - mtime) < 1e-6:
                continue                              # unchanged since last index
            cwd = rec.get("cwd") or ""
            project = os.path.basename(cwd.rstrip("/")) or cwd
            msgs = _extract_messages(path, rec.get("captured_at") or "")
            con.execute("DELETE FROM msgs WHERE session_id=?", (sid,))
            con.executemany(
                "INSERT INTO msgs (session_id, ts, cwd, project, role, text) VALUES (?,?,?,?,?,?)",
                [(sid, ts, cwd, project, role, txt) for role, txt, ts in msgs])
            con.execute("INSERT OR REPLACE INTO indexed (session_id, path, mtime, nmsg) VALUES (?,?,?,?)",
                        (sid, path, mtime, len(msgs)))
            n_sessions += 1
            n_msgs += len(msgs)
        con.commit()
    except sqlite3.OperationalError:   # index locked by a concurrent builder — caller treats None as unavailable
        return None
    finally:
        con.close()
    try:
        os.chmod(_db_path(data_dir), 0o600)   # owner-only: it holds the plaintext of your conversations
    except OSError:
        pass
    return (n_sessions, n_msgs)


def search(query, data_dir, project=None, limit=20):
    """Search past conversation messages. Returns a list of hit dicts (newest-relevant first), [] on
    no match, or None if FTS5 is unavailable. Refreshes the index incrementally first."""
    if build_index(data_dir) is None:
        return None
    terms = re.findall(r"\w+", query or "", flags=re.UNICODE)
    if not terms:
        return []
    match = " OR ".join(f'"{t}"*' for t in terms)
    con = sqlite3.connect(_db_path(data_dir))
    try:
        rows = con.execute(
            "SELECT session_id, ts, cwd, project, role, "
            "snippet(msgs, 5, '[', ']', ' … ', 14), bm25(msgs) "
            "FROM msgs WHERE msgs MATCH ? ORDER BY bm25(msgs) LIMIT 500",
            (match,)).fetchall()
    except sqlite3.OperationalError:
        return None
    finally:
        con.close()
    out = []
    for sid, ts, cwd, proj, role, snip, _score in rows:
        if project and proj != project and os.path.basename((cwd or "").rstrip("/")) != project:
            continue
        out.append({"session_id": sid, "ts": ts or "", "cwd": cwd, "project": proj,
                    "role": role, "snippet": " ".join((snip or "").split())})
        if len(out) >= limit:
            break
    return out


def list_sessions(data_dir, project=None, limit=20):
    """Recent sessions (newest last-activity first): id, project, message count, span, first user line.
    Returns a list, or None if FTS5 is unavailable."""
    if build_index(data_dir) is None:
        return None
    con = sqlite3.connect(_db_path(data_dir))
    try:
        sess = con.execute("SELECT session_id, project, MAX(cwd), COUNT(*), MIN(ts), MAX(ts) "
                           "FROM msgs GROUP BY session_id").fetchall()
        out = []
        for sid, proj, cwd, n, tmin, tmax in sess:
            if project and proj != project:
                continue
            first = con.execute("SELECT text FROM msgs WHERE session_id=? AND role='user' "
                                "ORDER BY rowid LIMIT 1", (sid,)).fetchone()
            out.append({"session_id": sid, "project": proj, "cwd": cwd, "nmsg": n,
                        "start": tmin or "", "end": tmax or "",
                        "first": " ".join((first[0] if first else "").split())[:120]})
    finally:
        con.close()
    out.sort(key=lambda s: s["end"], reverse=True)
    return out[:limit]
