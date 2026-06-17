#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""mem0ry4ai web UI — pure Python (stdlib only), the cross-platform successor to the PHP UI.

Imports mem.py for the data layer (parser, store, search, embeddings) and renders the same pages
the PHP UI served — identical HTML/CSS/JS in the browser, only the engine changes. Zero external
dependencies (no PHP, no pip): runs anywhere Python 3.9+ runs. Started via `mem.py serve`.

Migration status: ported pages are served here; the rest still live in PHP until ported. Until full
parity the PHP server keeps running for real use and this one is developed/tested alongside.
"""
import html
import http.server
import json
import os
import re
import secrets
import socketserver
import subprocess
import sys
import threading
import time
import urllib.parse
from http.cookies import SimpleCookie

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import mem  # the data layer (same store, same parser as the CLI)

ASSETS_DIR = os.path.join(HERE, "web", "assets")
# Local single-user tool: one CSRF token (double-submit on POST forms) is adequate. Persisted in
# staging/ (already gitignored, never synced) so it survives a server restart: the SessionStart
# hook relaunches the server and we restart it on upgrades, and a per-process token would break
# every already-open form with an opaque "CSRF" error.
def _load_or_make_csrf():
    path = os.path.join(mem.DATA, "staging", ".csrf")
    try:
        tok = open(path, encoding="utf-8").read().strip()
        if len(tok) >= 16:
            return tok
    except OSError:
        pass
    tok = secrets.token_hex(16)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(tok)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except OSError:
        pass
    return tok


_CSRF = _load_or_make_csrf()

# When the server runs detached (no console — e.g. launched by the SessionStart hook),
# every child process (git, the hook subprocess) pops its own cmd window that flashes on
# each request. CREATE_NO_WINDOW suppresses it. The flag only exists on Windows.
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

# ---------- per-request context (thread-safe language) ----------
_ctx = threading.local()


def lang():
    return getattr(_ctx, "lang", "en")


# ---------- i18n: English inline strings are the keys; Romanian overrides below ----------
# (subset for the ported pages; grows as more pages land. Missing keys fall back to English.)
RO = {
    "local memory": "memorie locala",
    "Dashboard": "Panou", "Memories": "Memorii", "Projects": "Proiecte", "Links": "Legaturi",
    "Git history": "Istoric git", "What Claude sees": "Ce vede Claude", "to review": "de revizuit",
    "System status": "Status sistem", "Health": "Health", "Recent activity": "Activitate recenta",
    "active": "active", "superseded": "superseded", "open todos": "todo deschise", "of": "din",
    "SessionStart injection": "Injectare SessionStart", "budget": "buget", "critical rules": "reguli critice",
    "active — last capture": "activ — ultima captura",
    "no evidence yet (no captures in staging)": "fara dovezi inca (nicio captura in staging)",
    "Source of truth:": "Sursa de adevar:", "(markdown + git). CLI:": "(markdown + git). CLI:",
    "Quick guide": "Ghid rapid", "Navigation": "Navigare",
    "Store writable": "Store writable", "write OK": "scriere OK",
    "Staging writable": "Staging writable", "queue functional": "coada functionala",
    "not created yet": "necreat inca", "FTS index": "Index FTS", "up to date": "la zi",
    "rebuilt just now": "reconstruit acum",
    "FTS5 unavailable — search falls back to substring": "FTS5 indisponibil — cautarea cade pe substring",
    "Embedder (semantic)": "Embedder (semantic)", "vectors": "vectori",
    "no vectors — optional, run mem.py embed": "fara vectori — optional, ruleaza mem.py embed",
    "Ollama offline (keyword search)": "Ollama oprit (cautare keyword)",
    "Review queue (health)": "Coada de review", "empty": "goala",
    "candidates to review": "candidati de revizuit",
    "Claude Code hooks": "Hooks Claude Code", "registered in settings.json": "inregistrate in settings.json",
    "NOT in settings.json": "NU in settings.json", "SessionStart injection": "Injectare SessionStart",
    "critical rules": "reguli critice", "Git store": "Git store",
    "uncommitted — auto-checkpoint at session end": "necomis — checkpoint automat la final de sesiune",
    "clean": "curat",
    # projects + project page
    "No projects yet.": "Inca niciun proiect.", "memories": "memorii",
    "What Claude sees here": "Ce vede Claude aici", "No active memories for": "Nicio memorie activa pentru",
    "To do": "De facut", "ready": "gata", "blocked": "blocate", "blocked by": "blocat de",
    "Edit / bulk:": "Editare / bulk:", "see the main list": "vezi lista principala",
    "files": "fisiere", "invalidated": "invalidat", "Project page": "Pagina de proiect", "Actions": "Actiuni",
    # inject (What Claude sees)
    "What Claude sees at SessionStart": "Ce vede Claude la SessionStart",
    "monorepo root (all projects)": "radacina monorepo (toate proiectele)",
    "Preview unavailable:": "Preview indisponibil:", "Run from the CLI:": "Ruleaza din CLI:",
    "The hook injects nothing for": "Hook-ul nu injecteaza nimic pentru",
    "(no relevant memories).": "(nicio memorie relevanta).", "Injection for": "Injectare pentru",
    "bytes": "bytes", "tokens (approx.)": "tokens (aprox.)", "budget": "buget",
    "critical rules are always in; the rest is trimmed deterministically.":
        "regulile critice intra mereu; restul se taie determinist.",
    "What this page is for": "La ce e pagina asta", "Modes": "Moduri", "If it looks bloated": "Daca pare umflat",
    # git history
    "Uncommitted changes": "Modificari necomise",
    "commit message (empty = default message)": "mesaj commit (gol = mesaj implicit)",
    "Commit the store": "Comite store-ul", "Store clean — everything is committed.": "Store curat — totul e comis.",
    "the store timeline · last": "timeline-ul store-ului · ultimele", "commits": "commit-uri",
    "Git unavailable (not a repo, or exec is disabled in PHP).": "Git indisponibil (nu e repo, sau exec dezactivat).",
    "Only commits touching": "Doar commit-urile care ating",
    "(the memory). Code has its own history in the same repo.": "(memoria). Codul are propriul istoric in acelasi repo.",
    "The page updates itself.": "Pagina se actualizeaza singura.", "Committed:": "Comis:", "Error": "Eroare",
    "(diff unavailable)": "(diff indisponibil)", "Loading...": "Se incarca...",
    "Failed to load the diff.": "Nu s-a putut incarca diff-ul.", "Diff": "Diff",
    "Commit from the UI": "Commit din UI", "Live": "Live",
    # queue
    "Review queue": "Coada de review", "candidates": "candidati", "Approve": "Aproba",
    "Edit & approve": "Editeaza & aproba", "Reject": "Respinge", "How review works": "Cum functioneaza review-ul",
    "Tip": "Sfat", "Queue:": "Coada:", "Cancel": "Anuleaza", "Type": "Tip", "Scope": "Scope",
    "global or project:slug": "global sau project:slug",
    "Reject this candidate? (it is dropped from the queue)": "Respingi candidatul? (e scos din coada)",
    # links
    "Suggested links": "Sugestii de legaturi", "Link": "Leaga", "Dismiss": "Respinge",
    "full content": "continut complet", "Cross-project": "Intre proiecte", "Global": "Global",
    "Project:": "Proiect:",
    # search light (memories page)
    "Semantic on": "Semantic pornit", "Classic": "Clasic",
    "Local LLM is running — searches use keyword + semantic.": "LLM-ul local ruleaza — cautarile folosesc keyword + semantic.",
    "Local LLM is running — this search is keyword + semantic": "LLM-ul local ruleaza — aceasta cautare e keyword + semantic",
    "FTS5 unavailable — substring match.": "FTS5 indisponibil — potrivire substring.",
    "Vectors exist but Ollama is not answering — start it for semantic search.": "Exista vectori dar Ollama nu raspunde — porneste-l pentru cautare semantica.",
    "No embeddings yet — run mem.py embed to enable semantic search.": "Inca nu exista embeddings — ruleaza mem.py embed pentru cautare semantica.",
    "Local LLM (Ollama) is off — searches use keyword only.": "LLM-ul local (Ollama) e oprit — cautarile folosesc doar keyword.",
    "Classic keyword search (FTS5).": "Cautare clasica keyword (FTS5).",
    # memories list + modal + bulk
    "Memory": "Memorie", "Added": "Adaugat", "select all": "selecteaza tot", "project page →": "pagina proiect →",
    "No memories match the current filter.": "Nicio memorie nu se potriveste filtrului curent.",
    "Types": "Tipuri", "Bulk": "Bulk", "View chain": "Vezi lantul", "Save": "Salveaza",
    # about-me page
    "About me": "Despre mine", "Stored in": "Salvat in", "created on first save": "se creeaza la prima salvare",
    "Write something about yourself first.": "Scrie intai ceva despre tine.",
    "A short profile about you — kept in your memory store and injected at the very top of every "
    "session, in every project, so the assistant tailors its help to you.":
        "Un scurt profil despre tine — pastrat in store-ul de memorie si injectat chiar in varful "
        "fiecarei sesiuni, in orice proiect, ca asistentul sa-si potriveasca ajutorul dupa tine.",
    "Who you are, your role and expertise, the environment you work in, what you build, and "
    "how you like to work. Plain text or markdown.":
        "Cine esti, rolul si expertiza ta, mediul in care lucrezi, ce construiesti si cum iti "
        "place sa lucrezi. Text simplu sau markdown.",
    # claude.md editor page
    "Saved": "Salvat", "Edit": "Editeaza", "monorepo root": "radacina monorepo",
    "refusing to write an empty file": "refuz sa scriu un fisier gol",
    "user-global (all projects)": "global per-utilizator (toate proiectele)",
    "(not present — saving creates it)": "(nu exista — salvarea il creeaza)",
    "The CLAUDE.md files Claude Code loads automatically for the selected project, alongside your "
    "memories — your static rules. Saving writes a timestamped .bak next to the file first.":
        "Fisierele CLAUDE.md pe care Claude Code le incarca automat pentru proiectul selectat, langa "
        "memoriile tale — regulile tale statice. La salvare se scrie intai un .bak cu timestamp langa fisier.",
    "critical rule: always injected, first, at SessionStart": "regula critica: injectata mereu, prima, la SessionStart",
    "Operation failed": "Operatie esuata", "selected": "selectate", "Clear selection": "Goleste selectia",
    "Supersede chain:": "Lant de supersedare:", "close": "inchide", "(no chain)": "(fara lant)", "back": "inapoi",
    "+ Add memory": "+ Adauga memorie", "search (FTS ranked)...": "cauta (FTS ranked)...",
    "all types": "toate tipurile", "all scopes": "toate scope-urile", "Add memory": "Adauga memorie",
    "one-line summary": "sumar pe o linie",
    "memory details (simple markdown, inline `code`)": "detalii (markdown simplu, `code` inline)",
    "Mark as superseded?": "Marchezi ca superseded?",
    "Delete permanently? (it stays in git history)": "Stergi definitiv? (ramane in istoricul git)",
    "New scope (global or project:slug):": "Scope nou (global sau project:slug):", "Network error": "Eroare de retea",
    "Session expired (the server restarted) — reload the page and try again.": "Sesiune expirata (serverul a repornit) — reincarca pagina si incearca din nou.",
    "help.intro": "O memorie e un fapt scurt pe care vrei sa-l pastrezi intre sesiuni. Sursa de adevar e "
                  "markdown + git; web UI-ul si <code>mem.py</code> sunt doua ferestre spre acelasi store.",
}


def t(s):
    return RO.get(s, s) if lang() == "ro" else s


def h(s):
    return html.escape("" if s is None else str(s), quote=True)


# ---------- helpers ported from lib.php ----------
def asset(rel):
    """Cache-busting URL for a static asset (?v=mtime)."""
    p = os.path.join(HERE, "web", rel)
    try:
        return f"{rel}?v={int(os.path.getmtime(p))}"
    except OSError:
        return rel


def scope_label(scope):
    return scope[8:] if scope.startswith("project:") else scope


def type_badge(ty):
    return f'<span class="badge t-{h(ty)}">{h(ty)}</span>'


def rec_summary(r):
    return mem.record_summary(r)


def queue_file_path():
    return os.path.join(mem.DATA, "staging", "queue.jsonl")


def queue_load():
    p = queue_file_path()
    out = []
    if not os.path.isfile(p):
        return out
    try:
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
    except OSError:
        pass
    return out


def queue_save(records):
    p = queue_file_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records))
    os.replace(tmp, p)
    try:
        os.chmod(p, 0o666)
    except OSError:
        pass


def queue_pending():
    return [r for r in queue_load() if r.get("status", "pending") == "pending"]


def queue_get(qid):
    for r in queue_load():
        if r.get("qid", "") == qid:
            return r
    return None


def queue_remove(qid):
    queue_save([r for r in queue_load() if r.get("qid", "") != qid])


def queue_approve(qid, over):
    """Write an approved candidate to the store (with optional corrections) and drop it from the queue.
    Redacts secrets on the way in, like every write path."""
    import redact
    r = queue_get(qid)
    if not r:
        return False
    typ = over.get("type") or r.get("type") or "fact"
    scope = over.get("scope") or r.get("scope") or "global"
    summary = over.get("summary") or r.get("summary") or ""
    body = str(over.get("body") or r.get("body") or "")
    conf = str(over.get("confidence") or r.get("confidence") or "0.8")
    source = r.get("source") or "llm"
    if redact.enabled():
        body = redact.redact(body)[0]
        summary = redact.redact(summary)[0]
    rid = mem.gen_id()
    path = mem.scope_file(scope)
    mem.ensure_header(path, scope)
    rec = mem.render_record(rid, typ, scope, summary, body, conf, source, mem.now_ts(), mem.now_ts(), "active")
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n" + rec)
    queue_remove(qid)
    return True


def store_version():
    """Cheap fingerprint (stat only). Changes on any write — drives the live poll."""
    parts = []
    for f in mem.store_files():
        try:
            st = os.stat(f)
            parts.append(f"{f}:{int(st.st_mtime)}:{st.st_size}")
        except OSError:
            pass
    q = os.path.join(mem.DATA, "staging", "queue.jsonl")
    if os.path.isfile(q):
        st = os.stat(q)
        parts.append(f"q:{int(st.st_mtime)}:{st.st_size}")
    import hashlib
    return hashlib.md5("|".join(parts).encode("utf-8")).hexdigest()


def store_stats():
    recs = mem.all_records()
    s = {"total": len(recs), "active": 0, "superseded": 0, "links": 0,
         "by_type": {}, "by_scope": {}, "todos": 0, "recent": []}
    active = []
    for r in recs:
        s["links"] += len(mem._list_meta(r, "related-to")) + len(mem._list_meta(r, "blocked-by"))
        st = r["meta"].get("status", "active")
        if st == "active":
            s["active"] += 1
            ty = r["meta"].get("type", "?")
            sc = r["meta"].get("scope", "?")
            s["by_type"][ty] = s["by_type"].get(ty, 0) + 1
            s["by_scope"][sc] = s["by_scope"].get(sc, 0) + 1
            if ty == "todo":
                s["todos"] += 1
            active.append(r)
        else:
            s["superseded"] += 1
    active.sort(key=lambda r: r["meta"].get("created", ""), reverse=True)
    s["recent"] = active[:8]
    s["by_type"] = dict(sorted(s["by_type"].items(), key=lambda kv: -kv[1]))
    s["by_scope"] = dict(sorted(s["by_scope"].items(), key=lambda kv: -kv[1]))
    return s


def embed_count():
    p = mem.embed_path()
    if not os.path.isfile(p):
        return 0
    try:
        import sqlite3
        con = sqlite3.connect(p)
        n = con.execute("SELECT count(*) FROM emb").fetchone()[0]
        con.close()
        return int(n)
    except Exception:
        return 0


def health_checks():
    out = []
    store = mem.STORE
    gfile = os.path.join(store, "global.md")
    writable = os.access(store, os.W_OK) and (not os.path.exists(gfile) or os.access(gfile, os.W_OK))
    out.append([t("Store writable"), bool(writable), t("write OK") if writable else "chmod store + *.md"])
    stg = os.path.join(mem.DATA, "staging")
    if os.path.isdir(stg):
        w = os.access(stg, os.W_OK)
        out.append([t("Staging writable"), w, t("queue functional") if w else "chmod staging"])
    else:
        out.append([t("Staging writable"), None, t("not created yet")])
    # FTS index: stale is not an error — rebuild on the spot
    if not mem.index_stale():
        out.append([t("FTS index"), True, t("up to date")])
    elif mem.build_index():
        out.append([t("FTS index"), True, t("rebuilt just now")])
    else:
        out.append([t("FTS index"), False, t("FTS5 unavailable — search falls back to substring")])
    # embedder (optional semantic layer) — informative, never an error
    vec = embed_count()
    emodel = os.environ.get("MEM_EMBED_MODEL", "all-minilm")
    if vec == 0:
        out.append([t("Embedder (semantic)"), None, t("no vectors — optional, run mem.py embed")])
    elif _ollama_up():
        out.append([t("Embedder (semantic)"), True, f"{emodel} · {vec} {t('vectors')}"])
    else:
        out.append([t("Embedder (semantic)"), None, f"{vec} {t('vectors')} · {t('Ollama offline (keyword search)')}"])
    nq = len(queue_pending())
    out.append([t("Review queue (health)"), nq == 0, t("empty") if nq == 0 else f"{nq} {t('candidates to review')}"])
    # hooks installed: settings.json authoritative; else empirical staged captures.
    # Parse the JSON (not raw text) so Windows backslashes — which json escapes to
    # "\\" in the file — un-escape to real separators before we compare; then
    # normalize \ vs / so the match is OS-agnostic.
    hooks, hdet = None, ""
    settings = os.path.join(os.path.expanduser("~"), ".claude", "settings.json")
    needle = os.path.join(HERE, "hooks").replace("\\", "/")
    try:
        with open(settings, encoding="utf-8") as f:
            cfg = json.load(f)
        cmds = [hh.get("command", "")
                for ev in (cfg.get("hooks") or {}).values()
                for entry in (ev or [])
                for hh in (entry.get("hooks", []) or [])]
        hooks = any(needle in c.replace("\\", "/") for c in cmds)
        hdet = t("registered in settings.json") if hooks else t("NOT in settings.json")
    except (OSError, ValueError):
        sess = os.path.join(mem.DATA, "staging", "sessions.jsonl")
        if os.path.isfile(sess) and os.path.getsize(sess) > 0:
            hooks = True
            hdet = t("active — last capture") + " " + time.strftime("%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(sess)))
        else:
            hdet = t("no evidence yet (no captures in staging)")
    out.append([t("Claude Code hooks"), hooks, hdet])
    # the injection must fit its own budget (below the harness persist/truncation threshold)
    isz, ibud, ncrit = injection_stats()
    if isz is not None:
        out.append([t("SessionStart injection"), isz <= ibud,
                    f"{round(isz / 1024, 1)} KB / {t('budget')} {round(ibud / 1024, 1)} KB · {ncrit} {t('critical rules')}"])
    # git store: real dirty state (dirty is informative, not an error — auto-checkpoint at session end)
    if os.path.isdir(os.path.join(mem.DATA, ".git")):
        out.append([t("Git store"), None, t("uncommitted — auto-checkpoint at session end")]
                   if git_dirty_files() else [t("Git store"), True, t("clean")])
    return out


def injection_stats():
    """Real injection size (root mode = the maximal case) vs budget + active critical-rule count."""
    hook = os.path.join(HERE, "hooks", "session_start.py")
    bud = int(os.environ.get("MEM_INJECT_BUDGET", "8000"))
    if not os.path.isfile(hook):
        return (None, bud, 0)
    try:
        payload = json.dumps({"cwd": os.path.dirname(HERE), "source": "health"})
        r = subprocess.run([sys.executable, hook], input=payload, capture_output=True, text=True,
                           timeout=30, creationflags=_NO_WINDOW)
        if r.returncode != 0:
            return (None, bud, 0)
        size = len(r.stdout.encode("utf-8"))
    except Exception:
        return (None, bud, 0)
    ncrit = sum(1 for rec in mem.all_records()
                if rec["meta"].get("priority") == "critical" and rec["meta"].get("status", "active") == "active")
    return (size, bud, ncrit)


def _ollama_up():
    import urllib.request
    url = (os.environ.get("OLLAMA_URL", "http://localhost:11434")) + "/api/tags"
    try:
        with urllib.request.urlopen(url, timeout=1.5) as r:
            return r.status == 200
    except Exception:
        return False


def embedder_live():
    """Semantic search is 'live' only when vectors exist AND Ollama answers."""
    return embed_count() > 0 and _ollama_up()


# ---------- record rendering + relations (ported from lib.php) ----------
def render_body(body):
    """Minimal body rendering: escape + inline `code` + keep line breaks."""
    esc = re.sub(r"`([^`]+)`", r"<code>\1</code>", h(body))
    return esc.replace("\n", "<br>\n")


def rec_ids(r, key):
    return mem._list_meta(r, key)


def rec_extras_html(r):
    out = ""
    files = rec_ids(r, "files")
    if files:
        out += ('<div class="rec-files">' + h(t("files")) + ": "
                + " ".join(f"<code>{h(f)}</code>" for f in files) + "</div>")
    inv = r["meta"].get("invalidated", "")
    if inv:
        reason = r["meta"].get("invalid-reason", "")
        out += ('<div class="inval-note">' + h(t("invalidated")) + " " + h(inv[:16])
                + ((" — " + h(reason)) if reason else "") + "</div>")
    return out


def known_scopes():
    s = {"global"}
    for r in mem.all_records():
        sc = r["meta"].get("scope", "")
        if sc:
            s.add(sc)
    return ["global"] + sorted(x for x in s if x != "global")


def records_by_id():
    return {r["id"]: r for r in mem.all_records()}


def related_in_index():
    idx = {}
    for r in mem.all_records():
        for tgt in rec_ids(r, "related-to"):
            idx.setdefault(tgt, []).append(r["id"])
    return idx


def id_chip(rid, by_id):
    summ = rec_summary(by_id[rid]) if rid in by_id else ""
    return f'<a class="idchip" href="/memories?id={h(rid)}" title="{h(summ)}">{h(rid[-6:])}</a>'


def related_html(r, rel_in, by_id):
    ids = list(dict.fromkeys(rec_ids(r, "related-to") + rel_in.get(r["id"], [])))
    if not ids:
        return ""
    return '<div class="rel">↔ ' + " ".join(id_chip(i, by_id) for i in ids) + "</div>"


def open_blockers(r, by_id):
    return mem._open_blockers(r, by_id)


def pair_key(a, b):
    return f"{a}|{b}" if a < b else f"{b}|{a}"


def dismiss_file_path():
    return os.path.join(mem.DATA, "staging", "link-dismissed.jsonl")


def dismissed_pairs():
    p = dismiss_file_path()
    out = set()
    if not os.path.isfile(p):
        return out
    try:
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if "pair" in d:
                    out.add(d["pair"])
    except OSError:
        pass
    return out


def dismiss_pair(a, b):
    p = dismiss_file_path()
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps({"pair": pair_key(a, b)}) + "\n")
        return True
    except OSError:
        return False


def link_records(a, b):
    """Add a related-to edge a->b (same effect as `mem.py link`)."""
    if not a or not b or a == b:
        return False
    try:
        mem._edit_list_meta(a, "related-to", add=[b])
        return True
    except Exception:
        return False


def all_links():
    """Relation edges: related-to (undirected, deduped) + blocked-by (directed: a=todo, b=blocker)."""
    by = records_by_id()
    seen, edges = set(), []
    for r in mem.all_records():
        for to in rec_ids(r, "related-to"):
            if to not in by:
                continue
            k = pair_key(r["id"], to)
            if k in seen:
                continue
            seen.add(k)
            edges.append({"kind": "related", "a": r, "b": by[to]})
        for to in rec_ids(r, "blocked-by"):
            if to in by:
                edges.append({"kind": "blocked", "a": r, "b": by[to]})
    return edges


def suggested_links(limit=12, threshold=0.62):
    """Closest UNLINKED active pairs (semantic), excluding existing edges + dismissed. [] if no vectors."""
    env = os.environ.get("MEM_SUGGEST_THRESHOLD")
    if env not in (None, ""):
        threshold = float(env)
    emb = mem.load_embeddings()
    if len(emb) < 2:
        return []
    by_id = records_by_id()
    emb = {i: v for i, v in emb.items() if i in by_id and by_id[i]["meta"].get("status", "active") == "active"}
    exist = dismissed_pairs()
    for r in by_id.values():
        for to in rec_ids(r, "related-to") + rec_ids(r, "blocked-by"):
            exist.add(pair_key(r["id"], to))
    # pre-normalize once -> cosine becomes a plain dot product (avoids re-computing norms per pair)
    import math
    norm = {}
    for i, v in emb.items():
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        norm[i] = [x / n for x in v]
    ids = list(norm.keys())[:1500]   # safety cap on the O(n^2) scan
    pairs = []
    for i in range(len(ids)):
        vi = norm[ids[i]]
        for j in range(i + 1, len(ids)):
            if pair_key(ids[i], ids[j]) in exist:
                continue
            s = sum(a * b for a, b in zip(vi, norm[ids[j]]))
            if s >= threshold:
                pairs.append({"a": ids[i], "b": ids[j], "sim": s})
    pairs.sort(key=lambda x: -x["sim"])
    return pairs[:limit]


def projects_overview():
    p = {}
    for r in mem.all_records():
        if r["meta"].get("status", "active") != "active":
            continue
        sc = r["meta"].get("scope", "")
        if not sc.startswith("project:"):
            continue
        d = p.setdefault(sc[8:], {"n": 0, "todo": 0, "status": "", "sdate": ""})
        d["n"] += 1
        ty = r["meta"].get("type", "")
        if ty == "todo":
            d["todo"] += 1
        elif ty == "status":
            cd = r["meta"].get("created", "")
            if cd > d["sdate"]:
                d["sdate"], d["status"] = cd, rec_summary(r)
    return dict(sorted(p.items()))


def supersede_chain(rec_id):
    """Ordered chain: [...older that this replaced, rec_id, ...newer that replaced it]."""
    all_recs = mem.all_records()
    by_id = {r["id"]: r for r in all_recs}
    back, cur = [], rec_id
    while True:
        prev = next((r["id"] for r in all_recs if r["meta"].get("superseded-by", "") == cur), None)
        if not prev:
            break
        back.append(prev)
        cur = prev
    fwd, cur = [], rec_id
    while by_id.get(cur, {}).get("meta", {}).get("superseded-by"):
        cur = by_id[cur]["meta"]["superseded-by"]
        if cur not in by_id or cur in fwd:
            break
        fwd.append(cur)
    return list(reversed(back)) + [rec_id] + fwd


def search_light(mode, q):
    """[state 'on'|'off', label, tooltip] for the search box — green when the local LLM is in play."""
    model = os.environ.get("MEM_EMBED_MODEL", "all-minilm")
    if q == "":
        if embedder_live():
            return ("on", t("Semantic on") + " · " + model, t("Local LLM is running — searches use keyword + semantic."))
        if embed_count() == 0:
            return ("off", t("Classic"), t("No embeddings yet — run mem.py embed to enable semantic search."))
        return ("off", t("Classic"), t("Local LLM (Ollama) is off — searches use keyword only."))
    if mode == "hybrid":
        return ("on", t("Semantic on") + " · " + model, t("Local LLM is running — this search is keyword + semantic") + ".")
    if mode == "substring":
        return ("off", t("Classic"), t("FTS5 unavailable — substring match."))
    if mode == "embedder-offline":
        return ("off", t("Classic"), t("Vectors exist but Ollama is not answering — start it for semantic search."))
    if mode == "no-vectors":
        return ("off", t("Classic"), t("No embeddings yet — run mem.py embed to enable semantic search."))
    return ("off", t("Classic"), t("Classic keyword search (FTS5)."))


def relations_block(r, rel_in, by_id):
    out = related_html(r, rel_in, by_id)
    if r["meta"].get("type") == "todo":
        bb = rec_ids(r, "blocked-by")
        if bb:
            cls = "blockedby" if open_blockers(r, by_id) else "rel"
            out += (f'<div class="{cls}" style="margin-top:4px;display:inline-block">'
                    + t("blocked by") + " " + " ".join(id_chip(i, by_id) for i in bb) + "</div>")
    return out


def render_row(r, by_id, rel_in):
    m = r["meta"]
    st = m.get("status", "active")
    summ = rec_summary(r)
    typ = m.get("type", "?")
    scope = m.get("scope", "global")
    hay = (r["title"] + " " + r["body"]).lower()
    scope_href = "/memories?scope=global" if scope == "global" else f"/project?slug={h(scope_label(scope))}"
    crit = ""
    if m.get("priority") == "critical":
        crit = (f'<span class="badge t-status" title="{h(t("critical rule: always injected, first, at SessionStart"))}">'
                f'critical</span> ')
    sup = ""
    if st == "superseded":
        by = m.get("superseded-by", "")
        link = f' → <a href="/memories?id={h(by)}">{h(by)}</a>' if by else ""
        sup = f'<span class="status-superseded">· superseded{link}</span>'
    return (
        f'<tr class="{"row-superseded" if st == "superseded" else ""}" data-id="{h(r["id"])}" data-status="{h(st)}" '
        f'data-hay="{h(hay)}" data-type="{h(typ)}" data-scope="{h(scope)}" data-summary="{h(summ)}" '
        f'data-confidence="{h(m.get("confidence","1.0"))}" data-body="{h(r["body"])}" data-created="{h(m.get("created",""))}">'
        f'<td class="sel"><input type="checkbox" class="rowsel"></td>'
        f'<td>{type_badge(typ)}</td>'
        f'<td><a class="scope-tag" href="{scope_href}">{h(scope_label(scope))}</a></td>'
        f'<td class="summary" onclick="toggleBody(this)">{crit}<b>{h(summ)}</b>{sup}'
        f'<div class="meta"><a href="/memories?id={h(r["id"])}">{h(r["id"])}</a> · conf {h(m.get("confidence","?"))} · {h(m.get("source",""))}</div></td>'
        f'<td class="meta" style="white-space:nowrap">{h(m.get("created",""))}</td>'
        f'<td style="text-align:right"><div class="actwrap">'
        f'<button type="button" class="act-toggle" onclick="toggleAct(this)">Actions ▾</button>'
        f'<div class="actmenu"><button type="button" class="act-edit">{t("Edit")}</button>'
        f'<button type="button" class="act-rescope">{t("Re-scope")}</button>'
        f'<button type="button" class="act-supersede">{t("Supersede")}</button>'
        f'<a href="/memories?id={h(r["id"])}">{t("View chain")}</a>'
        f'<button type="button" class="act-delete danger">{t("Delete")}</button></div></div></td></tr>'
        f'<tr class="bodyrow" data-bodyfor="{h(r["id"])}" style="display:none"><td colspan="6">'
        f'{render_body(r["body"])}{rec_extras_html(r)}{relations_block(r, rel_in, by_id)}</td></tr>')


# ---------- git history (subprocess) ----------
def git_run(args):
    root = mem.DATA
    if not os.path.isdir(os.path.join(root, ".git")):
        return None
    try:
        r = subprocess.run(["git", "-C", root, "-c", f"safe.directory={root}"] + list(args),
                           capture_output=True, text=True, timeout=15, creationflags=_NO_WINDOW)
        out = (r.stdout + r.stderr).rstrip("\n")
        return {"code": r.returncode, "lines": out.split("\n") if out else []}
    except Exception:
        return None


def git_store_log(n=40):
    r = git_run(["log", f"-{n}", "--date=format:%Y-%m-%d %H:%M", "--pretty=format:%h|%ad|%s", "--", "store"])
    if r is None or r["code"] != 0:
        return []
    out = []
    for line in r["lines"]:
        p = line.split("|", 2)
        if len(p) == 3:
            out.append({"hash": p[0], "date": p[1], "subject": p[2]})
    return out


def git_store_diff(hsh):
    if not re.match(r"^[0-9a-f]{7,40}$", hsh or ""):
        return None
    r = git_run(["show", hsh, "--stat", "--patch", "--no-color", "--", "store"])
    if r is None or r["code"] != 0:
        return None
    return "\n".join(r["lines"])


def git_dirty_files():
    r = git_run(["status", "--porcelain", "store"])
    if r is None or r["code"] != 0:
        return []
    return [line.strip() for line in r["lines"] if line.strip()]


def git_commit_store(msg):
    msg = (msg or "").strip() or "store: updates from the web UI"
    r = git_run(["add", "store"])
    if r is None or r["code"] != 0:
        return (False, "git add failed: " + " ".join((r or {}).get("lines", [])))
    root = mem.DATA
    try:
        rc = subprocess.run(["git", "-C", root, "-c", f"safe.directory={root}",
                             "-c", "commit.gpgsign=false", "-c", "user.name=mem0ry4ai web",
                             "-c", "user.email=web@mem0ry4ai.local", "commit", "-m", msg, "--", "store"],
                            capture_output=True, text=True, timeout=15, creationflags=_NO_WINDOW)
        lines = (rc.stdout + rc.stderr).rstrip("\n").split("\n")
        if rc.returncode != 0:
            return (False, " ".join(lines[:3]))
        return (True, lines[0] if lines and lines[0] else "committed")
    except Exception as e:
        return (False, str(e))


def git_page_version():
    import hashlib
    head = (git_run(["log", "-1", "--format=%H", "--", "store"]) or {"lines": []})["lines"]
    dirty = (git_run(["status", "--porcelain", "store"]) or {"lines": []})["lines"]
    return hashlib.md5(("|".join(head) + "#" + "|".join(dirty)).encode("utf-8")).hexdigest()


def render_dirty_card(dirty, has_log):
    if dirty:
        items = "".join(f"<li><code>{h(f)}</code></li>" for f in dirty)
        return (f'<div class="card"><h3>{t("Uncommitted changes")} ({len(dirty)})</h3>'
                f'<ul class="dirty-list">{items}</ul>'
                f'<form method="post" action="/git" class="form-actions" style="margin-top:10px">'
                f'<input type="hidden" name="csrf" value="{h(_CSRF)}">'
                f'<input type="text" name="msg" placeholder="{h(t("commit message (empty = default message)"))}" style="flex:1 1 280px">'
                f'<button class="btn btn-primary" type="submit">{t("Commit the store")}</button></form></div>')
    if has_log:
        return f'<p class="meta">{t("Store clean — everything is committed.")}</p>'
    return ""


def render_gitlog(log):
    rows = ""
    for c in log:
        rows += (f'<div class="gcommit" data-hash="{h(c["hash"])}"><div class="ghead" onclick="toggleDiff(this)">'
                 f'<code class="ghash">{h(c["hash"])}</code> <span class="gsubj">{h(c["subject"])}</span> '
                 f'<span class="gdate">{h(c["date"])}</span></div>'
                 f'<pre class="gdiff" style="display:none" data-loaded="0"></pre></div>')
    return f'<div class="gitlog">{rows}</div>'


def git_poll(qs):
    ver = git_page_version()
    cli = qs.get("ver", [""])[0] or ""
    if cli == ver:
        return {"changed": False, "ver": ver}
    log = git_store_log(40)
    return {"changed": cli != "", "ver": ver, "count": len(log),
            "dirty_html": render_dirty_card(git_dirty_files(), bool(log)), "log_html": render_gitlog(log)}


# ---------- CSRF + flash (post-redirect-get via a short-lived cookie) ----------
def csrf_ok(tok):
    return bool(tok) and secrets.compare_digest(tok, _CSRF)


def flash_html():
    f = getattr(_ctx, "flash", None)
    if not f:
        return ""
    msg, kind = f
    return f'<div class="flash flash-{h(kind)}">{h(msg)}</div>'


# ---------- shared layout ----------
FAVICON = ("data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'>"
           "<rect width='32' height='32' rx='7' fill='%23006fff'/><text x='16' y='23' font-size='19' "
           "text-anchor='middle' fill='white' font-family='sans-serif' font-weight='bold'>m</text></svg>")


def lang_switch():
    cur = lang()
    out = '<span class="lang-switch">'
    for l in ("en", "ro"):
        cls = ' class="on"' if l == cur else ""
        out += f'<a{cls} href="?lang={l}">{l.upper()}</a>'
    return out + "</span>"


def topbar(active):
    nav = [("dashboard", "/", t("Dashboard")), ("memories", "/memories", t("Memories")),
           ("projects", "/projects", t("Projects")), ("links", "/links", t("Links")),
           ("git", "/git", t("Git history")), ("inject", "/inject", t("What Claude sees")),
           ("claudemd", "/claude-md", "CLAUDE.md"), ("about", "/about", t("About me"))]
    nq = len(queue_pending())
    links = ""
    for k, href, label in nav:
        on = ' class="nav-on"' if active == k else ""
        links += f'<a{on} href="{href}">{h(label)}</a> '
    review = f'<a class="review-tag" href="/queue">{nq} {t("to review")}</a> ' if nq else ""
    return (f'<div class="topbar"><a class="brand" href="/">mem0ry4ai '
            f'<small>{t("local memory")}</small></a><div class="right">{links}{review}{lang_switch()}</div></div>')


def layout(title, active, content, aside="", scripts=""):
    return (f'<!doctype html>\n<html lang="{lang()}">\n<head>\n<meta charset="utf-8">\n'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            f'<title>{h(title)}</title>\n<link rel="icon" href="{FAVICON}">\n'
            f'<link rel="stylesheet" href="{h(asset("assets/style.css"))}">\n</head>\n<body>\n'
            f'{topbar(active)}\n<main>\n<div class="layout">\n<div class="content">\n{content}\n'
            f'</div><!-- /content -->\n{aside}\n</div><!-- /layout -->\n</main>\n{scripts}\n</body>\n</html>')


# ---------- dashboard fragments (shared with the poll endpoint) ----------
def dash_cards(stats):
    nproj = sum(1 for s in stats["by_scope"] if s.startswith("project:"))
    out = [
        f'<a class="card-stat" href="/memories?status=all"><div class="num">{stats["total"]}</div><div class="lbl">{t("Memories")}</div></a>',
        f'<a class="card-stat" href="/memories?status=active"><div class="num">{stats["active"]}</div><div class="lbl">{t("active")}</div></a>',
        f'<a class="card-stat" href="/memories?status=superseded"><div class="num">{stats["superseded"]}</div><div class="lbl">{t("superseded")}</div></a>',
        f'<a class="card-stat {"warn" if stats["todos"] > 0 else ""}" href="/memories?type=todo&status=active"><div class="num">{stats["todos"]}</div><div class="lbl">{t("open todos")}</div></a>',
        f'<a class="card-stat" href="/projects"><div class="num">{nproj}</div><div class="lbl">{t("Projects")}</div></a>',
        f'<a class="card-stat" href="/links"><div class="num">{stats.get("links", 0)}</div><div class="lbl">{t("Links")}</div></a>',
    ]
    shown = 0
    for ty, n in stats["by_type"].items():
        if ty == "todo":
            continue
        shown += 1
        if shown > 3:
            break
        out.append(f'<a class="card-stat" href="/memories?type={h(ty)}&status=active"><div class="num">{n}</div><div class="lbl">{h(ty)}</div></a>')
    return "\n      ".join(out)


def recent_list(stats):
    out = []
    for r in stats["recent"]:
        m = r["meta"]
        out.append(
            f'<li>{type_badge(m.get("type", "?"))} <a href="/memories?id={h(r["id"])}">{h(rec_summary(r)[:52])}</a>'
            f' <span class="src">{h(m.get("source", ""))}</span>'
            f' <span class="when">{h((m.get("created", "")[5:16]))}</span></li>')
    return "\n          ".join(out)


def page_projects(qs=None):
    projects = projects_overview()
    cards = []
    for slug, p in projects.items():
        todo = f' · <span class="ptodo">{p["todo"]} todo</span>' if p["todo"] else ""
        status = (f'<div class="pcstatus"><span class="badge t-status">status</span> {h(p["status"][:120])}</div>'
                  if p["status"] else "")
        cards.append(
            f'<a class="projcard" href="/project?slug={h(slug)}"><div class="pchead">'
            f'<span class="pcname">{h(slug)}</span>'
            f'<span class="pcmeta">{p["n"]} {t("memories")}{todo}</span></div>{status}</a>')
    empty = f'<div class="empty">{t("No projects yet.")}</div>' if not projects else ""
    content = (f'  <div class="crumb"><a href="/">{t("Dashboard")}</a> / {t("Projects")}</div>\n'
               f'  <h2>{t("Projects")} <span class="count">{len(projects)}</span></h2>\n  {empty}\n'
               f'  <div class="projcards">\n    {"".join(cards)}\n  </div>')
    aside_text = ('Toate proiectele dintr-o privire: cate memorii are fiecare, cate todo-uri deschise si unde '
                  'ai ramas (status). Click pe un proiect → pagina lui.' if lang() == "ro" else
                  'Every project at a glance: how many memories it has, open todos, and where you left off '
                  '(status). Click a project → its page.')
    aside = f'<aside class="help"><h3>{t("Projects")}</h3><p>{aside_text}</p></aside>'
    return layout(t("Projects") + " — mem0ry4ai", "projects", content, aside)


def page_project(qs=None):
    slug = ((qs or {}).get("slug", [""])[0] or "").strip()
    if not slug or "/" in slug or ".." in slug:
        return None  # -> redirect home
    scope = f"project:{slug}"
    allr = [r for r in mem.all_records() if r["meta"].get("scope", "") == scope]
    active = sorted([r for r in allr if r["meta"].get("status", "active") == "active"],
                    key=lambda r: r["meta"].get("created", ""), reverse=True)
    statuses = [r for r in active if r["meta"].get("type") == "status"]
    todos = [r for r in active if r["meta"].get("type") == "todo"]
    rest = [r for r in active if r["meta"].get("type") not in ("status", "todo")]
    by_id, rel_in = records_by_id(), related_in_index()
    ready, blocked = [], []
    for r in todos:
        ob = open_blockers(r, by_id)
        (blocked.append((r, ob)) if ob else ready.append(r))
    type_order = ["gotcha", "decision", "fact", "command", "procedural", "preference"]
    by_type = {}
    for r in rest:
        by_type.setdefault(r["meta"].get("type", "?"), []).append(r)
    by_type = dict(sorted(by_type.items(), key=lambda kv: type_order.index(kv[0]) if kv[0] in type_order else 9))

    sup = len(allr) - len(active)
    suptxt = f" · {sup} superseded" if sup else ""
    parts = [
        f'  <div class="crumb"><a href="/">{t("Dashboard")}</a> / <a href="/projects">{t("Projects")}</a> / {h(slug)}</div>',
        f'  <h2>{h(slug)} <span class="count">{len(active)} {t("active")}{suptxt}</span></h2>',
        f'  <p class="meta" style="margin-top:-6px"><a href="/inject?scope={h(scope)}">{t("What Claude sees here")} &rarr;</a></p>',
    ]
    if not active:
        parts.append(f'  <div class="empty">{t("No active memories for")} <code>{h(scope)}</code>.</div>')
    for r in statuses:
        parts.append(
            f'  <div class="pin"><h3><span class="badge t-status">status</span> {h(rec_summary(r))}'
            f' <span class="count" style="font-weight:400">· {h(r["meta"].get("created","")[:16])}</span></h3>'
            f'<div class="body">{render_body(r["body"])}</div>{related_html(r, rel_in, by_id)}</div>')
    if todos:
        bl = f' · {len(blocked)} {t("blocked")}' if blocked else ""
        items = ""
        for r in ready:
            b = r["body"].strip()
            exc = f' — {h(b.replace(chr(10), " ")[:160])}' if b else ""
            items += (f'<li><b>{h(rec_summary(r))}</b>{exc} '
                      f'<a class="meta" href="/memories?id={h(r["id"])}">{h(r["id"])}</a>{related_html(r, rel_in, by_id)}</li>')
        for r, ob in blocked:
            items += (f'<li class="blocked"><b>{h(rec_summary(r))}</b> '
                      f'<span class="blockedby">{t("blocked by")} {" ".join(id_chip(i, by_id) for i in ob)}</span> '
                      f'<a class="meta" href="/memories?id={h(r["id"])}">{h(r["id"])}</a>{related_html(r, rel_in, by_id)}</li>')
        parts.append(f'  <div class="pin todo"><h3><span class="badge t-todo">todo</span> {t("To do")} '
                     f'({len(ready)} {t("ready")}{bl})</h3><ul>{items}</ul></div>')
    for ty, rs in by_type.items():
        rows = ""
        for r in rs:
            rows += (f'<tr data-id="{h(r["id"])}"><td class="summary" onclick="toggleBody(this)">'
                     f'<b>{h(rec_summary(r))}</b><div class="meta"><a href="/memories?id={h(r["id"])}">{h(r["id"])}</a>'
                     f' · conf {h(r["meta"].get("confidence","?"))} · {h(r["meta"].get("source",""))} · '
                     f'{h(r["meta"].get("created","")[:16])}</div>{related_html(r, rel_in, by_id)}</td></tr>'
                     f'<tr class="bodyrow" style="display:none"><td>{render_body(r["body"])}{rec_extras_html(r)}</td></tr>')
        parts.append(f'  <div class="type-block"><h3>{type_badge(ty)} <span class="count">{len(rs)}</span></h3>'
                     f'<table class="mem"><tbody>{rows}</tbody></table></div>')
    parts.append(f'  <p class="foot">{t("Edit / bulk:")} '
                 f'<a href="/memories?scope={h(urllib.parse.quote(scope))}">{t("see the main list")}</a>'
                 f' · CLI: <code>./mem.py list --scope {h(scope)}</code></p>')
    aside_p = ('Echivalentul vizual al injectarii la SessionStart: <b>status</b> (unde ai ramas) si <b>todo</b> '
               '(ce urmeaza) sus, apoi cunostintele grupate pe tip.' if lang() == "ro" else
               'The visual equivalent of the SessionStart injection: <b>status</b> (where you left off) and '
               '<b>todo</b> (what is next) on top, then knowledge grouped by type.')
    aside = (f'<aside class="help"><h3>{t("Project page")}</h3><p>{aside_p}</p>'
             f'<h4>{t("Actions")}</h4><p>{"Click pe un rand → body-ul complet. Editare / supersede / re-scope: din lista principala." if lang()=="ro" else "Click a row → the full body. Edit / supersede / re-scope: from the main list."}</p></aside>')
    script = ('<script>function toggleBody(cell){ var r = cell.closest("tr").nextElementSibling;'
              ' if (r && r.classList.contains("bodyrow")) r.style.display = (r.style.display === "none") ? "" : "none"; }</script>')
    return layout(slug + " — mem0ry4ai", "projects", "\n".join(parts), aside, script)


def page_inject(qs=None):
    qs = qs or {}
    scopes = [s for s in known_scopes() if s != "global"]
    sel = (qs.get("scope", ["root"])[0] or "root").strip()
    repo_root = os.path.dirname(HERE)
    if sel.startswith("project:"):
        cwd, label = os.path.join(repo_root, scope_label(sel)), f"cwd = {scope_label(sel)}/"
    else:
        sel, cwd, label = "root", repo_root, t("monorepo root (all projects)")
    output, err = None, None
    hook = os.path.join(HERE, "hooks", "session_start.py")
    try:
        stdin = json.dumps({"cwd": cwd, "hook_event_name": "SessionStart", "source": "preview"})
        # sys.executable (not "python3") -> works on Windows too
        r = subprocess.run([sys.executable, hook], input=stdin, capture_output=True, text=True,
                           timeout=30, creationflags=_NO_WINDOW)
        output = r.stdout
        if r.returncode != 0:
            err = f"hook exit {r.returncode}: {r.stderr.strip()}"
    except Exception as e:
        err = str(e)
    nbytes = len(output.encode("utf-8")) if output else 0
    tokens = round(nbytes / 4)
    opts = f'<option value="root"{" selected" if sel == "root" else ""}>{t("monorepo root (all projects)")}</option>'
    for s in scopes:
        opts += f'<option value="{h(s)}"{" selected" if sel == s else ""}>cwd = {h(scope_label(s))}/</option>'
    if err is not None:
        bodyblock = (f'<div class="flash flash-error">{t("Preview unavailable:")} {h(err)}. {t("Run from the CLI:")} '
                     f'<code>echo \'{{"cwd":"{h(cwd)}","hook_event_name":"SessionStart"}}\' | python hooks/session_start.py</code></div>')
    elif not (output or "").strip():
        bodyblock = f'<div class="empty">{t("The hook injects nothing for")} {h(label)} {t("(no relevant memories).")}</div>'
    else:
        ibud = int(os.environ.get("MEM_INJECT_BUDGET", "8000"))
        exact = ('Acesta e output-ul exact al hook-ului real (<code>hooks/session_start.py</code>), nu o aproximare.'
                 if lang() == "ro" else
                 'This is the exact output of the real hook (<code>hooks/session_start.py</code>), not an approximation.')
        bodyblock = (f'<p class="inject-meta">{t("Injection for")} <b>{h(label)}</b>: <b>{nbytes:,} {t("bytes")}</b> '
                     f'≈ ~{tokens:,} {t("tokens (approx.)")} · {t("budget")} {ibud:,} bytes (MEM_INJECT_BUDGET) — '
                     f'{t("critical rules are always in; the rest is trimmed deterministically.")} {exact}</p>'
                     f'<pre class="inject">{h(output)}</pre>')
    foot = ('Hook-ul ruleaza la fiecare start de sesiune Claude Code (startup/resume/clear/compact).'
            if lang() == "ro" else
            'The hook runs at every Claude Code session start (startup/resume/clear/compact).')
    content = (f'  <div class="crumb"><a href="/">{t("Dashboard")}</a> / {t("What Claude sees")}</div>\n'
               f'  <h2>{t("What Claude sees at SessionStart")}</h2>\n'
               f'  <div class="toolbar"><form class="filters" method="get">'
               f'<select name="scope" onchange="this.form.submit()">{opts}</select>'
               f'<noscript><button class="btn" type="submit">OK</button></noscript></form></div>\n'
               f'  {bodyblock}\n  <p class="foot">{foot}</p>')
    hp = ('Transparenta: vezi exact ce context primeste Claude automat la startul sesiunii, si cat costa (bytes/tokens).'
          if lang() == "ro" else
          'Transparency: see exactly what context Claude receives automatically at session start, and what it costs (bytes/tokens).')
    hroot = ('<b>Root</b> = global complet + un index plafonat al tuturor proiectelor (status/todo primele, max 10/proiect, '
             'proiectele neatinse 30+ zile pliate).' if lang() == "ro" else
             '<b>Root</b> = full global + a capped index of all projects (status/todo first, max 10 per project, '
             'projects untouched for 30+ days collapsed).')
    hproj = ('<b>Sub-proiect</b> = global + TOATE memoriile acelui proiect.' if lang() == "ro" else
             "<b>Sub-project</b> = global + ALL of that project's memories.")
    hbloat = ('Supersede/sterge memoriile invechite din lista principala — injectarea scade imediat.' if lang() == "ro" else
              'Supersede/delete stale memories from the main list — the injection shrinks immediately.')
    aside = (f'<aside class="help"><h3>{t("What this page is for")}</h3><p>{hp}</p>'
             f'<h4>{t("Modes")}</h4><p>{hroot}</p><p>{hproj}</p>'
             f'<h4>{t("If it looks bloated")}</h4><p>{hbloat}</p></aside>')
    return layout(t("What Claude sees") + " — mem0ry4ai", "inject", content, aside)


def claudemd_path(key):
    """Resolve a CLAUDE.md file from a SAFE key only — never an arbitrary path from the client, so
    the editor cannot read/write outside the intended files. Keys:
      'global'          -> ~/.claude/CLAUDE.md (user-global, applies to all projects)
      'root'            -> <monorepo-root>/CLAUDE.md
      'project:<slug>'  -> <monorepo-root>/<slug>/CLAUDE.md, but ONLY for a scope that exists in the
                           store, and only if the resolved real path stays under the monorepo root.
    Returns None for anything else."""
    if key == "global":
        return os.path.join(os.path.expanduser("~"), ".claude", "CLAUDE.md")
    repo_root = os.path.realpath(os.path.dirname(HERE))
    if key == "root":
        return os.path.join(repo_root, "CLAUDE.md")
    if key.startswith("project:") and key in known_scopes():
        path = os.path.realpath(os.path.join(repo_root, scope_label(key), "CLAUDE.md"))
        try:
            if os.path.commonpath([repo_root, path]) == repo_root:
                return path
        except ValueError:  # different drive on Windows -> reject
            pass
    return None


# CLAUDE.md editor JS — verbatim vanilla JS; CSRF + TXT are prepended by the page.
_CLAUDEMD_JS_BODY = r'''
(function(){
  function setEditing(card, on){
    var ta = card.querySelector('.cmd-edit');
    ta.readOnly = !on;
    card.querySelector('.cmd-editbtn').hidden = on;
    card.querySelector('.cmd-savebtn').hidden = !on;
    card.querySelector('.cmd-cancelbtn').hidden = !on;
    if (on){ card.dataset.orig = ta.value; ta.focus(); }
  }
  document.addEventListener('click', function(e){
    var card = e.target.closest('.cmd-file'); if (!card) return;
    if (e.target.classList.contains('cmd-editbtn')){ setEditing(card, true); return; }
    if (e.target.classList.contains('cmd-cancelbtn')){
      card.querySelector('.cmd-edit').value = card.dataset.orig || '';
      var s = card.querySelector('.cmd-status'); s.textContent = ''; s.className = 'cmd-status';
      setEditing(card, false); return;
    }
    if (e.target.classList.contains('cmd-savebtn')){
      var ta = card.querySelector('.cmd-edit'), st = card.querySelector('.cmd-status'), btn = e.target;
      btn.disabled = true;
      var fd = new URLSearchParams(); fd.set('csrf', CSRF); fd.set('key', card.dataset.key); fd.set('content', ta.value);
      fetch('/claude-md', { method:'POST', headers:{'X-Requested-With':'XMLHttpRequest'}, body: fd })
        .then(function(r){ return r.json(); })
        .then(function(j){
          btn.disabled = false;
          if (!j.ok){
            if (j.error === 'CSRF'){ alert(TXT.csrf); location.reload(); return; }
            st.textContent = j.error || TXT.failed; st.className = 'cmd-status err'; return;
          }
          st.textContent = TXT.saved + (j.backup ? ' (' + j.backup + ')' : '');
          st.className = 'cmd-status ok';
          setEditing(card, false);
        })
        .catch(function(){ btn.disabled = false; st.textContent = TXT.network; st.className = 'cmd-status err'; });
    }
  });
})();
'''


def page_claudemd(qs=None):
    qs = qs or {}
    sel = (qs.get("scope", ["root"])[0] or "root").strip()
    proj_scopes = [s for s in known_scopes() if s.startswith("project:")]
    opts = f'<option value="root"{" selected" if sel == "root" else ""}>{t("monorepo root")}</option>'
    for s in proj_scopes:
        opts += f'<option value="{h(s)}"{" selected" if sel == s else ""}>{h(scope_label(s))}</option>'
    # Files Claude Code loads for the selected cwd: user-global + monorepo-root, plus the project's own.
    panes = [("global", claudemd_path("global"), t("user-global (all projects)")),
             ("root", claudemd_path("root"), t("monorepo root"))]
    if sel.startswith("project:") and sel in known_scopes():
        panes.append((sel, claudemd_path(sel), scope_label(sel)))
    cards = []
    for key, path, label in panes:
        content, exists = "", False
        if path and os.path.isfile(path):
            try:
                content = open(path, encoding="utf-8").read()
                exists = True
            except OSError:
                pass
        meta = (f'{len(content.encode("utf-8")):,} {t("bytes")}' if exists
                else t("(not present — saving creates it)"))
        cards.append(
            f'<div class="cmd-file" data-key="{h(key)}">'
            f'<div class="cmd-head"><b>{h(label)}</b> <code>{h(path or "—")}</code> '
            f'<span class="cmd-meta">{meta}</span></div>'
            f'<textarea class="cmd-edit" readonly spellcheck="false">{h(content)}</textarea>'
            f'<div class="cmd-actions">'
            f'<button type="button" class="btn cmd-editbtn">{t("Edit")}</button>'
            f'<button type="button" class="btn btn-primary cmd-savebtn" hidden>{t("Save")}</button>'
            f'<button type="button" class="btn cmd-cancelbtn" hidden>{t("Cancel")}</button>'
            f'<span class="cmd-status"></span></div></div>')
    style = ("<style>.cmd-file{border:1px solid var(--border,#e2e8f0);border-radius:8px;padding:14px;margin:14px 0;}"
             ".cmd-head{margin-bottom:8px;} .cmd-head code{font-size:12px;color:var(--muted,#64748b);}"
             ".cmd-meta{margin-left:8px;font-size:12px;color:var(--muted,#64748b);}"
             ".cmd-edit{width:100%;min-height:240px;box-sizing:border-box;resize:vertical;"
             "font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12.5px;line-height:1.5;padding:10px;"
             "border:1px solid var(--border,#e2e8f0);border-radius:6px;background:var(--bg2,#f8fafc);}"
             ".cmd-edit:not([readonly]){background:#fff;}"
             ".cmd-actions{margin-top:8px;display:flex;gap:8px;align-items:center;}"
             ".cmd-status{font-size:13px;} .cmd-status.ok{color:#1f9d4d;} .cmd-status.err{color:#dc2626;}</style>")
    intro = t("The CLAUDE.md files Claude Code loads automatically for the selected project, alongside your "
              "memories — your static rules. Saving writes a timestamped .bak next to the file first.")
    content = (f'{style}'
               f'  <div class="crumb"><a href="/">{t("Dashboard")}</a> / CLAUDE.md</div>\n'
               f'  <h2>CLAUDE.md</h2>\n'
               f'  <p class="foot">{intro}</p>\n'
               f'  <div class="toolbar"><form class="filters" method="get">'
               f'<select name="scope" onchange="this.form.submit()">{opts}</select>'
               f'<noscript><button class="btn" type="submit">OK</button></noscript></form></div>\n'
               + "\n".join(cards))
    txt = {"failed": t("Operation failed"), "network": t("Network error"),
           "csrf": t("Session expired (the server restarted) — reload the page and try again."),
           "saved": t("Saved")}
    js = ("<script>var CSRF = " + json.dumps(_CSRF) + "; var TXT = "
          + json.dumps(txt, ensure_ascii=False) + ";" + _CLAUDEMD_JS_BODY + "</script>")
    return layout("CLAUDE.md — mem0ry4ai", "claudemd", content, "", js)


def profile_record():
    """The single active global 'profile' record (the About-me), or None. Most recent wins if several."""
    cands = [r for r in mem.all_records()
             if r["meta"].get("type") == "profile"
             and r["meta"].get("scope") == "global"
             and r["meta"].get("status", "active") == "active"]
    cands.sort(key=lambda r: r["meta"].get("created", ""), reverse=True)
    return cands[0] if cands else None


# About-me editor JS — verbatim vanilla JS; CSRF + TXT are prepended by the page.
_ABOUT_JS_BODY = r'''
(function(){
  var btn = document.getElementById('about-save');
  if (!btn) return;
  btn.addEventListener('click', function(){
    var ta = document.getElementById('about-edit'), st = document.getElementById('about-status');
    btn.disabled = true;
    var fd = new URLSearchParams(); fd.set('csrf', CSRF); fd.set('body', ta.value);
    fetch('/about', { method:'POST', headers:{'X-Requested-With':'XMLHttpRequest'}, body: fd })
      .then(function(r){ return r.json(); })
      .then(function(j){
        btn.disabled = false;
        if (!j.ok){
          if (j.error === 'CSRF'){ alert(TXT.csrf); location.reload(); return; }
          st.textContent = j.error || TXT.failed; st.className = 'cmd-status err'; return;
        }
        st.textContent = TXT.saved; st.className = 'cmd-status ok';
      })
      .catch(function(){ btn.disabled = false; st.textContent = TXT.network; st.className = 'cmd-status err'; });
  });
})();
'''


def page_about(qs=None):
    rec = profile_record()
    body = rec["body"] if rec else ""
    path = rec["file"] if rec else mem.GLOBAL_FILE   # the store file the profile record lives in
    intro = t("A short profile about you — kept in your memory store and injected at the very top of every "
              "session, in every project, so the assistant tailors its help to you.")
    placeholder = t("Who you are, your role and expertise, the environment you work in, what you build, and "
                    "how you like to work. Plain text or markdown.")
    location = (f'{t("Stored in")} <code>{h(path)}</code>'
                + (f' · id <code>{h(rec["id"])}</code>' if rec else f' — {t("created on first save")}'))
    style = ("<style>.about-edit{width:100%;min-height:300px;box-sizing:border-box;resize:vertical;"
             "font-family:ui-monospace,Menlo,Consolas,monospace;font-size:13px;line-height:1.6;padding:12px;"
             "border:1px solid var(--border,#e2e8f0);border-radius:8px;background:#fff;}"
             ".cmd-actions{margin-top:10px;display:flex;gap:10px;align-items:center;}"
             ".cmd-status{font-size:13px;} .cmd-status.ok{color:#1f9d4d;} .cmd-status.err{color:#dc2626;}</style>")
    content = (f'{style}'
               f'  <div class="crumb"><a href="/">{t("Dashboard")}</a> / {t("About me")}</div>\n'
               f'  <h2>{t("About me")}</h2>\n'
               f'  <p class="foot">{intro}</p>\n'
               f'  <p class="foot">{location}</p>\n'
               f'  <textarea id="about-edit" class="about-edit" placeholder="{h(placeholder)}" spellcheck="false">{h(body)}</textarea>\n'
               f'  <div class="cmd-actions"><button type="button" class="btn btn-primary" id="about-save">{t("Save")}</button>'
               f'<span class="cmd-status" id="about-status"></span></div>')
    txt = {"failed": t("Operation failed"), "network": t("Network error"),
           "csrf": t("Session expired (the server restarted) — reload the page and try again."),
           "saved": t("Saved")}
    js = ("<script>var CSRF = " + json.dumps(_CSRF) + "; var TXT = "
          + json.dumps(txt, ensure_ascii=False) + ";" + _ABOUT_JS_BODY + "</script>")
    return layout(t("About me") + " — mem0ry4ai", "about", content, "", js)


# Force-directed graph IIFE — verbatim vanilla JS (no PHP); reads the GRAPH global set just above it.
_GRAPH_JS = r'''(function(){
  var W = 920, H = 540, CX = W/2, CY = H/2;
  var COLOR = { gotcha:'#c77f0a', fact:'#006fff', decision:'#7c3aed', command:'#475569',
                preference:'#1f9d4d', todo:'#e8590c', status:'#0c8599' };
  var SVG = 'http://www.w3.org/2000/svg';
  var svg = document.getElementById('graph');
  var gE = document.getElementById('g-edges'), gN = document.getElementById('g-nodes');
  var nodes = GRAPH.nodes, edges = GRAPH.edges;
  var byId = {}; nodes.forEach(function(n){ byId[n.id] = n; });
  nodes.forEach(function(n){ n.deg = 0; });
  edges.forEach(function(e){ if (byId[e.s]) byId[e.s].deg++; if (byId[e.t]) byId[e.t].deg++; });
  nodes.forEach(function(n, i){
    var a = (i / nodes.length) * Math.PI * 2;
    n.x = CX + Math.cos(a) * 150 + (i%3-1)*8; n.y = CY + Math.sin(a) * 150 + (i%2)*8;
    n.vx = 0; n.vy = 0; n.r = Math.min(20, 8 + n.deg * 1.6);
  });
  var adj = {}; nodes.forEach(function(n){ adj[n.id] = {}; });
  edges.forEach(function(e){ if (adj[e.s]&&adj[e.t]){ adj[e.s][e.t]=1; adj[e.t][e.s]=1; } });
  var eEls = edges.map(function(e){
    var ln = document.createElementNS(SVG, 'line');
    ln.setAttribute('class', 'gedge ' + (e.kind === 'blocked' ? 'gedge-blocked' : 'gedge-related'));
    if (e.kind === 'blocked') ln.setAttribute('marker-end', 'url(#arrow)');
    e._el = ln; gE.appendChild(ln); return ln;
  });
  var nEls = nodes.map(function(n){
    var g = document.createElementNS(SVG, 'g');
    g.setAttribute('class', 'gnode'); g.setAttribute('data-id', n.id); g.style.cursor = 'pointer';
    var c = document.createElementNS(SVG, 'circle');
    c.setAttribute('r', n.r); c.setAttribute('fill', COLOR[n.type] || '#888');
    c.setAttribute('stroke', '#fff'); c.setAttribute('stroke-width', '2');
    var ti = document.createElementNS(SVG, 'title'); ti.textContent = '[' + n.type + '] ' + n.label;
    c.appendChild(ti);
    var tx = document.createElementNS(SVG, 'text');
    tx.setAttribute('class', 'glabel'); tx.setAttribute('text-anchor', 'middle');
    tx.setAttribute('dy', n.r + 11); tx.textContent = n.label.length > 22 ? n.label.slice(0,21) + '…' : n.label;
    g.appendChild(c); g.appendChild(tx); n._el = g; n._circle = c; gN.appendChild(g); return g;
  });
  var lg = document.getElementById('graph-legend');
  var used = {}; nodes.forEach(function(n){ used[n.type] = 1; });
  Object.keys(used).forEach(function(t){
    var s = document.createElement('span'); s.className = 'lgitem';
    s.innerHTML = '<i style="background:' + (COLOR[t]||'#888') + '"></i>' + t; lg.appendChild(s);
  });
  var REP = 5200, LEN = 118, SPRING = 0.035, GRAV = 0.018, DAMP = 0.9, alpha = 1, drag = null;
  function physics(a){
    var i, j, n, dx, dy, d2, d, f, ux, uy;
    for (i = 0; i < nodes.length; i++) { nodes[i].fx = 0; nodes[i].fy = 0; }
    for (i = 0; i < nodes.length; i++) {
      for (j = i + 1; j < nodes.length; j++) {
        dx = nodes[i].x - nodes[j].x; dy = nodes[i].y - nodes[j].y;
        d2 = dx*dx + dy*dy || 0.01; d = Math.sqrt(d2); f = REP / d2; ux = dx/d; uy = dy/d;
        nodes[i].fx += ux*f; nodes[i].fy += uy*f; nodes[j].fx -= ux*f; nodes[j].fy -= uy*f;
      }
    }
    edges.forEach(function(e){
      var na = byId[e.s], nb = byId[e.t]; if (!na || !nb) return;
      var ex = nb.x - na.x, ey = nb.y - na.y, ed = Math.sqrt(ex*ex + ey*ey) || 0.01;
      var ef = (ed - LEN) * SPRING, eux = ex/ed, euy = ey/ed;
      na.fx += eux*ef; na.fy += euy*ef; nb.fx -= eux*ef; nb.fy -= euy*ef;
    });
    for (i = 0; i < nodes.length; i++) {
      n = nodes[i];
      n.fx += (CX - n.x) * GRAV; n.fy += (CY - n.y) * GRAV;
      if (n === drag) continue;
      n.vx = (n.vx + n.fx * a) * DAMP; n.vy = (n.vy + n.fy * a) * DAMP;
      n.x += n.vx; n.y += n.vy;
      n.x = Math.max(n.r+4, Math.min(W-n.r-4, n.x)); n.y = Math.max(n.r+4, Math.min(H-n.r-30, n.y));
    }
  }
  function tick(){ alpha = drag ? 0.5 : Math.max(0, alpha * 0.985); physics(alpha); render(); requestAnimationFrame(tick); }
  function render(){
    edges.forEach(function(e){
      var a = byId[e.s], b = byId[e.t]; if (!a || !b) return;
      e._el.setAttribute('x1', a.x); e._el.setAttribute('y1', a.y);
      e._el.setAttribute('x2', b.x); e._el.setAttribute('y2', b.y);
    });
    nodes.forEach(function(n){ n._el.setAttribute('transform', 'translate(' + n.x + ',' + n.y + ')'); });
  }
  var startPt = null, moved = false;
  function toSvg(ev){ var pt = svg.createSVGPoint(); pt.x = ev.clientX; pt.y = ev.clientY;
    return pt.matrixTransform(svg.getScreenCTM().inverse()); }
  gN.addEventListener('pointerdown', function(ev){
    var g = ev.target.closest('.gnode'); if (!g) return;
    drag = byId[g.getAttribute('data-id')]; startPt = toSvg(ev); moved = false; g.setPointerCapture(ev.pointerId);
  });
  gN.addEventListener('pointermove', function(ev){
    if (!drag) return; var p = toSvg(ev);
    if (Math.abs(p.x - startPt.x) + Math.abs(p.y - startPt.y) > 3) moved = true;
    drag.x = p.x; drag.y = p.y; drag.vx = 0; drag.vy = 0;
  });
  gN.addEventListener('pointerup', function(ev){
    var g = ev.target.closest('.gnode');
    if (drag && !moved && g) { window.location = byId[g.getAttribute('data-id')].url; }
    drag = null;
  });
  gN.addEventListener('mouseover', function(ev){
    var g = ev.target.closest('.gnode'); if (!g) return;
    var id = g.getAttribute('data-id');
    nodes.forEach(function(n){ n._el.classList.toggle('dim', n.id !== id && !adj[id][n.id]); });
    edges.forEach(function(e){ e._el.classList.toggle('hot', e.s === id || e.t === id); e._el.classList.toggle('dim', !(e.s === id || e.t === id)); });
  });
  gN.addEventListener('mouseout', function(){
    nodes.forEach(function(n){ n._el.classList.remove('dim'); });
    edges.forEach(function(e){ e._el.classList.remove('hot'); e._el.classList.remove('dim'); });
  });
  (function(){ var a = 1; for (var k = 0; k < 280; k++) { physics(a); a *= 0.985; } })();
  alpha = 0.06; render(); requestAnimationFrame(tick);
})();'''

_SUGGEST_JS_BODY = r'''
  document.getElementById('suggest-list').addEventListener('click', function(e){
    var li = e.target.closest('li[data-a]'); if (!li) return;
    if (e.target.closest('.sg-more')) { li.classList.toggle('expanded'); return; }
    var link = e.target.classList.contains('sg-link');
    var dismiss = e.target.classList.contains('sg-dismiss');
    if (!link && !dismiss) return;
    e.target.disabled = true;
    var fd = new URLSearchParams(); fd.set('csrf', CSRF);
    fd.set('action', link ? 'link' : 'dismiss'); fd.set('a', li.dataset.a); fd.set('b', li.dataset.b);
    fetch('/links', { method:'POST', headers:{'X-Requested-With':'XMLHttpRequest'}, body: fd })
      .then(function(r){ return r.json(); })
      .then(function(j){
        if (!j.ok) { e.target.disabled = false; return; }
        if (link) { location.reload(); return; }
        var band = li.closest('.sg-group'); li.remove();
        if (band && !band.querySelector('li[data-a]')) band.remove();
        var c = document.querySelector('.suggest .count'); if (c) c.textContent = document.querySelectorAll('#suggest-list li[data-a]').length;
      })
      .catch(function(){ e.target.disabled = false; });
  });'''


# memories-page JS — verbatim vanilla JS; CSRF/STATUS_FILTER/TXT are prepended by the page.
_MEM_JS_BODY = r'''function toggleBody(cell){ var r = cell.closest('tr').nextElementSibling;
  if (r && r.classList.contains('bodyrow')) r.style.display = (r.style.display === 'none') ? '' : 'none'; }
function toggleAct(btn){ var w = btn.closest('.actwrap'); var open = w.classList.contains('open');
  closeActMenus(); if (!open) w.classList.add('open'); }
function closeActMenus(){ document.querySelectorAll('.actwrap.open').forEach(function(x){ x.classList.remove('open'); }); }
document.addEventListener('click', function(e){ if (!e.target.closest('.actwrap')) closeActMenus(); });
document.querySelectorAll('tr.group-head').forEach(function(gh){
  gh.addEventListener('click', function(){
    gh.classList.toggle('closed');
    var hide = gh.classList.contains('closed');
    var tr = gh.nextElementSibling;
    while (tr) { tr.style.display = hide ? 'none' : (tr.classList.contains('bodyrow') ? 'none' : ''); tr = tr.nextElementSibling; }
  });
});
var live = document.getElementById('live');
if (live) live.addEventListener('input', function(){ var q = this.value.toLowerCase();
  document.querySelectorAll('table.mem tbody tr[data-hay]').forEach(function(tr){
    var show = tr.getAttribute('data-hay').indexOf(q) !== -1;
    tr.style.display = show ? '' : 'none';
    var b = document.querySelector('tr[data-bodyfor="'+tr.dataset.id+'"]'); if (b && !show) b.style.display='none';
  }); });
var sortState = {};
document.querySelectorAll('th.sortable').forEach(function(th){
  th.addEventListener('click', function(){
    var key = th.dataset.sort;
    sortState[key] = !(sortState[key] || false);
    var asc = sortState[key];
    document.querySelectorAll('th.sortable .arrow').forEach(function(a){ a.textContent = ''; });
    th.querySelector('.arrow').textContent = asc ? '▲' : '▼';
    document.querySelectorAll('table.mem tbody').forEach(function(tb){
      var pairs = [];
      tb.querySelectorAll('tr[data-id]').forEach(function(tr){
        pairs.push([tr, document.querySelector('tr[data-bodyfor="'+tr.dataset.id+'"]')]);
      });
      pairs.sort(function(a, b){
        var va = a[0].dataset[key] || '', vb = b[0].dataset[key] || '';
        return asc ? va.localeCompare(vb) : vb.localeCompare(va);
      });
      pairs.forEach(function(p){ tb.appendChild(p[0]); if (p[1]) tb.appendChild(p[1]); });
    });
  });
});
var modal = document.getElementById('modal');
var form = document.getElementById('mem-form');
function openModal(mode, d){
  d = d || {};
  document.getElementById('m-action').value = mode;
  document.getElementById('m-id').value = d.id || '';
  document.getElementById('m-type').value = d.type || 'gotcha';
  document.getElementById('m-scope').value = d.scope || 'global';
  document.getElementById('m-confidence').value = d.confidence || '1.0';
  document.getElementById('m-summary').value = d.summary || '';
  document.getElementById('m-body').value = d.body || '';
  document.getElementById('modal-title').textContent = (mode === 'edit') ? (TXT.editTitle + ' ' + (d.id || '')) : TXT.addTitle;
  var err = document.getElementById('m-err'); err.style.display = 'none'; err.textContent = '';
  modal.classList.add('open');
  setTimeout(function(){ document.getElementById('m-summary').focus(); }, 30);
}
function closeModal(){ modal.classList.remove('open'); }
document.getElementById('open-add').addEventListener('click', function(e){ e.preventDefault(); openModal('add'); });
document.querySelectorAll('[data-close]').forEach(function(b){ b.addEventListener('click', closeModal); });
modal.addEventListener('click', function(e){ if (e.target === modal) closeModal(); });
document.addEventListener('keydown', function(e){ if (e.key === 'Escape' && modal.classList.contains('open')) closeModal(); });
form.addEventListener('submit', function(e){
  e.preventDefault();
  var fd = new URLSearchParams(new FormData(form)); fd.set('csrf', CSRF);
  var btn = document.getElementById('m-submit'); btn.disabled = true;
  fetch('/memories', { method: 'POST', headers: { 'X-Requested-With': 'XMLHttpRequest' }, body: fd })
    .then(function(r){ return r.json(); })
    .then(function(j){
      btn.disabled = false;
      if (!j.ok){ var err = document.getElementById('m-err'); err.textContent = (j.error === 'CSRF' ? TXT.csrf : (j.error || TXT.failed)); err.style.display = 'block'; return; }
      location.reload();
    })
    .catch(function(){ btn.disabled = false; var err = document.getElementById('m-err'); err.textContent = TXT.network; err.style.display = 'block'; });
});
document.addEventListener('click', function(e){
  var tr = e.target.closest('tr[data-id]'); if (!tr) return;
  if (e.target.classList.contains('act-edit')){
    closeActMenus();
    openModal('edit', { id: tr.dataset.id, type: tr.dataset.type, scope: tr.dataset.scope,
      confidence: tr.dataset.confidence, summary: tr.dataset.summary, body: tr.dataset.body });
  } else if (e.target.classList.contains('act-rescope')){
    closeActMenus();
    var ns = prompt(TXT.rescopeQ, tr.dataset.scope);
    if (ns && ns !== tr.dataset.scope) postAction('rescope', tr.dataset.id, { scope: ns }).then(function(){ location.reload(); });
  } else if (e.target.classList.contains('act-supersede')){
    closeActMenus();
    if (confirm(TXT.supersedeQ)) postAction('supersede', tr.dataset.id).then(function(){ location.reload(); });
  } else if (e.target.classList.contains('act-delete')){
    closeActMenus();
    if (confirm(TXT.deleteQ)) postAction('delete', tr.dataset.id).then(function(){ location.reload(); });
  }
});
function postAction(action, id, extra){
  var fd = new URLSearchParams(); fd.set('csrf', CSRF); fd.set('action', action); fd.set('id', id);
  if (extra) Object.keys(extra).forEach(function(k){ fd.set(k, extra[k]); });
  return fetch('/memories', { method: 'POST', headers: { 'X-Requested-With': 'XMLHttpRequest' }, body: fd })
    .then(function(r){ return r.json(); })
    .then(function(j){ if (!j.ok) { if (j.error === 'CSRF') { alert(TXT.csrf); location.reload(); } else { alert(j.error || TXT.failed); } throw new Error(); } return j; });
}
function selected(){ return Array.from(document.querySelectorAll('.rowsel:checked')).map(function(c){ return c.closest('tr').dataset.id; }); }
function refreshBulk(){
  var n = selected().length;
  document.getElementById('bulkcount').textContent = n;
  document.getElementById('bulkbar').classList.toggle('show', n > 0);
}
document.addEventListener('change', function(e){
  if (e.target.classList.contains('rowsel')) refreshBulk();
  if (e.target.id === 'selall'){
    document.querySelectorAll('.rowsel').forEach(function(c){
      if (c.closest('tr').style.display !== 'none') c.checked = e.target.checked;
    });
    refreshBulk();
  }
});
function bulkRun(fn){
  var ids = selected(); if (!ids.length) return;
  (function next(i){
    if (i >= ids.length) { location.reload(); return; }
    fn(ids[i]).then(function(){ next(i + 1); }).catch(function(){ location.reload(); });
  })(0);
}
document.getElementById('bulk-supersede').addEventListener('click', function(){
  if (confirm(TXT.supersedeQ + ' (' + selected().length + ')')) bulkRun(function(id){ return postAction('supersede', id); });
});
document.getElementById('bulk-delete').addEventListener('click', function(){
  if (confirm(TXT.deleteQ + ' (' + selected().length + ')')) bulkRun(function(id){ return postAction('delete', id); });
});
document.getElementById('bulk-rescope').addEventListener('click', function(){
  var ns = prompt(TXT.rescopeQ);
  if (ns) bulkRun(function(id){ return postAction('rescope', id, { scope: ns }); });
});
document.getElementById('bulk-clear').addEventListener('click', function(){
  document.querySelectorAll('.rowsel:checked').forEach(function(c){ c.checked = false; });
  var sa = document.getElementById('selall'); if (sa) sa.checked = false;
  refreshBulk();
});
var pollVer = '', pollBusy = false;
function pollStore(){
  if (pollBusy || document.hidden) return;
  pollBusy = true;
  fetch('/poll?ver=' + encodeURIComponent(pollVer))
    .then(function(r){ return r.json(); })
    .then(function(j){
      pollBusy = false;
      if (!j.ver) return;
      if (pollVer === '') { pollVer = j.ver; return; }
      if (!j.changed) return;
      pollVer = j.ver;
      var h2c = document.querySelector('h2 .count');
      if (h2c) h2c.textContent = h2c.textContent.replace(/^\d+/, j.active);
    })
    .catch(function(){ pollBusy = false; });
}
setInterval(pollStore, 4000);
pollStore();'''


def _lk_endpoint_html(r):
    sc = r["meta"].get("scope", "")
    scl = f' <span class="lk-scope">{h(scope_label(sc))}</span>' if sc != "global" else ""
    return (f'{type_badge(r["meta"].get("type", "?"))} '
            f'<a class="lk-sum" href="/memories?id={h(r["id"])}">{h(rec_summary(r)[:90])}</a>{scl}')


def _sg_end_html(r):
    sc = r["meta"].get("scope", "")
    scl = f'<span class="sg-scope">{h(scope_label(sc))}</span> ' if sc != "global" else ""
    return (f'<div class="sg-end"><div class="sg-top">{type_badge(r["meta"].get("type", "?"))} '
            f'<a class="sg-sum" href="/memories?id={h(r["id"])}">{scl}{h(rec_summary(r))}</a></div>'
            f'<div class="sg-full">{render_body(r["body"])}{rec_extras_html(r)}</div></div>')


def page_links(qs=None):
    edges = all_links()
    suggestions = suggested_links()
    by_id = records_by_id()
    # graph nodes/edges
    nodemap = {}
    for e in edges:
        for k in ("a", "b"):
            r = e[k]
            if r["id"] not in nodemap:
                sc = r["meta"].get("scope", "global")
                nodemap[r["id"]] = {"id": r["id"], "label": rec_summary(r)[:34],
                                    "type": r["meta"].get("type", "?"),
                                    "scope": "global" if sc == "global" else scope_label(sc),
                                    "url": "/memories?id=" + r["id"]}
    gnodes = list(nodemap.values())
    gedges = [{"s": e["a"]["id"], "t": e["b"]["id"], "kind": e["kind"]} for e in edges]
    # detailed list grouped by scope of "a"
    groups = {}
    for e in edges:
        groups.setdefault(e["a"]["meta"].get("scope", "global"), []).append(e)
    gkeys = sorted(groups.keys(), key=lambda x: (0, "") if x == "global" else (1, x))

    parts = [f'  <div class="crumb"><a href="/">{t("Dashboard")}</a> / {t("Links")}</div>',
             f'  <h2>{t("Links")} <span class="count">{len(edges)}</span></h2>']

    # suggestions (grouped by project, zebra bands)
    if suggestions:
        sg_groups = {}
        for s in suggestions:
            A, B = by_id.get(s["a"]), by_id.get(s["b"])
            if not A or not B:
                continue
            sa = A["meta"].get("scope", "global")
            sB = B["meta"].get("scope", "global")
            gk = sa if sa == sB else (sB if sa == "global" else (sa if sB == "global" else "__cross__"))
            sg_groups.setdefault(gk, []).append((s, A, B))
        ordered = sorted(sg_groups.keys(),
                         key=lambda x: (0, "") if x == "global" else ((2, "") if x == "__cross__" else (1, x)))
        sg_html = ""
        for gk in ordered:
            items = sg_groups[gk]
            glabel = t("Global") if gk == "global" else (t("Cross-project") if gk == "__cross__" else scope_label(gk))
            rows = ""
            for s, A, B in items:
                rows += (f'<li data-a="{h(s["a"])}" data-b="{h(s["b"])}" data-group="{h(gk)}">'
                         f'<div class="sg-head"><span class="sg-pct" title="semantic similarity">{round(s["sim"] * 100)}%</span>'
                         f'<span class="sg-act"><button type="button" class="btn btn-primary sg-link">{t("Link")}</button> '
                         f'<button type="button" class="btn btn-ghost sg-dismiss">{t("Dismiss")}</button></span></div>'
                         f'<div class="sg-pair">{_sg_end_html(A)}<div class="sg-mid">&harr;</div>{_sg_end_html(B)}</div>'
                         f'<button type="button" class="sg-more"><span class="sg-arrow">&#9662;</span> {t("full content")}</button></li>')
            sg_html += (f'<li class="sg-group" data-group="{h(gk)}">'
                        f'<div class="sg-group-head">{h(glabel)} <span class="sg-gcount">{len(items)}</span></div>'
                        f'<ul class="sg-items">{rows}</ul></li>')
        smallnote = "semantic — tu confirmi" if lang() == "ro" else "semantic — you confirm"
        parts.append(f'  <div class="suggest"><h3>{t("Suggested links")} <span class="count">{len(suggestions)}</span>'
                     f' <small>{smallnote}</small></h3><ul id="suggest-list">{sg_html}</ul></div>')

    scripts = ""
    if not edges:
        noedge = ('Nicio legatura inca. Leaga memorii inrudite cu <code>mem.py link &lt;id&gt; &lt;alt&gt;</code> '
                  'sau <code>mem.py block &lt;todo&gt; &lt;blocker&gt;</code>.' if lang() == "ro" else
                  'No links yet. Connect related memories with <code>mem.py link &lt;id&gt; &lt;other&gt;</code> '
                  'or <code>mem.py block &lt;todo&gt; &lt;blocker&gt;</code>.')
        parts.append(f'  <div class="empty">{noedge}</div>')
    else:
        parts.append(
            '  <div class="graphwrap"><svg id="graph" viewBox="0 0 920 540" preserveAspectRatio="xMidYMid meet" '
            'role="img" aria-label="relations graph"><defs>'
            '<marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" '
            'orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="#c98a3a"></path></marker></defs>'
            '<g id="g-edges"></g><g id="g-nodes"></g></svg><div class="graph-legend" id="graph-legend"></div></div>')
        det = "Lista detaliata" if lang() == "ro" else "Detailed list"
        list_html = ""
        for sc in gkeys:
            es = groups[sc]
            head = t("Global") if sc == "global" else (t("Project:") + " " + h(scope_label(sc)))
            lis = ""
            for e in es:
                rel = ("⟂ " + t("blocked by")) if e["kind"] == "blocked" else "↔"
                lis += (f'<li class="lk lk-{e["kind"]}">{_lk_endpoint_html(e["a"])}'
                        f'<span class="lk-rel">{rel}</span>{_lk_endpoint_html(e["b"])}</li>')
            list_html += (f'<div class="lk-group"><h3>{head} <span class="count">{len(es)}</span></h3>'
                          f'<ul class="links">{lis}</ul></div>')
        parts.append(f'  <details class="lk-listwrap"><summary>{det} ({len(edges)})</summary>{list_html}</details>')
        scripts += ('<script>var GRAPH = '
                    + json.dumps({"nodes": gnodes, "edges": gedges}, ensure_ascii=False)
                    + ';\n' + _GRAPH_JS + '</script>')
    if suggestions:
        scripts += "<script>(function(){ var CSRF = " + json.dumps(_CSRF) + ";" + _SUGGEST_JS_BODY + "})();</script>"

    gh = ('Graful tuturor legaturilor dintre memorii. Linie plina <b>↔</b> = inrudite (related-to); sageata '
          'portocalie <b>⟂</b> = un todo blocat de altceva. Culoarea nodului = tipul; marimea = cate legaturi are.'
          if lang() == "ro" else
          'The graph of every link between memories. A solid line <b>↔</b> = related (related-to); an orange arrow '
          '<b>⟂</b> = a todo blocked by something. Node color = memory type; size = how many links it has.')
    gi = ('Trage un nod ca sa rearanjezi. Click pe un nod → memoria lui. Treci cu mouse-ul pentru a evidentia vecinii.'
          if lang() == "ro" else
          'Drag a node to rearrange. Click a node → its memory. Hover to highlight its neighbours.')
    ititle = "Interactiune" if lang() == "ro" else "Interaction"
    aside = f'<aside class="help"><h3>{t("Links")}</h3><p>{gh}</p><h4>{ititle}</h4><p>{gi}</p></aside>'
    return layout(t("Links") + " — mem0ry4ai", "links", "\n".join(parts), aside, scripts)


def page_queue(qs=None):
    pending = sorted(queue_pending(), key=lambda r: float(r.get("confidence") or 0), reverse=True)
    scopes = known_scopes()
    intro = ('Candidati extrasi automat de LLM-ul local din transcripturile sesiunilor. <b>Nimic nu intra in '
             'memorie pana nu aprobi tu.</b> Modelul e un generator de ciorne — corecteaza tip/scope/summary inainte.'
             if lang() == "ro" else
             'Candidates extracted automatically by the local LLM from session transcripts. <b>Nothing enters '
             'memory until you approve it.</b> The model is a draft generator — fix type/scope/summary before approving.')
    if not pending:
        empty = ('Coada e goala. Ruleaza <code>python3 consolidate.py --write</code> ca sa extragi candidati.'
                 if lang() == "ro" else
                 'The queue is empty. Run <code>python3 consolidate.py --write</code> to extract candidates from captured sessions.')
        cards = f'<div class="empty">{empty}</div>'
    else:
        cards = ""
        for r in pending:
            cards += (
                f'<div class="qcard" data-qid="{h(r.get("qid",""))}" data-type="{h(r.get("type","fact"))}" '
                f'data-scope="{h(r.get("scope","global"))}" data-summary="{h(r.get("summary",""))}" '
                f'data-body="{h(r.get("body",""))}" data-confidence="{h(str(r.get("confidence","")))}">'
                f'<div class="qhead">{type_badge(r.get("type","?"))} '
                f'<span class="scope-tag">{h(scope_label(r.get("scope","global")))}</span> '
                f'<span class="conf">conf {h(str(r.get("confidence","?")))}</span> '
                f'<span class="qsrc">{h(r.get("source",""))}</span></div>'
                f'<div class="qsum">{h(r.get("summary",""))}</div>'
                f'<div class="qbody">{render_body(r.get("body",""))}</div>'
                f'<div class="qactions">'
                f'<button type="button" class="btn btn-primary q-approve">{t("Approve")}</button> '
                f'<button type="button" class="btn q-edit">{t("Edit & approve")}</button> '
                f'<button type="button" class="btn btn-danger q-reject">{t("Reject")}</button></div></div>')
    type_opts = "".join(f"<option>{h(ty)}</option>" for ty in mem.TYPES)
    scope_opts = "".join(f'<option value="{h(s)}">' for s in scopes)
    content = (
        f'  <div class="crumb"><a href="/">{t("Dashboard")}</a> / {t("Review queue")}</div>\n'
        f'  <h2>{t("Review queue")} <span class="count">{len(pending)} {t("candidates")}</span></h2>\n'
        f'  <p class="meta" style="margin-top:0">{intro}</p>\n'
        f'  <div id="cards">{cards}</div>\n'
        f'  <p class="foot">{t("Source of truth:")} <code>store/*.md</code>. {t("Queue:")} <code>staging/queue.jsonl</code>.</p>')
    qhelp = ('LLM-ul local (Ollama) citeste transcripturile si propune candidati. E zgomotos si supra-increzator '
             '(~1.0 pe tot), deci <b>tu decizi</b>.' if lang() == "ro" else
             'The local LLM (Ollama) reads session transcripts and proposes candidates. It is noisy and over-confident '
             '(~1.0 confidence on everything), so <b>you decide</b>.')
    qact = ('<b>Aproba</b> = scrie in store ca atare. <b>Editeaza & aproba</b> = corecteaza intai. <b>Respinge</b> = arunca.'
            if lang() == "ro" else
            '<b>Approve</b> = write to the store as-is. <b>Edit &amp; approve</b> = fix it first. <b>Reject</b> = drop it.')
    qtip = ('Verifica <b>tip</b> si <b>scope</b>. Task-uri efemere ("am facut X") = Respinge.' if lang() == "ro" else
            'Double-check <b>type</b> and <b>scope</b>. Ephemeral tasks ("did X") = Reject.')
    aside = (f'<aside class="help"><h3>{t("How review works")}</h3><p>{qhelp}</p>'
             f'<h4>{t("Actions")}</h4><p>{qact}</p><h4>{t("Tip")}</h4><p>{qtip}</p></aside>')
    modal = (
        f'<div class="modal-overlay" id="modal"><div class="modal">'
        f'<div class="modal-head"><h3 id="modal-title">{t("Edit & approve")}</h3>'
        f'<button type="button" class="modal-close" data-close>&times;</button></div>'
        f'<form id="mem-form"><input type="hidden" name="qid" id="m-qid">'
        f'<div class="form-row"><label>{t("Type")} <select name="type" id="m-type">{type_opts}</select></label>'
        f'<label>{t("Scope")} <small>{t("global or project:slug")}</small>'
        f'<input type="text" name="scope" id="m-scope" list="scopes"></label>'
        f'<label>Confidence <input type="text" name="confidence" id="m-confidence"></label></div>'
        f'<label>Summary <input type="text" name="summary" id="m-summary" required></label>'
        f'<label>Body <textarea name="body" id="m-body" required></textarea></label>'
        f'<div class="modal-err" id="m-err" style="display:none"></div>'
        f'<div class="form-actions"><button class="btn btn-primary" type="submit">{t("Approve")}</button>'
        f'<button type="button" class="btn btn-ghost" data-close>{t("Cancel")}</button></div></form></div></div>'
        f'<datalist id="scopes">{scope_opts}</datalist>')
    empty_done = json.dumps('Coada e goala. Toti candidatii au fost procesati.' if lang() == "ro"
                            else 'The queue is empty. All candidates have been processed.')
    script = f'''{modal}
<script>
var CSRF = {json.dumps(_CSRF)};
var TXT = {{ rejectQ: {json.dumps(t("Reject this candidate? (it is dropped from the queue)"))},
  error: {json.dumps(t("Error"))}, candidates: {json.dumps(t("candidates"))}, emptyDone: {empty_done} }};
var modal = document.getElementById('modal'), form = document.getElementById('mem-form');
function closeModal(){{ modal.classList.remove('open'); }}
document.querySelectorAll('[data-close]').forEach(function(b){{ b.addEventListener('click', closeModal); }});
modal.addEventListener('click', function(e){{ if (e.target === modal) closeModal(); }});
document.addEventListener('keydown', function(e){{ if (e.key === 'Escape') closeModal(); }});
function post(data){{
  var fd = new URLSearchParams(); fd.set('csrf', CSRF);
  Object.keys(data).forEach(function(k){{ fd.set(k, data[k]); }});
  return fetch('/queue', {{ method:'POST', headers:{{'X-Requested-With':'XMLHttpRequest'}}, body: fd }}).then(function(r){{ return r.json(); }});
}}
function removeCard(qid){{ var c = document.querySelector('.qcard[data-qid="'+qid+'"]'); if (c) c.remove();
  var left = document.querySelectorAll('.qcard').length;
  document.querySelector('.count').textContent = left + ' ' + TXT.candidates;
  if (!left) document.getElementById('cards').innerHTML = '<div class="empty">' + TXT.emptyDone + '</div>'; }}
document.getElementById('cards').addEventListener('click', function(e){{
  var card = e.target.closest('.qcard'); if (!card) return;
  var qid = card.dataset.qid;
  if (e.target.classList.contains('q-approve')){{
    post({{ action:'approve', qid: qid }}).then(function(j){{ if (j.ok) removeCard(qid); else alert(j.error||TXT.error); }});
  }} else if (e.target.classList.contains('q-reject')){{
    if (!confirm(TXT.rejectQ)) return;
    post({{ action:'reject', qid: qid }}).then(function(j){{ if (j.ok) removeCard(qid); else alert(j.error||TXT.error); }});
  }} else if (e.target.classList.contains('q-edit')){{
    document.getElementById('m-qid').value = qid;
    document.getElementById('m-type').value = card.dataset.type;
    document.getElementById('m-scope').value = card.dataset.scope;
    document.getElementById('m-confidence').value = card.dataset.confidence;
    document.getElementById('m-summary').value = card.dataset.summary;
    document.getElementById('m-body').value = card.dataset.body;
    document.getElementById('m-err').style.display = 'none';
    modal.classList.add('open');
  }}
}});
form.addEventListener('submit', function(e){{
  e.preventDefault();
  var data = {{ action:'approve', qid: document.getElementById('m-qid').value,
    type: document.getElementById('m-type').value, scope: document.getElementById('m-scope').value,
    confidence: document.getElementById('m-confidence').value, summary: document.getElementById('m-summary').value,
    body: document.getElementById('m-body').value }};
  post(data).then(function(j){{ if (j.ok){{ removeCard(data.qid); closeModal(); }}
    else {{ var er = document.getElementById('m-err'); er.textContent = j.error||TXT.error; er.style.display='block'; }} }});
}});
</script>'''
    return layout(t("Review queue") + " — mem0ry4ai", "", content, aside, script)


def page_git(qs=None):
    dirty = git_dirty_files()
    log = git_store_log(40)
    empty = (f'<div class="empty">{t("Git unavailable (not a repo, or exec is disabled in PHP).")}</div>'
             if not log and not dirty else "")
    content = (
        f'  <div class="crumb"><a href="/">{t("Dashboard")}</a> / {t("Git history")}</div>\n'
        f'  <h2>{t("Git history")} <span class="count">{t("the store timeline · last")} '
        f'<span id="gcount">{len(log)}</span> {t("commits")}</span></h2>\n'
        f'  {flash_html()}\n  {empty}\n'
        f'  <div id="dirty-area">{render_dirty_card(dirty, bool(log))}</div>\n'
        f'  <div id="gitlog-area">{render_gitlog(log)}</div>\n'
        f'  <p class="foot">{t("Only commits touching")} <code>store/</code> '
        f'{t("(the memory). Code has its own history in the same repo.")} {t("The page updates itself.")}</p>')
    ghelp = ('Fiecare commit pe <code>store/</code> e un pas in evolutia memoriei: ce s-a invatat, ce s-a '
             'supersedat, cand. Checkpoint-urile se fac automat la final de sesiune, un commit per scope.'
             if lang() == "ro" else
             "Every commit on <code>store/</code> is one step in the memory's evolution: what was learned, "
             "what got superseded, when. Checkpoints happen automatically at session end, one commit per scope.")
    gdiff = ('Click pe un commit → diff-ul lui (doar fisiere store). Verde = adaugat, rosu = sters.'
             if lang() == "ro" else
             "Click a commit → its diff (store files only). Green = added, red = removed.")
    gcommit = ('Butonul comite DOAR <code>store/</code>, autor <code>mem0ry4ai web</code>, fara semnare. '
               'Optional — checkpoint-ul automat vine oricum la final de sesiune.' if lang() == "ro" else
               'The button commits ONLY <code>store/</code>, authored as <code>mem0ry4ai web</code>, no signing. '
               'Optional — the automatic checkpoint comes at session end anyway.')
    glive = ('Lista se actualizeaza cand apar commit-uri/modificari (poll 4s). Diff-urile deschise raman deschise.'
             if lang() == "ro" else
             'The list updates itself when new commits or changes appear (4s poll). Open diffs stay open.')
    aside = (f'<aside class="help"><h3>{t("Git history")}</h3><p>{ghelp}</p>'
             f'<h4>{t("Diff")}</h4><p>{gdiff}</p>'
             f'<h4>{t("Commit from the UI")}</h4><p>{gcommit}</p>'
             f'<h4>{t("Live")}</h4><p>{glive}</p></aside>')
    txt_loading = json.dumps(t("Loading..."))
    txt_fail = json.dumps(t("Failed to load the diff."))
    script = f'''<script>
var TXT = {{ loading: {txt_loading}, fail: {txt_fail} }};
function toggleDiff(head){{
  var box = head.parentNode.querySelector('.gdiff');
  if (box.style.display !== 'none') {{ box.style.display = 'none'; return; }}
  box.style.display = '';
  if (box.dataset.loaded === '1') return;
  box.textContent = TXT.loading;
  fetch('/git?diff=' + encodeURIComponent(head.parentNode.dataset.hash))
    .then(function(r){{ return r.text(); }})
    .then(function(t){{
      box.dataset.loaded = '1'; box.innerHTML = '';
      t.split('\\n').forEach(function(line){{
        var span = document.createElement('span');
        if (line.startsWith('+') && !line.startsWith('+++')) span.className = 'dl-add';
        else if (line.startsWith('-') && !line.startsWith('---')) span.className = 'dl-del';
        else if (line.startsWith('@@') || line.startsWith('diff ') || line.startsWith('commit ')) span.className = 'dl-meta';
        span.textContent = line; box.appendChild(span); box.appendChild(document.createTextNode('\\n'));
      }});
    }})
    .catch(function(){{ box.textContent = TXT.fail; }});
}}
var pollVer = '', pollBusy = false;
function pollGit(){{
  if (pollBusy || document.hidden) return;
  pollBusy = true;
  fetch('/git?poll=1&ver=' + encodeURIComponent(pollVer))
    .then(function(r){{ return r.json(); }})
    .then(function(j){{
      pollBusy = false;
      if (!j.ver) return;
      if (pollVer === '') {{ pollVer = j.ver; return; }}
      if (!j.changed) return;
      pollVer = j.ver;
      var open = [];
      document.querySelectorAll('.gcommit').forEach(function(c){{
        var d = c.querySelector('.gdiff'); if (d && d.style.display !== 'none') open.push(c.dataset.hash);
      }});
      document.getElementById('dirty-area').innerHTML = j.dirty_html;
      var ga = document.getElementById('gitlog-area'); ga.innerHTML = j.log_html;
      var gc = document.getElementById('gcount'); if (gc) gc.textContent = j.count;
      flashEl(ga);
      open.forEach(function(hh){{ var c = document.querySelector('.gcommit[data-hash="' + hh + '"] .ghead'); if (c) toggleDiff(c); }});
    }})
    .catch(function(){{ pollBusy = false; }});
}}
function flashEl(el){{ el.style.transition='none'; el.style.background='rgba(0,111,255,.07)';
  setTimeout(function(){{ el.style.transition='background 1.2s'; el.style.background=''; }}, 60); }}
setInterval(pollGit, 4000); pollGit();
</script>'''
    return layout(t("Git history") + " — mem0ry4ai", "git", content, aside, script)


def page_memories(qs=None):
    qs = qs or {}
    q = (qs.get("q", [""])[0] or "").strip()
    fscope = (qs.get("scope", [""])[0] or "").strip()
    ftype = (qs.get("type", [""])[0] or "").strip()
    fstat = (qs.get("status", ["active"])[0] or "active").strip()
    fid = (qs.get("id", [""])[0] or "").strip()
    records = mem.all_records()
    by_id, rel_in = records_by_id(), related_in_index()
    chain_ids = []
    if fid:
        chain_ids = supersede_chain(fid)
        fstat = "all"
    fts_ids, mode = None, ""
    if q and not fid:
        fts_ids, mode = mem.hybrid_search(q)   # ids None -> substring fallback

    def keep(r):
        m = r["meta"]
        if fid:
            return r["id"] in chain_ids
        if fscope and m.get("scope", "") != fscope:
            return False
        if ftype and m.get("type", "") != ftype:
            return False
        if fstat != "all" and m.get("status", "active") != fstat:
            return False
        if q:
            if isinstance(fts_ids, list):
                return r["id"] in fts_ids
            blob = (r["title"] + " " + r["body"] + " " + m.get("scope", "")).lower()
            return q.lower() in blob
        return True

    rows = [r for r in records if keep(r)]
    if q and isinstance(fts_ids, list):
        pos = {i: n for n, i in enumerate(fts_ids)}
        rows.sort(key=lambda r: pos.get(r["id"], 9999))
    elif fid:
        pos = {i: n for n, i in enumerate(chain_ids)}
        rows.sort(key=lambda r: pos.get(r["id"], 9999))
    else:
        rows.sort(key=lambda r: r["meta"].get("created", ""), reverse=True)

    grouped = fscope == "" and q == "" and fid == ""
    stats = store_stats()
    scopes = known_scopes()

    def mkqs(over):
        p = {"q": q, "scope": fscope, "type": ftype, "status": fstat}
        p.update(over)
        p = {k: v for k, v in p.items() if v != ""}
        return "/memories?" + urllib.parse.urlencode(p)

    # ----- header + chain + toolbar -----
    parts = [f'  <div class="crumb"><a href="/">{t("Dashboard")}</a> / {t("Memories")}</div>',
             f'  <h2>{t("Memories")} <span class="count">{len(rows)} {t("of")} {stats["total"]}</span></h2>']
    if fid and len(chain_ids) > 1:
        chain = ""
        for i, cid in enumerate(chain_ids):
            chain += ("<span class=\"arrow\">→</span>" if i else "") + f'<a href="/memories?id={h(cid)}">{h(cid)}</a>'
        parts.append(f'  <div class="chain"><b>{t("Supersede chain:")}</b> {chain} '
                     f'<a class="btn btn-ghost" style="margin-left:12px" href="/memories">{t("close")}</a></div>')
    elif fid:
        parts.append(f'  <div class="chain"><code>{h(fid)}</code> {t("(no chain)")}. <a href="/memories">{t("back")}</a></div>')
    parts.append(f'  <a href="#" class="add-link" id="open-add">{t("+ Add memory")}</a>')
    parts.append('  <datalist id="scopes">' + "".join(f'<option value="{h(s)}">' for s in scopes) + "</datalist>")

    type_opts = '<option value="">' + t("all types") + "</option>"
    for ty in mem.TYPES:
        type_opts += f'<option{" selected" if ftype == ty else ""}>{h(ty)}</option>'
    scope_opts = '<option value="">' + t("all scopes") + "</option>"
    for s in scopes:
        sel = " selected" if fscope == s else ""
        scope_opts += f'<option{sel} value="{h(s)}">{h(scope_label(s))}</option>'
    lcls, llbl, ltip = search_light(mode, q)
    pills = ""
    for st_key in ("active", "superseded", "all"):
        cls = "active" if fstat == st_key else ""
        pills += f'<a class="{cls}" href="{h(mkqs({"status": st_key}))}">{t(st_key)}</a>'
    parts.append(
        '  <div class="toolbar"><form class="filters" method="get">'
        f'<input type="search" name="q" value="{h(q)}" placeholder="{h(t("search (FTS ranked)..."))}" id="live">'
        f'<select name="type" onchange="this.form.submit()">{type_opts}</select>'
        f'<select name="scope" onchange="this.form.submit()">{scope_opts}</select>'
        f'<input type="hidden" name="status" value="{h(fstat)}">'
        f'<button class="btn" type="submit">{t("Search")}</button>'
        f'<span class="search-light {lcls}" title="{h(ltip)}">{h(llbl)}</span></form>'
        f'<div class="pills">{pills}</div></div>')

    # ----- table -----
    if not rows:
        parts.append(f'  <div id="table-wrap"><div class="empty">{t("No memories match the current filter.")}</div></div>')
    else:
        thead = ('<thead><tr><th class="sel"><input type="checkbox" id="selall" title="' + h(t("select all")) + '"></th>'
                 f'<th class="sortable" data-sort="type">{t("Type")}<span class="arrow"></span></th>'
                 f'<th class="sortable" data-sort="scope">{t("Scope")}<span class="arrow"></span></th>'
                 f'<th class="sortable" data-sort="summary">{t("Memory")}<span class="arrow"></span></th>'
                 f'<th class="sortable" data-sort="created">{t("Added")}<span class="arrow"></span></th><th></th></tr></thead>')
        body = ""
        if grouped:
            groups = {}
            for r in rows:
                groups.setdefault(r["meta"].get("scope", "global"), []).append(r)
            for sc in sorted(groups.keys(), key=lambda x: (0, "") if x == "global" else (1, x)):
                grs = groups[sc]
                head = t("Global") if sc == "global" else (t("Project:") + " " + h(scope_label(sc)))
                proj_link = ("" if sc == "global" else
                             f'<a class="gcount" style="float:right" href="/project?slug={h(scope_label(sc))}" '
                             f'onclick="event.stopPropagation()">{t("project page →")}</a>')
                body += (f'<tbody class="grp" data-scope="{h(sc)}"><tr class="group-head"><td colspan="6">{head} '
                         f'<span class="gcount">· {len(grs)} {t("memories")}</span>{proj_link}</td></tr>'
                         + "".join(render_row(r, by_id, rel_in) for r in grs) + "</tbody>")
        else:
            body = "<tbody>" + "".join(render_row(r, by_id, rel_in) for r in rows) + "</tbody>"
        parts.append(f'  <div id="table-wrap"><table class="mem">{thead}{body}</table></div>')
    parts.append(f'  <p class="foot">{t("Source of truth:")} <code>store/*.md</code> {t("(markdown + git). CLI:")} <code>./mem.py</code>.</p>')

    # ----- aside -----
    intro = (t("help.intro") if lang() == "ro" else
             "A memory is one short fact you want to keep between sessions. The source of truth is markdown + git; "
             "the web UI and <code>mem.py</code> are two windows onto the same store.")
    type_defs = [("gotcha", 'Trap / lesson: "X breaks because of Y, do Z".', 'Capcana / lectie: "X se strica din cauza Y, fa Z".'),
                 ("fact", "Stable fact: IP, port, path, deploy target.", "Fapt stabil: IP, port, cale, tinta de deploy."),
                 ("decision", "Architecture decision + the why.", "Decizie de arhitectura + de ce."),
                 ("command", "A useful command.", "O comanda utila."),
                 ("procedural", "A reusable multi-step workflow / runbook.", "Un flux reutilizabil / runbook."),
                 ("preference", "One of your preferences.", "Una dintre preferintele tale."),
                 ("todo", "What remains to be done. Done = superseded.", "Ce ramane de facut. Gata = superseded."),
                 ("status", "Where the project stands / where you left off.", "Unde e proiectul / unde ai ramas.")]
    dl = "".join(f'<dt><span class="badge t-{ty}">{ty}</span></dt><dd>{ro if lang() == "ro" else en}</dd>'
                 for ty, en, ro in type_defs)
    navh = (t("help.nav") if lang() == "ro" else
            "Click a <b>scope</b> → the project page (status + todos on top). Click an <b>id</b> → the supersede chain. "
            "Click a row → its body. Group header → collapse.")
    bulkh = (t("help.bulk") if lang() == "ro" else "Tick rows → bottom bar: Supersede / Re-scope / Delete all at once.")
    searchh = (t("help.search") if lang() == "ro" else
               "Field + Enter = FTS ranked (same index as <code>mem.py search</code>). The light by the button shows the "
               "search mode: <b>green</b> = the local LLM is up, so search is keyword + semantic; <b>gray</b> = it falls "
               "back to classic keyword search automatically. No toggle. Typing also live-filters what is on screen.")
    aside = (f'<aside class="help"><h3>{t("Quick guide")}</h3><p>{intro}</p>'
             f'<h4>{t("Types")}</h4><dl>{dl}</dl>'
             f'<h4>{t("Navigation")}</h4><p>{navh}</p>'
             f'<h4>{t("Bulk")}</h4><p>{bulkh}</p>'
             f'<h4>{t("Search")}</h4><p>{searchh}</p></aside>')

    # ----- bulk bar + modal -----
    after = (
        f'<div class="bulkbar" id="bulkbar"><span class="bn" id="bulkcount">0</span> {t("selected")} '
        f'<button class="btn" id="bulk-rescope">{t("Re-scope")}</button> '
        f'<button class="btn" id="bulk-supersede">{t("Supersede")}</button> '
        f'<button class="btn btn-danger" id="bulk-delete">{t("Delete")}</button> '
        f'<button class="btn btn-ghost" id="bulk-clear">{t("Clear selection")}</button></div>'
        f'<div class="modal-overlay" id="modal" aria-hidden="true"><div class="modal">'
        f'<div class="modal-head"><h3 id="modal-title">{t("Add memory")}</h3>'
        f'<button type="button" class="modal-close" data-close aria-label="close">&times;</button></div>'
        f'<form id="mem-form"><input type="hidden" name="action" id="m-action" value="add">'
        f'<input type="hidden" name="id" id="m-id" value="">'
        f'<div class="form-row"><label>{t("Type")} <select name="type" id="m-type">'
        + "".join(f"<option>{h(ty)}</option>" for ty in mem.TYPES) + "</select></label>"
        f'<label>{t("Scope")} <small>{t("global or project:slug")}</small>'
        f'<input type="text" name="scope" id="m-scope" value="global" list="scopes"></label>'
        f'<label>Confidence <input type="text" name="confidence" id="m-confidence" value="1.0"></label></div>'
        f'<label>Summary <input type="text" name="summary" id="m-summary" placeholder="{h(t("one-line summary"))}" required></label>'
        f'<label>Body <textarea name="body" id="m-body" placeholder="{h(t("memory details (simple markdown, inline `code`)"))}" required></textarea></label>'
        f'<div class="modal-err" id="m-err" style="display:none"></div>'
        f'<div class="form-actions"><button class="btn btn-primary" id="m-submit" type="submit">{t("Save")}</button>'
        f'<button type="button" class="btn btn-ghost" data-close>{t("Cancel")}</button></div></form></div></div>')

    txt = {"supersedeQ": t("Mark as superseded?"),
           "deleteQ": t("Delete permanently? (it stays in git history)"),
           "rescopeQ": t("New scope (global or project:slug):"), "failed": t("Operation failed"),
           "network": t("Network error"), "addTitle": t("Add memory"), "editTitle": t("Edit"),
           "csrf": t("Session expired (the server restarted) — reload the page and try again.")}
    js = ("<script>\nvar CSRF = " + json.dumps(_CSRF) + ";\nvar STATUS_FILTER = " + json.dumps(fstat)
          + ";\nvar TXT = " + json.dumps(txt, ensure_ascii=False) + ";\n" + _MEM_JS_BODY + "\n</script>")
    return layout(t("Memories") + " — mem0ry4ai", "memories", "\n".join(parts), aside, after + js)


def page_index(qs=None):
    stats = store_stats()
    health = health_checks()
    nav_help = (
        'Cardurile de sus duc in <a href="/memories">Memorii</a> (lista filtrabila) si '
        '<a href="/projects">Proiecte</a> (sumar per proiect). <a href="/inject">Ce vede Claude</a> '
        '= injectarea la SessionStart; <a href="/git">Istoric git</a> = timeline-ul store-ului.'
        if lang() == "ro" else
        'The cards above lead to <a href="/memories">Memories</a> (the filterable list) and '
        '<a href="/projects">Projects</a> (per-project summary). <a href="/inject">What Claude sees</a> '
        '= the SessionStart injection; <a href="/git">Git history</a> = the store timeline.')
    intro = (t("help.intro") if lang() == "ro" else
             "A memory is one short fact you want to keep between sessions. The source of truth is "
             "markdown + git; the web UI and <code>mem.py</code> are two windows onto the same store.")
    health_html = "\n        ".join(
        f'<li><span class="dot {"dot-unk" if ok is None else ("dot-ok" if ok else "dot-err")}"></span> '
        f'{h(lbl)} <span class="hd">{h(det)}</span></li>'
        for lbl, ok, det in health)
    content = f'''  <h2>{t("System status")}</h2>
  <div class="dash" id="dash-cards">{dash_cards(stats)}</div>
  <div class="dash-row">
    <div class="dash-col">
      <h4>{t("Health")}</h4>
      <ul class="health">
        {health_html}
      </ul>
    </div>
    <div class="dash-col">
      <h4>{t("Recent activity")}</h4>
      <ul class="recent" id="recent-list">{recent_list(stats)}</ul>
    </div>
  </div>
  <p class="foot">{t("Source of truth:")} <code>store/*.md</code> {t("(markdown + git). CLI:")} <code>./mem.py</code>.</p>'''
    aside = f'''<aside class="help">
  <h3>{t("Quick guide")}</h3>
  <p>{intro}</p>
  <h4>{t("Navigation")}</h4>
  <p>{nav_help}</p>
</aside>'''
    scripts = '''<script>
var pollVer = '', pollBusy = false;
function pollStore(){
  if (pollBusy || document.hidden) return;
  pollBusy = true;
  fetch('/poll?ver=' + encodeURIComponent(pollVer))
    .then(function(r){ return r.json(); })
    .then(function(j){
      pollBusy = false;
      if (!j.ver) return;
      if (pollVer === '') { pollVer = j.ver; return; }
      if (!j.changed) return;
      pollVer = j.ver;
      var dc = document.getElementById('dash-cards'), rl = document.getElementById('recent-list');
      if (dc && j.cards_html) { dc.innerHTML = j.cards_html; flashEl(dc); }
      if (rl && j.recent_html) { rl.innerHTML = j.recent_html; flashEl(rl); }
    })
    .catch(function(){ pollBusy = false; });
}
function flashEl(el){ el.style.transition='none'; el.style.background='rgba(0,111,255,.07)';
  setTimeout(function(){ el.style.transition='background 1.2s'; el.style.background=''; }, 60); }
setInterval(pollStore, 4000); pollStore();
</script>'''
    return layout("mem0ry4ai", "dashboard", content, aside, scripts)


def endpoint_poll(qs):
    client_ver = (qs.get("ver", [""])[0] or "").strip()
    ver = store_version()
    if client_ver and client_ver == ver:
        return {"changed": False, "ver": ver}
    stats = store_stats()
    return {"changed": bool(client_ver), "ver": ver, "active": stats["active"],
            "cards_html": dash_cards(stats), "recent_html": recent_list(stats)}


# ---------- HTTP server ----------
ROUTES_HTML = {"/": page_index, "/projects": page_projects, "/project": page_project,
               "/inject": page_inject, "/git": page_git, "/queue": page_queue, "/links": page_links,
               "/memories": page_memories, "/claude-md": page_claudemd, "/about": page_about}
ROUTES_JSON = {"/poll": endpoint_poll}


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "mem0ry4ai"

    def log_message(self, *a):
        pass  # quiet

    def _set_lang(self, qs):
        q = (qs.get("lang", [""])[0] or "")
        if q in ("en", "ro"):
            _ctx.lang = q
            return
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        c = cookie["mem_lang"].value if "mem_lang" in cookie else ""
        envdef = "ro" if os.environ.get("MEM_UI_LANG") == "ro" else "en"
        _ctx.lang = c if c in ("en", "ro") else envdef

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)
        self._set_lang(qs)
        self._read_flash()
        if path.startswith("/assets/"):
            return self._serve_static(path)
        if path == "/git" and "diff" in qs:
            return self._send_text(git_store_diff(qs["diff"][0]) or t("(diff unavailable)"))
        if path == "/git" and "poll" in qs:
            return self._send_json(git_poll(qs))
        if path in ROUTES_JSON:
            return self._send_json(ROUTES_JSON[path](qs))
        if path in ROUTES_HTML:
            body = ROUTES_HTML[path](qs)
            if body is None:
                return self._redirect("/")
            return self._send_html(body)
        self._send_html(f"<h1>404</h1><p>{h(path)} — not found. <a href=\"/\">Dashboard</a></p>", 404)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        self._set_lang(urllib.parse.parse_qs(parsed.query))
        length = int(self.headers.get("Content-Length", 0) or 0)
        form = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8")) if length else {}
        if parsed.path == "/git":
            if not csrf_ok(form.get("csrf", [""])[0]):
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"CSRF")
                return
            ok, msg = git_commit_store(form.get("msg", [""])[0])
            fm = (t("Committed:") + " " + msg) if ok else (t("Error") + ": " + msg)
            return self._redirect_with_flash("/git", fm, "success" if ok else "error")
        if parsed.path == "/queue":
            if not csrf_ok(form.get("csrf", [""])[0]):
                return self._send_json({"ok": False, "error": "CSRF"})
            action = form.get("action", [""])[0]
            qid = (form.get("qid", [""])[0] or "").strip()
            try:
                if action == "approve":
                    over = {}
                    for k in ("type", "scope", "summary", "body", "confidence"):
                        v = form.get(k, [""])[0]
                        if v != "":
                            over[k] = v.strip()
                    ok = queue_approve(qid, over)
                elif action == "reject":
                    queue_remove(qid)
                    ok = True
                else:
                    return self._send_json({"ok": False, "error": "unknown action"})
                return self._send_json({"ok": ok, "qid": qid, "remaining": len(queue_pending())})
            except Exception as e:
                return self._send_json({"ok": False, "error": str(e)})
        if parsed.path == "/links":
            if not csrf_ok(form.get("csrf", [""])[0]):
                return self._send_json({"ok": False})
            action = form.get("action", [""])[0]
            a = (form.get("a", [""])[0] or "").strip()
            b = (form.get("b", [""])[0] or "").strip()
            if action == "link":
                ok = link_records(a, b)
            elif action == "dismiss":
                ok = dismiss_pair(a, b)
            else:
                ok = False
            return self._send_json({"ok": ok})
        if parsed.path == "/about":
            if not csrf_ok(form.get("csrf", [""])[0]):
                return self._send_json({"ok": False, "error": "CSRF"})
            body = form.get("body", [""])[0].replace("\r\n", "\n").strip()
            if not body:
                return self._send_json({"ok": False, "error": t("Write something about yourself first.")})
            try:
                rec = profile_record()
                if rec:  # update the existing profile in place
                    ok = mem.update_memory(rec["id"], body=body)
                else:    # first time: create the single global profile record
                    mem.add_memory("profile", "global", "About me", body, "1.0", "web")
                    ok = True
                return self._send_json({"ok": bool(ok)})
            except Exception as e:
                return self._send_json({"ok": False, "error": str(e)})
        if parsed.path == "/claude-md":
            if not csrf_ok(form.get("csrf", [""])[0]):
                return self._send_json({"ok": False, "error": "CSRF"})
            # path is resolved server-side from a SAFE key (claudemd_path) — never from the client
            path = claudemd_path(form.get("key", [""])[0])
            if not path:
                return self._send_json({"ok": False, "error": "invalid target"})
            content = form.get("content", [""])[0].replace("\r\n", "\n")
            if not content.strip():  # never silently wipe the user's CLAUDE.md to zero bytes
                return self._send_json({"ok": False, "error": t("refusing to write an empty file")})
            if not content.endswith("\n"):
                content += "\n"
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                bak = None
                if os.path.exists(path):  # back up the user's existing file before overwriting it
                    with open(path, encoding="utf-8") as f:
                        old = f.read()
                    stamp = time.strftime("%Y%m%d_%H%M%S")
                    n = 0
                    while True:  # exclusive create -> never clobber a backup (same-second/concurrent saves)
                        bak = path + ".bak." + stamp + ("" if n == 0 else f".{n}")
                        try:
                            with open(bak, "x", encoding="utf-8") as f:
                                f.write(old)
                            break
                        except FileExistsError:
                            n += 1
                # atomic replace: the live file is always whole-old or whole-new, never truncated
                tmp = path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    f.write(content)
                os.replace(tmp, path)
                return self._send_json({"ok": True, "backup": os.path.basename(bak) if bak else None})
            except Exception as e:
                return self._send_json({"ok": False, "error": str(e)})
        if parsed.path == "/memories":
            if not csrf_ok(form.get("csrf", [""])[0]):
                return self._send_json({"ok": False, "error": "CSRF"})
            action = form.get("action", [""])[0]
            rid = (form.get("id", [""])[0] or "").strip()
            ok, err = True, None
            try:
                if action == "add":
                    rid = mem.add_memory(form.get("type", [""])[0].strip(), form.get("scope", [""])[0].strip(),
                                         form.get("summary", [""])[0].strip(), form.get("body", [""])[0],
                                         (form.get("confidence", ["1.0"])[0].strip() or "1.0"), "web")
                elif action == "edit":
                    new_scope = form.get("scope", [""])[0].strip()
                    ok = mem.update_memory(rid, rtype=form.get("type", [""])[0].strip() or None,
                                           summary=form.get("summary", [""])[0].strip(),
                                           body=form.get("body", [""])[0],
                                           confidence=(form.get("confidence", ["1.0"])[0].strip() or "1.0"))
                    if ok and new_scope:   # scope change = move the record's file (PHP left it misfiled)
                        rec = next((r for r in mem.all_records() if r["id"] == rid), None)
                        if rec and rec["meta"].get("scope", "") != new_scope:
                            ok = mem.rescope_memory(rid, new_scope)
                elif action == "rescope":
                    ok = mem.rescope_memory(rid, form.get("scope", [""])[0].strip())
                elif action == "supersede":
                    ok = mem.supersede_memory(rid, form.get("by", [""])[0].strip()) is not None
                elif action == "delete":
                    ok = mem.delete_memory(rid)
                else:
                    ok, err = False, "unknown action"
            except Exception as e:
                ok, err = False, str(e)
            if err is not None:
                return self._send_json({"ok": False, "error": err})
            if not ok:
                return self._send_json({"ok": False, "error": t("Operation failed")})
            resp = {"ok": True, "action": action, "id": rid}
            if action in ("add", "edit", "supersede", "rescope"):
                rec = next((r for r in mem.all_records() if r["id"] == rid), None)
                if rec:
                    resp["html"] = render_row(rec, records_by_id(), related_in_index())
                    resp["status"] = rec["meta"].get("status", "active")
                    resp["scope"] = rec["meta"].get("scope", "")
            return self._send_json(resp)
        self.send_response(404)
        self.end_headers()

    def _read_flash(self):
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        if "mem_flash" in cookie and cookie["mem_flash"].value:
            kind, _, msg = urllib.parse.unquote(cookie["mem_flash"].value).partition("|")
            _ctx.flash = (msg, kind)
        else:
            _ctx.flash = None

    def _redirect(self, location):
        self.send_response(302)
        self.send_header("Location", location)
        self.end_headers()

    def _redirect_with_flash(self, location, msg, kind):
        self.send_response(302)
        self.send_header("Location", location)
        ck = SimpleCookie()
        ck["mem_flash"] = urllib.parse.quote(f"{kind}|{msg}")
        ck["mem_flash"]["path"] = "/"
        self.send_header("Set-Cookie", ck["mem_flash"].OutputString())
        self.end_headers()

    def _send_text(self, text):
        data = text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _serve_static(self, path):
        rel = path[len("/assets/"):]
        full = os.path.normpath(os.path.join(ASSETS_DIR, rel))
        if not full.startswith(ASSETS_DIR) or not os.path.isfile(full):
            return self._send_html("<h1>404</h1>", 404)
        ext = os.path.splitext(full)[1].lower()
        ctype = {".css": "text/css", ".js": "text/javascript", ".svg": "image/svg+xml",
                 ".png": "image/png", ".woff2": "font/woff2"}.get(ext, "application/octet-stream")
        with open(full, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith("text") else ""))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "max-age=300")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, body, code=200):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        if getattr(_ctx, "lang", None):
            ck = SimpleCookie()
            ck["mem_lang"] = _ctx.lang
            ck["mem_lang"]["path"] = "/"
            ck["mem_lang"]["max-age"] = 31536000
            self.send_header("Set-Cookie", ck["mem_lang"].OutputString())
        if getattr(_ctx, "flash", None):   # shown once, then cleared
            fk = SimpleCookie()
            fk["mem_flash"] = ""
            fk["mem_flash"]["path"] = "/"
            fk["mem_flash"]["max-age"] = 0
            self.send_header("Set-Cookie", fk["mem_flash"].OutputString())
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, obj):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def _load_local_env():
    """Per-machine overrides (gitignored .mem-local.env next to the code): KEY=value lines,
    e.g. MEM_UI_LANG=ro. Does not override variables already set in the environment."""
    p = os.path.join(HERE, ".mem-local.env")
    if not os.path.isfile(p):
        return
    try:
        with open(p, encoding="utf-8") as f:
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


def _reload_watcher(interval=2.0):
    """Auto-reload (on by default; opt out with MEM_NO_RELOAD=1): re-exec the server when one of its
    own source files changes AND still compiles — so editing the tool or `git pull`-ing an update takes
    effect with no manual restart. A syntax error or half-saved file is ignored, so it never kills a
    running server. The hooks run as fresh subprocesses each time, so they need no watching."""
    import py_compile
    watched = [os.path.abspath(__file__)]
    for m in (mem, sys.modules.get("redact")):
        f = getattr(m, "__file__", None)
        if f:
            watched.append(os.path.abspath(f))

    def mtime(p):
        try:
            return os.path.getmtime(p)
        except OSError:
            return None

    seen = {p: mtime(p) for p in watched}

    def loop():
        while True:
            time.sleep(interval)
            if not any(mtime(p) != seen[p] for p in watched):
                continue
            try:                       # only reload if everything still compiles
                for p in watched:
                    py_compile.compile(p, doraise=True)
            except Exception:
                seen.update({p: mtime(p) for p in watched})   # remember; retry once it compiles again
                continue
            os.execv(sys.executable, [sys.executable] + sys.argv)   # replace this process with fresh code

    threading.Thread(target=loop, daemon=True).start()


def serve(host="127.0.0.1", port=None):
    _load_local_env()
    port = int(port or os.environ.get("MEM_WEB_PORT", "8841"))
    httpd = Server((host, port), Handler)
    reload_on = os.environ.get("MEM_NO_RELOAD", "").strip().lower() not in ("1", "true", "yes")
    if reload_on:
        _reload_watcher()
    print(f"mem0ry4ai web UI on http://{host}:{port}/  (data: {mem.DATA})"
          + ("  · auto-reload on" if reload_on else ""))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    serve(port=sys.argv[1] if len(sys.argv) > 1 else None)
