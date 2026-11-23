#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""SessionStart hook (Claude Code): inject relevant memories into the session context.

Reads stdin JSON {cwd, source, session_id, ...}, scopes to the current project
(slug = basename of cwd), pulls active memories through mem.py and writes them
as markdown to stdout. Exit 0 => stdout text is added to the model context.

Registered once per `--part` (profile, rules, digests, knowledge). Claude Code caps a hook's
output at 10,000 characters *per hook*, and the profile plus the critical rules had grown to fill
one hook's worth on their own: measured on a 1,600-memory store, the knowledge sections were
getting 0 characters at the monorepo root and 733 inside a project with 131 memories. Nothing
errored -- the injection just stopped carrying knowledge. Split, each slice has its own ceiling.
Without `--part` the whole injection is one output, as before (an older settings.json).

Robust by design: ANY error => exit 0 with no output (never breaks session start).
"""
import datetime
import json
import os
import re
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

PARTS = ("profile", "rules", "digests", "knowledge")
# The ceiling for one part. Claude Code's cap is 10,000 CHARACTERS; past it the output is replaced
# by a file path and a 2,000-character preview -- an uncontrolled cut. Sizes here are UTF-8 BYTES,
# never fewer than the character count, so staying under this is staying under the cap.
PART_MAX = 9600
# Starts the line that says a part did not fit. The web UI's health check looks for it, so a rule
# that got cut turns a light red instead of only shortening a paragraph nobody rereads.
OVERFLOW_MARK = "> [mem0ry4ai] over the per-hook limit:"


# Said at the top of what is injected, because nothing else says it to an agent that is not
# Claude Code: a memory is a statement about the past. The store keeps records about files that
# were since renamed and flags that were since removed, and reads exactly as confident as the day
# it was written. (Same sentence in recall.py and MEM0RY4AI.md; a test holds the three together.)
READ_RULE = ("A memory records what was true when it was written: if it names a file, function "
             "or flag, check that it still exists before relying on it.")


def _count(n, one, many, ro=False):
    """'43 memories' / '43 de memorii'. Romanian wants `de` from twenty up, except ..01 to ..19."""
    de = " de" if ro and n >= 20 and not 1 <= n % 100 <= 19 else ""
    return f"{n}{de} {one if n == 1 else many}"


def _digest_title(summary):
    """A digest's summary without the word that files it ("Digest: Design" -> "Design")."""
    return re.sub(r"^\s*Digest(?:\s*\([^)]*\))?\s*:\s*", "", summary or "", flags=re.I)


def _notice(n_mem, n_rules, has_profile, n_digests=0):
    """The one line the USER is shown about this session start.

    Everything else this hook writes goes to the model. Without this the user never learns what
    their agent was handed, so an injection that has quietly gone wrong -- too little, the wrong
    project, nothing at all -- looks exactly like one that worked."""
    ro = os.environ.get("MEM_UI_LANG") == "ro"
    bits = [_count(n_mem, "memorie", "memorii", True) + " in context" if ro
            else _count(n_mem, "memory", "memories") + " in context"]
    if n_rules:
        bits.append(_count(n_rules, "regula", "reguli", True) if ro
                    else _count(n_rules, "standing rule", "standing rules"))
    if n_digests:
        bits.append(_count(n_digests, "digest", "digest-uri", True) if ro
                    else _count(n_digests, "digest", "digests"))
    if has_profile:
        bits.append("profil" if ro else "profile")
    return "mem0ry4ai: " + " · ".join(bits)


def _part():
    """Which slice this invocation emits. None = all of it in one output."""
    argv = sys.argv[1:]
    if "--part" in argv:
        i = argv.index("--part")
        if i + 1 < len(argv) and argv[i + 1] in PARTS:
            return argv[i + 1]
    return None


def _nbytes(s):
    return len(s.encode("utf-8"))


def _cut_bytes(s, n):
    """`s` cut to at most n UTF-8 bytes, never through the middle of a character."""
    return s.encode("utf-8")[:max(n, 0)].decode("utf-8", "ignore")


def _fit_bodies(bodies, room):
    """The per-body allowance that makes `bodies` fit in `room` bytes, or None if they already do.

    Water-filling: a body shorter than the allowance is kept whole, and the longer ones are all cut
    to the same length. So one very long rule gives way before any short rule loses a character --
    the opposite of a flat per-record cap, which cut a 4,000-character rule to its first quarter
    while leaving room unused."""
    sizes = sorted(_nbytes(b) for b in bodies)
    if sum(sizes) <= room:
        return None
    left, n = max(room, 0), len(sizes)
    for i, s in enumerate(sizes):
        share = left // (n - i)
        if s > share:
            return share
        left -= s
    return None


def _data_dir():
    """Same resolution as mem.py (MEM_DATA_DIR > plugin dir > code dir) — call AFTER _load_local_env."""
    d = os.environ.get("MEM_DATA_DIR")
    if d:
        return os.path.abspath(os.path.expanduser(d))
    if PLUGIN_MODE:
        return os.path.join(os.path.expanduser("~"), ".mem0ry4ai")
    return PROJ


def _export_session(session_id):
    """Tell the session's own shell commands which session they belong to.

    Claude Code hands a SessionStart hook CLAUDE_ENV_FILE: what is appended there is set for every
    command the agent runs afterwards. Without it mem.py can only guess the session from a marker
    file, and the marker belongs to whichever session started LAST -- with several open, a search
    or a new memory is credited to the wrong one. Only a plain id is ever written: the file is
    sourced by a shell."""
    path = os.environ.get("CLAUDE_ENV_FILE")
    sid = str(session_id or "")
    if not path or not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", sid):
        return
    line = f"export MEM_SESSION_ID={sid}\n"
    try:
        try:
            with open(path, encoding="utf-8") as f:
                if line in f.read():
                    return                  # a resume or a compaction starts the same session again
        except OSError:
            pass
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass


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


def _days_env(key, default):
    """A number of days from the env, kept within what a date can be moved by. A value typed by
    hand to mean "never" (999999) overflowed the date arithmetic and took the whole part down
    with it; a hundred years says the same thing and cannot."""
    return max(-36500, min(_int_env(key, default), 36500))


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
        if not os.path.exists(MEM) or os.environ.get("MEM_WEB_AUTOSTART", "1") == "0":
            return                      # the switch is for tests and for anyone who runs it themselves
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
    part = _part()
    # The parts run in parallel, as separate processes. Whatever must happen once per session start
    # (the web server, the session marker, the context-reset mark) belongs to exactly one of them.
    owner = part in (None, "knowledge")
    if owner:
        ensure_web_server()
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0
    cwd = data.get("cwd") or os.getcwd()
    # Not for a health check or a preview: nothing started, and the marker decides which session
    # the next memory is attributed to. A preview run with a made-up session id stamped that id on
    # four memories another session was writing at the time.
    if owner and (data.get("source") or "") not in ("health", "preview"):
        _write_session_marker(data.get("session_id"), cwd)   # provenance marker for `session:` on writes
        _export_session(data.get("session_id"))              # and the exact answer, where the harness allows
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
    if part:
        BUDGET = min(BUDGET, PART_MAX)   # a part is one hook's output: the harness cap is the ceiling
    RECORD_CAP = _int_env("MEM_INJECT_RECORD_CAP", "1000")

    # the user profile ("About me"): the single most-recent GLOBAL profile, matching the web editor.
    # Injected first, on its own, outside the budget; excluded from the lists below so it never doubles.
    # (Any other profile record — a CLI/manual duplicate or a project-scoped one — flows normally.)
    profile = sorted((r for r in recs if r.get("type") == "profile" and r.get("scope") == "global"),
                     key=lambda r: r.get("created") or "", reverse=True)[:1]
    prof_ids = {r["id"] for r in profile}

    critical = [r for r in recs if r["id"] not in prof_ids and r.get("priority") == "critical"]
    # Each part accounts for what IT emitted, so the three logs add up to one injection.
    if part in (None, "profile"):
        injected_ids.update(prof_ids)
    if part in (None, "rules"):
        injected_ids.update(r["id"] for r in critical)

    # Digests whose moment is "always": pages of preferences distilled by theme, approved by the
    # user. They have a part of their own -- their own hook, their own ceiling -- and every record
    # that waits for a moment stays out of the lists below, where it would be one more title.
    def waits(r):
        return bool((r.get("when") or "").strip())

    digests = [r for r in recs if r["id"] not in prof_ids and r.get("priority") != "critical"
               and "always" in [c.strip() for c in (r.get("when") or "").split(";")]
               and (r.get("body") or "").strip() and (r.get("scope") == "global" or not is_root)]

    normal = [r for r in recs if r["id"] not in prof_ids and r.get("priority") != "critical"
              and not (part and waits(r))]

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

    # A status says where something stands. One nobody has touched in two months is the last word
    # on a thing that is finished, or a snapshot of a thing that moved on without it -- and either
    # way it is not worth a line at the start of every session. It is NOT retired: the record
    # stays active and stays in every search, `mem.py stale` lists it, and a project's latest
    # still comes with that project's brief (the latest only: a brief carries one status). The
    # one exception is the status of the project the session started in: that is the "where was
    # I", however long ago.
    STATUS_DAYS = _days_env("MEM_INJECT_STATUS_DAYS", "60")
    status_cutoff = (datetime.datetime.now() - datetime.timedelta(days=STATUS_DAYS)).strftime("%Y-%m-%d")

    def aged(r):
        if STATUS_DAYS <= 0 or r.get("type") != "status":
            return False
        if not is_root and r.get("scope") == f"project:{slug}":
            return False
        stamp = (r.get("updated") or r.get("created") or "")[:10]
        return bool(stamp) and stamp < status_cutoff

    # A summary is cut for the LIST only; the store keeps every character, and the id that opens
    # the line is how the rest is read. Summaries have grown into paragraphs (median 156
    # characters), and each one costs a line that another memory could have had.
    TITLE_MAX = _int_env("MEM_INJECT_TITLE", "120")

    def listed(r):
        s = " ".join((r.get("summary") or "").split())
        return s if TITLE_MAX <= 0 or len(s) <= TITLE_MAX else s[:TITLE_MAX - 1].rstrip() + "…"

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
            else f"Summaries only; one in full: `{MEM_CMD} get <id>`. More: `{MEM_CMD} search \"...\"` or "
                 f"`{MEM_CMD} list --scope project:<slug>`.")
    if part == "profile":
        lines = ["# User profile (mem0ry4ai)", ""]
    elif part == "rules":
        lines = ["# Standing rules (mem0ry4ai)", ""]
    elif part == "digests":
        lines = ["# House rules (mem0ry4ai)",
                 "The user's standing preferences, distilled by theme and reviewed by them. Each line "
                 f"cites the memories it comes from; `{MEM_CMD} get <id>` opens one.", ""]
    else:
        lines = [
            "# Relevant memories (mem0ry4ai)",
            f"Persistent context for {where} (source: mem0ry4ai store/*.md). {hint}",
            READ_RULE,
            "",
        ]
    header_len = len(lines)

    # --- About me: the user profile, first of all, with body, outside the budget ---
    # Outside the budget, but not unbounded: this is the one section that was capped by nothing at
    # all, so an oversized profile silently blew past the whole budgeting system the settings page
    # exists to enforce. Its own generous cap (MEM_INJECT_PROFILE_CAP, 4000 chars — several times
    # the per-record cap, because the profile earns the room) keeps it first and full in practice
    # while making "it can never dominate the context" true rather than merely likely.
    if profile and part in (None, "profile"):
        PROFILE_CAP = _int_env("MEM_INJECT_PROFILE_CAP", "4000")
        if part:
            PROFILE_CAP = min(PROFILE_CAP, PART_MAX - 400)
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
    if critical and part in (None, "rules"):
        rules = sorted(critical, key=lambda r: (r["scope"] != "global", rec_date(r)))
        # A rule whose full text also lives in an always-loaded instruction file (`mirrored-in`)
        # is named here and not repeated: the agent already has the text, and repeating it was
        # costing a third of the room the rules have.
        heads, texts = {}, {}
        for r in rules:
            sl = "global" if r["scope"] == "global" else r["scope"].split(":", 1)[1]
            heads[r["id"]] = f"- **[{r['type']} · {sl}]** {r['summary']}"
            if (r.get("mirrored_in") or "").strip():
                heads[r["id"]] += (f"\n  (full text is in {r['mirrored_in'].strip()}; stored copy: "
                                   f"`{MEM_CMD} get {r['id']}`)")
            else:
                texts[r["id"]] = "\n".join(_body_lines((r.get("body") or "").strip(), "  "))
        cut_note = "\n  (cut to fit — `{cmd} get {rid}` for the rest)"
        allowance = None
        if part:
            # Own hook, own ceiling: whole bodies. If they still do not fit, the longest give way
            # first, and the output says so in a line the health check can see.
            warn = (f"{OVERFLOW_MARK} {{n}} of {len(rules)} critical rules below are cut short. "
                    f"Shorten one, unpin one, or mark one mirrored (`{MEM_CMD} mirror <id> CLAUDE.md`).")
            fixed = (_nbytes("\n".join(lines)) + _nbytes(warn) + 80
                     + sum(_nbytes(h) + 1 for h in heads.values())
                     + sum(_nbytes(cut_note.format(cmd=MEM_CMD, rid=i)) + 1 for i in texts))
            allowance = _fit_bodies(list(texts.values()), PART_MAX - fixed)
            if allowance is not None:
                lines.append(warn.format(n=sum(1 for t in texts.values() if _nbytes(t) > allowance)))
                lines.append("")
        lines.append("## Critical rules (mandatory in every task)")
        for r in rules:
            lines.append(heads[r["id"]])
            if r["id"] not in texts:
                continue
            text = texts[r["id"]]
            if part:
                if allowance is not None and _nbytes(text) > allowance:
                    text = _cut_bytes(text, allowance) + cut_note.format(cmd=MEM_CMD, rid=r["id"])
            else:
                body_str = (r.get("body") or "").strip()
                if len(body_str) > RECORD_CAP:
                    body_str = body_str[:RECORD_CAP] + f"\n  (body truncated by per-record cap of {RECORD_CAP} chars — search/get to read full)"
                text = "\n".join(_body_lines(body_str, "  "))
            lines.append(text)
        lines.append("")

    # --- Digests for every session: whole pages, in the order they were written ---
    if digests and part == "digests":
        for r in sorted(digests, key=rec_date):
            title = _digest_title(r["summary"])
            block = [f"## {title}"] + _body_lines((r.get("body") or "").strip()) + [""]
            lines.extend(block)
            if _nbytes("\n".join(lines)) > PART_MAX - 200:
                del lines[len(lines) - len(block):]
                lines.append(f"{OVERFLOW_MARK} the digest \"{title}\" does not fit at session start. "
                             f"Shorten it, or give it a moment of its own "
                             f"(`{MEM_CMD} digest {r['id']} --when paths=...`).")
                lines.append("")
                continue
            injected_ids.add(r["id"])

    size = lambda: len(("\n".join(lines)).encode("utf-8"))

    def emit_budgeted(title, rs, list_hint, limit=None):
        """Emit a section item by item while the budget allows; announce what was cut.

        `limit` caps THIS section (sub-allocation), so one big section cannot starve
        the ones after it; the global BUDGET still bounds the total.
        """
        rs = [r for r in rs if not aged(r)]
        if not rs:
            return True
        cap = min(limit or BUDGET, BUDGET)
        head_at = len(lines)
        lines.append(f"## {title}")
        shown = 0
        for r in rs:
            item = [f"- [{r['id']}] **[{r['type']}]** {listed(r)}{todo_note(r)}"]
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

    ROOT_RECENT_DAYS = _days_env("MEM_INJECT_ROOT_RECENT_DAYS", "30")
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
            live = [r for r in ordered(rs) if not aged(r)]
            shown = live[:ROOT_MAX_PER_PROJECT]
            block = [f"## Project: {name}"]
            block += [f"- [{r['id']}] **[{r['type']}]** {listed(r)}{todo_note(r)}" for r in shown]
            if len(rs) > len(shown):
                block.append(f"- (+{len(rs) - len(shown)} more — `{hint_cmd}`)")
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

    # What is stepped over is said, with where to find it: an omission nobody announces is how a
    # thing gets lost while still being on disk.
    old = [r for r in normal if aged(r)]
    old_note = ""
    if old and part in (None, "knowledge"):
        n = len(old)
        old_note = (f"({n} status{'es' if n != 1 else ''} older than {STATUS_DAYS} days "
                    f"{'are' if n != 1 else 'is'} not listed here. Nothing was retired: each is still "
                    f"active and searchable, a project's latest comes with that project's brief, and "
                    f"`{MEM_CMD} stale` lists them all.")
        # A global status has no project whose brief would bring it back. Those few stay named,
        # with their ids, so that leaving the list is never the same as leaving sight.
        homeless = [r for r in old if r.get("scope") == "global"]
        if homeless:
            def brief_name(r):
                s = " ".join((r.get("summary") or "").split())
                return f"[{r['id']}] " + (s if len(s) <= 60 else s[:59].rstrip() + "…")
            old_note += (" The global ones, which no brief brings back: "
                         + "; ".join(brief_name(r) for r in homeless[:6])
                         + (f"; +{len(homeless) - 6} more" if len(homeless) > 6 else "") + ".")
        old_note += ")"
        BUDGET -= _nbytes(old_note) + 2

    if part in ("profile", "rules", "digests"):
        pass                                   # the knowledge sections belong to the other part
    elif is_root:
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

    if old_note and len(lines) > header_len:
        lines += [old_note, ""]
    if len(lines) <= header_len:
        return 0                               # this part has nothing to say: say nothing
    out = "\n".join(lines).rstrip() + "\n"
    if part and _nbytes(out) > PART_MAX:
        # Last resort, and it should never run: every section above budgets itself. But the one
        # failure that must not happen is the harness swapping the whole output for a file path.
        out = _cut_bytes(out, PART_MAX - 120) + f"\n{OVERFLOW_MARK} output cut at {PART_MAX} bytes.\n"

    # What the user sees. One line, from the one part that owns the once-per-start work, and it
    # speaks for all three: the other parts' counts are known here, the records are the same.
    # Never for a health check or a preview -- the web UI reads this output as the injection text.
    source = data.get("source") or ""
    notice = ""
    if owner and source not in ("health", "preview") \
            and os.environ.get("MEM_RECALL_NOTICE", "1").strip().lower() not in ("0", "no", "false", "off"):
        crit_ids = {r["id"] for r in critical}
        notice = _notice(len(injected_ids - prof_ids - crit_ids), len(critical), bool(profile),
                         len(digests))
    if notice:
        # JSON only because `systemMessage` needs it; the text the model gets is byte-for-byte the
        # plain output, moved into `additionalContext`. Each field has its own 10,000-char cap.
        sys.stdout.write(json.dumps({"systemMessage": notice,
                                     "hookSpecificOutput": {"hookEventName": "SessionStart",
                                                            "additionalContext": out}},
                                    ensure_ascii=False))
    else:
        sys.stdout.write(out)

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
    if injected_ids and source not in ("health", "preview"):
        cmd = [sys.executable, MEM, "log-access", "--ids", ",".join(sorted(injected_ids)),
               "--action", "inject"]
        if data.get("session_id"):
            cmd += ["--session", str(data["session_id"])]
        if owner and source in ("compact", "clear"):
            cmd.append("--reset")      # the context was rebuilt: earlier pushes are no longer in it
        try:
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=_NO_WINDOW)
        except Exception:
            pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
