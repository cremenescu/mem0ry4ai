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
import os
import re
import shutil
import sqlite3
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

TYPES = ["gotcha", "fact", "decision", "command", "preference", "todo", "status"]

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
                  priority=None):
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
    rid = gen_id()
    rec = render_record(rid, a.type, a.scope, summary, body,
                        a.confidence, a.source, now_ts(), now_ts(), "active",
                        priority="critical" if a.critical else None)
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
            "priority": r["meta"].get("priority"), "body": r["body"],
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
        con.execute("INSERT INTO mem (id, summary, body) VALUES (?, ?, ?)",
                    (r["id"], record_summary(r), r["body"]))
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
            "SELECT id FROM mem WHERE mem MATCH ? ORDER BY bm25(mem)", (match,)
        ).fetchall()
    except sqlite3.OperationalError:
        return None
    finally:
        con.close()
    return [r[0] for r in rows]


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


def cmd_search(a):
    since, until = _norm_date(a.since), _norm_date(a.until, end=True)
    ids = fts_search(a.query)
    if ids is not None:
        by_id = {r["id"]: r for r in all_records()}
        hits = [by_id[i] for i in ids
                if i in by_id and _match_filters(by_id[i], a.scope, a.type, "all", since, until)]
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
    _print_hits(hits)


def cmd_reindex(a):
    if build_index():
        print(f"FTS5 index rebuilt: {len(all_records())} records -> {os.path.relpath(index_path(), DATA)}")
    else:
        print("FTS5 unavailable in this sqlite — search falls back to substring scan.")


def cmd_supersede(a):
    for path in store_files():
        recs = parse_file(path)
        for r in recs:
            if r["id"] != a.id:
                continue
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            new_block = []
            for k in range(r["start"], r["end"] + 1):
                line = lines[k]
                if META_RE.match(line.rstrip("\n")):
                    mm = META_RE.match(line.rstrip("\n"))
                    if mm.group("k") == "status":
                        line = "- status: superseded\n"
                    elif mm.group("k") == "updated":
                        line = f"- updated: {now_ts()}\n"
                new_block.append(line)
            # insert superseded-by right after the status line when --by is given
            if a.by:
                rebuilt = []
                for line in new_block:
                    rebuilt.append(line)
                    if line.strip() == "- status: superseded":
                        rebuilt.append(f"- superseded-by: {a.by}\n")
                new_block = rebuilt
            lines[r["start"]:r["end"] + 1] = new_block
            with open(path, "w", encoding="utf-8") as f:
                f.writelines(lines)
            extra = f" (replaced by {a.by})" if a.by else ""
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
    ps.set_defaults(func=cmd_search)

    pp = sub.add_parser("supersede", help="mark a memory as superseded")
    pp.add_argument("id")
    pp.add_argument("--by", help="id of the record that replaces it")
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

    px = sub.add_parser("reindex", help="rebuild the derived FTS5 index from markdown")
    px.set_defaults(func=cmd_reindex)

    a = p.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
