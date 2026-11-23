#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Recall at the point of action: a project's brief the first time a session works in that
project, and the memories anchored to a file the first time the session touches the file.

The other two injections are decided before any work happens. SessionStart chooses by working
directory and the per-prompt recall by the prompt's wording -- and on the store this was built
for, neither knows where the work is. 205 of 206 sessions start at the monorepo root, so each of
them is handed the same index: the five projects somebody touched last. A session that had spent
weeks in one project came back from a compaction with the status of five others.

The first path a tool touches says where the session is working, exactly, and says it when the
knowledge is needed. So this listens there:

  the brief    a project's latest status, the todos nothing blocks, its preferences, and the
               knowledge that has been opened most. Given when a tool first touches a path inside
               the project, or when a prompt names the project in full.
  the anchors  memories whose `files:` name the file a tool just touched.
  the digests  pages of preferences distilled by theme and approved by the user, each given at
               the moment its `when:` names: a file of a kind, a command of a kind.

Each is given once per session, and once more after the session's context is rebuilt. What a
session was already shown -- by this, by the per-prompt recall or at session start -- is not
shown again.

It runs after EVERY tool call, so the common case has to be nearly free: one small read of the
derived index, one of the access log, and no output. The store itself is parsed only when there
is something to say.
"""
import datetime
import fnmatch
import os
import re
import shlex
import sqlite3

import mem
import recall

# The tools whose input names a place on disk. hooks/tool_recall.py carries the matcher that
# registers the hook for them; a test keeps the two in step.
FILE_TOOLS = {"Read": "file_path", "Edit": "file_path", "Write": "file_path",
              "MultiEdit": "file_path", "NotebookEdit": "notebook_path"}
DIR_TOOLS = ("Grep", "Glob")
SHELL_TOOLS = ("Bash",)

HOOK_MAX = 9600                    # one hook's output; the harness swaps anything longer for a path
_OFF = ("0", "no", "false", "off")
_PUNCT = frozenset("();<>|&")
_ASSIGN = re.compile(r"[A-Za-z_]\w*=")
_WRAPPERS = frozenset(("sudo", "env", "time", "nohup", "exec", "command", "xargs", "nice", "caffeinate"))
_INTERPRETER = re.compile(r"(?:python|pypy|bash|sh|zsh|php|node|ruby|perl|swift)[\d.]*$")
# "ca la X", "like in X": X is a model to copy, not the place of the work.
_REFERENCE = re.compile(r"(?:\bca|\bprecum|\blike|\bas|\bsame as|\bsimilar(?: cu| to)?)\s+"
                        r"(?:(?:la|in|pe|din|si|de la|on|at|for|the|proiectul|project)\s+){0,2}$")


def _on(key, default="1"):
    return (os.environ.get(key, default) or "").strip().lower() not in _OFF


def session_key(data):
    """What "this session" means for a hook input: the session, or one subagent inside it.

    A subagent has a context of its own. A brief given to it is not in the main conversation, and
    one given to the main conversation is not in it -- so each is briefed on its own account."""
    sid = str(data.get("session_id") or "").strip()
    if not sid:
        return None
    agent = str(data.get("agent_id") or "").strip()
    return f"{sid}#{agent}" if agent else sid


# ----- from a tool call to a place in a project

def _aliases():
    """{folder: scope} from MEM_SCOPE_ALIASES ("folder=slug, other=slug"): a folder whose memories
    are filed under another project's name -- an app filed under the product it replaces, a fork
    filed under its upstream."""
    out = {}
    for pair in (os.environ.get("MEM_SCOPE_ALIASES") or "").split(","):
        folder, sep, slug = pair.partition("=")
        folder, slug = folder.strip().strip("/"), slug.strip()
        if sep and folder and slug:
            out[folder] = slug if slug.startswith("project:") else f"project:{slug}"
    return out


def _root():
    return os.path.realpath(mem._repo_root())


def locate(path, root=None):
    """(folder, rel): the top-level folder of the repo root that holds `path`, and the path inside
    that folder ("" for the folder itself). (None, None) for everything else -- a path outside
    the root, a file lying directly in it, a dot-folder (tool state, not a project), and the
    store: reading a memory file is using the memory, not working on the project that keeps it."""
    root = (root or _root()).rstrip(os.sep)
    try:
        p = os.path.realpath(path)
        store = os.path.realpath(mem.STORE)
    except (OSError, ValueError):
        return None, None
    if not p.startswith(root + os.sep) or p == store or p.startswith(store + os.sep):
        return None, None
    folder, _, rest = p[len(root) + 1:].partition(os.sep)
    if not folder or folder.startswith(".") or (not rest and not os.path.isdir(p)):
        return None, None
    return folder, rest.replace(os.sep, "/")


def scope_of(folder, known):
    """The scope whose memories describe `folder`, if the store has one."""
    alias = _aliases().get(folder)
    if alias in known:
        return alias
    plain = f"project:{folder}"
    if plain in known:
        return plain
    low = plain.lower()                     # a case-insensitive filesystem spells folders freely
    return next((s for s in known if s.lower() == low), None)


def _shell_places(command, cwd):
    """The existing files and directories a shell command names: [(path, is_file)].

    Not a shell parser, and it does not have to be one: a token counts only when it IS a path that
    exists, so a wrong split costs a miss and never invents a place. `cd X` moves the directory
    that later tokens are resolved against. The directory the command ran in counts as touched.

    What a command RUNS is not a place it works in. `tools/deploy.sh staging`, or the memory CLI
    itself, is run from every project; counting the program as touched would brief each session on
    the project its tools happen to live in. So the command word is skipped, and with it the script
    handed to an interpreter. Its arguments are what it works on."""
    out, here = [], cwd or ""
    if here:
        out.append((here, False))
    lex = shlex.shlex(command[:4000], posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    toks = []
    try:
        for tok in lex:
            toks.append(tok)
            if len(toks) >= 150:
                break
    except ValueError:              # an unclosed quote -- a heredoc, usually: keep what was read
        pass
    prev, head, script = "", True, False        # head: the next word is the program being run
    for tok in toks:
        word, was, prev = tok, prev, tok
        if not word or all(c in _PUNCT for c in word):
            prev, head, script = "", True, False
            continue
        if head:
            if _ASSIGN.match(word) or word in _WRAPPERS:
                continue                                    # VAR=x cmd, sudo cmd: still not there
            head, script = False, bool(_INTERPRETER.match(os.path.basename(word)))
            continue                                        # the program itself
        if word.startswith("-"):
            if "=" not in word:
                continue
            word = word.split("=", 1)[1]                    # --file=path
        elif script:
            script = False
            continue                                        # the script an interpreter runs
        if not word or len(word) > 500 or any(c in word for c in "*?$`{}\n"):
            continue
        if word.startswith("~"):
            word = os.path.expanduser(word)
        word = re.sub(r":\d+(?::\d+)?$", "", word)          # path:line[:col]
        path = word if os.path.isabs(word) else (os.path.join(here, word) if here else "")
        if not path:
            continue
        try:
            if os.path.isdir(path):
                out.append((path, False))
                if was in ("cd", "pushd"):
                    here = path
            elif os.path.isfile(path):
                out.append((path, True))
        except (OSError, ValueError):
            continue
    return out


def touched(data):
    """[(path, is_file)]: the places one tool call names."""
    tool = data.get("tool_name") or ""
    inp = data.get("tool_input")
    cwd = data.get("cwd") or ""
    if not isinstance(inp, dict):
        return []
    if tool in FILE_TOOLS:
        p = inp.get(FILE_TOOLS[tool])
        return [(p, True)] if isinstance(p, str) and p else []
    if tool in DIR_TOOLS:
        p = inp.get("path") or cwd
        if not isinstance(p, str) or not p:
            return []
        p = p if os.path.isabs(p) else os.path.join(cwd, p)
        return [(p, os.path.isfile(p))]
    if tool in SHELL_TOOLS:
        cmd = inp.get("command")
        return _shell_places(cmd, cwd) if isinstance(cmd, str) else []
    return []


# ----- what the store knows, asked cheaply

def _ro(path):
    """A read-only connection. The plain form CREATES a missing file, and an empty database at the
    index path reads as a fresh index with nothing in it."""
    uri = "file:" + path.replace("%", "%25").replace("?", "%3f").replace("#", "%23") + "?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=2.0)


def _index():
    """({scope: active records}, [(id, scope, files)], [(id, scope, when)]) from the derived
    index, or None. The third list is the digests that wait for a moment.

    This is the read that happens after every tool call, which is why the store is not asked:
    parsing 3,300 records takes 200 ms. An index from before the `meta` table existed is rebuilt,
    once. A stale index is used as it is -- scopes and anchors change rarely, and a rebuild here
    would put half a second on a tool call."""
    for attempt in (0, 1):
        if not os.path.exists(mem.index_path()):
            if attempt or not mem.build_index():
                return None
            continue
        con = None
        try:
            con = _ro(mem.index_path())
            known = dict(con.execute("SELECT scope, count(*) FROM meta WHERE status = 'active' "
                                     "GROUP BY scope"))
            anchors = con.execute("SELECT id, scope, files FROM meta WHERE status = 'active' "
                                  "AND files <> ''").fetchall()
            triggers = con.execute("SELECT id, scope, trig FROM meta WHERE status = 'active' "
                                   "AND trig <> ''").fetchall()
            return known, anchors, triggers
        except sqlite3.Error:
            if attempt or not mem.build_index():
                return None
        finally:
            if con is not None:
                con.close()
    return None


def _covers(anchor, rel, exact=False):
    """Whether a stored anchor names the file at `rel`. Whole path components only, as in
    `mem._match_anchor`, plus the direction that function has no use for: a directory anchor
    (trailing slash) covers every file under it. `exact` refuses the shorthand of naming a file
    by the tail of its path."""
    a = anchor.strip()
    if a.endswith("/"):
        return (rel + "/").startswith(a.strip("/") + "/")
    a = a.strip("/")
    if not a:
        return False
    return a == rel or (not exact and rel.endswith("/" + a))


def anchored(anchors, scope, folder, rel):
    """Ids of the records anchored to <folder>/<rel>, in index order.

    Inside the file's own project an anchor may be as short as a basename: the scope already says
    which project is meant. From anywhere else it has to spell the path from the repo root -- a
    global memory anchored to `config.php` is not about every project's config.php."""
    full = f"{folder}/{rel}"
    out = []
    for rid, sc, files in anchors:
        for a in (files or "").split(","):
            if sc == scope and (_covers(a, rel) or _covers(a, full, exact=True)) \
                    or sc != scope and _covers(a, full, exact=True):
                out.append(rid)
                break
    return out


def _reads(ids, days):
    """{id: (opened, listed)} from the recent access log: how often each was opened with `get`,
    and how often it turned up in a search."""
    out, ids = {}, list(ids)
    if not ids or not os.path.exists(mem.access_db_path()):
        return out
    since = (datetime.datetime.now() - datetime.timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        con = _ro(mem.access_db_path())
        try:
            for i in range(0, len(ids), 400):
                chunk = ids[i:i + 400]
                marks = ",".join("?" * len(chunk))
                for rid, opened, listed in con.execute(
                        f"SELECT id, sum(action = 'get'), sum(action = 'search') FROM access "
                        f"WHERE ts >= ? AND id IN ({marks}) GROUP BY id", [since] + chunk):
                    out[rid] = (opened or 0, listed or 0)
        finally:
            con.close()
    except sqlite3.Error:
        pass
    return out


# ----- what is said

def build_brief(scope, records, pushed=(), cwd=None):
    """(text, ids): a project's brief, or ("", []) when it would say nothing new.

    Four short sections, each the answer to something a person asks on walking back into a
    project: where it stands (the latest status), what is open (the todos nothing blocks), how
    the user wants it done (its preferences -- the type the session-start index never has room
    for), and what has been worth looking up (the knowledge opened most; newest first among
    equals, so a project nobody has read yet is still briefed with its latest)."""
    by_id = {r["id"]: r for r in records}
    mine = []
    for r in records:
        m = r["meta"]
        if m.get("scope") != scope or m.get("status", "active") != "active":
            continue
        if m.get("type") == "profile" or m.get("priority") == "critical":
            continue                        # both are injected at every session start
        # The egress choke point, on the freshly parsed record: a private memory never reaches a
        # model, however it was found.
        summary = mem.emit_for(r, mem.DEST_AGENT)[0]
        if summary is None:
            continue
        mine.append((r, summary))
    if not mine:
        return "", []

    def newest(pair):
        return pair[0]["meta"].get("created") or ""

    def of(rtype):
        return sorted((p for p in mine if p[0]["meta"].get("type") == rtype), key=newest, reverse=True)

    def fresh(pairs, n):
        return [p for p in pairs if p[0]["id"] not in pushed][:max(0, n)]

    status = of("status")[:1]
    todos = of("todo")
    ready = [p for p in todos if not mem._open_blockers(p[0], by_id)]
    rest = [p for p in mine if p[0]["meta"].get("type") not in ("status", "todo", "preference")]
    reads = _reads([p[0]["id"] for p in rest], recall._int("MEM_BRIEF_DAYS", "120"))

    def worth(pair):
        opened, listed = reads.get(pair[0]["id"], (0, 0))
        return 3.0 * opened + 0.3 * min(listed, 10)

    rest.sort(key=newest, reverse=True)
    rest.sort(key=worth, reverse=True)

    width = recall._int("MEM_BRIEF_TITLE", "240")
    open_head = f"Open todos ({len(ready)} ready"
    if len(todos) > len(ready):
        open_head += f", {len(todos) - len(ready)} blocked"
    open_head += ")"
    rows = []                                   # (section, id, line)
    for r, s in status:
        rows.append(("Where it stands", r["id"],
                     f"- [{r['id']}] {(r['meta'].get('created') or '')[:10]} — {recall._short(s, width)}"))
    n_status = len(rows)
    for r, s in fresh(ready, recall._int("MEM_BRIEF_TODOS", "4")):
        rows.append((open_head, r["id"], f"- [{r['id']}] {recall._short(s, width)}"))
    for r, s in fresh(of("preference"), recall._int("MEM_BRIEF_PREFS", "4")):
        rows.append(("How the user wants this project done", r["id"],
                     f"- [{r['id']}] {recall._short(s, width)}"))
    for r, s in fresh(rest, recall._int("MEM_BRIEF_KNOWLEDGE", "8")):
        rows.append(("Knowledge, most consulted first", r["id"],
                     f"- [{r['id']}] {r['meta'].get('type', '?')} — {recall._short(s, width)}"))
    # Nothing but a status the session already has: the session-start index covered this project.
    if len(rows) == n_status and (not status or status[0][0]["id"] in pushed):
        return "", []

    cmd = recall._mem_cmd(cwd)
    name = scope.split(":", 1)[1]
    out = [f"Project brief — {name} (mem0ry4ai: the first time this session works in this project). "
           "A memory records what was true when it was written: if it names a file, function or "
           f"flag, check that it still exists. One in full: `{cmd} get <id>`."]
    budget = recall._int("MEM_BRIEF_MAX_CHARS", "3500")
    ids, used, section = [], len(out[0]), None
    for sec, rid, line in rows:
        cost = len(line) + 1 + (len(sec) + 2 if sec != section else 0)
        if ids and used + cost > budget:
            break
        if sec != section:
            out.append(sec + ":")
            section = sec
        out.append(line)
        ids.append(rid)
        used += cost
    if len(mine) > len(ids):
        out.append(f"(+{len(mine) - len(ids)} more in this project: `{cmd} list --scope {scope}`)")
    return "\n".join(out), ids


def build_anchored(ids, records, where, cwd=None):
    """(text, [(id, summary)]) for the memories anchored to the file at `where`: the newest few,
    and a pointer to the rest. A file is noted once per session, so what does not fit is named,
    not saved for the next touch."""
    by_id = {r["id"]: r for r in records}
    picked = sorted((by_id[i] for i in ids if i in by_id),
                    key=lambda r: r["meta"].get("created") or "", reverse=True)
    width, most = recall._int("MEM_BRIEF_TITLE", "240"), recall._int("MEM_FILE_RECALL_MAX", "4")
    lines, shown, more = [], [], 0
    for r in picked:
        m = r["meta"]
        if m.get("status", "active") != "active":
            continue                    # retired since the index was built
        summary = mem.emit_for(r, mem.DEST_AGENT)[0]
        if summary is None:
            continue
        if len(shown) >= most:
            more += 1
            continue
        sc = m.get("scope", "")
        label = sc.split(":", 1)[1] if sc.startswith("project:") else sc
        lines.append(f"- [{r['id']}] {m.get('type', '?')} · {label} — {recall._short(summary, width)}")
        shown.append((r["id"], summary))
    if not shown:
        return "", []
    cmd = recall._mem_cmd(cwd)
    if more:
        lines.append(f"(+{more} more anchored to it: `{cmd} list --files {where.split('/', 1)[-1]}`)")
    return (f"Memories anchored to {where} (mem0ry4ai: written about this file at some earlier "
            f"time — check that they still hold; one in full: `{cmd} get <id>`).\n"
            + "\n".join(lines)), shown


def _count(n, ro):
    if ro:
        de = " de" if n >= 20 and not 1 <= n % 100 <= 19 else ""
        return f"{n}{de} {'memorie' if n == 1 else 'memorii'}"
    return f"{n} {'memory' if n == 1 else 'memories'}"


def notice(briefs, files, rules=()):
    """The one line the USER is shown: which rules arrived, which project was briefed, which
    file brought memories.

    For the reason the per-prompt notice exists. The rest of what is written here goes to the
    model only, so a brief for the wrong project -- a folder named in passing -- would be
    invisible to the one person who can tell."""
    if not (briefs or files or rules) or not _on("MEM_RECALL_NOTICE"):
        return ""
    ro = os.environ.get("MEM_UI_LANG") == "ro"
    bits = [f"{'reguli' if ro else 'rules'}: {title}" for title in rules]
    bits += [f"brief {name}: {_count(n, ro)}" for name, n in briefs]
    for where, shown in files:
        bits.append(f"{_count(len(shown), ro)} {'despre' if ro else 'about'} {where}: "
                    + " · ".join(f"{rid} {recall._short(title, 50)}" for rid, title in shown))
    return "mem0ry4ai · " + " · ".join(bits)


def _give_briefs(scopes, sk, records, pushed, cwd):
    """Build, claim and log the brief of each scope: ([text], [(name, count)])."""
    parts, said = [], []
    for sc in scopes:
        text, ids = build_brief(sc, records, pushed, cwd)
        if not mem.claim_seen(sk, "scope", sc):
            continue                    # a parallel tool call of the same turn got there first
        if not text:
            continue                    # nothing the session has not been shown: marked, and silent
        mem.log_access(ids, "brief", sk)
        pushed.update(ids)
        parts.append(text)
        said.append((sc.split(":", 1)[1], len(ids)))
    return parts, said


def _fit(parts):
    text = "\n\n".join(parts)
    if len(text) > HOOK_MAX:
        text = text[:HOOK_MAX - 60].rstrip() + "\n(cut at the limit of one hook's output)"
    return text


# ----- the two moments

def _command(data):
    """The shell command of a tool call, for the digests that wait for one."""
    inp = data.get("tool_input")
    cmd = inp.get("command") if isinstance(inp, dict) and data.get("tool_name") in SHELL_TOOLS else None
    return cmd if isinstance(cmd, str) else ""


def due_digests(triggers, places, command, scopes=()):
    """Ids of the digests whose moment this tool call is: a shell command their `commands=`
    matches, or a touched file one of their `paths=` globs matches (from the repo root). A digest
    that belongs to one project waits for a call that is in that project."""
    paths = [f"{folder}/{rel}" for folder, rel, is_file in places if is_file and rel]
    out = []
    for rid, scope, spec in triggers:
        if scope != "global" and scope not in scopes:
            continue
        try:
            when = mem.parse_when(spec)
        except ValueError:
            continue                    # hand-edited into something that does not parse: it cannot fire
        if (command and when["commands"] and when["commands"].search(command)) \
                or any(fnmatch.fnmatchcase(p, g) for p in paths for g in when["paths"]):
            out.append(rid)
    return out


def digest_title(summary):
    """A digest's summary without the word that files it ("Digest: Design" -> "Design")."""
    return re.sub(r"^\s*Digest(?:\s*\([^)]*\))?\s*:\s*", "", summary or "", flags=re.I)


def _give_digests(ids, sk, records, cwd):
    """Claim, log and render the digests that are due: ([text], [title])."""
    by_id = {r["id"]: r for r in records}
    parts, said = [], []
    for rid in ids:
        r = by_id.get(rid)
        if not r or r["meta"].get("status", "active") != "active":
            continue                    # retired, or never approved, whatever the index still says
        # The egress choke point: a private digest is given to nobody, and one whose body stays
        # on this machine has nothing to give.
        summary, body = mem.emit_for(r, mem.DEST_AGENT)
        if summary is None or not (body or "").strip():
            continue
        if not mem.claim_seen(sk, "digest", rid):
            continue                    # a parallel tool call of the same turn got there first
        mem.log_access([rid], "digest", sk)
        title = digest_title(summary)
        parts.append(f"House rules — {title} (mem0ry4ai digest [{rid}]: the user's standing "
                     "preferences on this, distilled and reviewed by them. Each line cites the "
                     f"memories it comes from; `{recall._mem_cmd(cwd)} get <id>` opens one).\n"
                     + body.strip())
        said.append(title)
    return parts, said


def on_tool(data):
    """(context, notice) for a tool call that just finished. ("", "") is the usual answer."""
    briefs_on, files_on, rules_on = _on("MEM_BRIEF"), _on("MEM_FILE_RECALL"), _on("MEM_DIGEST")
    sk = session_key(data)
    if not sk or not (briefs_on or files_on or rules_on):
        return "", ""                   # "once per session" cannot be kept without the session
    root = _root()
    places, dup = [], set()
    for path, is_file in touched(data):
        folder, rel = locate(path, root)
        if folder and (folder, rel) not in dup:
            dup.add((folder, rel))
            places.append((folder, rel, is_file))
    # A digest can wait for a command that names no place at all (a push, an ssh).
    command = _command(data) if rules_on else ""
    if not places and not command:
        return "", ""
    idx = _index()
    if idx is None:
        return "", ""
    known, anchors, triggers = idx
    scopes = {}                                 # folder -> scope, for folders the store knows
    for folder, _, _ in places:
        if folder not in scopes:
            sc = scope_of(folder, known)
            if sc:
                scopes[folder] = sc

    due = due_digests(triggers, places, command, set(scopes.values())) if rules_on and triggers else []
    if due:
        given = mem.seen_this_session(sk, "digest", due)
        due = [rid for rid in due if rid not in given]
    if not scopes and not due:
        return "", ""

    want = list(dict.fromkeys(scopes.values()))
    new = []
    if briefs_on and want:
        seen = mem.seen_this_session(sk, "scope", want)
        new = [sc for sc in want if sc not in seen]
        if len(new) > recall._int("MEM_BRIEF_AT_ONCE", "2"):
            new = []            # a command that walks many projects is a survey, not work in one
    hits = []                                   # (where, [ids])
    if files_on:
        for folder, rel, is_file in places:
            if is_file and rel and folder in scopes:
                ids = anchored(anchors, scopes[folder], folder, rel)
                if ids:
                    hits.append((f"{folder}/{rel}", ids))
        if hits:
            done = mem.seen_this_session(sk, "file", [where for where, _ in hits])
            hits = [(where, ids) for where, ids in hits if where not in done]
    if not new and not hits and not due:
        return "", ""

    pushed = mem.pushed_this_session(sk)
    # A file is noted once per session, whatever the note turns out to hold: the mark is made
    # here, before the store is read, so that a file whose memories were all shown already (or
    # are all private) costs this once and not on every touch.
    hits = [(where, ids) for where, ids in hits if mem.claim_seen(sk, "file", where)]
    if not new and not due and not any(i not in pushed for _, ids in hits for i in ids):
        return "", ""

    records = mem.all_records()
    cwd = data.get("cwd")
    # Rules first: if the hook's ceiling cuts anything, it is the end of a note, not a rule.
    rules, said_rules = _give_digests(due, sk, records, cwd)
    parts, said = _give_briefs(new, sk, records, pushed, cwd)
    files = []
    for where, ids in hits:
        text, shown = build_anchored([i for i in ids if i not in pushed], records, where, cwd)
        if not text:
            continue
        got = [rid for rid, _ in shown]
        mem.log_access(got, "file", sk)
        pushed.update(got)
        parts.append(text)
        files.append((where, shown))
    return _fit(rules + parts), notice(said, files, said_rules)


def named_projects(prompt, known):
    """Scopes the prompt names IN FULL, as the place of the work and not a model for it.

    The whole name, because a part of one is how a project gets mentioned in passing; and not
    after "like in", because that is a different request. A one-word name can also be an everyday
    word -- the client every project is for. Whether it is, the user's own prompts say: a word
    used in a fifth of all sessions is talk, and `recall` already keeps that count."""
    text = recall.fold(recall.own_words(prompt or ""))[:1500]
    found = []
    for scope in known:
        if not scope.startswith("project:"):
            continue
        name = recall.fold(scope.split(":", 1)[1])
        if len(name) < 4:
            continue
        m = re.search(r"(?<![\w.-])" + re.escape(name) + r"(?![\w-])", text)
        if m and not _REFERENCE.search(text[max(0, m.start() - 30):m.start()]):
            found.append((scope, name))
    plain = {name: (recall.stem(name) if len(name) >= 5 else name)
             for _, name in found if recall._TOKEN.fullmatch(name)}
    spread = recall._spread(list(plain.values())) if plain else {}
    return [scope for scope, name in found
            if name not in plain or recall._topicality(spread.get(plain[name], 0.0)) > 0]


def on_prompt(prompt, session=None, cwd=None):
    """(context, notice) for a prompt that names a project the session has not worked in yet."""
    if not session or not (_on("MEM_BRIEF") and _on("MEM_BRIEF_ON_PROMPT")):
        return "", ""
    if len((prompt or "").strip()) < recall._int("MEM_PROMPT_MIN_CHARS", "12"):
        return "", ""
    idx = _index()
    if idx is None:
        return "", ""
    named = named_projects(prompt, idx[0])
    if not named or len(named) > recall._int("MEM_BRIEF_AT_ONCE", "2"):
        return "", ""
    seen = mem.seen_this_session(session, "scope", named)
    new = [sc for sc in named if sc not in seen]
    if not new:
        return "", ""
    parts, said = _give_briefs(new, session, mem.all_records(), mem.pushed_this_session(session), cwd)
    return _fit(parts), notice(said, [])


if __name__ == "__main__":
    import sys
    args = sys.argv[1:]
    if len(args) == 2 and args[0] == "--file":
        # What touching this file would bring, for any session: nothing is marked or logged.
        folder, rel = locate(os.path.abspath(args[1]))
        idx = _index()
        sc = scope_of(folder, idx[0]) if folder and idx else None
        ids = anchored(idx[1], sc, folder, rel) if sc and rel else []
        print(build_anchored(ids, mem.all_records(), f"{folder}/{rel}")[0] or "(nothing anchored)")
    elif len(args) == 1:
        scope = args[0] if args[0].startswith("project:") else f"project:{args[0]}"
        print(build_brief(scope, mem.all_records())[0] or "(nothing to brief)")
    else:
        sys.exit("usage: brief.py <project-slug> | brief.py --file <path>")
