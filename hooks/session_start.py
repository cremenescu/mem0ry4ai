#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""SessionStart hook (Claude Code): inject relevant memories into the session context.

Reads stdin JSON {cwd, source, session_id, ...}, scopes to the current project
(slug = basename of cwd), pulls active memories through mem.py and writes them
as markdown to stdout. Exit 0 => stdout text is added to the model context.

Robust by design: ANY error => exit 0 with no output (never breaks session start).
"""
import datetime
import json
import os
import subprocess
import sys

HOOK_DIR = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HOOK_DIR)
MEM = os.path.join(PROJ, "mem.py")
REPO_ROOT = os.path.dirname(PROJ)  # monorepo root (parent of the mem0ry4ai folder)


def ensure_web_server():
    """Start the web UI (server_web.sh, idempotent) together with the Claude session.

    Fire-and-forget: we do not wait and do not report errors — injection must not depend on it.
    """
    try:
        script = os.path.join(PROJ, "server_web.sh")
        if os.access(script, os.X_OK):
            subprocess.Popen([script, "start"], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
    except Exception:
        pass


def main():
    ensure_web_server()
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0
    cwd = data.get("cwd") or os.getcwd()
    is_root = os.path.normpath(cwd) == os.path.normpath(REPO_ROOT)
    slug = os.path.basename(os.path.normpath(cwd))

    try:
        out = subprocess.run(
            [sys.executable, MEM, "list", "--status", "active", "--json"],
            capture_output=True, text=True, timeout=30,
        )
        recs = json.loads(out.stdout or "[]")
    except Exception:
        return 0

    if is_root:
        # monorepo root: inject an index of ALL projects + global
        recs = [r for r in recs
                if r.get("scope") == "global" or str(r.get("scope", "")).startswith("project:")]
    else:
        want = {"global", f"project:{slug}"}
        recs = [r for r in recs if r.get("scope") in want]
    if not recs:
        return 0

    by_scope = {}
    for r in recs:
        by_scope.setdefault(r["scope"], []).append(r)

    # Progressive disclosure: above the threshold inject summaries only, below it bodies too.
    BODY_THRESHOLD = 12
    include_bodies = len(recs) <= BODY_THRESHOLD

    where = "all projects" if is_root else f"project `{slug}`"
    hint = ("Look up details with `mem.py search \"...\"`." if include_bodies
            else "Summaries only; details: `mem.py search \"...\"` or `mem.py list --scope project:<slug>`.")
    lines = [
        "# Relevant memories (mem0ry4ai)",
        f"Persistent context for {where} (source: mem0ry4ai store/*.md). {hint}",
        "",
    ]

    def emit(scope, title):
        rs = by_scope.get(scope)
        if not rs:
            return
        lines.append(f"## {title}")
        for r in rs:
            lines.append(f"- **[{r['type']}]** {r['summary']}")
            if include_bodies:
                body = (r.get("body") or "").strip()
                for bl in body.splitlines():
                    lines.append(f"  {bl}")
        lines.append("")

    # Root mode: per-project cap + collapse for projects not touched recently (keeps injection lean).
    ROOT_RECENT_DAYS = 30
    ROOT_MAX_PER_PROJECT = 10
    TYPE_PRIO = {"status": 0, "todo": 1, "gotcha": 2, "decision": 3, "preference": 4, "fact": 5, "command": 6}

    def rec_date(r):
        return (r.get("created") or "")[:19]

    def emit_project_capped(scope):
        rs = by_scope[scope]
        name = scope.split(":", 1)[1]
        newest = max((rec_date(r) for r in rs), default="")
        cutoff = (datetime.datetime.now() - datetime.timedelta(days=ROOT_RECENT_DAYS)).strftime("%Y-%m-%d")
        if newest[:10] < cutoff:
            lines.append(f"## Project: {name}")
            lines.append(f"- ({len(rs)} memories, not touched recently — `mem.py list --scope {scope}`)")
            lines.append("")
            return
        # status/todo first, then the rest; within the same type, most recent first (stable double sort)
        rs = sorted(rs, key=rec_date, reverse=True)
        rs = sorted(rs, key=lambda r: TYPE_PRIO.get(r["type"], 9))
        shown, rest = rs[:ROOT_MAX_PER_PROJECT], rs[ROOT_MAX_PER_PROJECT:]
        lines.append(f"## Project: {name}")
        for r in shown:
            lines.append(f"- **[{r['type']}]** {r['summary']}")
        if rest:
            lines.append(f"- (+{len(rest)} more — `mem.py list --scope {scope}`)")
        lines.append("")

    emit("global", "Global")
    if is_root:
        for sc in sorted(s for s in by_scope if s.startswith("project:")):
            emit_project_capped(sc)
    else:
        emit(f"project:{slug}", f"Project: {slug}")

    sys.stdout.write("\n".join(lines).rstrip() + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
