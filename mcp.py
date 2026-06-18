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


def _essentials():
    """Render the GLOBAL non-negotiables — the user profile + every critical rule — straight from the
    store, to PUSH them in the MCP `instructions`. This is the equivalent, for hook-less agents, of what
    the Claude Code SessionStart hook injects: the must-haves are present before the model's first turn,
    instead of relying on it to call a tool. Global only — at initialize the server doesn't yet know the
    project (roots arrive after init), so project context stays a memory_resume/memory_search pull."""
    try:
        recs = [r for r in mem.all_records() if r["meta"].get("status", "active") == "active"]
    except Exception:
        return ""
    profile = sorted((r for r in recs if r["meta"].get("type") == "profile"
                      and r["meta"].get("scope") == "global"),
                     key=lambda r: r["meta"].get("created", ""), reverse=True)[:1]
    critical = sorted((r for r in recs if r["meta"].get("priority") == "critical"),
                      key=lambda r: (r["meta"].get("scope", "") != "global", r["meta"].get("created", "")))
    if not profile and not critical:
        return ""
    parts = []
    if profile:
        parts.append("## About this user\n" + (profile[0].get("body") or "").strip())
    if critical:
        rules = []
        for r in critical:
            body = (r.get("body") or "").strip()
            rules.append(f"- **{mem.record_summary(r)}**"
                         + ("\n  " + body.replace("\n", "\n  ") if body else ""))
        parts.append("## Critical rules — follow in every task\n" + "\n".join(rules))
    return "\n\n".join(parts)


def _instructions(client=""):
    """Agent guidance (MEM0RY4AI.md) + the user's global essentials, surfaced via the initialize result.

    The essentials (profile + critical rules) are pushed for hook-less clients. Claude Code already gets
    them from its SessionStart hook, so we skip them there to avoid double-injecting the same rules."""
    try:
        with open(os.path.join(HERE, "MEM0RY4AI.md"), encoding="utf-8") as f:
            base = f.read()
    except OSError:
        base = ("mem0ry4ai durable memory. Call memory_search to recall gotchas/decisions/facts/"
                "preferences BEFORE answering; memory_get to load a record by id; memory_resume for "
                "a 'where was I' briefing. Save durable knowledge with memory_add (if enabled).")
    if "claude" in (client or "").lower():   # Claude Code's SessionStart hook already pushes these
        return base
    ess = _essentials()
    if not ess:
        return base
    return (base + "\n\n---\n\n# Already recalled for you — the user's profile & standing rules\n"
            "(Follow these; don't re-derive them. They were pushed at connect so you start informed.)\n\n"
            + ess + "\n\nThis is the GLOBAL slice. For the project you're working in, call `memory_resume` "
            "(scope `project:<the project folder name>`) at the start, and `memory_search` as you go.")


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
    matched = [by[i] for i in ids
               if i in by and by[i]["meta"].get("status", "active") != "working"  # scratch notes aren't recall
               and (not scope or by[i]["meta"].get("scope") == scope)
               and (not typ or by[i]["meta"].get("type") == typ)]
    hits = matched[:limit]
    if not hits:
        return f"(no matches for {q!r})"
    head = f"{len(hits)} match(es) [{mode}]"
    if len(matched) > len(hits):   # never truncate silently — tell the caller more exist
        head += f" — showing top {len(hits)} of {len(matched)}; pass a higher `limit` for the rest"
    return head + ":\n\n" + "\n\n".join(_fmt(r) for r in hits)


def t_get(a):
    # fragment refs: id may be "<id>", "<id>:5", "<id>:5-9" (leading @ ok); or an explicit `lines` arg
    rid, rng = mem.parse_ref(a.get("id") or "")
    if a.get("lines"):
        _, rng = mem.parse_ref(f"{rid}:{a['lines']}")
    r = mem.get_record(rid)
    if not r:
        return f"(no record {rid})"
    m = r["meta"]
    st = m.get("status", "active")
    head = f"[{r['id']}] {m.get('type', '?')} · {m.get('scope', '?')}" + ("" if st == "active" else f" ({st})")
    return f"{head}\n{mem.record_summary(r)}\n\n{mem.number_body(r.get('body', ''), rng)}".rstrip()


def t_list(a):
    scope, typ = a.get("scope"), a.get("type")
    status, limit = (a.get("status") or "active"), int(a.get("limit") or 30)
    matched = [r for r in mem.all_records()
               if (status == "all" or r["meta"].get("status", "active") == status)
               and (not scope or r["meta"].get("scope") == scope)
               and (not typ or r["meta"].get("type") == typ)]
    matched.sort(key=lambda r: r["meta"].get("created", ""), reverse=True)
    out = matched[:limit]
    if not out:
        return "(no records)"
    head = f"{len(out)} record(s)"
    if len(matched) > len(out):   # never truncate silently — tell the caller more exist
        head += f" — showing newest {len(out)} of {len(matched)}; pass a higher `limit` for the rest"
    return head + ":\n\n" + "\n".join(_fmt(r) for r in out)


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


def t_note(a):
    """Write a WORKING (scratch) note — status=working, not injected, hidden from default recall."""
    if not WRITE_ENABLED:
        return "error: writing is disabled (set MEM_MCP_WRITE=1 to enable)"
    try:
        rid = mem.add_memory((a.get("type") or "fact").strip(), (a.get("scope") or "").strip(),
                             (a.get("summary") or "").strip(), a.get("body") or "",
                             (a.get("confidence") or "0.85"), "mcp", status="working")
        return f"working note {rid} — promote it with memory_promote once it earns a durable place"
    except Exception as e:
        return f"error: {e}"


def t_promote(a):
    if not WRITE_ENABLED:
        return "error: writing is disabled (set MEM_MCP_WRITE=1 to enable)"
    rid = (a.get("id") or "").strip()
    try:
        res = mem.promote_memory(rid)
    except Exception as e:
        return f"error: {e}"
    if res is None:
        return f"error: no record {rid}"
    if res is False:
        return f"error: {rid} is not a working note"
    return f"promoted {rid} (working -> active)"


TOOLS = [
    {"name": "memory_search", "fn": t_search,
     "description": "Search durable memory (hybrid keyword+semantic). Call BEFORE answering to recall "
                    "gotchas, decisions, infra facts, commands, and the user's preferences.",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string"}, "scope": {"type": "string", "description": "global or project:<slug>"},
         "type": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["query"]}},
    {"name": "memory_get", "fn": t_get,
     "description": "Get one memory by id, body shown with 1-based line numbers. Fragment refs: pass id "
                    "as '<id>', '<id>:5' or '<id>:5-9' (or a separate `lines` arg like '5-9') to get only "
                    "those lines — handy for citing a precise line of a longer memory.",
     "inputSchema": {"type": "object", "properties": {
         "id": {"type": "string", "description": "record id, optionally '<id>:<line>[-<line>]'"},
         "lines": {"type": "string", "description": "optional line range, e.g. '5' or '5-9'"}},
         "required": ["id"]}},
    {"name": "memory_list", "fn": t_list,
     "description": "List/browse memories, newest first; filter by scope/type/status. (status 'working' "
                    "lists scratch notes; they are hidden from search and from status 'active'.)",
     "inputSchema": {"type": "object", "properties": {
         "scope": {"type": "string"}, "type": {"type": "string"},
         "status": {"type": "string", "description": "active|working|superseded|all"},
         "limit": {"type": "integer"}}}},
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
    {"name": "memory_note", "fn": t_note,
     "description": "Jot a WORKING (scratch) note for the current task — saved with status=working: NOT "
                    "injected at session start and hidden from default search/list, so it won't pollute "
                    "durable recall. Use for in-progress findings you're not yet sure are durable, then "
                    "memory_promote the ones that earn a place. Same fields as memory_add (type defaults "
                    "to 'fact'). Review them with memory_list status='working'.",
     "inputSchema": {"type": "object", "properties": {
         "type": {"type": "string"}, "scope": {"type": "string"}, "summary": {"type": "string"},
         "body": {"type": "string"}, "confidence": {"type": "string"}},
         "required": ["scope", "summary", "body"]}},
    {"name": "memory_promote", "fn": t_promote,
     "description": "Promote a working note to a durable memory (status working -> active, so it starts "
                    "being injected and searchable). Give the note's id.",
     "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}},
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
        client = (params.get("clientInfo") or {}).get("name", "")
        return _result(mid, {
            # echo the client's version if we support it, else advertise our newest
            "protocolVersion": client_ver if client_ver in SUPPORTED_VERSIONS else PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "mem0ry4ai", "version": _version()},
            "instructions": _instructions(client)})
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
