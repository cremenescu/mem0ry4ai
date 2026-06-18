#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""mem0ry4ai MCP server — hand-rolled stdio JSON-RPC 2.0 (stdlib only, NO SDK / NO pip install).

Exposes the memory store as MCP tools so ANY MCP-compatible runtime (Claude Code, Gemini, Cursor,
OpenCode, ...) can PULL durable memory on demand — complementary to the Claude Code SessionStart
hook, which PUSHes. Read tools are always on; memory_add (write) is gated by MEM_MCP_WRITE
(default on; set MEM_MCP_WRITE=0 to disable).

Transport: stdio, newline-delimited JSON-RPC. stdout carries ONLY protocol messages — anything else
(logs, banners) goes to stderr, or it corrupts the stream. Run via:  python3 mem.py mcp
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import mem  # noqa: E402  — the data layer (same store/parser as the CLI)

PROTOCOL_VERSION = "2025-06-18"
# Protocol revisions we understand; on initialize we echo the client's only if it's one of these.
SUPPORTED_VERSIONS = {"2024-11-05", "2025-03-26", "2025-06-18"}
WRITE_ENABLED = os.environ.get("MEM_MCP_WRITE", "1").strip().lower() not in ("0", "false", "no", "off")


def _log(msg):
    print(msg, file=sys.stderr, flush=True)


def _version():
    try:
        with open(os.path.join(HERE, ".claude-plugin", "plugin.json"), encoding="utf-8") as f:
            return json.load(f).get("version", "dev")
    except Exception:
        return "dev"


def _instructions():
    """The canonical agent guidance (MEM0RY4AI.md), surfaced to the LLM via the initialize result."""
    try:
        with open(os.path.join(HERE, "MEM0RY4AI.md"), encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ("mem0ry4ai durable memory. Call memory_search to recall gotchas/decisions/facts/"
                "preferences BEFORE answering; memory_get to load a record by id; memory_resume for "
                "a 'where was I' briefing. Save durable knowledge with memory_add (if enabled).")


# ---------- record -> text ----------
def _excerpt(r, n=200):
    b = " ".join((r.get("body") or "").split())
    return (b[:n] + "…") if len(b) > n else b


def _fmt(r, full=False):
    m = r["meta"]
    st = "" if m.get("status", "active") == "active" else f" ({m.get('status')})"
    head = f"[{r['id']}] {m.get('type', '?')} · {m.get('scope', '?')}{st}"
    if full:
        return f"{head}\n{mem.record_summary(r)}\n\n{r.get('body', '')}".rstrip()
    return f"{head}\n  {mem.record_summary(r)}\n  {_excerpt(r)}"


# ---------- tools (return text; strings starting 'error:' map to isError) ----------
def t_search(a):
    q = (a.get("query") or "").strip()
    if not q:
        return "error: query required"
    ids, mode = mem.hybrid_search(q)
    by = {r["id"]: r for r in mem.all_records()}
    if ids is None:   # FTS5 unavailable -> simple substring scan
        ql = q.lower()
        ids = [r["id"] for r in by.values() if ql in (mem.record_summary(r) + " " + r.get("body", "")).lower()]
        mode = "substring"
    scope, typ, limit = a.get("scope"), a.get("type"), int(a.get("limit") or 10)
    hits = []
    for i in ids:
        r = by.get(i)
        if not r or (scope and r["meta"].get("scope") != scope) or (typ and r["meta"].get("type") != typ):
            continue
        hits.append(r)
        if len(hits) >= limit:
            break
    if not hits:
        return f"(no matches for {q!r})"
    return f"{len(hits)} match(es) [{mode}]:\n\n" + "\n\n".join(_fmt(r) for r in hits)


def t_get(a):
    rid = (a.get("id") or "").strip()
    r = next((x for x in mem.all_records() if x["id"] == rid), None)
    return _fmt(r, full=True) if r else f"(no record {rid})"


def t_list(a):
    scope, typ = a.get("scope"), a.get("type")
    status, limit = (a.get("status") or "active"), int(a.get("limit") or 30)
    out = [r for r in mem.all_records()
           if (status == "all" or r["meta"].get("status", "active") == status)
           and (not scope or r["meta"].get("scope") == scope)
           and (not typ or r["meta"].get("type") == typ)]
    out.sort(key=lambda r: r["meta"].get("created", ""), reverse=True)
    out = out[:limit]
    return (f"{len(out)} record(s):\n\n" + "\n".join(_fmt(r) for r in out)) if out else "(no records)"


def t_resume(a):
    scope = a.get("scope")
    recs = [r for r in mem.all_records() if r["meta"].get("status", "active") == "active"
            and (not scope or r["meta"].get("scope") == scope)]
    newest = lambda rs: sorted(rs, key=lambda r: r["meta"].get("created", ""), reverse=True)
    status = newest([r for r in recs if r["meta"].get("type") == "status"])[:1]
    todos = newest([r for r in recs if r["meta"].get("type") == "todo"])[:8]
    recent = newest(recs)[:8]
    parts = []
    if status:
        parts.append("STATUS:\n" + _fmt(status[0], full=True))
    if todos:
        parts.append("OPEN TODOS:\n" + "\n".join(_fmt(r) for r in todos))
    if recent:
        parts.append("RECENT:\n" + "\n".join(_fmt(r) for r in recent))
    return "\n\n".join(parts) or "(nothing to resume)"


def t_add(a):
    if not WRITE_ENABLED:
        return "error: writing is disabled (set MEM_MCP_WRITE=1 to enable)"
    try:
        rid = mem.add_memory((a.get("type") or "").strip(), (a.get("scope") or "").strip(),
                             (a.get("summary") or "").strip(), a.get("body") or "",
                             (a.get("confidence") or "0.85"), "mcp")
        return f"saved {rid}"
    except Exception as e:
        return f"error: {e}"


TOOLS = [
    {"name": "memory_search", "fn": t_search,
     "description": "Search durable memory (hybrid keyword+semantic). Call BEFORE answering to recall "
                    "gotchas, decisions, infra facts, commands, and the user's preferences.",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string"}, "scope": {"type": "string", "description": "global or project:<slug>"},
         "type": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["query"]}},
    {"name": "memory_get", "fn": t_get,
     "description": "Get one memory record (full body) by id.",
     "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}},
    {"name": "memory_list", "fn": t_list,
     "description": "List/browse memories, newest first; filter by scope/type/status.",
     "inputSchema": {"type": "object", "properties": {
         "scope": {"type": "string"}, "type": {"type": "string"},
         "status": {"type": "string", "description": "active|superseded|all"}, "limit": {"type": "integer"}}}},
    {"name": "memory_resume", "fn": t_resume,
     "description": "'Where was I' briefing for a scope: latest status + open todos + recent memories.",
     "inputSchema": {"type": "object", "properties": {"scope": {"type": "string"}}}},
    {"name": "memory_add", "fn": t_add,
     "description": "Save a durable memory (secrets are redacted). type: gotcha|fact|decision|command|"
                    "preference|procedural|todo|status. scope: global or project:<slug>. Write "
                    "AGENT-NEUTRAL (2nd person, not tied to one model) — the store is shared across "
                    "agents. To REVISE a memory there is no update/delete tool: supersede the old + add "
                    "a new one (CLI/web). With several agents writing, memory_search first to avoid dupes.",
     "inputSchema": {"type": "object", "properties": {
         "type": {"type": "string"}, "scope": {"type": "string"}, "summary": {"type": "string"},
         "body": {"type": "string"}, "confidence": {"type": "string"}},
         "required": ["type", "scope", "summary", "body"]}},
]
TOOLS_BY_NAME = {t["name"]: t for t in TOOLS}


def _result(mid, result):
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _err(mid, code, msg):
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": msg}}


def handle(msg):
    mid, method, params = msg.get("id"), msg.get("method"), (msg.get("params") or {})
    if mid is None:   # a notification (e.g. notifications/initialized) — never gets a response
        return None
    if method == "initialize":
        client_ver = params.get("protocolVersion")
        return _result(mid, {
            # echo the client's version if we support it, else advertise our newest
            "protocolVersion": client_ver if client_ver in SUPPORTED_VERSIONS else PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "mem0ry4ai", "version": _version()},
            "instructions": _instructions()})
    if method == "ping":
        return _result(mid, {})
    if method == "tools/list":
        return _result(mid, {"tools": [{"name": t["name"], "description": t["description"],
                                        "inputSchema": t["inputSchema"]} for t in TOOLS]})
    if method == "tools/call":
        name = params.get("name")
        if not name:
            return _err(mid, -32602, "missing required parameter: name")
        tool = TOOLS_BY_NAME.get(name)
        if not tool:
            return _err(mid, -32602, f"unknown tool: {name}")
        try:
            text = tool["fn"](params.get("arguments") or {})
        except Exception as e:
            text = f"error: {e}"
        is_err = isinstance(text, str) and text.startswith("error:")
        return _result(mid, {"content": [{"type": "text", "text": text}], "isError": is_err})
    return _err(mid, -32601, f"method not found: {method}")


def serve_stdio():
    _log(f"mem0ry4ai MCP server (stdio) — write {'ON' if WRITE_ENABLED else 'OFF'} · data {mem.DATA}")
    while True:
        line = sys.stdin.readline()   # readline (not `for line in`): no read-ahead, process per message
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        try:
            resp = handle(msg)
        except Exception as e:
            resp = _err(msg.get("id"), -32603, str(e))
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    serve_stdio()
