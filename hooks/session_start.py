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
# plugin install: the agent cannot guess the plugin path — inject the full mem.py invocation
PLUGIN_MODE = f"{os.sep}.claude{os.sep}plugins{os.sep}" in PROJ + os.sep

# This hook is invoked by the (console-less) detached web server for the health check on every
# dashboard refresh; without this flag the `mem.py list` child below pops a cmd window each time.
# Windows-only — the flag does not exist on POSIX, where creationflags=0 is a no-op.
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def _data_dir():
    """Same resolution as mem.py (MEM_DATA_DIR > plugin dir > code dir) — call AFTER _load_local_env."""
    d = os.environ.get("MEM_DATA_DIR")
    if d:
        return os.path.abspath(os.path.expanduser(d))
    if PLUGIN_MODE:
        return os.path.join(os.path.expanduser("~"), ".mem0ry4ai")
    return PROJ


def _write_session_marker(session_id, cwd):
    """Record the current session id (staging/.session) so mem.py write paths can stamp `session:`
    provenance on memories written this session. Best-effort; never fail session start over it."""
    if not session_id:
        return
    import datetime
    try:
        staging = os.path.join(_data_dir(), "staging")
        os.makedirs(staging, exist_ok=True)
        with open(os.path.join(staging, ".session"), "w", encoding="utf-8") as f:
            json.dump({"session_id": session_id, "cwd": cwd,
                       "ts": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}, f)
    except Exception:
        pass


def _load_local_env():
    """Ingest .mem-local.env (KEY=VALUE) into os.environ via setdefault, so settings
    saved in the web UI — e.g. MEM_INJECT_BUDGET and the injection knobs below — are honored by THIS
    hook too, not just the web server. Self-contained on purpose: the hook never imports mem, so it
    stays bulletproof. setdefault => a real shell export still wins."""
    # Same resolution as mem.py: MEM_DATA_DIR wins, code dir is the fallback. Kept in step by hand
    # because this hook deliberately imports nothing — but it must agree, or the settings the web UI
    # saves and the settings the injection honours drift apart.
    d = os.environ.get("MEM_DATA_DIR")
    envfile = (os.path.join(os.path.abspath(os.path.expanduser(d)), ".mem-local.env") if d
               else os.path.join(PROJ, ".mem-local.env"))
    try:
        with open(envfile, encoding="utf-8") as f:
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


def _int_env(key, default):
    """int(os.environ[key]) but never raises — a malformed value in .mem-local.env (or the shell) must
    NOT break session start, so fall back to the default."""
    try:
        return int(os.environ.get(key, default))
    except (TypeError, ValueError):
        return int(default)


def _float_env(key, default):
    try:
        v = float(os.environ.get(key, default))
        return v if v == v and v not in (float("inf"), float("-inf")) else float(default)  # reject nan/inf
    except (TypeError, ValueError):
        return float(default)


def mem_cmd(cwd):
    """The mem.py invocation to show in hints — correct whether mem0ry4ai is the repo
    itself (standalone) or a subfolder of a monorepo: a path relative to where you work."""
    if PLUGIN_MODE:
        return f"python3 {MEM}"
    try:
        rel = os.path.relpath(MEM, cwd)
        return rel if not rel.startswith("../../") else MEM
    except Exception:
        return "mem.py"


def ensure_web_server():
    """Start the pure-Python web UI (`mem.py serve`) with the Claude session — cross-platform,
    idempotent (skips if the port is already served), fire-and-forget. Injection never depends on it.
    """
    try:
        if not os.path.exists(MEM):
            return
        import socket
        port = int(os.environ.get("MEM_WEB_PORT", "8841"))
        s = socket.socket()
        s.settimeout(0.3)
        busy = s.connect_ex(("127.0.0.1", port)) == 0   # already serving -> don't spawn a duplicate
        s.close()
        if busy:
            return
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen([sys.executable, MEM, "serve"], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, **kwargs)
    except Exception:
        pass


def _body_lines(body_str, indent=""):
    """Render a memory body into injected-context lines, neutralizing any line that would open a
    markdown heading (leading '#', with 0-3 leading spaces per CommonMark) so a memory body can't
    forge one of the hook's own trust-bearing sections — e.g. a normal memory (creatable by a
    low-trust agent write) whose body contains '## Critical rules (mandatory in every task)'. A
    backslash makes the '#' literal, so the line is no longer an ATX heading. The 2-space indent
    some callers use does NOT neutralize a heading on its own (CommonMark allows up to 3 leading
    spaces), which is why this escape is applied even to indented bodies."""
    out = []
    for bl in body_str.splitlines():
        out.append(indent + ("\\" + bl if bl.lstrip().startswith("#") else bl))
    return out


def main():
    _load_local_env()   # honor web-UI-saved settings (port, injection knobs) before anything reads them
    ensure_web_server()
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0
    cwd = data.get("cwd") or os.getcwd()
    _write_session_marker(data.get("session_id"), cwd)   # provenance marker for `session:` on writes
    is_root = os.path.normpath(cwd) == os.path.normpath(REPO_ROOT)
    slug = os.path.basename(os.path.normpath(cwd))
    MEM_CMD = mem_cmd(cwd)

    # --dest agent: everything below ends up in a model's context, so it goes through mem.py's
    # egress choke point first -- private memories never arrive here at all, and a redacted one
    # arrives without its body. Enforced there rather than in this file, so no surface can differ.
    # No near-duplicate suppression here on purpose — measured on a 799-record store, the pairs
    # that clear any firing threshold are stale statuses and adjacent-but-distinct facts, not
    # copies, and `consolidate` already proposes those for review at a lower bar. Hiding them
    # from the injection would lose information rather than save budget. See `dedup_near`.
    try:
        out = subprocess.run(
            [sys.executable, MEM, "list", "--status", "active", "--json", "--dest", "agent"],
            capture_output=True, text=True, timeout=30, creationflags=_NO_WINDOW,
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

    injected_ids = set()

    # BUDGET: the injection trims ITSELF below the harness threshold. If the output grew
    # unbounded, the harness would persist it to a file and show the model only a small
    # preview — an UNCONTROLLED cut that can silently drop a rule the agent must follow.
    # Here the cut is ours: critical rules always in, the rest fills the budget, anything
    # omitted is announced explicitly with the command that retrieves it.
    BUDGET = _int_env("MEM_INJECT_BUDGET", "8000")
    RECORD_CAP = _int_env("MEM_INJECT_RECORD_CAP", "1000")

    # the user profile ("About me"): the single most-recent GLOBAL profile, matching the web editor.
    # Injected first, on its own, outside the budget; excluded from the lists below so it never doubles.
    # (Any other profile record — a CLI/manual duplicate or a project-scoped one — flows normally.)
    profile = sorted((r for r in recs if r.get("type") == "profile" and r.get("scope") == "global"),
                     key=lambda r: r.get("created") or "", reverse=True)[:1]
    prof_ids = {r["id"] for r in profile}
    injected_ids.update(prof_ids)

    critical = [r for r in recs if r["id"] not in prof_ids and r.get("priority") == "critical"]
    injected_ids.update(r["id"] for r in critical)

    normal = [r for r in recs if r["id"] not in prof_ids and r.get("priority") != "critical"]

    by_scope = {}
    for r in normal:
        by_scope.setdefault(r["scope"], []).append(r)

    # blocked-todo annotation: a todo is "blocked" while a blocker is still an active todo
    active_by_id = {r["id"]: r for r in recs}

    def todo_note(r):
        if r.get("type") != "todo":
            return ""
        n = 0
        for b in (r.get("blocked_by") or "").split(","):
            x = active_by_id.get(b.strip())
            if x and x.get("status", "active") == "active" and x.get("type") == "todo":
                n += 1
        return f"  (blocked by {n})" if n else ""

    # Progressive disclosure: above the threshold inject summaries only, below it bodies too.
    BODY_THRESHOLD = _int_env("MEM_INJECT_BODY_THRESHOLD", "12")
    include_bodies = len(recs) <= BODY_THRESHOLD

    TYPE_PRIO = {"status": 0, "todo": 1, "gotcha": 2, "decision": 3, "preference": 4, "fact": 5, "command": 6}

    def rec_date(r):
        return (r.get("created") or "")[:19]

    def access_date(r):
        return (r.get("last_accessed") or r.get("created") or "")[:19]

    def ordered(rs):
        # Sort by access recency first, then creation date desc, then type priority
        rs = sorted(rs, key=rec_date, reverse=True)
        rs = sorted(rs, key=access_date, reverse=True)
        return sorted(rs, key=lambda r: TYPE_PRIO.get(r["type"], 9))

    where = "all projects" if is_root else f"project `{slug}`"
    hint = (f"Look up details with `{MEM_CMD} search \"...\"`." if include_bodies
            else f"Summaries only; details: `{MEM_CMD} search \"...\"` or `{MEM_CMD} list --scope project:<slug>`.")
    lines = [
        "# Relevant memories (mem0ry4ai)",
        f"Persistent context for {where} (source: mem0ry4ai store/*.md). {hint}",
        "",
    ]

    # --- About me: the user profile, first of all, with body, outside the budget ---
    # Outside the budget, but not unbounded: this is the one section that was capped by nothing at
    # all, so an oversized profile silently blew past the whole budgeting system the settings page
    # exists to enforce. Its own generous cap (MEM_INJECT_PROFILE_CAP, 4000 chars — several times
    # the per-record cap, because the profile earns the room) keeps it first and full in practice
    # while making "it can never dominate the context" true rather than merely likely.
    if profile:
        PROFILE_CAP = _int_env("MEM_INJECT_PROFILE_CAP", "4000")
        lines.append("## About me")
        for r in profile:
            body_str = (r.get("body") or "").strip()
            if len(body_str) > PROFILE_CAP:
                body_str = (body_str[:PROFILE_CAP]
                            + f"\n(profile truncated at {PROFILE_CAP} chars — shorten it, or raise "
                              f"MEM_INJECT_PROFILE_CAP)")
            lines.extend(_body_lines(body_str))
        lines.append("")

    # --- Critical rules: ALWAYS first, with bodies, outside the budget ---
    if critical:
        lines.append("## Critical rules (mandatory in every task)")
        for r in sorted(critical, key=lambda r: (r["scope"] != "global", rec_date(r))):
            sl = "global" if r["scope"] == "global" else r["scope"].split(":", 1)[1]
            lines.append(f"- **[{r['type']} · {sl}]** {r['summary']}")
            body_str = (r.get("body") or "").strip()
            if len(body_str) > RECORD_CAP:
                body_str = body_str[:RECORD_CAP] + f"\n  (body truncated by per-record cap of {RECORD_CAP} chars — search/get to read full)"
            lines.extend(_body_lines(body_str, "  "))
        lines.append("")

    size = lambda: len(("\n".join(lines)).encode("utf-8"))

    def emit_budgeted(title, rs, list_hint, limit=None):
        """Emit a section item by item while the budget allows; announce what was cut.

        `limit` caps THIS section (sub-allocation), so one big section cannot starve
        the ones after it; the global BUDGET still bounds the total.
        """
        if not rs:
            return True
        cap = min(limit or BUDGET, BUDGET)
        head_at = len(lines)
        lines.append(f"## {title}")
        shown = 0
        for r in rs:
            item = [f"- **[{r['type']}]** {r['summary']}{todo_note(r)}"]
            if include_bodies:
                body_str = (r.get("body") or "").strip()
                if len(body_str) > RECORD_CAP:
                    body_str = body_str[:RECORD_CAP] + f"\n  (body truncated by per-record cap of {RECORD_CAP} chars — search/get to read full)"
                item += _body_lines(body_str, "  ")
            lines.extend(item)
            if size() > cap - 120:   # reserve for the omission note
                del lines[len(lines) - len(item):]
                break
            shown += 1
            injected_ids.add(r["id"])
        if shown < len(rs):
            if shown == 0:
                del lines[head_at:]
                lines.append(f"## {title}")
                lines.append(f"- ({len(rs)} memories — `{list_hint}`)")
                lines.append("")
                return False
            lines.append(f"- (+{len(rs) - shown} omitted by budget — `{list_hint}`)")
            lines.append("")
            return False
        lines.append("")
        return True

    ROOT_RECENT_DAYS = _int_env("MEM_INJECT_ROOT_RECENT_DAYS", "30")
    ROOT_MAX_PER_PROJECT = _int_env("MEM_INJECT_ROOT_MAX_PER_PROJECT", "6")

    def emit_project_capped(scope):
        """Root mode: one capped block per project. Returns False when the budget is gone."""
        rs = by_scope[scope]
        name = scope.split(":", 1)[1]
        newest = max((rec_date(r) for r in rs), default="")
        cutoff = (datetime.datetime.now() - datetime.timedelta(days=ROOT_RECENT_DAYS)).strftime("%Y-%m-%d")
        hint_cmd = f"{MEM_CMD} list --scope {scope}"
        if newest[:10] < cutoff:
            block = [f"## Project: {name}",
                     f"- ({len(rs)} memories, not touched recently — `{hint_cmd}`)", ""]
        else:
            rs = ordered(rs)
            shown, rest = rs[:ROOT_MAX_PER_PROJECT], rs[ROOT_MAX_PER_PROJECT:]
            block = [f"## Project: {name}"]
            block += [f"- **[{r['type']}]** {r['summary']}{todo_note(r)}" for r in shown]
            if rest:
                block.append(f"- (+{len(rest)} more — `{hint_cmd}`)")
            block.append("")
            # Add to set of injected IDs
            for r in shown:
                injected_ids.add(r["id"])
        lines.extend(block)
        if size() > BUDGET - 120:   # reserve for the omission note
            # the full block does not fit: try the one-line index, still budget-tracked
            del lines[len(lines) - len(block):]
            oneliner = [f"## Project: {name}", f"- ({len(rs)} memories — `{hint_cmd}`)", ""]
            lines.extend(oneliner)
            if size() > BUDGET - 120:
                del lines[len(lines) - len(oneliner):]
                return False
        return True

    if is_root:
        # global gets at most ~40% of what remains after the rules — the rest belongs to
        # the project index (the value of root mode = "where was I, everywhere")
        split = _float_env("MEM_INJECT_GLOBAL_SPLIT", "0.4")
        glimit = size() + int((BUDGET - size()) * split)
        emit_budgeted("Global", ordered(by_scope.get("global", [])),
                      f"{MEM_CMD} list --scope global", limit=glimit)
        # recently touched projects first — under budget pressure they matter, not the alphabet
        scopes = sorted((s for s in by_scope if s.startswith("project:")),
                        key=lambda s: max((rec_date(r) for r in by_scope[s]), default=""),
                        reverse=True)
        for i, sc in enumerate(scopes):
            if not emit_project_capped(sc):
                left = len(scopes) - i
                lines.append(f"(+{left} projects omitted by budget — `{MEM_CMD} list --scope project:<slug>`)")
                lines.append("")
                break
    else:
        # the project first (status/todo = "where was I"), then global
        emit_budgeted(f"Project: {slug}", ordered(by_scope.get(f"project:{slug}", [])),
                      f"{MEM_CMD} list --scope project:{slug}")
        emit_budgeted("Global", ordered(by_scope.get("global", [])), f"{MEM_CMD} list --scope global")

    sys.stdout.write("\n".join(lines).rstrip() + "\n")

    # Log injected memory IDs asynchronously (fire-and-forget background process).
    #
    # Only for a REAL session. The web UI runs this same hook to measure injection size
    # ("health") and to preview it ("preview"); nothing was injected into anything, so counting
    # those as injections falsifies the access log — and the Docker healthcheck was calling the
    # health path every 30 seconds, forever.
    #
    # Skipping them also removes an orphan source. This child outlives the hook by design, so it
    # gets reparented to PID 1; inside the container PID 1 is `mem.py serve`, which never reaps.
    # 28.844 zombies accumulated on the NAS that way, one per healthcheck, until nothing on the
    # HOST could fork (2026-08-06 and 2026-08-17). The container also runs with an init now, but
    # not creating the orphan is the half that does not depend on how anyone starts it.
    if injected_ids and (data.get("source") or "") not in ("health", "preview"):
        try:
            subprocess.Popen(
                [sys.executable, MEM, "log-access", "--ids", ",".join(injected_ids), "--action", "inject"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=_NO_WINDOW
            )
        except Exception:
            pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
