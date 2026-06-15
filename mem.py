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
import datetime
import hashlib
import math
import os
import re
import shutil
import sqlite3
import struct
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
import redact  # noqa: E402


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

TYPES = ["gotcha", "fact", "decision", "command", "procedural", "preference", "todo", "status"]

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


def scope_file(scope):
    """Map a scope to its storage file."""
    if scope == "global":
        return GLOBAL_FILE
    if scope.startswith("project:"):
        slug = scope.split(":", 1)[1].strip()
        if not slug or "/" in slug or ".." in slug:
            sys.exit(f"invalid scope: {scope}")
        return os.path.join(PROJ_DIR, f"{slug}.md")
    sys.exit(f"unknown scope: {scope} (use 'global' or 'project:<slug>')")


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


def render_record(rid, rtype, scope, summary, body, confidence, source, created, updated, status,
                  priority=None, files=None):
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
    lines += [
        f"- confidence: {confidence}",
        f"- source: {source}",
        "",
        body.strip(),
        END_MARK,
    ]
    return "\n".join(lines) + "\n"


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
    ensure_header(path, a.scope)
    files = ", ".join(x.strip() for x in (a.files or "").split(",") if x.strip()) or None
    rid = gen_id()
    rec = render_record(rid, a.type, a.scope, summary, body,
                        a.confidence, a.source, now_ts(), now_ts(), "active",
                        priority="critical" if a.critical else None, files=files)
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n" + rec)
    crit = "  [CRITICAL]" if a.critical else ""
    print(f"added {rid}  [{a.type} · {a.scope}]{crit}  -> {os.path.relpath(path, DATA)}")


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
    if getattr(a, "json", False):
        import json
        out = [{
            "id": r["id"], "type": r["meta"].get("type"), "scope": r["meta"].get("scope"),
            "summary": record_summary(r), "status": r["meta"].get("status", "active"),
            "confidence": r["meta"].get("confidence"), "source": r["meta"].get("source"),
            "created": r["meta"].get("created"), "updated": r["meta"].get("updated"),
            "superseded_by": r["meta"].get("superseded-by"),
            "priority": r["meta"].get("priority"),
            "related_to": r["meta"].get("related-to"),
            "blocked_by": r["meta"].get("blocked-by"),
            "files": r["meta"].get("files"),
            "invalidated": r["meta"].get("invalidated"),
            "invalid_reason": r["meta"].get("invalid-reason"),
            "body": r["body"],
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
    print(f"\n{len(recs)} memories")


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
    tmp = idx + ".build"
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
                if i in by_id and _match_filters(by_id[i], a.scope, a.type, "all", since, until)]
        _print_mode(mode, len(hits))
        _print_hits(hits)
        return
    # fallback: substring scan (ripgrep when available) if FTS5 is missing
    q = a.query
    if shutil.which("rg"):
        try:
            out = subprocess.run(["rg", "-l", "-i", q, STORE], capture_output=True, text=True).stdout
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
            if ql in blob and _match_filters(r, a.scope, a.type, "all", since, until):
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
    con = sqlite3.connect(embed_path())
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


def _cosine(a, b):
    if len(a) != len(b):
        return 0.0
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return sum(x * y for x, y in zip(a, b)) / (na * nb) if na and nb else 0.0


def hybrid_search(query, allow_semantic=True):
    """Keyword (FTS5 + recency) fused with semantic similarity when an embedder is available.
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
    nkw = max(1, len(fts))
    kw = {i: 1.0 - (n / nkw) for n, i in enumerate(fts)}    # 1..0 by keyword rank
    cand = set(fts) | {i for i, _ in sorted(sims.items(), key=lambda x: -x[1])[:25]}
    ranked = sorted(cand, key=lambda i: 0.5 * sims.get(i, 0.0) + 0.5 * kw.get(i, 0.0), reverse=True)
    return ranked, "hybrid"


def cmd_embed(a):
    r = embed_index(force=a.force)
    if r is None:
        print("embedder unavailable — start Ollama and `ollama pull all-minilm` (or set MEM_EMBED_MODEL).")
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
    for path in store_files():
        recs = parse_file(path)
        for r in recs:
            if r["id"] != a.id:
                continue
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            # bi-temporal: record WHEN it stopped being valid (= when we learned) + WHY,
            # and keep it (supersede never deletes). Drop any prior copies so re-supersede is clean.
            inserts = []
            if a.by:
                inserts.append(f"- superseded-by: {a.by}\n")
            inserts.append(f"- invalidated: {now_ts()}\n")
            if getattr(a, "reason", None):
                inserts.append(f"- invalid-reason: {a.reason}\n")
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
            extra = f" (replaced by {a.by})" if a.by else ""
            extra += f" — {a.reason}" if getattr(a, "reason", None) else ""
            print(f"superseded {a.id}{extra}  in {os.path.relpath(path, DATA)}")
            return
    sys.exit(f"id not found: {a.id}")


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
    sys.exit(f"id not found: {rec_id}")


def _warn_unknown(ids):
    have = {r["id"] for r in all_records()}
    missing = [i for i in ids if i not in have]
    if missing:
        print(f"warning: unknown id(s): {', '.join(missing)}")


def cmd_link(a):
    _warn_unknown(a.others)
    rel, cur = _edit_list_meta(a.id, "related-to", add=a.others)
    print(f"linked {a.id} related-to: {', '.join(cur) or '(none)'}  in {rel}")


def cmd_unlink(a):
    rel, cur = _edit_list_meta(a.id, "related-to", remove=a.others)
    print(f"unlinked {a.id} related-to: {', '.join(cur) or '(none)'}  in {rel}")


def cmd_block(a):
    _warn_unknown(a.blockers)
    rel, cur = _edit_list_meta(a.todo, "blocked-by", add=a.blockers)
    print(f"{a.todo} blocked-by: {', '.join(cur) or '(none)'}  in {rel}")


def cmd_unblock(a):
    remove = a.blockers
    if not remove:  # no ids given -> clear all blockers
        cur0 = next((_list_meta(r, "blocked-by") for r in all_records() if r["id"] == a.todo), [])
        remove = cur0
    rel, cur = _edit_list_meta(a.todo, "blocked-by", remove=remove)
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


def cmd_audit(a):
    """Report records containing secret-like patterns. Never modifies anything."""
    recs = [r for r in all_records() if _match_filters(r, a.scope, None, "all")]
    findings = []
    for r in recs:
        labels = redact.scan(record_summary(r) + "\n" + r["body"])
        if labels:
            findings.append((r, labels))
    if not findings:
        print(f"audit clean: no secret-like patterns in {len(recs)} records")
        return
    for r, labels in findings:
        st = r["meta"].get("status", "active")
        flag = "" if st == "active" else f"  ({st})"
        print(f"{r['id']}  [{r['meta'].get('type','?')} · {r['meta'].get('scope','?')}]{flag}  -> {', '.join(labels)}")
        print(f"    {record_summary(r)[:100]}")
    print(f"\n{len(findings)} of {len(recs)} records contain secret-like patterns.")
    print("Nothing was modified — review them (edit in the web UI, or supersede + re-add redacted).")
    sys.exit(1)


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
    pa.add_argument("--critical", action="store_true",
                    help="critical action rule: ALWAYS injected, first, regardless of the budget")
    pa.set_defaults(func=cmd_add)

    pl = sub.add_parser("list", help="list memories")
    pl.add_argument("--scope")
    pl.add_argument("--type")
    pl.add_argument("--status", default="active", help="active|superseded|all")
    pl.add_argument("--since", help="created on/after (YYYY-MM-DD or 'YYYY-MM-DD HH:MM')")
    pl.add_argument("--until", help="created on/before (YYYY-MM-DD or 'YYYY-MM-DD HH:MM')")
    pl.add_argument("--json", action="store_true", help="JSON output (for tooling/tests)")
    pl.set_defaults(func=cmd_list)

    ps = sub.add_parser("search", help="search memories (FTS5 ranked)")
    ps.add_argument("query")
    ps.add_argument("--scope")
    ps.add_argument("--type")
    ps.add_argument("--since", help="created on/after (YYYY-MM-DD or 'YYYY-MM-DD HH:MM')")
    ps.add_argument("--until", help="created on/before (YYYY-MM-DD or 'YYYY-MM-DD HH:MM')")
    ps.add_argument("--no-semantic", action="store_true",
                    help="keyword-only (skip the semantic embedder even if it is available)")
    ps.set_defaults(func=cmd_search)

    pp = sub.add_parser("supersede", help="mark a memory as superseded (records when + why; never deletes)")
    pp.add_argument("id")
    pp.add_argument("--by", help="id of the record that replaces it")
    pp.add_argument("--reason", help="why it is no longer valid (kept for the audit trail)")
    pp.set_defaults(func=cmd_supersede)

    pr = sub.add_parser("propose", help="queue a candidate for human review (NOT written to the store)")
    pr.add_argument("--type", required=True, help=f"one of: {', '.join(TYPES)}")
    pr.add_argument("--scope", required=True, help="global or project:<slug>")
    pr.add_argument("--summary", required=True, help="one-line summary")
    pr.add_argument("--body", help="body (or pipe it on stdin)")
    pr.add_argument("--confidence", default="0.8")
    pr.add_argument("--source", default="claude:live")
    pr.add_argument("--no-redact", action="store_true", help="keep secret values verbatim (redacted by default)")
    pr.set_defaults(func=cmd_propose)

    pu = sub.add_parser("audit", help="report secret-like patterns in the store (read-only)")
    pu.add_argument("--scope")
    pu.set_defaults(func=cmd_audit)

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

    px = sub.add_parser("reindex", help="rebuild the derived FTS5 index (+ embeddings if Ollama is up)")
    px.set_defaults(func=cmd_reindex)

    pe = sub.add_parser("embed", help="build/refresh the optional semantic index (needs Ollama + embed model)")
    pe.add_argument("--force", action="store_true", help="re-embed everything, not just changed records")
    pe.set_defaults(func=cmd_embed)

    a = p.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
