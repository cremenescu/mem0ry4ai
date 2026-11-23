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
import math
import os
import re
import secrets
import signal
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
        # create owner-only in one step — no open()+chmod window where the token is world-readable
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(tok)
    except OSError:
        pass
    return tok


_CSRF = _load_or_make_csrf()

# When the server runs detached (no console — e.g. launched by the SessionStart hook),
# every child process (git, the hook subprocess) pops its own cmd window that flashes on
# each request. CREATE_NO_WINDOW suppresses it. The flag only exists on Windows.
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def _int_env(key, default):
    """int(os.environ[key]) that never raises — a hand-edited .mem-local.env must not 500 a page."""
    try:
        return int(os.environ.get(key, default))
    except (TypeError, ValueError):
        return int(default)


# ---------- per-request context (thread-safe language) ----------
_ctx = threading.local()
# A record id as the store mints it: YYYYMMDD + '-' + a short suffix. Used to tell "go to this
# record" apart from "search for this text" when something is typed into the search box.
_ID_RE = re.compile(r"^\d{8}-[A-Za-z0-9]{4,12}$")
# Rows per page on /memories. The default view listed every active record -- 9.5 MB of HTML for
# ~2,200 records once each body rode along three times; a page of 200 lean rows is ~290 KB,
# 60 KB of which is the page itself (measured 2026-10-06).
PAGE_ROWS = 200


def lang():
    return getattr(_ctx, "lang", "en")


# ---------- i18n: English inline strings are the keys; Romanian overrides below ----------
# (subset for the ported pages; grows as more pages land. Missing keys fall back to English.)
RO = {
    "local memory": "memorie locala",
    # settings (power-user)
    "Settings": "Setari", "Power-user settings": "Setari avansate", "advanced": "avansat",
    "Working notes": "Note de lucru", "Working": "De lucru", "Promote": "Promoveaza", "Dismiss": "Renunta",
    "No working notes left.": "Nicio nota de lucru ramasa.",
    "Dismiss this working note? (it is marked superseded, not deleted)":
        "Renunti la aceasta nota de lucru? (e marcata superseded, nu stearsa)",
    "applies on": "se aplica la", "overridden": "modificat", "next session": "sesiunea urmatoare",
    "restart web server": "repornire server web", "next MCP session": "sesiunea MCP urmatoare",
    "Search & ranking": "Cautare & ranking", "Web UI": "Interfata web",
    "Embeddings & local LLM (Ollama)": "Embeddings & LLM local (Ollama)", "Safety": "Siguranta",
    "Save settings": "Salveaza setarile", "Reset all to defaults": "Reseteaza tot la implicit",
    "restart the web server to apply": "reporneste serverul web ca sa se aplice",
    "Tune the knobs that control injection, search and the UI. Saved to .mem-local.env "
    "(gitignored) and honored by the hook, the CLI and the MCP server. On load, a value "
    "exported in your shell takes precedence over the file.":
        "Regleaza parametrii care controleaza injectarea, cautarea si interfata. Salvate in "
        ".mem-local.env (gitignored) si respectate de hook, CLI si serverul MCP. La incarcare, o "
        "valoare exportata in shell are prioritate fata de fisier.",
    "Reset every setting to its default? This clears your overrides in .mem-local.env.":
        "Resetezi toate setarile la implicit? Asta sterge override-urile din .mem-local.env.",
    "Dashboard": "Panou", "Memories": "Memorii", "Projects": "Proiecte", "Links": "Legaturi",
    "Git history": "Istoric git", "What Claude sees": "Ce vede Claude", "to review": "de revizuit",
    "System status": "Status sistem", "Health": "Health", "Recent activity": "Activitate recenta",
    "active": "active", "superseded": "superseded", "open todos": "todo deschise", "of": "din",
    "SessionStart injection": "Injectare SessionStart", "budget": "buget", "critical rules": "reguli critice",
    "profile": "profil", "rules": "reguli", "knowledge": "cunostinte", "per hook": "per hook",
    "limit": "limita", "a part was cut to fit": "o parte a fost taiata ca sa incapa",
    "one hook for everything — re-run hooks/install.py to give each section its own limit":
        "un singur hook pentru tot — ruleaza din nou hooks/install.py ca fiecare sectiune sa aiba limita ei",
    "Claude Code caps each hook's output separately, so each part has its own limit.":
        "Claude Code plafoneaza separat iesirea fiecarui hook, deci fiecare parte are limita ei.",
    "active — last capture": "activ — ultima captura",
    "no evidence yet (no captures in staging)": "fara dovezi inca (nicio captura in staging)",
    "Source of truth:": "Sursa de adevar:", "(markdown + git). CLI:": "(markdown + git). CLI:",
    "Quick guide": "Ghid rapid", "Navigation": "Navigare", "Guide": "Ghid", "Filters": "Filtre",
    "Status": "Status", "all scopes": "toate domeniile", "Show help": "Arata ghidul paginii",
    "Store writable": "Store writable", "write OK": "scriere OK",
    "Staging writable": "Staging writable", "queue functional": "coada functionala",
    "not created yet": "necreat inca", "FTS index": "Index FTS", "up to date": "la zi",
    "rebuilt just now": "reconstruit acum",
    "FTS5 unavailable — search falls back to substring": "FTS5 indisponibil — cautarea cade pe substring",
    "Embedder (semantic)": "Embedder (semantic)", "vectors": "vectori",
    "no vectors — optional, run mem.py embed": "fara vectori — optional, ruleaza mem.py embed",
    "Ollama offline (keyword search)": "Ollama oprit (cautare keyword)",
    "Review queue (health)": "Coada de review", "empty": "goala",
    "Code drift": "Drift fata de cod", "anchored": "ancorate", "The cards": "Cardurile",
    "adjusted to the allowed range": "ajustat la intervalul permis",
    "without a vector — run mem.py embed": "fara vector — ruleaza mem.py embed",
    "Open a row to read it, then pick one: the lesson still holds and only the path "
    "moved, so re-point it with <code>mem.py anchor &lt;id&gt; &lt;paths&gt;</code>; the "
    "lesson is dead with the code it described, so supersede it from Actions; or it was "
    "never really about those files, so clear the anchor with "
    "<code>mem.py anchor &lt;id&gt; -</code> and it stops being reported.":
        "Deschide randul, citeste-l, apoi alege una din trei: lectia inca e valabila si doar calea "
        "s-a mutat, deci o repointezi cu <code>mem.py anchor &lt;id&gt; &lt;cai&gt;</code>; lectia a "
        "murit odata cu codul pe care il descria, deci o retragi din Actions (Supersede); sau n-a fost de fapt "
        "despre acele fisiere, deci stergi ancora cu <code>mem.py anchor &lt;id&gt; -</code> si nu mai "
        "e raportata.",
    "No such project": "Proiectul nu exista", "Did you mean": "Ai vrut sa spui",
    "All projects": "Toate proiectele",
    "No memory has ever been written for this scope. If you expected memories here, "
    "check the spelling — a project only appears once something is saved to it.":
        "Nicio memorie nu a fost scrisa vreodata pentru acest scope. Daca te asteptai sa gasesti ceva, "
        "verifica scrierea — un proiect apare abia dupa ce se salveaza ceva in el.",
    "already reviewed, or no such candidate": "deja revizuit, sau candidatul nu exista",
    "not found": "negasit", "not a working note": "nu e nota de lucru",
    "could not write .mem-local.env (permissions? disk full?) — nothing was saved":
        "nu s-a putut scrie .mem-local.env (permisiuni? disc plin?) — nu s-a salvat nimic",
    "could not write .mem-local.env (permissions? disk full?) — nothing was reset":
        "nu s-a putut scrie .mem-local.env (permisiuni? disc plin?) — nu s-a resetat nimic",
    "drift": "drift", "Show all memories": "Arata toate memoriile",
    "Showing only memories anchored to files that have since gone missing, moved, or "
    "been committed to many times. They are not proven wrong — they are worth re-reading.":
        "Se afiseaza doar memoriile ancorate la fisiere care intre timp au disparut, s-au mutat sau "
        "au primit multe commit-uri. Nu sunt dovedite gresite — merita recitite.",
    "no memory is anchored to files yet": "nicio memorie nu e ancorata la fisiere inca",
    "all files present and quiet": "toate fisierele exista si sunt linistite",
    "worth re-reading": "de recitit",
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
    "files": "fisiere", "Anchored to a file": "Ancorat la un fisier",
    "Showing only memories anchored to": "Doar memoriile ancorate la",
    "A trailing slash matches a whole directory.": "Slash la final = tot directorul.", "invalidated": "invalidat", "Project page": "Pagina de proiect", "Actions": "Actiuni",
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
    "Statuses not listed at start": "Status-uri nelistate la start",
    "kept, searchable — mem.py stale": "pastrate, cautabile — mem.py stale",
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
    # links: suggestion threshold
    "Threshold": "Prag", "Apply": "Aplica",
    "higher = fewer, stronger matches": "mai mare = mai putine, mai puternice",
    "No matches at this threshold — lower it to see more.": "Nicio potrivire la acest prag — coboara-l ca sa vezi mai multe.",
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
    "+ Add memory": "+ Adauga memorie", "search — Enter = full text, typing filters this page": "cauta — Enter = full text, tastarea filtreaza pagina",
    "all types": "toate tipurile", "all scopes": "toate scope-urile", "Add memory": "Adauga memorie",
    "one-line summary": "sumar pe o linie",
    "memory details (simple markdown, inline `code`)": "detalii (markdown simplu, `code` inline)",
    "Mark as superseded?": "Marchezi ca superseded?",
    "Delete permanently? (it stays in git history)": "Stergi definitiv? (ramane in istoricul git)",
    "New scope (global or project:slug):": "Scope nou (global sau project:slug):", "Network error": "Eroare de retea",
    "Session expired (the server restarted) — reload the page and try again.": "Sesiune expirata (serverul a repornit) — reincarca pagina si incearca din nou.",
    # duplicates -- the keys carry the page as context ("dups|"): short words that read differently elsewhere
    "Duplicates": "Dubluri", "Duplicates threshold": "Pragul de dubluri",
    "How alike two active memories of the same type must be to be offered as a group on the Duplicates "
    "page and by 'mem.py dups'. Raise it to be shown only near-identical texts; lower it for more groups "
    "to look through, more of which will turn out not to be duplicates. Default 0.80.":
        "Cat de asemanatoare trebuie sa fie doua memorii active de acelasi tip ca sa apara ca grup pe pagina "
        "Dubluri si in 'mem.py dups'. Mai sus: doar textele aproape identice. Mai jos: mai multe grupuri de "
        "parcurs, dintre care mai multe se vor dovedi a nu fi dubluri. Implicit 0.80.",
    "dups|groups": "grupuri", "dups|groups to review": "grupuri de vazut", "dups|extra copies": "copii in plus",
    "dups|identical or nearly identical text": "cu text identic sau aproape", "dups|settled": "rezolvate",
    "dups|threshold, same type · Settings": "prag, acelasi tip · Setari", "dups|marked": "marcat",
    "dups|= words that do not appear in the memory that stays": "= cuvinte care nu apar in memoria care ramane",
    "dups|State": "Stare", "dups|to review": "de vazut", "dups|all": "toate", "dups|Where": "Unde",
    "dups|anywhere": "oriunde", "dups|same project": "acelasi proiect", "dups|across projects": "intre proiecte",
    "dups|order": "ordine", "dups|most alike": "cele mai asemanatoare", "dups|least to lose": "pierzi cel mai putin",
    "dups|newest first": "cele mai noi", "dups|largest groups": "grupurile mari",
    "dups|latest settled": "ultimele rezolvate", "dups|likeness": "asemanare",
    "dups|1 memory": "o memorie", "dups|memories": "memorii",
    "dups|written {x} apart": "scrise la {x}", "dups|written in the same second": "scrise in aceeasi secunda",
    "dups|1 second": "o secunda", "dups|seconds": "secunde", "dups|1 minute": "un minut", "dups|minutes": "minute",
    "dups|1 hour": "o ora", "dups|hours": "ore", "dups|1 day": "o zi", "dups|days": "zile",
    "dups|1 month": "o luna", "dups|months": "luni",
    "dups|stays": "ramane", "dups|to retire": "se retrage", "dups|retired": "retrasa",
    "dups|newest": "cea mai noua", "dups|says the most": "spune cel mai mult",
    "dups|identical text": "text identic", "dups|{n}% only here": "{n}% doar aici",
    "dups|words that do not appear in the memory that stays": "cuvinte care nu apar in memoria care ramane",
    "dups|Title": "Titlu", "dups|Written": "Scrisa", "dups|Size": "Marime",
    "dups|1 character": "un caracter", "dups|characters": "caractere",
    "dups|Opened by agents": "Deschisa de agenti", "dups|never": "niciodata", "dups|once": "o data",
    "dups|times": "ori", "dups|Cited by": "Citata de", "dups|Anchors": "Ancore",
    "dups|Keep this one": "Pastreaz-o pe asta", "dups|Not duplicates": "Nu sunt dubluri", "dups|Skip": "Sar peste",
    "dups|Edit first": "Editeaza intai", "dups|Retire the other": "Retrage cealalta",
    "dups|Retire the other {n}": "Retrage celelalte {n}", "dups|Undo": "Anuleaza", "dups|Groups": "Grupuri",
    "dups|1 to review": "unul de vazut", "dups|{n} to review": "{n} de vazut",
    "dups|1 settled": "unul rezolvat", "dups|{n} settled": "{n} rezolvate",
    "dups|1 retired": "retrasa una", "dups|{n} retired": "retrase {n}",
    "dups|both left": "lasate amandoua", "dups|all {n} left": "lasate toate {n}",
    "dups|No group matches this filter.": "Niciun grup la filtrul asta.",
    "dups|Next to review": "Urmatorul de vazut",
    "dups|Comparing memories… {n} left": "Se compara memoriile… {n} ramase",
    "dups|No vectors yet: likeness comes from the semantic index — run mem.py embed.":
        "Inca nu exista vectori: asemanarea vine din indexul semantic — ruleaza mem.py embed.",
    "dups|No duplicates above the threshold.": "Nicio dublura peste prag.",
    "dups|Nothing settled yet.": "Nimic rezolvat inca.",
    "dups|Active again. No note of this retirement is left, so the memory that stayed keeps the links and "
    "anchors it was given.":
        "Sunt din nou active. Nota retragerii nu mai exista, asa ca memoria pastrata ramane cu legaturile si "
        "ancorele primite atunci.",
    "dups|Active again, but no longer alike enough at this threshold to be offered as a group.":
        "Sunt din nou active, dar la pragul asta nu mai sunt destul de asemanatoare ca sa apara ca grup.",
    "dups|Saved. These memories are no longer offered as a group.": "Salvat. Memoriile astea nu mai apar ca grup.",
    # what the store answers when it refuses (the id it names follows the text)
    "dups|id not found": "id negasit", "dups|nothing to retire": "nimic de retras",
    "dups|it takes at least two memories to be distinct from one another":
        "e nevoie de cel putin doua memorii ca sa nu fie dubluri una alteia",
    "dups|the memory to keep is not active": "memoria care ar ramane nu mai e activa",
    "dups|not active, nothing to retire": "nu mai e activa, nu e nimic de retras",
    "dups|cannot retire protected memory": "memoria e protejata si nu poate fi retrasa",
    "dups|cannot retire a critical rule in favour of a memory that is not one":
        "o regula critica nu se retrage in favoarea unei memorii care nu e regula critica",
    "dups|protected": "protejata", "dups|a protected memory is not retired": "o memorie protejata nu se retrage",
    "dups|the memory that was kept is no longer active": "memoria pastrata nu mai e activa",
    "dups|nothing is retired as a duplicate of": "nimic nu e retras ca dublura a memoriei",
    "help.intro": "O memorie e un fapt scurt pe care vrei sa-l pastrezi intre sesiuni. Sursa de adevar e "
                  "markdown + git; web UI-ul si <code>mem.py</code> sunt doua ferestre spre acelasi store.",
    "help.nav": "Click pe un <b>scope</b> → pagina proiectului (status + todo sus). Click pe un <b>id</b> → lantul "
                "de supersedare. Click pe un rand → corpul memoriei (se incarca la prima deschidere). Antetul "
                "grupului → pliere.",
    "help.bulk": "Bifezi randuri → bara de jos: Supersede / Re-scope / Delete pe toate deodata.",
    "help.search": "Camp + Enter = FTS ranked (acelasi index ca <code>mem.py search</code>). Ledul de langa buton "
                   "arata modul de cautare: <b>verde</b> = LLM-ul local e pornit, cautarea e keyword + semantic; "
                   "<b>gri</b> = cade automat pe cautarea clasica pe cuvinte. Fara comutator. Ce tastezi in camp "
                   "filtreaza randurile deja afisate pe pagina asta, dupa rezumat, scope si tip — corpul unei "
                   "memorii se cauta doar prin cautarea full-text (Enter), iar listele lungi vin in pagini de "
                   + str(PAGE_ROWS) + ".",
    "all in this scope →": "tot din acest scope →", "previous": "inapoi", "next": "inainte",
    "loading…": "se incarca…",
}


def t(s, ctx=None):
    """`ctx` tells apart short words that read differently from one page to another ("all", "State",
    "never"): the Romanian is then looked up as "ctx|words" first. The English stays the words."""
    if lang() != "ro":
        return s
    return RO.get(f"{ctx}|{s}", RO.get(s, s)) if ctx else RO.get(s, s)


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
    """Drop a candidate. Returns whether one was actually there — callers report success on the
    strength of it, and "rejected" on an id that no longer exists means the user believes they
    turned down something that was in fact already approved and is now a live memory."""
    rows = queue_load()
    keep = [r for r in rows if r.get("qid", "") != qid]
    if len(keep) == len(rows):
        return False
    queue_save(keep)
    return True


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


_DRIFT_CACHE = {"key": None, "at": 0.0, "val": None}


def drift_findings(records):
    """`mem.check_drift`, memoised for a few seconds — WEB ONLY; the CLI stays exact.

    Drift depends on the working tree, so it is deliberately not persisted. But it is asked for
    three times on a page load (the health row, the pill count, the filtered list) and it costs one
    git call per project, and a dashboard that spends a third of a second recomputing the same
    answer for every visitor is paying that tax constantly. A few seconds of staleness cannot
    mislead anyone here: the output is a suggestion to RE-READ a memory, never an edit, and nobody
    commits ten times and reloads inside the window expecting the number to have moved.

    The key includes each record's id and last-touched stamp, so editing or re-anchoring a memory
    invalidates the cache immediately — which is exactly the interaction where a stale answer would
    be noticed and would look like the fix had not worked.
    """
    key = tuple(sorted((r["id"], r["meta"].get("updated") or r["meta"].get("created") or "",
                        r["meta"].get("files") or "") for r in records))
    now = time.time()
    try:
        ttl = float(os.environ.get("MEM_WEB_DRIFT_TTL", "5"))
    except (TypeError, ValueError):
        ttl = 5.0
    c = _DRIFT_CACHE
    if c["key"] == key and (now - c["at"]) < ttl:
        return c["val"]
    val = mem.check_drift(records)
    c.update(key=key, at=now, val=val)
    return val


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
    emodel = os.environ.get("MEM_EMBED_MODEL", "bge-m3")
    # Coverage, not just a count. "864 vectors" next to a green light says nothing about whether
    # every memory HAS one — and a record without a vector is invisible to semantic search, silently.
    try:
        emb = mem.load_embeddings()
        n_missing = sum(1 for r in mem.all_records()
                        if r["meta"].get("status", "active") == "active" and r["id"] not in emb)
    except Exception:
        n_missing = 0
    gap = f" · {n_missing} {t('without a vector — run mem.py embed')}" if n_missing else ""
    if vec == 0:
        out.append([t("Embedder (semantic)"), None, t("no vectors — optional, run mem.py embed")])
    elif _ollama_up():
        # Amber, not green, when some memories are missing: a partial index that reports success is
        # how you end up trusting a search that cannot see part of the store.
        out.append([t("Embedder (semantic)"), not n_missing, f"{emodel} · {vec} {t('vectors')}{gap}"])
    else:
        out.append([t("Embedder (semantic)"), None,
                    f"{vec} {t('vectors')}{gap} · {t('Ollama offline (keyword search)')}"])
    # Informative, never an error: these are kept, they are only not listed at session start.
    try:
        n_old = len(mem.stale_statuses())
    except Exception:
        n_old = 0
    if n_old:
        out.append([t("Statuses not listed at start"), None,
                    f"{n_old} · {t('kept, searchable — mem.py stale')}"])
    nq = len(queue_pending())
    out.append([t("Review queue (health)"), nq == 0, t("empty") if nq == 0 else f"{nq} {t('candidates to review')}"])
    # Anchored memories whose files moved on. Unknown-state (grey) rather than red when nothing is
    # anchored: no signal is not the same as a clean bill of health, and saying "OK" there would be
    # the dashboard lying by omission — the check simply had nothing to look at.
    try:
        anchored = [r for r in mem.all_records()
                    if r["meta"].get("status", "active") == "active" and (r["meta"].get("files") or "").strip()]
        if not anchored:
            out.append([t("Code drift"), None, t("no memory is anchored to files yet")])
        else:
            nd = len(drift_findings(anchored))
            out.append([t("Code drift"), nd == 0,
                        f"{len(anchored)} {t('anchored')} · "
                        + (t("all files present and quiet") if nd == 0
                           else f"{nd} {t('worth re-reading')}"),
                        None if nd == 0 else "/memories?drift=1"])
    except Exception:
        pass
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
    parts, ibud, ncrit = injection_stats()
    if parts:
        # Links to what is actually being injected. A red light with no way to see what is in it,
        # or where the budget is set, is a complaint rather than a diagnostic.
        if len(parts) == 1:
            # The original layout: everything through one hook, so one ceiling for all of it.
            isz = parts[0][1]
            out.append([t("SessionStart injection"), isz <= ibud,
                        f"{round(isz / 1024, 1)} KB / {t('budget')} {round(ibud / 1024, 1)} KB · "
                        f"{ncrit} {t('critical rules')} · "
                        + t("one hook for everything — re-run hooks/install.py to give each section its own limit"),
                        "/inject"])
        else:
            cap = _hook_consts()[1]
            ok = all(sz <= cap and not cut for _, sz, cut in parts)
            det = " · ".join(f"{t(name)} {round(sz / 1024, 1)}" for name, sz, _ in parts)
            out.append([t("SessionStart injection"), ok,
                        f"{det} KB ({t('limit')} {round(cap / 1024, 1)} KB {t('per hook')}) · "
                        f"{ncrit} {t('critical rules')}"
                        + (f" · {t('a part was cut to fit')}" if any(cut for _, _, cut in parts) else ""),
                        "/inject"])
    # git store: real dirty state (dirty is informative, not an error — auto-checkpoint at session end)
    if os.path.isdir(os.path.join(mem.DATA, ".git")):
        out.append([t("Git store"), None, t("uncommitted — auto-checkpoint at session end")]
                   if git_dirty_files() else [t("Git store"), True, t("clean")])
    return out


def _hook_consts():
    """(PARTS, PART_MAX, OVERFLOW_MARK) read from the hook itself, so this page and the hook cannot
    come to disagree about what the limit is or how an overflow is announced."""
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "_m4a_session_start", os.path.join(HERE, "hooks", "session_start.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return tuple(mod.PARTS), int(mod.PART_MAX), str(mod.OVERFLOW_MARK)
    except Exception:
        return ("profile", "rules", "knowledge"), 9600, "> [mem0ry4ai] over the per-hook limit:"


def hook_layout():
    """How the SessionStart hook is registered: 'split' (one hook per part) or 'single'.

    Read from settings.json rather than assumed, because the page exists to show what the agent
    actually receives: after an upgrade the code can split while the registration still calls the
    hook once, and a preview of three roomy parts would then describe a session nobody is having."""
    needle = os.path.join(HERE, "hooks").replace("\\", "/")
    try:
        with open(os.path.join(os.path.expanduser("~"), ".claude", "settings.json"), encoding="utf-8") as f:
            cfg = json.load(f)
        cmds = [hh.get("command", "").replace("\\", "/")
                for entry in ((cfg.get("hooks") or {}).get("SessionStart") or [])
                for hh in (entry.get("hooks", []) or [])]
        ours = [c for c in cmds if needle in c and "session_start.py" in c]
        if ours:
            return "split" if any("--part" in c for c in ours) else "single"
    except (OSError, ValueError):
        pass
    return "split"      # not registered here (plugin install, another machine): show the default layout


def run_injection(cwd, source):
    """The hook's real output for `cwd`, as [(part, text)] in the registered layout, or raises."""
    hook = os.path.join(HERE, "hooks", "session_start.py")
    payload = json.dumps({"cwd": cwd, "hook_event_name": "SessionStart", "source": source})
    names = _hook_consts()[0] if hook_layout() == "split" else (None,)
    out = []
    for name in names:
        # sys.executable (not "python3") -> works on Windows too
        r = subprocess.run([sys.executable, hook] + (["--part", name] if name else []),
                           input=payload, capture_output=True, text=True,
                           timeout=30, creationflags=_NO_WINDOW)
        if r.returncode != 0:
            raise RuntimeError(f"hook exit {r.returncode}: {r.stderr.strip()}")
        out.append((name or "all", r.stdout))
    return out


def injection_stats():
    """([(part, bytes, was_cut)], budget, critical-rule count) for the real injection.
    Root mode is the maximal case, so that is what gets measured. ([], ...) when it cannot run."""
    bud = _int_env("MEM_INJECT_BUDGET", "8000")
    if not os.path.isfile(os.path.join(HERE, "hooks", "session_start.py")):
        return ([], bud, 0)
    mark = _hook_consts()[2]
    try:
        parts = [(name, len(text.encode("utf-8")), mark in text)
                 for name, text in run_injection(os.path.dirname(HERE), "health")]
    except Exception:
        return ([], bud, 0)
    ncrit = sum(1 for rec in mem.all_records()
                if rec["meta"].get("priority") == "critical" and rec["meta"].get("status", "active") == "active")
    return (parts, bud, ncrit)


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
    tier = mem.record_tier(r)
    if tier != "open":
        # The whole point of a tier is that the user can see, at a glance, what an agent will never
        # be told. A classification nobody can check is a classification nobody trusts.
        label = {"private": t("private — never sent to a model"),
                 "redacted": t("redacted — summary only reaches a model")}[tier]
        out += f'<div class="rec-files"><code>{h(tier)}</code> {h(label)}</div>'
    files = rec_ids(r, "files")
    if files:
        # Each anchor links to everything else anchored to the same file. That is the question an
        # anchor invites -- "what else did I learn about this file?" -- and leaving it as inert
        # text means the field is only ever readable, never usable.
        out += ('<div class="rec-files">' + h(t("files")) + ": "
                + " ".join('<a href="/memories?files={}"><code>{}</code></a>'.format(
                    h(urllib.parse.quote(f)), h(f)) for f in files) + "</div>")
    sess = (r["meta"].get("session") or "").strip()
    if sess:   # provenance: which conversation produced this memory (search it via `mem.py sessions`)
        out += ('<div class="rec-files" title="' + h(t("Session that produced this memory")) + '">session: '
                + f"<code>{h(sess[:8])}</code></div>")
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


def supersede_steps(rec_id):
    """The supersede chain around a record, as steps: [..., [what it replaced], [rec_id], [what
    replaced it], ...]. Every memory in a step was replaced by one in the next.

    A step holds several ids where one memory replaced several: the memory kept from a group of
    three duplicates replaced the other two. Following only the first one found, as this did, left
    the second copy out of its own keeper's chain -- retired, and one click further away than the
    page said."""
    all_recs = mem.all_records()
    by_id = {r["id"]: r for r in all_recs}
    replaced = {}
    for r in sorted(all_recs, key=lambda r: (r["meta"].get("created", ""), r["id"])):
        nxt = r["meta"].get("superseded-by", "")
        if nxt:
            replaced.setdefault(nxt, []).append(r["id"])
    seen, back, step = {rec_id}, [], [rec_id]
    while step:
        before = []
        for cur in step:
            for prev in replaced.get(cur, []):
                if prev not in seen:     # a cyclic superseded-by edge must end the walk, not loop it
                    seen.add(prev)
                    before.append(prev)
        if before:
            back.append(before)
        step = before
    fwd, cur = [], rec_id
    while by_id.get(cur, {}).get("meta", {}).get("superseded-by"):
        cur = by_id[cur]["meta"]["superseded-by"]
        if cur not in by_id or cur in seen:
            break
        seen.add(cur)
        fwd.append([cur])
    return list(reversed(back)) + [[rec_id]] + fwd


def supersede_chain(rec_id):
    """The same chain as one ordered list: [...what this replaced, rec_id, ...what replaced it]."""
    return [i for step in supersede_steps(rec_id) for i in step]


def search_light(mode, q):
    """[state 'on'|'off', label, tooltip] for the search box — green when the local LLM is in play."""
    model = os.environ.get("MEM_EMBED_MODEL", "bge-m3")
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


def render_row(r, drift_why=None):
    m = r["meta"]
    st = m.get("status", "active")
    summ = rec_summary(r)
    typ = m.get("type", "?")
    scope = m.get("scope", "global")
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
    # The row carries only what the page scripts read off it: the id and the fields the sort and
    # the live filter use. The body used to ride along three times -- as the filter haystack, as
    # the edit modal's seed and as a hidden pre-rendered row -- which put 9.5 MB behind the
    # default view. It now comes from /record on demand (toggleBody, the edit action).
    return (
        f'<tr class="{"row-superseded" if st == "superseded" else ""}" data-id="{h(r["id"])}" data-status="{h(st)}" '
        f'data-type="{h(typ)}" data-scope="{h(scope)}" data-created="{h(m.get("created",""))}">'
        f'<td class="sel"><input type="checkbox" class="rowsel"></td>'
        f'<td>{type_badge(typ)}</td>'
        f'<td><a class="scope-tag" href="{scope_href}">{h(scope_label(scope))}</a></td>'
        f'<td class="summary" onclick="toggleBody(this)">{crit}<b>{h(summ)}</b>{sup}'
        # Why this row is in a filtered view. Listing memories without saying what put them there
        # leaves the reader with a problem and no way to judge it — the two drifted memories on the
        # author's own store needed opposite actions, and the page gave no way to tell them apart.
        + (f'<div class="drift-why">{h(drift_why)}</div>' if drift_why else "")
        + f'<div class="meta"><a href="/memories?id={h(r["id"])}">{h(r["id"])}</a> · conf {h(m.get("confidence","?"))} · {h(m.get("source",""))}</div></td>'
        f'<td class="meta" style="white-space:nowrap">{h(m.get("created",""))}</td>'
        f'<td style="text-align:right"><div class="actwrap">'
        f'<button type="button" class="act-toggle" onclick="toggleAct(this)">Actions ▾</button>'
        f'<div class="actmenu"><button type="button" class="act-edit">{t("Edit")}</button>'
        f'<button type="button" class="act-rescope">{t("Re-scope")}</button>'
        f'<button type="button" class="act-supersede">{t("Supersede")}</button>'
        f'<a href="/memories?id={h(r["id"])}">{t("View chain")}</a>'
        f'<button type="button" class="act-delete danger">{t("Delete")}</button></div></div></td></tr>')


def expanded_row_html(r, by_id, rel_in):
    """The content of a record's expanded row: body, extras (tier, anchors, session, invalidation)
    and relations. Served by /record and inserted by the page when a row is first opened."""
    return render_body(r["body"]) + rec_extras_html(r) + relations_block(r, rel_in, by_id)


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
        mem.ensure_store_repo()
        rc = subprocess.run(["git", "-C", root, "-c", f"safe.directory={root}",
                             *mem.git_identity("web"), "commit", "-m", msg, "--", "store"],
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


def self_url(**over):
    """The URL of the page being rendered, with some parameters overridden (None removes one).

    Exists so 'same page, one thing different' links stop dropping state. A bare `?lang=ro` keeps
    the path and replaces the entire query, which silently discards every filter the user set —
    the language switcher did that on every page, and on /project it discarded the ?slug too."""
    path = getattr(_ctx, "path", "/") or "/"
    p = {k: list(v) for k, v in (getattr(_ctx, "qs", None) or {}).items()}
    for k, v in over.items():
        if v is None:
            p.pop(k, None)
        else:
            p[k] = [str(v)]
    qs = urllib.parse.urlencode([(k, vv) for k, vs in p.items() for vv in vs])
    return path + ("?" + qs if qs else "")


def lang_switch():
    cur = lang()
    out = '<span class="lang-switch">'
    for l in ("en", "ro"):
        cls = ' class="on"' if l == cur else ""
        out += f'<a{cls} href="{h(self_url(lang=l))}">{l.upper()}</a>'
    return out + "</span>"


# The mark next to the name: an "m" on the accent, the shape every app in the family uses.
BRAND_MARK = ('<svg class="brand-mark" viewBox="0 0 26 26" aria-hidden="true"><rect width="26" height="26" rx="6" fill="#006fff"/>'
              '<path d="M7 18V9l3 4 3-4v9M17 9v9M17 9h2.5a2.5 2.5 0 0 1 0 5H17" fill="none" stroke="#fff" '
              'stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/></svg>')


def topbar(active, has_help=False):
    nwk = sum(1 for r in mem.all_records() if r["meta"].get("status") == "working")
    nav = [("dashboard", "/", t("Dashboard")), ("memories", "/memories", t("Memories")),
           ("projects", "/projects", t("Projects")), ("links", "/links", t("Links")),
           ("dups", "/duplicates", t("Duplicates")),
           ("git", "/git", t("Git history")), ("inject", "/inject", t("What Claude sees")),
           ("claudemd", "/claude-md", "CLAUDE.md"), ("about", "/about", t("About me")),
           ("settings", "/settings", t("Settings")),
           ("working", "/working", t("Working") + (f" ({nwk})" if nwk else ""))]
    nq = len(queue_pending())
    links = ""
    for k, href, label in nav:
        on = ' class="on"' if active == k else ""
        links += f'<a{on} href="{href}">{h(label)}</a>'
    review = f'<a class="review-tag" href="/queue">{nq} {t("to review")}</a>' if nq else ""
    # The page's guide opens on demand from here, so no page carries a column of explanations.
    helpb = (f'<button type="button" class="helpbtn" id="help-open" title="{h(t("Show help"))}" '
             f'aria-label="{h(t("Show help"))}">?</button>' if has_help else "")
    return (f'<header class="topbar"><a class="brand" href="/">{BRAND_MARK}mem0ry4ai '
            f'<small>{t("local memory")}</small></a><nav class="modnav">{links}</nav>'
            f'<div class="userbox">{review}{helpb}{lang_switch()}</div></header>')


_HELP_JS = ('<script>(function(){var m=document.getElementById("help-modal"),b=document.getElementById("help-open");'
            'if(!m||!b)return;function close(){m.classList.remove("open");}'
            'b.addEventListener("click",function(){m.classList.add("open");});'
            'm.querySelectorAll("[data-help-close]").forEach(function(x){x.addEventListener("click",close);});'
            'm.addEventListener("click",function(e){if(e.target===m)close();});'
            'document.addEventListener("keydown",function(e){if(e.key==="Escape"&&m.classList.contains("open"))close();});'
            '})();</script>')


def layout(title, active, content, aside="", scripts="", side=""):
    """`aside` is the page's guide: it opens from the ? in the topbar instead of taking a column.
    `side` is a real right-hand panel (actions, filters) that a page wants next to its content."""
    help_modal = ""
    if aside:
        help_modal = (f'<div class="modal-overlay" id="help-modal" aria-hidden="true"><div class="modal modal-help">'
                      f'<div class="modal-head"><h3>{t("Guide")}</h3>'
                      f'<button type="button" class="modal-close" data-help-close aria-label="close">&times;</button></div>'
                      f'<div class="modal-body">{aside}</div></div></div>{_HELP_JS}')
    main = (f'<main class="page">\n<div class="layout">\n<div class="content">\n{content}\n</div><!-- /content -->\n'
            f'{side}\n</div><!-- /layout -->\n</main>' if side else f'<main class="page">\n{content}\n</main>')
    return (f'<!doctype html>\n<html lang="{lang()}">\n<head>\n<meta charset="utf-8">\n'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            f'<title>{h(title)}</title>\n<link rel="icon" href="{FAVICON}">\n'
            f'<link rel="stylesheet" href="{h(asset("assets/style.css"))}">\n</head>\n<body>\n'
            f'{topbar(active, bool(aside))}\n{main}\n'
            f'<footer class="sitefoot">mem0ry4ai<span class="sep">&middot;</span>{t("local memory")}</footer>\n'
            f'{help_modal}{scripts}\n</body>\n</html>')


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
    if not allr:
        _ctx.status = 404      # a wrong URL should say so, not render a normal-looking empty page
        # A project that has never held a memory is a wrong URL, not an empty one. Rendering the
        # normal page with a zero count invites the reading "this project exists and I lost
        # everything in it" — a typo should look like a typo.
        known = sorted(s.split(":", 1)[1] for s in known_scopes() if s.startswith("project:"))
        near = [k for k in known if k.startswith(slug[:3])][:5] if len(slug) >= 3 else []
        hint = (("<p>" + t("Did you mean") + ": "
                 + ", ".join(f'<a href="/project?slug={h(k)}">{h(k)}</a>' for k in near) + "?</p>")
                if near else "")
        return layout(
            f"{slug} — mem0ry4ai", "projects",
            f'  <div class="crumb"><a href="/">{t("Dashboard")}</a> / '
            f'<a href="/projects">{t("Projects")}</a> / {h(slug)}</div>\n'
            f'  <h2>{h(slug)}</h2>\n'
            f'  <div class="empty"><b>{t("No such project")}</b><p>'
            + t("No memory has ever been written for this scope. If you expected memories here, "
                "check the spelling — a project only appears once something is saved to it.")
            + f'</p>{hint}<p><a href="/projects">{t("All projects")}</a></p></div>')
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
    output, err, parts = None, None, []
    try:
        parts = [(name, text) for name, text in run_injection(cwd, "preview") if text.strip()]
        output = "\n".join(text for _, text in parts)
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
        ibud = _int_env("MEM_INJECT_BUDGET", "8000")
        exact = ('Acesta e output-ul exact al hook-ului real (<code>hooks/session_start.py</code>), nu o aproximare.'
                 if lang() == "ro" else
                 'This is the exact output of the real hook (<code>hooks/session_start.py</code>), not an approximation.')
        bodyblock = (f'<p class="inject-meta">{t("Injection for")} <b>{h(label)}</b>: <b>{nbytes:,} {t("bytes")}</b> '
                     f'≈ ~{tokens:,} {t("tokens (approx.)")} · {t("budget")} {ibud:,} bytes (MEM_INJECT_BUDGET) — '
                     f'{t("critical rules are always in; the rest is trimmed deterministically.")} {exact}</p>')
        if len(parts) > 1:
            cap = _hook_consts()[1]
            # Taken out of the f-string: before Python 3.12 an f-string expression cannot hold a
            # backslash, and the apostrophe would need one.
            caps_note = t("Claude Code caps each hook's output separately, so each part has its own limit.")
            bodyblock += f'<p class="inject-meta">{caps_note}</p>'
            for name, text in parts:
                sz = len(text.encode("utf-8"))
                bodyblock += (f'<p class="inject-meta"><b>{h(t(name))}</b>: {sz:,} {t("bytes")} / '
                              f'{t("limit")} {cap:,}</p><pre class="inject">{h(text)}</pre>')
        else:
            bodyblock += f'<pre class="inject">{h(output)}</pre>'
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
    style = ""
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
                + (f' · id <a href="/memories?id={h(rec["id"])}"><code>{h(rec["id"])}</code></a>'
                   if rec else f' — {t("created on first save")}'))
    style = ""
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


# ---------- power-user settings (config knobs editable from the UI) ----------
SETTINGS_GROUPS = [
    ("injection", "SessionStart injection",
     "The memory block injected into every session. Changes apply at the NEXT session start "
     "(the hook re-reads these each time)."),
    ("search", "Search & ranking",
     "Hybrid keyword+semantic retrieval and the duplicate guard, used by the CLI and the MCP server."),
    ("web", "Web UI", "This dashboard."),
    ("ollama", "Embeddings & local LLM (Ollama)",
     "Optional semantic features. These bind when a module loads, so they apply after a server restart."),
    ("safety", "Safety", "Security-relevant toggles — change with care."),
    ("maintenance", "Scheduled maintenance",
     "The local launchd hygiene job (mem_maintenance.py). These apply on its NEXT run; whether the job "
     "is installed, its interval, and status are managed from the CLI (mem_maintenance.py install/status)."),
]


def _S(key, group, vtype, default, label, hlp, **kw):
    return {"key": key, "group": group, "vtype": vtype, "default": default, "label": label, "help": hlp,
            "min": kw.get("min"), "max": kw.get("max"), "step": kw.get("step"),
            "choices": kw.get("choices"), "advanced": kw.get("advanced", False),
            "warn": kw.get("warn"), "apply": kw.get("apply", "live")}


SETTINGS = [
    _S("MEM_INJECT_BUDGET", "injection", "int", "8000", "Injection byte budget",
       "Target UTF-8 size of the memory block injected at the top of every session. Your critical rules "
       "and profile are always injected in full and never trimmed, but they DO count toward this budget — "
       "they eat into the room left for everything else, and if they alone are large the total can exceed "
       "it. The budget bounds the trimmable sections, which cut deterministically and announce what they "
       "drop. Raise it to keep more memories in context up front (more tokens every session); lower it for "
       "a leaner start. Default 8000.", min=2000, max=32000, step=500, apply="next session"),
    _S("MEM_INJECT_BODY_THRESHOLD", "injection", "int", "12", "Full-body threshold",
       "Progressive disclosure: when the number of active in-scope memories is at or below this, each is "
       "injected with its full body; above it, only one-line summaries go in (with a hint to search for "
       "the rest). Raise it to keep full detail in larger projects (more tokens); lower it to fall back to "
       "summaries sooner. Default 12.", min=3, max=60, apply="next session"),
    _S("MEM_INJECT_ROOT_MAX_PER_PROJECT", "injection", "int", "6", "Root: max items per project",
       "Only in monorepo-root mode (when you start a session from the repo root). Caps how many memory "
       "summaries each recently-touched project shows before the rest collapse to a '+N more' line. Higher "
       "= more depth per project but fewer projects fit the budget; lower = more breadth across projects. "
       "Default 6.", min=1, max=30, apply="next session"),
    _S("MEM_INJECT_ROOT_RECENT_DAYS", "injection", "int", "30", "Root: recency window (days)",
       "Only in monorepo-root mode. A project whose newest memory is older than this many days is shown as "
       "a single '(N memories, not touched recently)' line instead of a full list, so the budget goes to "
       "active work. A larger window shows detail for more projects; a smaller one collapses stale ones "
       "faster. Default 30.", min=1, max=365, apply="next session"),
    _S("MEM_INJECT_GLOBAL_SPLIT", "injection", "float", "0.4", "Root: global budget share",
       "Only in monorepo-root mode. Of the budget left after the always-injected sections (profile + "
       "critical rules), this fraction goes to the Global section (cross-project facts and preferences) "
       "and the rest to the per-project index. Raise it to favour global knowledge, lower it to favour the "
       "'where was I in each project' index. Default 0.4 (40%).",
       min=0.1, max=0.8, step=0.05, apply="next session"),
    _S("MEM_INJECT_RECORD_CAP", "injection", "int", "1000", "Per-record body cap",
       "Maximum characters of any single memory's body injected at session start; a longer body is "
       "truncated with a marker pointing to search/get for the full text. Stops one huge memory from "
       "eating the whole injection budget. Applies to critical rules and full-body memories. Default 1000.",
       min=200, max=8000, step=100, apply="next session"),
    _S("MEM_INJECT_STATUS_DAYS", "injection", "int", "60", "Status age limit (days)",
       "A status nobody has touched for this many days is no longer listed at session start, so the "
       "line goes to something current. It is not retired: it stays active and searchable, "
       "`mem.py stale` lists it (`--keep <id>` says it still stands), and a project's latest still "
       "comes with that project's brief. The status of the project a session starts in is always "
       "listed. 0 turns this off. "
       "Default 60.", min=0, max=3650, apply="next session"),
    _S("MEM_INJECT_TITLE", "injection", "int", "120", "Title width at session start",
       "Longer summaries are cut to this many characters in the session-start list, with an ellipsis; "
       "the id at the start of the line opens the whole memory. The store is never changed by this. "
       "0 lists every summary whole. Default 120.", min=0, max=1000, step=10, apply="next session"),
    _S("MEM_RECALL_NOTICE", "injection", "bool", "1", "Show what was injected",
       "After an injection, show YOU one line about it: how many memories went into context at session "
       "start, and — when a prompt pulls memories in — which ones, with their project and a shortened "
       "title. Everything else the hooks write goes to the model only, so this is how a wrong recall "
       "gets noticed. Default on.", apply="next session"),
    _S("MEM_BRIEF", "injection", "bool", "1", "Project brief on first touch",
       "The first time a session works in a project — a tool touches a path inside its folder, or a "
       "prompt names the project in full — add that project's brief to the context: its latest status, "
       "the todos nothing blocks, its preferences, and the knowledge that has been opened most. Once "
       "per session and project, again after a compaction, and never what the session was already "
       "shown. Needs the PostToolUse hook (hooks/install.py). Default on."),
    _S("MEM_FILE_RECALL", "injection", "bool", "1", "Memories anchored to a touched file",
       "The first time a session reads or edits a file, add the memories whose `files:` anchor names "
       "that file: the newest few, and a pointer to the rest. Once per session and file. Needs the "
       "PostToolUse hook. Default on."),
    _S("MEM_DIGEST", "injection", "bool", "1", "Digests at their moment",
       "A digest is a page of preferences distilled by theme, which you approved by promoting it. "
       "Each is given when its `when:` line says: at every session start, or the first time a "
       "session touches a file or runs a command of the kind it names. Off stops the ones that wait "
       "for a tool call; the ones for session start have their own hook. `mem.py digest` lists them "
       "and says which have sources that changed. Default on."),
    _S("MEM_SCOPE_ALIASES", "injection", "string", "", "Folder aliases",
       "For a project folder whose memories are filed under another project's name: comma-separated "
       "folder=slug pairs, e.g. `client-app=old-product, fork=upstream`. Touching the folder then "
       "brings the named project's brief and anchored memories. Empty by default.", advanced=True),

    _S("MEM_RECENCY_WEIGHT", "search", "float", "1.5", "Recency weight",
       "In keyword (bm25) search, a recency bonus is folded into each hit's score so newer memories edge "
       "out older ones on near-ties. 0 = rank purely by text relevance; higher = let recency tip the "
       "balance more aggressively. Default 1.5.", min=0, max=10, step=0.1),
    _S("MEM_RRF_K", "search", "float", "60", "RRF k",
       "The constant in Reciprocal Rank Fusion, which merges the keyword ranking and the semantic ranking "
       "into one list. A small k lets the top hit from each method dominate the blend; a large k flattens "
       "it so lower-ranked hits also count. Only matters when semantic vectors exist. Default 60.",
       min=1, max=200, step=1, advanced=True),
    _S("MEM_DUP_CHECK", "search", "bool", "1", "Duplicate guard on add",
       "When you add a memory, warn if a very similar one already exists — so you supersede it instead of "
       "piling up duplicates. Turn off to silence those warnings entirely. Default on.", advanced=True),
    _S("MEM_DUP_THRESHOLD", "search", "float", "0.62", "Duplicate: semantic threshold",
       "Cosine-similarity cutoff for the duplicate warning: two memories count as near-duplicates above "
       "this. Raise it so only near-identical memories are flagged (fewer warnings); lower it to catch "
       "looser overlaps. Used only when semantic vectors are available. Default 0.62.",
       min=0, max=1, step=0.01, advanced=True),
    _S("MEM_DUP_JACCARD", "search", "float", "0.5", "Duplicate: keyword fallback",
       "Fallback duplicate cutoff when no embeddings exist: the share of shared words (Jaccard) above "
       "which two memories are flagged. Same direction as the semantic threshold — higher = stricter, "
       "lower = more aggressive. Default 0.5.", min=0, max=1, step=0.01, advanced=True),
    _S("MEM_SEARCH_LOG", "search", "bool", "1", "Log what agents search for",
       "Keep one row per search an agent runs (CLI or MCP): the query, redacted and cut to 200 "
       "characters, and how much came back. It is what shows where the store is asked for something "
       "it does not hold. Local only, in the access database; your prompts are never stored. "
       "Default on."),

    _S("MEM_UI_LANG", "web", "enum", "en", "Default UI language",
       "The default language for this dashboard when a request has no ?lang query and no saved cookie. The "
       "EN/RO switch in the top bar overrides it for your session and is remembered in a cookie. "
       "Default en.", choices=["en", "ro"]),
    _S("MEM_SUGGEST_THRESHOLD", "web", "float", "0.62", "Link suggestion threshold",
       "Minimum semantic similarity for a pair to appear under 'suggested links' on the Links page. Raise "
       "it for fewer, tighter suggestions; lower it for more candidates (and more noise). Same control as "
       "the slider on the Links page. Default 0.62.", min=0.5, max=0.95, step=0.01),
    _S("MEM_DUPS_THRESHOLD", "web", "float", "0.80", "Duplicates threshold",
       "How alike two active memories of the same type must be to be offered as a group on the Duplicates "
       "page and by 'mem.py dups'. Raise it to be shown only near-identical texts; lower it for more groups "
       "to look through, more of which will turn out not to be duplicates. Default 0.80.",
       min=0.6, max=0.99, step=0.01),
    _S("MEM_WEB_PORT", "web", "int", "8841", "Web UI port",
       "TCP port this web dashboard listens on. Change it only if 8841 clashes with another service; "
       "you'll need to restart the server and reopen it on the new port. Default 8841.",
       min=1024, max=65535, advanced=True, apply="restart web server"),
    _S("MEM_NO_RELOAD", "web", "bool", "0", "Disable auto-reload",
       "Normally the server watches its own source files and restarts itself when they change (e.g. after "
       "a git-pull). Enable this to disable that auto-restart — useful if the re-exec interrupts "
       "something. Default off (auto-reload on).", advanced=True, apply="restart web server"),

    _S("OLLAMA_URL", "ollama", "string", "http://localhost:11434", "Ollama URL",
       "Base URL of the local Ollama server used for semantic embeddings and the optional offline "
       "extraction LLM. Leave the default unless Ollama runs on another host or port. Bound when the "
       "process starts, so a change needs a server restart. Default http://localhost:11434.",
       advanced=True, apply="restart web server"),
    _S("MEM_LLM_MODEL", "ollama", "string", "qwen2.5:7b-instruct", "LLM model",
       "The Ollama chat model for the optional offline memory-extraction fallback (unused when extraction "
       "runs through Claude directly). Must be a model you have pulled in Ollama. Applies after a restart. "
       "Default qwen2.5:7b-instruct.", advanced=True, apply="restart web server"),
    _S("MEM_EMBED_MODEL", "ollama", "string", "bge-m3", "Embedding model",
       "The Ollama model that turns memories into vectors for semantic search and link suggestions. "
       "Changing it invalidates the existing vectors, so re-run `mem.py embed --force` afterwards — plain "
       "`mem.py embed` is incremental (keyed on a record's text, not the model) and skips unchanged "
       "records, silently keeping the old-model vectors. Applies after a restart. Default bge-m3.",
       advanced=True, apply="restart web server"),
    _S("MEM_EMBED_MAXCHARS", "ollama", "int", "1200", "Embedding max chars",
       "How many characters of a memory are sent to the embedder. Small embedding models have a short "
       "context and reject long inputs, so the embedder also retries with shorter slices; this caps the "
       "first attempt. Raise it only for a larger embedding model. Default 1200.",
       min=200, max=8000, step=100, advanced=True),

    _S("MEM_REDACT", "safety", "bool", "1", "Redact secrets on write",
       "On every write, replace secret-looking values (API keys, tokens, passwords) with a "
       "[REDACTED:LABEL] marker, so credentials never reach the markdown store or its git history. Keep "
       "this on.", advanced=True,
       warn="Turning this OFF lets API keys, tokens and passwords land in the markdown store and its "
            "git history."),
    _S("MEM_MCP_WRITE", "safety", "bool", "1", "Allow MCP memory_add",
       "Allow MCP clients (Claude Code, Gemini, ...) to create memories via the memory_add tool. Turn it "
       "off to make a client read-only. Applies to the next MCP session.", advanced=True,
       apply="next MCP session",
       warn="With several agents writing to one store, leave this OFF on the extra agents to avoid "
            "duplicate records."),

    _S("MEM_MAINT_WORKING_DAYS", "maintenance", "int", "30", "Stale working-note age (days)",
       "The scheduled maintenance job treats a working (scratch) note older than this many days as stale — "
       "it reports them, and prunes them only if the toggle below is on. Default 30.",
       min=1, max=365, apply="next maintenance run"),
    _S("MEM_MAINT_PRUNE_WORKING", "maintenance", "bool", "0", "Prune stale working notes",
       "When on, the maintenance job DELETES stale working notes (older than the age above) instead of only "
       "reporting them. Deletions are logged and git-reversible, but the job acts unattended. Default off "
       "(report only).", apply="next maintenance run",
       warn="When on, the scheduled job removes old scratch notes on its own — git keeps the history, but "
            "nothing prompts you first."),
]
_FALSEY = ("0", "false", "no", "off", "")


def validate_setting(s, raw):
    """Return (value_str, error): validate & clamp a raw form value for setting `s`."""
    raw = (raw or "").strip()
    vt = s["vtype"]
    if vt == "bool":
        return ("1" if raw.lower() not in _FALSEY else "0"), None
    if vt == "enum":
        return (raw, None) if raw in (s["choices"] or []) else (None, "invalid choice")
    if vt == "string":
        # reject control chars: a newline would inject a second KEY=VALUE line into .mem-local.env
        # (env-var smuggling); other C0 chars / DEL have no business in a URL or model name.
        if any(ord(c) < 0x20 or ord(c) == 0x7f for c in raw):
            return None, "control characters not allowed"
        if len(raw) > 500:
            return None, "too long"
        return raw, None
    try:
        n = int(raw) if vt == "int" else float(raw)
    except ValueError:
        return None, "not a number"
    if vt == "float" and not math.isfinite(n):   # nan/inf would poison ranking/dedup silently
        return None, "not a finite number"
    if s["min"] is not None:
        n = max(n, s["min"])
    if s["max"] is not None:
        n = min(n, s["max"])
    return (str(int(n)) if vt == "int" else f"{float(n):g}"), None


def _local_env_keys():
    keys = set()
    try:
        with open(mem.LOCAL_ENV, encoding="utf-8") as f:
            for line in f:
                t_ = line.strip()
                base = t_[7:].strip() if t_.startswith("export ") else t_
                if base and not base.startswith("#") and "=" in base:
                    keys.add(base.partition("=")[0].strip())
    except OSError:
        pass
    return keys


def render_setting_row(s, cur, overridden):
    key, vt = s["key"], s["vtype"]
    if vt == "bool":
        chk = "checked" if str(cur).lower() not in _FALSEY else ""
        ctrl = f'<label class="auto-snap"><input type="checkbox" data-key="{key}" data-type="bool" {chk}></label>'
    elif vt == "enum":
        opts = "".join(f'<option value="{h(c)}"{" selected" if str(cur) == c else ""}>{h(c)}</option>'
                       for c in (s["choices"] or []))
        ctrl = f'<select data-key="{key}" data-type="enum">{opts}</select>'
    elif vt == "string":
        ctrl = f'<input type="text" data-key="{key}" data-type="string" value="{h(str(cur))}" size="32">'
    else:
        step = s["step"] if s["step"] is not None else ("1" if vt == "int" else "0.01")
        mn = f' min="{s["min"]}"' if s["min"] is not None else ""
        mx = f' max="{s["max"]}"' if s["max"] is not None else ""
        ctrl = (f'<input type="number" data-key="{key}" data-type="{vt}" value="{h(str(cur))}" '
                f'step="{step}"{mn}{mx}>')
    badges = ""
    if s["advanced"]:
        badges += f'<span class="st-badge st-adv">{t("advanced")}</span>'
    if s["apply"] != "live":
        badges += f'<span class="st-badge st-apply">{t("applies on")}: {h(t(s["apply"]))}</span>'
    if overridden:
        badges += f'<span class="st-badge st-over">{t("overridden")}</span>'
    warn = f'<div class="st-warn">{h(t(s["warn"]))}</div>' if s["warn"] else ""
    return (f'<div class="st-row"><div class="st-main"><label class="st-label">{h(t(s["label"]))} '
            f'<code>{key}</code></label>{badges}<div class="st-help">{h(t(s["help"]))}</div>{warn}</div>'
            f'<div class="st-ctrl">{ctrl}</div></div>')


_SETTINGS_JS = r'''
(function(){
  var save = document.getElementById('st-save'), reset = document.getElementById('st-reset'),
      st = document.getElementById('st-status');
  function collect(){
    var fd = new URLSearchParams(); fd.set('csrf', CSRF); fd.set('action','save');
    document.querySelectorAll('[data-key]').forEach(function(el){
      var v = (el.getAttribute('data-type')==='bool') ? (el.checked ? '1':'0') : el.value;
      fd.set(el.getAttribute('data-key'), v);
    });
    return fd;
  }
  if (save) save.addEventListener('click', function(){
    save.disabled = true; st.textContent = '';
    fetch('/settings',{method:'POST',headers:{'X-Requested-With':'XMLHttpRequest'},body:collect()})
      .then(function(r){return r.json();}).then(function(j){
        save.disabled = false;
        if(!j.ok){ if(j.error==='CSRF'){ alert(TXT.csrf); location.reload(); return; }
                   st.textContent = j.error || TXT.failed; st.className = 'cmd-status err'; return; }
        if(j.values){ Object.keys(j.values).forEach(function(k){   // reflect clamps back into the field
          var el = document.querySelector('[data-key="'+k+'"]'); if(!el) return;
          if(el.getAttribute('data-type')==='bool') el.checked = (j.values[k] !== '0');
          else el.value = j.values[k];
        }); }
        st.textContent = TXT.saved + (j.note ? (' — ' + j.note) : '');
        st.className = 'cmd-status ok';
      })
      .catch(function(){ save.disabled=false; st.textContent=TXT.network; st.className='cmd-status err'; });
  });
  if (reset) reset.addEventListener('click', function(){
    if(!confirm(TXT.resetConfirm)) return;
    var fd = new URLSearchParams(); fd.set('csrf', CSRF); fd.set('action','reset');
    fetch('/settings',{method:'POST',headers:{'X-Requested-With':'XMLHttpRequest'},body:fd})
      .then(function(r){return r.json();}).then(function(j){
        if(j.ok){ location.reload(); }
        else { st.textContent = j.error || TXT.failed; st.className = 'cmd-status err'; }
      });
  });
})();
'''


def page_settings(qs=None):
    over = _local_env_keys()
    groups = []
    for gkey, gtitle, gdesc in SETTINGS_GROUPS:
        rows = "".join(render_setting_row(s, os.environ.get(s["key"], s["default"]), s["key"] in over)
                       for s in SETTINGS if s["group"] == gkey)
        groups.append(f'<div class="card"><h3>{h(t(gtitle))}</h3>'
                      f'<p class="foot">{h(t(gdesc))}</p>{rows}</div>')
    intro = t("Tune the knobs that control injection, search and the UI. Saved to .mem-local.env "
              "(gitignored) and honored by the hook, the CLI and the MCP server. On load, a value "
              "exported in your shell takes precedence over the file.")
    style = ""
    actions = (f'<div class="cmd-actions">'
               f'<button type="button" class="btn btn-primary" id="st-save">{h(t("Save settings"))}</button>'
               f'<button type="button" class="btn" id="st-reset">{h(t("Reset all to defaults"))}</button>'
               f'<span class="cmd-status" id="st-status"></span></div>')
    content = (f'{style}'
               f'  <div class="crumb"><a href="/">{t("Dashboard")}</a> / {t("Settings")}</div>\n'
               f'  <h2>{t("Power-user settings")}</h2>\n'
               f'  <p class="foot">{h(intro)}</p>\n'
               + "".join(groups) + actions)
    txt = {"failed": t("Operation failed"), "network": t("Network error"),
           "csrf": t("Session expired (the server restarted) — reload the page and try again."),
           "saved": t("Saved"),
           "resetConfirm": t("Reset every setting to its default? This clears your overrides in "
                             ".mem-local.env.")}
    js = ("<script>var CSRF = " + json.dumps(_CSRF) + "; var TXT = "
          + json.dumps(txt, ensure_ascii=False) + ";" + _SETTINGS_JS + "</script>")
    return layout(t("Settings") + " — mem0ry4ai", "settings", content, "", js)


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
  var sl = document.getElementById('suggest-list');
  if (sl) sl.addEventListener('click', function(e){
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
  });
  var thBtn = document.getElementById('sg-th-apply');
  if (thBtn) thBtn.addEventListener('click', function(){
    var inp = document.getElementById('sg-th-input');
    thBtn.disabled = true;
    var fd = new URLSearchParams(); fd.set('csrf', CSRF); fd.set('action', 'threshold'); fd.set('value', inp ? inp.value : '');
    fetch('/links', { method:'POST', headers:{'X-Requested-With':'XMLHttpRequest'}, body: fd })
      .then(function(r){ return r.json(); })
      .then(function(j){ if (j.ok) { location.reload(); } else { thBtn.disabled = false; alert(j.error || 'failed'); } })
      .catch(function(){ thBtn.disabled = false; });
  });'''


# memories-page JS — verbatim vanilla JS; CSRF/STATUS_FILTER/TXT are prepended by the page.
_MEM_JS_BODY = r'''function bodyRowFor(tr){ return document.querySelector('tr[data-bodyfor="'+tr.dataset.id+'"]'); }
function fetchRecord(id){
  return fetch('/record?id=' + encodeURIComponent(id)).then(function(r){ return r.json(); })
    .then(function(j){ if (!j.ok) throw new Error(j.error || TXT.failed); return j; });
}
function toggleBody(cell){
  var tr = cell.closest('tr'); var r = bodyRowFor(tr);
  if (r) { r.style.display = (r.style.display === 'none') ? '' : 'none'; return; }
  // First open: the body is not in the page (2,000 bodies shipped three times made the default
  // view 9.5 MB), so it is fetched once and kept in the DOM from then on.
  r = document.createElement('tr'); r.className = 'bodyrow'; r.setAttribute('data-bodyfor', tr.dataset.id);
  var td = document.createElement('td'); td.colSpan = 6; td.textContent = TXT.loading; r.appendChild(td);
  tr.parentNode.insertBefore(r, tr.nextSibling);
  fetchRecord(tr.dataset.id).then(function(j){ td.innerHTML = j.html; })
    .catch(function(e){ td.textContent = e.message || TXT.failed; });
}
function rowHay(tr){
  // What is on screen -- summary, id, source, scope, type -- not the body. Full text is the form
  // submit (FTS); this box narrows the rows already on this page.
  if (tr._hay === undefined) {
    var s = tr.querySelector('td.summary'), sc = tr.querySelector('.scope-tag');
    tr._hay = ((s ? s.textContent : '') + ' ' + (sc ? sc.textContent : '') + ' ' + (tr.dataset.scope || '')
      + ' ' + (tr.dataset.type || '')).replace(/\s+/g, ' ').toLowerCase();
  }
  return tr._hay;
}
function rowSummary(tr){ var b = tr.querySelector('td.summary b'); return b ? b.textContent : ''; }
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
  document.querySelectorAll('table.mem tbody tr[data-id]').forEach(function(tr){
    var show = rowHay(tr).indexOf(q) !== -1;
    tr.style.display = show ? '' : 'none';
    var b = bodyRowFor(tr); if (b && !show) b.style.display='none';
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
      tb.querySelectorAll('tr[data-id]').forEach(function(tr){ pairs.push([tr, bodyRowFor(tr)]); });
      pairs.sort(function(a, b){
        var va = key === 'summary' ? rowSummary(a[0]) : (a[0].dataset[key] || '');
        var vb = key === 'summary' ? rowSummary(b[0]) : (b[0].dataset[key] || '');
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
    fetchRecord(tr.dataset.id).then(function(j){
      openModal('edit', { id: j.id, type: j.type, scope: j.scope, confidence: j.confidence, summary: j.summary, body: j.body });
    }).catch(function(e){ alert(e.message || TXT.failed); });
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

    # suggestions + threshold control (shown whenever the semantic layer has vectors)
    nvec = embed_count()
    if nvec >= 2:
        sg_html = ""
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
        cur_th = (os.environ.get("MEM_SUGGEST_THRESHOLD") or "").strip() or "0.62"
        ctl = (f'<div class="sg-th">'
               f'<label>{t("Threshold")} <input type="number" id="sg-th-input" min="0.5" max="0.95" step="0.01" '
               f'value="{h(cur_th)}"></label>'
               f'<button type="button" class="btn btn-primary" id="sg-th-apply">{t("Apply")}</button>'
               f'<small>{t("higher = fewer, stronger matches")}</small></div>')
        body = (f'<ul id="suggest-list">{sg_html}</ul>' if suggestions
                else f'<ul id="suggest-list"></ul><div class="empty">{t("No matches at this threshold — lower it to see more.")}</div>')
        parts.append(f'  <div class="suggest"><h3>{t("Suggested links")} <span class="count">{len(suggestions)}</span>'
                     f' <small>{smallnote}</small></h3>{ctl}{body}</div>')

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
    if nvec >= 2:
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
    fdrift = (qs.get("drift", [""])[0] or "").strip() == "1"
    ffiles = (qs.get("files", [""])[0] or "").strip()
    records = mem.all_records()
    drift_ids, drift_why = set(), {}
    if fdrift:
        # Computed here, not cached: the answer depends on the working tree right now, and a stale
        # "this memory is fine" is worse than a slow page.
        try:
            for r, findings in drift_findings([x for x in records
                                                if x["meta"].get("status", "active") == "active"
                                                and (x["meta"].get("files") or "").strip()]):
                drift_ids.add(r["id"])
                drift_why[r["id"]] = "; ".join(f"{rel}: {verdict} ({detail})" for rel, verdict, detail in findings)
        except Exception:
            pass
    chain_ids, chain_steps = [], []
    if fid:
        chain_steps = supersede_steps(fid)
        chain_ids = [i for step in chain_steps for i in step]
        fstat = "all"
    # Typing (or pasting) a record id into the search box means "take me to that record", not
    # "rank the store against this string" — which is what a hybrid search does with an id, and it
    # returns dozens of unrelated rows because the id itself matches nothing. Ids are recognisable
    # (YYYYMMDD-xxxxxx), so treat one as the id filter; anything not an existing id still searches.
    if q and not fid and _ID_RE.match(q.strip().lstrip("@")):
        maybe = q.strip().lstrip("@")
        if mem.get_record(maybe):
            fid, q = maybe, ""
            chain_steps = supersede_steps(fid)
            chain_ids = [i for step in chain_steps for i in step]
            fstat = "all"
    fts_ids, mode = None, ""
    if q and not fid:
        fts_ids, mode = mem.hybrid_search(q)   # ids None -> substring fallback

    def keep(r):
        m = r["meta"]
        if fid:
            return r["id"] in chain_ids
        if fdrift and r["id"] not in drift_ids:
            return False
        if ffiles and not mem._match_anchor(r, ffiles):
            return False
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
    # Pages of PAGE_ROWS, grouped or not: a 1,700-row list is a page nobody reads top to bottom.
    # The group order is applied before the cut so a page boundary never reorders anything --
    # global first, then projects by name, newest first inside each (the stable sort keeps the
    # created-desc order within a scope).
    if grouped:
        rows.sort(key=lambda r: (0, "") if r["meta"].get("scope", "global") == "global"
                  else (1, r["meta"].get("scope", "")))
    total_rows = len(rows)
    try:
        offset = max(0, int(qs.get("offset", ["0"])[0] or 0))
    except ValueError:
        offset = 0
    if offset >= total_rows:   # past the end (a stale link after deletions): the last page, not an empty one
        offset = max(0, (total_rows - 1) // PAGE_ROWS * PAGE_ROWS)
    page_rows = rows[offset:offset + PAGE_ROWS]
    stats = store_stats()
    scopes = known_scopes()

    def mkqs(over):
        # Every filter in effect must survive a click on any other filter, or the page silently
        # drops state the user set on purpose — `drift` was doing exactly that, so arriving from
        # the dashboard and then touching anything sent you back to square one. The carry set is
        # read off the query string that actually arrived instead of a hardcoded list of filters:
        # a hardcoded list is what caused that bug, and it grows a fresh hole every time a filter
        # is added. Only the three keys the page rewrites are restated -- `status` because its
        # default is not the empty string, `q`/`id` because pasting an id into the search box moves
        # the value from one to the other.
        p = {k: v[0] for k, v in qs.items() if v and v[0] != ""}
        # The page cut is not a filter. A different filter starts over from the first page, or
        # "gotcha" clicked from page 4 of everything lands on an empty page 4 of gotchas.
        p.pop("offset", None)
        p.update({"q": q, "status": fstat})
        if fid:
            p["id"] = fid
        p.update(over)
        p = {k: v for k, v in p.items() if v != ""}
        return "/memories?" + urllib.parse.urlencode(p)

    # The form posts, so anything carried in the URL that is not one of its named controls has to
    # ride along as a hidden input or submitting the form drops it -- same failure as above, other
    # mechanism.
    _named = ("q", "scope")
    hidden = "".join(f'<input type="hidden" name="{h(k)}" value="{h(v)}">'
                     for k, v in sorted(urllib.parse.parse_qsl(mkqs({}).split("?", 1)[1]))
                     if k not in _named)

    # ----- header + chain + toolbar -----
    parts = [f'  <div class="crumb"><a href="/">{t("Dashboard")}</a> / {t("Memories")}</div>',
             f'  <h2>{t("Memories")} <span class="count">{len(rows)} {t("of")} {stats["total"]}</span></h2>']
    if fid and len(chain_ids) > 1:
        # an arrow between steps, a comma between the memories one step holds together
        chain = '<span class="arrow">→</span>'.join(
            ", ".join(f'<a href="/memories?id={h(cid)}">{h(cid)}</a>' for cid in step) for step in chain_steps)
        parts.append(f'  <div class="chain"><b>{t("Supersede chain:")}</b> {chain} '
                     f'<a class="btn btn-ghost" style="margin-left:12px" href="/memories">{t("close")}</a></div>')
    elif fid:
        parts.append(f'  <div class="chain"><code>{h(fid)}</code> {t("(no chain)")}. <a href="/memories">{t("back")}</a></div>')
    parts.append('  <datalist id="scopes">' + "".join(f'<option value="{h(s)}">' for s in scopes) + "</datalist>")

    scope_opts = '<option value="">' + t("all scopes") + "</option>"
    for s in scopes:
        sel = " selected" if fscope == s else ""
        scope_opts += f'<option{sel} value="{h(s)}">{h(scope_label(s))}</option>'
    lcls, llbl, ltip = search_light(mode, q)
    pills = ""
    for st_key in ("active", "superseded", "all"):
        cls = "on" if fstat == st_key else ""
        pills += f'<a class="{cls}" href="{h(mkqs({"status": st_key}))}"><span class="n">{t(st_key)}</span></a>'
    # The drift view was reachable only from the dashboard, so once you were here you could not get
    # back to it. Shown only when there is something to show — a pill that always reads 0 is furniture.
    if fdrift:
        pills += f'<a class="on warn" href="{h(mkqs({"drift": ""}))}"><span class="n">{t("drift")}</span></a>'
    else:
        try:
            n_drift = len(drift_findings([r for r in records
                                           if r["meta"].get("status", "active") == "active"
                                           and (r["meta"].get("files") or "").strip()]))
        except Exception:
            n_drift = 0
        if n_drift:
            pills += (f'<a class="warn" href="{h(mkqs({"drift": "1"}))}"><span class="n">{t("drift")}</span>'
                      f'<span class="c">{n_drift}</span></a>')
    parts.append(
        '  <div class="toolbar"><form class="filters" method="get">'
        f'<input type="search" name="q" value="{h(q)}" '
        f'placeholder="{h(t("search — Enter = full text, typing filters this page"))}" id="live">'
        f'<select name="scope" onchange="this.form.submit()">{scope_opts}</select>'
        + hidden
        + f'<button class="btn" type="submit">{t("Search")}</button>'
        f'<span class="search-light {lcls}" title="{h(ltip)}">{h(llbl)}</span></form></div>')
    # The filters live in the panel on the right, as links that carry the rest of the state
    # (mkqs), so a click never silently drops the search or the drift view.
    side_types = (f'<a href="{h(mkqs({"type": ""}))}" class="{"on" if not ftype else ""}">'
                  f'<span class="n">{t("all types")}</span><span class="c">{stats["active"]}</span></a>')
    for ty in mem.TYPES:
        if ty == "profile":
            continue
        side_types += (f'<a href="{h(mkqs({"type": ty}))}" class="{"on" if ftype == ty else ""}">'
                       f'<span class="n">{h(ty)}</span><span class="c">{stats["by_type"].get(ty, 0)}</span></a>')
    side = (f'<aside class="side"><div class="side-head"><div class="side-title">{t("Actions")}</div></div>'
            f'<div class="side-sec"><button type="button" class="btn btn-primary" id="open-add">{t("+ Add memory")}</button></div>'
            f'<div class="side-head"><div class="side-title">{t("Status")}</div></div>'
            f'<div class="side-sec sidelinks">{pills}</div>'
            f'<div class="side-head"><div class="side-title">{t("Types")}</div></div>'
            f'<div class="side-sec sidelinks">{side_types}</div></aside>')

    # An active filter that is invisible is a trap: you cannot tell why rows are missing, and you
    # cannot turn it off without knowing the URL. Say it is on, say what it means, offer the exit.
    if ffiles:
        parts.append(
            '  <div class="notice notice-drift">'
            + '<div><b>{}</b> — {} <code>{}</code>. {}</div>'.format(
                t("Anchored to a file"), t("Showing only memories anchored to"), h(ffiles),
                t("A trailing slash matches a whole directory."))
            + f'<a class="btn btn-sm" href="{h(mkqs({"files": ""}))}">{t("Show all memories")}</a></div>')

    if fdrift:
        clear_url = mkqs({"drift": ""})
        explain = t("Showing only memories anchored to files that have since gone missing, moved, or "
                    "been committed to many times. They are not proven wrong — they are worth re-reading.")
        # What to DO, not only what happened. Each row shows its own reason above; the three
        # outcomes below are the only ones there are, and which applies depends on that reason.
        todo = t("Open a row to read it, then pick one: the lesson still holds and only the path "
                 "moved, so re-point it with <code>mem.py anchor &lt;id&gt; &lt;paths&gt;</code>; the "
                 "lesson is dead with the code it described, so supersede it from Actions; or it was "
                 "never really about those files, so clear the anchor with "
                 "<code>mem.py anchor &lt;id&gt; -</code> and it stops being reported.")
        parts.append(
            '  <div class="notice notice-drift">'
            f'<div><b>{t("Code drift")}</b> — {h(explain)}<div class="notice-todo">{todo}</div></div>'
            f'<a class="btn btn-sm" href="{h(clear_url)}">{t("Show all memories")}</a></div>')

    # ----- pager -----
    # Above and below the table, only when there is more than one page. Its links go through
    # mkqs like every other link here, so the filters in effect survive a page turn.
    pager = ""
    if total_rows > PAGE_ROWS:
        lo, hi = offset + 1, min(offset + PAGE_ROWS, total_rows)
        prev_href = mkqs({"offset": str(offset - PAGE_ROWS) if offset - PAGE_ROWS > 0 else ""})
        next_href = mkqs({"offset": str(offset + PAGE_ROWS)})
        pager = ('<div class="pager">'
                 + (f'<a href="{h(prev_href)}">‹ {t("previous")}</a>' if offset > 0
                    else f'<span class="off">‹ {t("previous")}</span>')
                 + f'<span class="range">{lo}–{hi} {t("of")} {total_rows}</span>'
                 + (f'<a href="{h(next_href)}">{t("next")} ›</a>' if hi < total_rows
                    else f'<span class="off">{t("next")} ›</span>')
                 + "</div>")
        parts.append("  " + pager)

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
            groups, full = {}, {}
            for r in rows:
                sc = r["meta"].get("scope", "global")
                full[sc] = full.get(sc, 0) + 1
            for r in page_rows:
                groups.setdefault(r["meta"].get("scope", "global"), []).append(r)
            for sc in sorted(groups.keys(), key=lambda x: (0, "") if x == "global" else (1, x)):
                grs = groups[sc]
                head = t("Global") if sc == "global" else (t("Project:") + " " + h(scope_label(sc)))
                cut = len(grs) < full.get(sc, 0)
                links = []
                if cut:
                    # The page cut went through this group: say so, and offer the whole scope (the
                    # ungrouped view, which keeps the other filters and has pages of its own).
                    links.append(f'<a class="gcount" href="{h(mkqs({"scope": sc}))}" '
                                 f'onclick="event.stopPropagation()">{t("all in this scope →")}</a>')
                if sc != "global":
                    links.append(f'<a class="gcount" href="/project?slug={h(scope_label(sc))}" '
                                 f'onclick="event.stopPropagation()">{t("project page →")}</a>')
                right = f'<span class="gcount" style="float:right">{" · ".join(links)}</span>' if links else ""
                count = (f'· {len(grs)} {t("of")} {full[sc]} {t("memories")}' if cut
                         else f'· {len(grs)} {t("memories")}')
                body += (f'<tbody class="grp" data-scope="{h(sc)}"><tr class="group-head"><td colspan="6">{head} '
                         f'<span class="gcount">{count}</span>{right}</td></tr>'
                         + "".join(render_row(r, drift_why.get(r["id"])) for r in grs) + "</tbody>")
        else:
            body = "<tbody>" + "".join(render_row(r, drift_why.get(r["id"])) for r in page_rows) + "</tbody>"
        parts.append(f'  <div id="table-wrap"><table class="mem">{thead}{body}</table></div>')
        if pager:
            parts.append("  " + pager)
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
            "Click a row → its body (loaded when first opened). Group header → collapse.")
    bulkh = (t("help.bulk") if lang() == "ro" else "Tick rows → bottom bar: Supersede / Re-scope / Delete all at once.")
    searchh = (t("help.search") if lang() == "ro" else
               "Field + Enter = FTS ranked (same index as <code>mem.py search</code>). The light by the button shows the "
               "search mode: <b>green</b> = the local LLM is up, so search is keyword + semantic; <b>gray</b> = it falls "
               "back to classic keyword search automatically. No toggle. Typing in the box narrows the rows already "
               "on this page by summary, scope and type — the body of a memory is reached only by the full-text "
               "search (Enter), and long lists come in pages of " + str(PAGE_ROWS) + ".")
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
           "loading": t("loading…"),
           "csrf": t("Session expired (the server restarted) — reload the page and try again.")}
    js = ("<script>\nvar CSRF = " + json.dumps(_CSRF) + ";\nvar STATUS_FILTER = " + json.dumps(fstat)
          + ";\nvar TXT = " + json.dumps(txt, ensure_ascii=False) + ";\n" + _MEM_JS_BODY + "\n</script>")
    return layout(t("Memories") + " — mem0ry4ai", "memories", "\n".join(parts), aside, after + js, side=side)


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
    def health_li(row):
        lbl, ok, det = row[0], row[1], row[2]
        href = row[3] if len(row) > 3 else None
        dot = "dot-unk" if ok is None else ("dot-ok" if ok else "dot-err")
        # det stays escaped; only the wrapper is markup, so a check can link without becoming a
        # hole through which any future detail string could inject HTML.
        body = f'<a href="{h(href)}">{h(det)}</a>' if href else h(det)
        return f'<li><span class="dot {dot}"></span> {h(lbl)} <span class="hd">{body}</span></li>'

    health_html = "\n        ".join(health_li(row) for row in health)
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
    if lang() == "ro":
        cards_help = (
            'Fiecare card e un filtru: <b>Memorii</b> = tot ce s-a scris vreodata, '
            '<b>active</b> = ce se injecteaza si se cauta, <b>superseded</b> = ce a fost inlocuit '
            '(pastrat, niciodata sters — de acolo afli <i>ce credeai si cand ai incetat</i>). '
            '<b>todo deschise</b> se coloreaza cand exista. Restul cardurilor sunt tipurile tale '
            'cele mai numeroase.')
        health_help = (
            '<b>Store / Staging writable</b> — se poate scrie pe disc. '
            '<b>Index FTS</b> — indexul de cautare e la zi cu fisierele markdown; e derivat, '
            'se poate sterge si reface oricand. '
            '<b>Embedder</b> — cati vectori exista si daca Ollama raspunde; oprit inseamna doar '
            'ca se cauta pe cuvinte cheie, nu ca s-a stricat ceva. '
            '<b>Coada de review</b> — candidati propusi de LLM-ul local, care asteapta decizia ta. '
            '<b>Drift fata de cod</b> — memorii ancorate la fisiere (campul <code>files:</code>) '
            'ale caror fisiere lipsesc, s-au mutat sau au primit multe commit-uri de cand a fost '
            'scrisa memoria; nu inseamna ca memoria e gresita, ci ca merita recitita. '
            '<b>Hooks</b> — daca injectarea automata e inregistrata. '
            '<b>Injectare SessionStart</b> — cat ocupa acum fata de buget; rosu inseamna ca trece de el. '
            'Click pe cifra arata exact ce se injecteaza; bugetul se schimba in '
            '<a href="/settings">Setari</a>, la <code>MEM_INJECT_BUDGET</code>. '
            '<b>Git store</b> — daca sunt memorii necomise.')
        dots_help = ('<b>Verde</b> = in regula. <b>Rosu</b> = cere atentie. '
                     '<b>Gri</b> = nu se stie / nu se aplica — de exemplu drift-ul cand nicio memorie '
                     'nu e ancorata inca: lipsa semnalului nu e acelasi lucru cu „e bine".')
        recent_help = ('Ultimele memorii scrise, cu sursa lor: <code>claude:live</code> = scrise de '
                       'agent in conversatie, <code>mcp</code> = prin tool-ul MCP, '
                       '<code>web</code> = de aici, de mana.')
    else:
        cards_help = (
            'Each card is a filter: <b>Memories</b> = everything ever written, '
            '<b>active</b> = what gets injected and searched, <b>superseded</b> = what was replaced '
            '(kept, never deleted — this is where you read <i>what you believed and when you '
            'stopped</i>). <b>open todos</b> turns amber when there are any. The remaining cards are '
            'your most common types.')
        health_help = (
            '<b>Store / Staging writable</b> — the disk accepts writes. '
            '<b>FTS index</b> — the search index is current with the markdown; it is derived, so '
            'deleting and rebuilding it is always safe. '
            '<b>Embedder</b> — how many vectors exist and whether Ollama answers; offline only means '
            'search falls back to keywords, nothing is broken. '
            '<b>Review queue</b> — candidates the local LLM proposed, waiting on your decision. '
            '<b>Code drift</b> — memories anchored to files (the <code>files:</code> field) whose '
            'files are now missing, moved, or heavily committed to since the memory was written. It '
            'does not mean the memory is wrong, only that it is worth re-reading. '
            '<b>Hooks</b> — whether automatic injection is registered. '
            '<b>SessionStart injection</b> — how much of its budget it uses now; red means it is over. '
            'Click the figure to see exactly what gets injected; the budget itself lives in '
            '<a href="/settings">Settings</a>, as <code>MEM_INJECT_BUDGET</code>. '
            '<b>Git store</b> — whether any memories are uncommitted.')
        dots_help = ('<b>Green</b> = fine. <b>Red</b> = wants attention. '
                     '<b>Grey</b> = unknown or not applicable — for instance drift when nothing is '
                     'anchored yet: no signal is not the same as a clean bill of health.')
        recent_help = ('The most recent writes and where they came from: <code>claude:live</code> = '
                       'written by the agent mid-conversation, <code>mcp</code> = through the MCP '
                       'tool, <code>web</code> = by hand from here.')
    aside = f'''<aside class="help">
  <h3>{t("Quick guide")}</h3>
  <p>{intro}</p>
  <h4>{t("The cards")}</h4>
  <p>{cards_help}</p>
  <h4>{t("Health")}</h4>
  <p>{health_help}</p>
  <p>{dots_help}</p>
  <h4>{t("Recent activity")}</h4>
  <p>{recent_help}</p>
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


def endpoint_record(qs):
    """One record for the page scripts: the raw fields the edit modal seeds itself with, and the
    rendered expanded row. The web UI is the user's own machine, so DEST_LOCAL withholds nothing --
    the text still passes through emit_for, the one place that decides what a record shows."""
    rid = (qs.get("id", [""])[0] or "").strip()
    r = mem.get_record(rid) if _ID_RE.match(rid) else None
    if r is None:
        return {"ok": False, "error": "not found"}
    summary, body = mem.emit_for(r, mem.DEST_LOCAL)
    if summary is None:
        return {"ok": False, "error": "not found"}
    m = r["meta"]
    return {"ok": True, "id": r["id"], "summary": summary, "body": body or "",
            "type": m.get("type", ""), "scope": m.get("scope", "global"),
            "status": m.get("status", "active"), "confidence": m.get("confidence", "1.0"),
            "created": m.get("created", ""), "source": m.get("source", ""),
            "html": expanded_row_html(r, records_by_id(), related_in_index())}


# ---------- HTTP server ----------
_WORKING_JS = r'''
(function(){
  var box = document.getElementById('wcards'); if(!box) return;
  function post(data){
    var fd = new URLSearchParams(); fd.set('csrf', CSRF);
    Object.keys(data).forEach(function(k){ fd.set(k, data[k]); });
    return fetch('/working', {method:'POST', headers:{'X-Requested-With':'XMLHttpRequest'}, body: fd})
      .then(function(r){ return r.json(); });
  }
  function drop(id){
    var c = box.querySelector('.qcard[data-id="'+id+'"]'); if(c) c.remove();
    var left = box.querySelectorAll('.qcard').length;
    var cnt = document.querySelector('.count'); if(cnt) cnt.textContent = left;
    if(!left) box.innerHTML = '<div class="empty">'+TXT.empty+'</div>';
  }
  box.addEventListener('click', function(e){
    var card = e.target.closest('.qcard'); if(!card) return;
    var id = card.dataset.id;
    if(e.target.classList.contains('w-promote')){
      post({action:'promote', id:id}).then(function(j){ if(j.ok) drop(id); else alert(j.error||TXT.error); });
    } else if(e.target.classList.contains('w-dismiss')){
      if(!confirm(TXT.dismissQ)) return;
      post({action:'dismiss', id:id}).then(function(j){ if(j.ok) drop(id); else alert(j.error||TXT.error); });
    }
  });
})();
'''


def page_working(qs=None):
    notes = sorted([r for r in mem.all_records() if r["meta"].get("status") == "working"],
                   key=lambda r: r["meta"].get("created", ""), reverse=True)
    intro = ('Note de lucru (scratch), status <code>working</code> — NU se injecteaza la SessionStart si sunt '
             'ascunse din cautare/recall. Promoveaza-le pe cele care merita tinute durabil; arunca restul.'
             if lang() == "ro" else
             'Working (scratch) notes, status <code>working</code> — NOT injected at session start and hidden '
             'from search/recall. Promote the ones worth keeping; dismiss the rest.')
    if not notes:
        empty = ('Nicio nota de lucru. Un agent le scrie cu <code>memory_note</code> (MCP) sau '
                 '<code>mem.py add --working</code>.' if lang() == "ro" else
                 'No working notes. An agent writes them with <code>memory_note</code> (MCP) or '
                 '<code>mem.py add --working</code>.')
        cards = f'<div class="empty">{empty}</div>'
    else:
        cards = ""
        for r in notes:
            m = r["meta"]
            cards += (
                f'<div class="qcard" data-id="{h(r["id"])}">'
                f'<div class="qhead">{type_badge(m.get("type", "?"))} '
                f'<span class="scope-tag">{h(scope_label(m.get("scope", "global")))}</span> '
                f'<span class="qsrc">{h(m.get("source", ""))}</span> '
                f'<span class="conf">{h((m.get("created", "") or "")[:16])}</span></div>'
                f'<div class="qsum">{h(mem.record_summary(r))}</div>'
                f'<div class="qbody">{render_body(r.get("body", ""))}</div>'
                f'<div class="qactions">'
                f'<button type="button" class="btn btn-primary w-promote">{t("Promote")}</button> '
                f'<button type="button" class="btn btn-danger w-dismiss">{t("Dismiss")}</button></div></div>')
    content = (
        f'  <div class="crumb"><a href="/">{t("Dashboard")}</a> / {t("Working notes")}</div>\n'
        f'  <h2>{t("Working notes")} <span class="count">{len(notes)}</span></h2>\n'
        f'  <p class="meta" style="margin-top:0">{intro}</p>\n'
        f'  <div id="wcards">{cards}</div>')
    aside_txt = ('<b>Promote</b> = devine memorie durabila (working &rarr; active): se injecteaza si devine '
                 'cautabila. <b>Dismiss</b> = marcata superseded (ramane in git, dar iese din lista).'
                 if lang() == "ro" else
                 '<b>Promote</b> = becomes a durable memory (working &rarr; active): injected and searchable. '
                 '<b>Dismiss</b> = marked superseded (kept in git, leaves the list).')
    aside = f'<aside class="help"><h3>{t("Actions")}</h3><p>{aside_txt}</p></aside>'
    txt = {"error": t("Error"),
           "dismissQ": t("Dismiss this working note? (it is marked superseded, not deleted)"),
           "empty": t("No working notes left.")}
    script = ("<script>var CSRF = " + json.dumps(_CSRF) + "; var TXT = "
              + json.dumps(txt, ensure_ascii=False) + ";" + _WORKING_JS + "</script>")
    return layout(t("Working notes") + " — mem0ry4ai", "working", content, aside, script)


# ---------- duplicates ----------
# The page script: verbatim vanilla JS. CSRF, LANG and TXT are prepended by the page.
_DUP_JS_BODY = r'''
(function () {
  'use strict';
  var page = document.getElementById('dp-page');
  var modal = document.getElementById('dp-edit'), form = document.getElementById('mem-form');
  // `view` is what the list holds: 'open' = groups still to review, 'done' = what was settled
  var S = { view: 'open', type: '', where: '', sorts: { open: 'sim', done: 'at' }, sel: null };
  var G = [], D = { threshold: 0, vectors: 0 };
  var READY = false, PENDING = null, BUSY = false, ERR = '', NOTE = '', editing = null;

  // ------------------------------------------------------------------ helpers
  function esc(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function fmt(n) { return String(n).replace(/\B(?=(\d{3})+(?!\d))/g, TXT.thousands); }
  function pct(x) { return Math.round(x * 100) + '%'; }
  function scopeLabel(s) { return s.indexOf('project:') === 0 ? s.slice(8) : s; }
  // "1 minute", "5 minutes"; Romanian puts "de" after a number from 20 up: "un minut", "5 minute", "27 de minute"
  function count(n, forms) {
    if (n === 1) return forms[0];
    var r = n % 100;
    return fmt(n) + (LANG === 'ro' && n >= 20 && (r === 0 || r >= 20) ? ' de ' : ' ') + forms[1];
  }
  function pick(n, forms) { return (n === 1 ? forms[0] : forms[1]).replace('{n}', n); }
  function ts(s) { return new Date(String(s).replace(' ', 'T')).getTime(); }
  function gapSeconds(g) {
    var t = g.members.map(function (m) { return ts(m.created); });
    return Math.round((Math.max.apply(null, t) - Math.min.apply(null, t)) / 1000);
  }
  function gapText(g) {
    var s = gapSeconds(g), u = TXT.units;
    if (!isFinite(s)) return '';
    if (s < 1) return TXT.sameSecond;
    return TXT.apart.replace('{x}', s < 60 ? count(s, u.s) : s < 3600 ? count(Math.round(s / 60), u.m) :
      s < 86400 ? count(Math.round(s / 3600), u.h) : s < 86400 * 60 ? count(Math.round(s / 86400), u.d) :
      count(Math.round(s / (86400 * 30.4)), u.mo));
  }
  function has(g, id) { return g.members.some(function (m) { return m.id === id; }); }
  function ids(g) { return g.members.map(function (m) { return m.id; }); }
  function keeper(g) { return g.members.filter(function (m) { return m.id === g.keep; })[0] || g.members[0]; }
  function others(g) { var K = keeper(g); return g.members.filter(function (m) { return m !== K; }); }
  function byId(gid) { return G.filter(function (g) { return g.gid === gid; })[0]; }
  function short(t, n) { n = n || 110; return t.length > n ? t.slice(0, n - 1) + '…' : t; }
  function byNewest(a, b) { return a.created < b.created ? 1 : a.created > b.created ? -1 : a.id < b.id ? 1 : -1; }

  // What `a` says that `b` does not: the words of `a` that appear nowhere in `b`. Only words that carry
  // content count (four letters or more, not a filler word), compared without diacritics and, for plain
  // words, on their first six letters, so an inflected form is the same word. Two memories that say the
  // same thing in different sentences score low; a number, a path or a name found in one only stands out.
  var STOP = {};
  ('acest aceasta aceste acestea acel acea acolo aici care cand catre ceea chiar cum daca dar deci din dintre doar dupa ' +
   'este fara fiecare fiind fost face foarte iar inainte inca intr intre insa mai mult nici niste numai orice pana pentru ' +
   'peste prin sau sunt spre sub toate tot unde unui unei era avea avem trebuie poate that this with from have has were ' +
   'what when where which will would there their then than them they been being also into only over such some more most ' +
   'other your about after before because while should could').split(/\s+/).forEach(function (w) { STOP[w] = 1; });
  function wordKey(t) {
    var w = t.normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLowerCase().replace(/^[^\p{L}\p{N}]+|[^\p{L}\p{N}]+$/gu, '');
    if (w.length < 4 || STOP[w]) return '';
    return /^[a-z]+$/.test(w) && w.length > 6 ? w.slice(0, 6) : w;
  }
  var cache = {};
  function cmp(a, b) {
    var key = a.id + '|' + b.id;
    if (cache[key]) return cache[key];
    var have = {}, i, j;
    (b.body.match(/\S+/g) || []).forEach(function (t) { var k = wordKey(t); if (k) have[k] = 1; });
    var parts = a.body.match(/\s+|\S+/g) || [], flag = [], total = 0, uniq = 0;
    for (i = 0; i < parts.length; i++) {
      var k = /\S/.test(parts[i]) ? wordKey(parts[i]) : '';
      if (!k) { flag.push(null); continue; }
      total++;
      if (have[k]) flag.push(false); else { flag.push(true); uniq++; }
    }
    // bridge: up to two filler words between two marked ones join the same mark, so a phrase reads as one
    for (i = 0; i < parts.length; i++) {
      if (flag[i] !== true) continue;
      var fill = 0;
      for (j = i + 1; j < parts.length && flag[j] === null; j++) if (/\S/.test(parts[j])) fill++;
      if (j < parts.length && flag[j] === true && fill <= 2) for (var m = i + 1; m < j; m++) flag[m] = true;
    }
    var html = '', open = false;
    for (i = 0; i < parts.length; i++) {
      var on = flag[i] === true;
      if (on && !open) { html += '<mark class="dp-uq">'; open = true; }
      if (!on && open) { html += '</mark>'; open = false; }
      html += esc(parts[i]);
    }
    if (open) html += '</mark>';
    return (cache[key] = { html: html, share: total ? uniq / total : 0 });
  }
  // how much would leave the active set if `m` were the one kept
  function lossIfKept(g, m) {
    var rest = g.members.filter(function (o) { return o !== m; });
    return rest.length ? Math.max.apply(null, rest.map(function (o) { return cmp(o, m).share; })) : 0;
  }
  function maxShare(g) {
    var K = keeper(g), o = others(g);
    return o.length ? Math.max.apply(null, o.map(function (x) { return cmp(x, K).share; })) : 0;
  }
  function shareChip(x) {
    if (x === 0) return '<span class="dp-chip dp-chip-same">' + TXT.identical + '</span>';
    return '<span class="dp-chip ' + (x <= 0.15 ? 'dp-chip-same' : 'dp-chip-diff') + '" title="' + TXT.onlyHereTitle + '">' +
      TXT.onlyHere.replace('{n}', Math.max(1, Math.round(x * 100))) + '</span>';
  }
  function idchip(id) { return '<a class="idchip" href="/memories?id=' + esc(id) + '" target="_blank" rel="noopener">' + esc(id) + '</a>'; }

  // ------------------------------------------------------------- the model
  // A row as the server sends it, plus what the page adds: which memory stays (`keep`), the tab
  // open on a group of three or more, and `acted` -- settled or taken back in this view, so it
  // stays in the list where it was until the view changes.
  function prepare(g) {
    var scopes = {};
    g.members.forEach(function (m) { scopes[m.scope] = 1; });
    g.where = Object.keys(scopes).length > 1 ? 'cross' : 'same';
    g.tab = 0;
    g.acted = false;
    var newest = g.members.slice().sort(byNewest)[0];
    g.newest = newest.id;
    g.newestAt = ts(newest.created) || 0;
    if (g.state === 'open') {
      // proposed keeper: the newest, unless an older one leaves clearly less of the others unsaid
      var best = newest, loss = lossIfKept(g, best);
      g.members.forEach(function (m) { var l = lossIfKept(g, m); if (l < loss - 0.15) { best = m; loss = l; } });
      // before either: a memory the store does not retire as a copy -- a critical rule first, then a protected one
      var held = g.members.filter(function (m) { return m.pinned || m.protected; })
        .sort(function (a, b) { return (b.pinned ? 1 : 0) - (a.pinned ? 1 : 0) || byNewest(a, b); })[0];
      g.held = !!held;
      if (held) best = held;
      g.keep = g.proposed = best.id;
    } else if (g.state === 'distinct') {
      g.keep = g.newest;        // nothing was kept or retired here: the first column is simply the newest
    }
    return g;
  }
  function natural(g) { return (S.view === 'open') === (g.state === 'open'); }
  function inView(g) { return g.acted || natural(g); }
  function sortKey(g) {
    var by = S.sorts[S.view];
    if (by === 'n') return g.members.length;
    if (by === 'uq') return maxShare(g);
    if (by === 'created') return g.newestAt;
    if (by === 'at') return g.at || '';
    return g.sim === null ? -1 : g.sim;
  }
  function visible() {
    var dir = S.sorts[S.view] === 'uq' ? 1 : -1;
    return G.filter(function (g) { return inView(g) && (!S.type || g.type === S.type) && (!S.where || g.where === S.where); })
      .map(function (g) { return { g: g, k: sortKey(g) }; })
      .sort(function (a, b) {
        return (a.k < b.k ? -1 : a.k > b.k ? 1 : 0) * dir || (b.g.sim || 0) - (a.g.sim || 0) ||
          (a.g.gid < b.g.gid ? -1 : a.g.gid > b.g.gid ? 1 : 0);
      })
      .map(function (x) { return x.g; });
  }
  function counts(field) {
    var c = {};
    G.forEach(function (g) {
      if (!inView(g)) return;
      if (field === 'type' ? (!S.where || g.where === S.where) : (!S.type || g.type === S.type)) c[g[field]] = (c[g[field]] || 0) + 1;
    });
    return c;
  }

  // ------------------------------------------------------------ the pieces
  function dash() {
    var open = G.filter(function (g) { return g.state === 'open'; });
    var copies = open.reduce(function (a, g) { return a + g.members.length - 1; }, 0);
    var same = open.filter(function (g) { return maxShare(g) <= 0.15; }).length;
    function stat(n, l) { return '<div class="card-stat"><div class="num">' + n + '</div><div class="lbl">' + l + '</div></div>'; }
    return '<div class="dash dp-dash">' + stat(open.length, TXT.cardOpen) + stat(copies, TXT.cardCopies) +
      stat(same, TXT.cardSame) + stat(G.length - open.length, TXT.cardDone) +
      '<a class="card-stat" href="/settings"><div class="num">' + pct(D.threshold) + '</div><div class="lbl">' + TXT.cardThreshold + '</div></a></div>';
  }
  function toolbar() {
    var ct = counts('type'), cw = counts('where'), total = 0, wt = 0, k;
    for (k in ct) total += ct[k];
    for (k in cw) wt += cw[k];
    var types = Object.keys(ct).sort(function (a, b) { return ct[b] - ct[a] || (a < b ? -1 : 1); });
    if (S.type && !ct[S.type]) types.push(S.type);      // a filter that matches nothing stays visible, to be turned off
    function pill(group, val, label, n) {
      return '<a data-act="filter" data-f="' + group + '" data-val="' + esc(val) + '" class="' + (S[group] === val ? 'active' : '') + '">' +
        label + '<small>' + n + '</small></a>';
    }
    var nOpen = G.filter(function (g) { return g.state === 'open'; }).length;
    var sorts = [['sim', TXT.sortSim], ['uq', TXT.sortUq], ['created', TXT.sortNew], ['n', TXT.sortBig]];
    if (S.view === 'done') sorts.unshift(['at', TXT.sortAt]);
    return '<div class="toolbar dp-toolbar"><span class="dp-flabel">' + TXT.fState + '</span><div class="pills">' +
      pill('view', 'open', TXT.vOpen, nOpen) + pill('view', 'done', TXT.vDone, G.length - nOpen) + '</div>' +
      '<span class="dp-flabel">' + TXT.fType + '</span><div class="pills">' + pill('type', '', TXT.all, total) +
      types.map(function (t) { return pill('type', t, esc(t), ct[t] || 0); }).join('') + '</div>' +
      '<span class="dp-flabel">' + TXT.fWhere + '</span><div class="pills">' + pill('where', '', TXT.anywhere, wt) +
      pill('where', 'same', TXT.sameProject, cw.same || 0) + pill('where', 'cross', TXT.crossProject, cw.cross || 0) + '</div>' +
      '<span class="grow"></span><select id="dp-sort" title="' + TXT.order + '" aria-label="' + TXT.order + '">' +
      sorts.map(function (o) {
        return '<option value="' + o[0] + '"' + (S.sorts[S.view] === o[0] ? ' selected' : '') + '>' + o[1] + '</option>';
      }).join('') + '</select></div>';
  }
  function legend() {
    return '<p class="dp-legend"><mark class="dp-uq">' + TXT.marked + '</mark> ' + TXT.legend + '</p>';
  }
  function headLine(g) {
    var seen = {}, tags = '', gap = gapText(g);
    g.members.forEach(function (m) {
      if (!seen[m.scope]) { seen[m.scope] = 1; tags += '<span class="scope-tag">' + esc(scopeLabel(m.scope)) + '</span>'; }
    });
    return (g.sim === null ? '' : '<span class="dp-pct" title="' + TXT.likeness + '">' + pct(g.sim) + '</span>') +
      '<span class="badge t-' + esc(g.type) + '">' + esc(g.type) + '</span>' + tags +
      '<span class="dp-when">' + count(g.members.length, TXT.mem) + (gap ? ' · ' + gap : '') + '</span>';
  }
  function why(g, m) {
    return (m.pinned ? '<span class="badge t-status" title="' + TXT.pinnedTitle + '">critical</span>' : '') +
      (m.protected ? '<span class="dp-new" title="' + TXT.protectedTitle + '">' + TXT.protectedChip + '</span>' : '') +
      (m.id === g.newest ? '<span class="dp-new">' + TXT.newest + '</span>' : '') +
      (g.state === 'open' && !g.held && m.id === g.proposed && m.id !== g.newest ? '<span class="dp-new">' + TXT.saysMost + '</span>' : '');
  }
  function doneLine(g) {
    var K = keeper(g), n = g.members.length - 1, t;
    if (g.state === 'retired') {
      t = '<span class="ok">&#10003;</span><span class="t">' + TXT.stays + ' ' + idchip(K.id) + ' ' + esc(short(K.title)) + '</span><span>' +
        pick(n, TXT.retiredN) + (g.at ? ' · ' + esc(g.at.slice(0, 16)) : '') + '</span>';
    } else {
      t = '<span class="t">' + (n === 1 ? TXT.leftBoth : TXT.leftAll.replace('{n}', n + 1)) + ' · ' + esc(short(K.title)) + '</span>';
    }
    return '<div class="dp-done" data-g="' + esc(g.gid) + '">' + t + '<button type="button" class="btn btn-sm" data-act="undo"' +
      (BUSY ? ' disabled' : '') + '>' + TXT.undo + '</button></div>';
  }
  // One group, compared: the memory that stays on the left, a copy on the right. A settled group shows
  // the same comparison without the actions, under the line that takes it back.
  function stage(g, pos, total, left) {
    var open = g.state === 'open', judged = g.state === 'distinct', dis = BUSY ? ' disabled' : '';
    var K = keeper(g), o = others(g), cur = o[Math.min(g.tab, o.length - 1)], c = cmp(cur, K);
    var kc = judged ? '' : ' keep';
    function row(label, a, b) { return '<div class="k">' + label + '</div><div class="v' + kc + '">' + a + '</div><div class="v">' + b + '</div>'; }
    function opened(m) { return !m.opened ? TXT.never : m.opened === 1 ? TXT.times[0] : TXT.timesPre + count(m.opened, TXT.times); }
    function cited(m) { return m.cited ? count(m.cited, TXT.mem) : '—'; }
    function files(m) { return m.files.length ? m.files.map(function (f) { return '<code>' + esc(f) + '</code>'; }).join(' ') : '—'; }
    function written(m) { return esc(m.created.slice(0, 16)) + ' · ' + esc(scopeLabel(m.scope)); }
    var tabs = o.length > 1 ? '<span class="dp-tabs">' + o.map(function (x, i) {
      return '<button type="button" data-act="tab" data-i="' + i + '" class="' + (x === cur ? 'on' : '') + '">' + esc(x.id.slice(-6)) + ' · ' +
        Math.round(cmp(x, K).share * 100) + '%</button>';
    }).join('') + '</span>' : '';
    var next = !open && !natural(g) && left ? '<a data-act="next">' + TXT.nextOpen + '</a>' : '';
    return (open ? '' : doneLine(g)) +
      '<div class="dp-group dp-stage" data-g="' + esc(g.gid) + '"><div class="dp-head">' + headLine(g) +
      '<span class="dp-headact">' + next + '<span class="dp-prog">' + pos + ' ' + TXT.of + ' ' + total + '</span></span></div>' +
      '<div class="dp-cmp"><div class="k top"></div>' +
      '<div class="v top' + kc + '">' + (judged ? '' : '<span class="dp-chip dp-chip-keep">' + TXT.stays + '</span>') + idchip(K.id) + why(g, K) + '</div>' +
      '<div class="v top">' + (judged ? '' : '<span class="dp-chip dp-chip-out">' + (open ? TXT.toRetire : TXT.retired) + '</span>') +
      idchip(cur.id) + why(g, cur) + tabs +
      (open ? '<button type="button" class="btn btn-sm" data-act="swap" data-id="' + esc(cur.id) + '"' + dis + '>' + TXT.keepThis +
              '<span class="dp-key">S</span></button>' : '') + '</div>' +
      row(TXT.rTitle, '<span class="dp-title">' + esc(K.title) + '</span>', '<span class="dp-title">' + esc(cur.title) + '</span>') +
      row(TXT.rWritten, written(K), written(cur)) +
      row(TXT.rSize, count(K.chars, TXT.chars), count(cur.chars, TXT.chars)) +
      row(TXT.rOpened, opened(K), opened(cur)) +
      row(TXT.rCited, cited(K), cited(cur)) +
      row(TXT.rAnchors, files(K), files(cur)) +
      row(TXT.rText, '<div class="dp-body">' + esc(K.body) + '</div>', shareChip(c.share) + '<div class="dp-body dp-body-gap">' + c.html + '</div>') +
      '</div>' + (open ? '<div class="dp-foot"><button type="button" class="btn btn-ghost" data-act="dismiss"' + dis + '>' + TXT.notDups +
      '<span class="dp-key">N</span></button><button type="button" class="btn btn-ghost" data-act="skip">' + TXT.skip +
      '<span class="dp-key">J</span></button><span class="grow"></span><button type="button" class="btn" data-act="edit"' + dis + '>' +
      TXT.editFirst + '<span class="dp-key">E</span></button><button type="button" class="btn btn-primary" data-act="retire"' + dis + '>' +
      pick(o.length, TXT.retireN) + '<span class="dp-key">&#9166;</span></button></div>' : '') + '</div>';
  }
  function emptyText(all) {
    if (all) return TXT.noMatch;
    if (S.view === 'done') return TXT.noneDone;
    return D.vectors < 2 ? TXT.noVectors : TXT.noneOpen;
  }
  function pageHtml() {
    var head = '<div class="crumb"><a href="/">' + TXT.dashboard + '</a> / ' + TXT.title + '</div>';
    var msg = (ERR ? '<div class="flash flash-error dp-err" role="alert">' + esc(ERR) + '</div>' : '') +
      (NOTE ? '<div class="flash flash-info dp-note" role="status">' + esc(NOTE) + '</div>' : '');
    if (!READY || PENDING !== null) {
      return head + '<h2>' + TXT.title + '</h2>' + msg + (ERR ? '' : '<div class="empty dp-wait">' +
        (PENDING === null ? TXT.loading : TXT.comparing.replace('{n}', fmt(PENDING))) + '</div>');
    }
    var vis = visible(), idx = -1, all = G.filter(inView).length;
    vis.forEach(function (g, i) { if (g.gid === S.sel) idx = i; });
    if (idx < 0 && vis.length) { idx = 0; S.sel = vis[0].gid; }
    var out = head + '<h2>' + TXT.title + ' <span class="count">' + vis.length + ' ' + TXT.of + ' ' + all + ' ' + TXT.groups + '</span></h2>' +
      dash() + toolbar() + legend() + msg;
    if (!vis.length) return out + '<div class="empty">' + emptyText(all) + '</div>';
    var left = vis.filter(natural).length;
    return out + '<div class="dp-triage"><div class="dp-list"><div class="side-head"><div class="side-title">' + TXT.listTitle +
      ' <span class="side-count">' + pick(left, S.view === 'open' ? TXT.nOpen : TXT.nDone) + '</span></div></div><div class="dp-items sidelinks">' +
      vis.map(function (g) {
        var K = keeper(g);
        return '<a data-act="select" data-g="' + esc(g.gid) + '" class="' + (g.gid === S.sel ? 'on ' : '') + (natural(g) ? '' : 'dp-isdone ') +
          (g.state === 'retired' ? 'kept' : '') + '">' +
          (g.sim === null ? '<span class="dp-pct dp-nopct">&ndash;</span>' : '<span class="dp-pct">' + pct(g.sim) + '</span>') +
          '<span class="n" title="' + esc(K.title) + '">' + esc(K.title) + '</span><span class="c">' +
          (g.state === 'retired' ? '&#10003;' : g.state === 'distinct' ? '&ne;' : '&times;' + g.members.length) + '</span></a>';
      }).join('') + '</div></div><div>' + stage(vis[idx], idx + 1, vis.length, left) + '</div></div>';
  }

  // -------------------------------------------------------------- behaviour
  function render() {
    var y = window.scrollY, list = page.querySelector('.dp-items'), ly = list ? list.scrollTop : 0;
    page.innerHTML = pageHtml();
    page.classList.toggle('dp-busy', BUSY);
    window.scrollTo(0, y);
    list = page.querySelector('.dp-items');
    if (!list) return;
    list.scrollTop = ly;
    // bring the selected row into the list's own window, without moving the page under the reader
    var on = list.querySelector('a.on');
    if (on) {
      var r = on.getBoundingClientRect(), b = list.getBoundingClientRect();
      if (r.top < b.top) list.scrollTop -= b.top - r.top;
      else if (r.bottom > b.bottom) list.scrollTop += r.bottom - b.bottom;
    }
  }
  // J / K: the next or the previous row that belongs to this view (a row settled here is passed over)
  function step(d) {
    var vis = visible(), at = d > 0 ? -1 : vis.length, i;
    vis.forEach(function (g, k) { if (g.gid === S.sel) at = k; });
    for (i = at + d; i >= 0 && i < vis.length; i += d) if (natural(vis[i])) { S.sel = vis[i].gid; return true; }
    return false;
  }
  // after settling a group: on to the next one still waiting, from the top again when the list ends
  function onward() {
    var vis = visible(), start = 0, i;
    vis.forEach(function (g, k) { if (g.gid === S.sel) start = k; });
    for (i = 1; i <= vis.length; i++) {
      var g = vis[(start + i) % vis.length];
      if (natural(g)) { S.sel = g.gid; return true; }
    }
    return false;
  }
  function pickFirst() {
    var vis = visible(), first = vis.filter(natural)[0] || vis[0];
    S.sel = first ? first.gid : null;
  }
  function setView(v) {
    if (S.view === v) return;
    S.view = v;
    G.forEach(function (g) { g.acted = false; });      // a new view starts clean: only what belongs to it
    pickFirst();
  }

  // The server's answer is the truth after every action: the rows are rebuilt from it, and what the
  // reader had chosen on a row that is still there (which memory stays, which tab) is carried over.
  function take(d, then) {
    if (!d || d.ok === false) { ERR = (d && d.error) || TXT.failed; render(); return; }
    if (d.pending !== undefined) {
      PENDING = d.pending;
      render();
      setTimeout(function () { load(then); }, 700);
      return;
    }
    var old = {};
    G.forEach(function (g) { old[g.gid] = g; });
    PENDING = null; READY = true; cache = {};
    D.threshold = d.threshold; D.vectors = d.vectors;
    G = d.groups.filter(function (g) { return g.members.length > 1; }).map(prepare);
    G.forEach(function (g) {
      var o = old[g.gid];
      if (!o) return;
      g.acted = o.acted; g.tab = o.tab;
      if (o.at && !g.at) g.at = o.at;
      if (g.state === 'open' && has(g, o.keep)) g.keep = o.keep;
    });
    if (then) then();
    if (!visible().some(function (g) { return g.gid === S.sel; })) pickFirst();
    render();
  }
  function load(then) {
    // the failure handler sits beside the success one, not after it: a fault in drawing the answer is
    // then a fault in the console, not a "network error" on the page
    fetch('/duplicates/data', { cache: 'no-store' }).then(function (r) { return r.json(); })
      .then(function (d) { take(d, then); }, function () { PENDING = null; ERR = TXT.network; render(); });
  }
  function run(params, done) {
    if (BUSY) return;
    BUSY = true; ERR = ''; NOTE = '';
    render();
    var fd = new URLSearchParams();
    fd.set('csrf', CSRF);
    Object.keys(params).forEach(function (k) { fd.set(k, params[k]); });
    fetch('/duplicates', { method: 'POST', headers: { 'X-Requested-With': 'XMLHttpRequest' }, body: fd })
      .then(function (r) { return r.json(); })
      .then(function (j) {
        BUSY = false;
        if (!j.ok) ERR = j.error === 'CSRF' ? TXT.csrf : (j.error || TXT.failed);
        if (j.data) take(j.data, j.ok ? function () { done(j); } : null); else render();
      }, function () { BUSY = false; ERR = TXT.network; render(); });
  }
  // the row that now holds a settled group stays where the group was, and the reader moves on
  function settled(g, row) {
    if (row) {
      row.acted = true;
      if (row.sim === null) row.sim = g.sim;
      S.sel = row.gid;
    }
    if (S.view === 'open') onward();      // among the settled there is no queue to move along
  }
  function retire(g) {
    var K = keeper(g);
    run({ action: 'retire', keep: K.id, ids: ids({ members: others(g) }).join(',') }, function () {
      settled(g, byId('retired:' + K.id));
    });
  }
  function dismiss(g) {
    var mine = ids(g);
    run({ action: 'distinct', ids: mine.join(',') }, function () {
      settled(g, G.filter(function (n) { return n.state === 'distinct' && mine.every(function (i) { return has(n, i); }); })[0]);
    });
  }
  function undo(g) {
    var mine = ids(g);
    run(g.state === 'retired' ? { action: 'restore', keep: g.keep } : { action: 'alike', ids: (g.ids || mine).join(',') }, function (j) {
      var rows = G.filter(function (n) { return n.state === 'open' && mine.some(function (i) { return has(n, i); }); });
      rows.forEach(function (n) { n.acted = true; if (g.at) n.at = g.at; });
      if (rows.length) S.sel = rows[0].gid;
      NOTE = [j.exact === false ? TXT.undoInexact : '', rows.length ? '' : TXT.undoNoGroup].filter(Boolean).join(' ');
    });
  }

  // "Edit first": the memory that stays, in the editor of the Memories page, with the copies beside it
  function editOpen() { return modal.classList.contains('open'); }
  function closeEdit() { modal.classList.remove('open'); modal.setAttribute('aria-hidden', 'true'); editing = null; }
  function field(id) { return document.getElementById(id); }
  function showEdit(g) {
    var K = keeper(g);
    if (BUSY) return;
    BUSY = true; ERR = ''; NOTE = '';
    render();
    fetch('/record?id=' + encodeURIComponent(K.id)).then(function (r) { return r.json(); }).then(function (d) {
      BUSY = false;
      if (!d.ok) { ERR = d.error || TXT.failed; render(); return; }
      editing = g.gid;
      field('m-id').value = d.id;
      field('m-type').value = d.type;
      field('m-scope').value = d.scope;
      field('m-confidence').value = d.confidence || '1.0';
      field('m-summary').value = d.summary;
      field('m-body').value = d.body;
      field('m-err').style.display = 'none';
      field('m-submit').disabled = false;
      field('dp-edit-title').textContent = TXT.editTitle + ' ' + d.id;
      field('dp-edit-side').innerHTML = others(g).map(function (o) {
        var c = cmp(o, K);
        return '<div class="dp-other"><div class="dp-colhead"><span class="dp-chip dp-chip-out">' + TXT.toRetire + '</span>' + idchip(o.id) +
          shareChip(c.share) + '</div><div class="dp-title">' + esc(o.title) + '</div><div class="dp-body">' + c.html + '</div></div>';
      }).join('');
      modal.classList.add('open'); modal.setAttribute('aria-hidden', 'false');
      render();
      setTimeout(function () { field('m-body').focus(); }, 30);
    }, function () { BUSY = false; ERR = TXT.network; render(); });
  }
  function editFailed(text) { var e = field('m-err'); e.textContent = text; e.style.display = 'block'; field('m-submit').disabled = false; }
  form.addEventListener('submit', function (e) {
    e.preventDefault();
    var fd = new URLSearchParams(new FormData(form)), gid = editing;
    fd.set('csrf', CSRF);
    field('m-submit').disabled = true;
    fetch('/memories', { method: 'POST', headers: { 'X-Requested-With': 'XMLHttpRequest' }, body: fd })
      .then(function (r) { return r.json(); })
      .then(function (j) {
        if (!j.ok) { editFailed(j.error === 'CSRF' ? TXT.csrf : (j.error || TXT.failed)); return; }
        closeEdit();
        ERR = ''; NOTE = j.warning || '';
        load(function () {
          if (byId(gid)) S.sel = gid; else NOTE = [TXT.goneAfterEdit, NOTE].filter(Boolean).join(' ');
        });
      }, function () { editFailed(TXT.network); });
  });
  // closed by its own buttons or Escape, never by a click beside it: a half-moved paragraph is not lost to a stray click
  modal.addEventListener('click', function (e) { if (e.target.closest('[data-close]')) closeEdit(); });

  page.addEventListener('click', function (e) {
    var el = e.target.closest('[data-act]');
    if (!el || e.target.closest('a.idchip')) return;
    var act = el.dataset.act, box = el.closest('[data-g]'), g = box ? byId(box.dataset.g) : null;
    if (act === 'filter') {
      if (el.dataset.f === 'view') setView(el.dataset.val); else S[el.dataset.f] = el.dataset.val;
      ERR = ''; NOTE = ''; render();
    }
    else if (act === 'next') { onward(); ERR = ''; NOTE = ''; render(); }
    else if (act === 'skip') { if (step(1)) { ERR = ''; NOTE = ''; render(); } }
    else if (!g) return;
    else if (act === 'select') { S.sel = g.gid; ERR = ''; NOTE = ''; render(); }
    else if (act === 'tab') { g.tab = +el.dataset.i; render(); }
    else if (BUSY) return;
    else if (act === 'swap') { g.keep = el.dataset.id; g.tab = 0; render(); }
    else if (act === 'dismiss') dismiss(g);
    else if (act === 'retire') retire(g);
    else if (act === 'undo') undo(g);
    else if (act === 'edit') showEdit(g);
  });
  page.addEventListener('change', function (e) {
    if (e.target.id === 'dp-sort') { S.sorts[S.view] = e.target.value; render(); }
  });
  document.addEventListener('keydown', function (e) {
    if (editOpen()) { if (e.key === 'Escape') { e.preventDefault(); closeEdit(); } return; }
    var guide = document.getElementById('help-modal');
    if ((guide && guide.classList.contains('open')) || !READY || e.metaKey || e.ctrlKey || e.altKey ||
        /^(INPUT|SELECT|TEXTAREA)$/.test(e.target.tagName)) return;
    var k = (e.key || '').toLowerCase(), g = byId(S.sel);
    if (k === 'j' || k === 'k') { if (step(k === 'j' ? 1 : -1)) { ERR = ''; NOTE = ''; render(); } }
    else if (e.repeat || BUSY || !g || g.state !== 'open') return;
    else if (k === 'enter') { if (/^(A|BUTTON)$/.test(e.target.tagName)) return; retire(g); }
    else if (k === 'n') dismiss(g);
    else if (k === 's') { var o = others(g); g.keep = o[Math.min(g.tab, o.length - 1)].id; g.tab = 0; render(); }
    else if (k === 'e') showEdit(g);
    else return;
    e.preventDefault();
  });

  render();
  load(pickFirst);
})();
'''

_CITED_RE = re.compile(r"(?<![\w-])\d{8}-[A-Za-z0-9]{4,12}(?![\w-])")


def duplicates_payload():
    """What the Duplicates page shows, as data: the groups still to review and what was settled.

    One answer for the whole page, sent again after every action, so the page never has to work
    out what an action did to the rows around it: a retirement in favour of a memory that already
    kept another copy widens that row, and taking it back offers every one of its copies again.

    The pairs come from the table mem.sim_sync keeps. When the embedder has left vectors that the
    table has not compared yet, the answer is {"pending": how many are left}: the page says so and
    asks again, instead of holding one request open for as long as a first build takes."""
    waiting = mem.sim_sync(budget=1.5)[1]
    if waiting:
        return {"ok": True, "pending": waiting}
    records = mem.all_records()
    opened = mem.opened_counts()
    cited = {}
    for r in records:
        if r["meta"].get("status", "active") != "active":
            continue
        for i in set(_CITED_RE.findall(rec_summary(r) + "\n" + r["body"])):
            if i != r["id"]:
                cited[i] = cited.get(i, 0) + 1

    def member(r):
        summary, body = mem.emit_for(r, mem.DEST_LOCAL)
        body = (body or "").strip()
        return {"id": r["id"], "title": summary or "", "scope": r["meta"].get("scope", "global"),
                "created": r["meta"].get("created", ""), "chars": len(body),
                "opened": opened.get(r["id"], 0), "cited": cited.get(r["id"], 0),
                "files": mem._list_meta(r, "files"), "body": body,
                # what the store does not retire as somebody's copy: the page proposes it as the one that stays
                "pinned": r["meta"].get("priority") == "critical",
                "protected": r["meta"].get("protected", "").strip().lower() in ("true", "yes", "1")}

    groups = []
    for g in mem.duplicate_groups(records=records):
        groups.append({"gid": "open:" + min(r["id"] for r in g["members"]), "state": "open", "sim": g["sim"],
                       "type": g["type"], "members": [member(r) for r in g["members"]]})
    for g in mem.resolved_duplicates(records):
        if g["kind"] == "retired":   # the memory that was kept comes first, then what it replaced
            groups.append({"gid": "retired:" + g["keep"]["id"], "state": "retired", "sim": g["sim"],
                           "type": g["type"], "keep": g["keep"]["id"], "at": g["at"],
                           "members": [member(g["keep"])] + [member(r) for r in g["members"]]})
        else:
            groups.append({"gid": "distinct:" + g["ids"][0], "state": "distinct", "sim": g["sim"],
                           "type": g["type"], "ids": g["ids"], "members": [member(r) for r in g["members"]]})
    return {"ok": True, "threshold": mem.duplicates_threshold(), "vectors": embed_count(), "groups": groups}


def endpoint_duplicates(qs):
    return duplicates_payload()


# What the engine says when it refuses one of the page's actions. It says it in English, for the
# CLI; on the page the same reason is given in the page's language, with the id it names left as is.
_DUPS_REFUSALS = ("id not found", "it takes at least two memories to be distinct from one another",
                  "nothing to retire", "the memory to keep is not active", "not active, nothing to retire",
                  "cannot retire protected memory",
                  "cannot retire a critical rule in favour of a memory that is not one",
                  "the memory that was kept is no longer active",
                  "nothing is retired as a duplicate of")


def _dups_refusal(e):
    text = str(e)
    for known in _DUPS_REFUSALS:
        if text.startswith(known):
            return t(known, "dups") + text[len(known):]
    return text


def page_duplicates(qs=None):
    def T(s):
        return t(s, "dups")

    txt = {
        "title": t("Duplicates"), "dashboard": t("Dashboard"), "of": t("of"), "groups": T("groups"),
        "thousands": "." if lang() == "ro" else ",",
        "cardOpen": T("groups to review"), "cardCopies": T("extra copies"),
        "cardSame": T("identical or nearly identical text"), "cardDone": T("settled"),
        "cardThreshold": T("threshold, same type · Settings"),
        "marked": T("marked"), "legend": T("= words that do not appear in the memory that stays"),
        "fState": T("State"), "vOpen": T("to review"), "vDone": T("settled"), "fType": t("Type"), "all": T("all"),
        "fWhere": T("Where"), "anywhere": T("anywhere"), "sameProject": T("same project"),
        "crossProject": T("across projects"), "order": T("order"), "sortSim": T("most alike"),
        "sortUq": T("least to lose"), "sortNew": T("newest first"), "sortBig": T("largest groups"),
        "sortAt": T("latest settled"), "likeness": T("likeness"), "mem": [T("1 memory"), T("memories")],
        "apart": T("written {x} apart"), "sameSecond": T("written in the same second"),
        "units": {"s": [T("1 second"), T("seconds")], "m": [T("1 minute"), T("minutes")],
                  "h": [T("1 hour"), T("hours")], "d": [T("1 day"), T("days")], "mo": [T("1 month"), T("months")]},
        "stays": T("stays"), "toRetire": T("to retire"), "retired": T("retired"),
        "newest": T("newest"), "saysMost": T("says the most"),
        "identical": T("identical text"), "onlyHere": T("{n}% only here"),
        "onlyHereTitle": T("words that do not appear in the memory that stays"),
        "rTitle": T("Title"), "rWritten": T("Written"), "rSize": T("Size"),
        "chars": [T("1 character"), T("characters")], "rOpened": T("Opened by agents"), "never": T("never"),
        "times": [T("once"), T("times")], "timesPre": "de " if lang() == "ro" else "",
        "rCited": T("Cited by"), "rAnchors": T("Anchors"), "rText": T("Text"),
        "keepThis": T("Keep this one"), "notDups": T("Not duplicates"), "skip": T("Skip"),
        "editFirst": T("Edit first"), "retireN": [T("Retire the other"), T("Retire the other {n}")],
        "undo": T("Undo"), "listTitle": T("Groups"),
        "nOpen": [T("1 to review"), T("{n} to review")], "nDone": [T("1 settled"), T("{n} settled")],
        "retiredN": [T("1 retired"), T("{n} retired")], "leftBoth": T("both left"), "leftAll": T("all {n} left"),
        "noMatch": T("No group matches this filter."), "nextOpen": T("Next to review"),
        "comparing": T("Comparing memories… {n} left"), "loading": t("Loading..."),
        "noVectors": T("No vectors yet: likeness comes from the semantic index — run mem.py embed."),
        "noneOpen": T("No duplicates above the threshold."), "noneDone": T("Nothing settled yet."),
        "undoInexact": T("Active again. No note of this retirement is left, so the memory that stayed keeps "
                         "the links and anchors it was given."),
        "undoNoGroup": T("Active again, but no longer alike enough at this threshold to be offered as a group."),
        "goneAfterEdit": T("Saved. These memories are no longer offered as a group."),
        "pinnedTitle": t("critical rule: always injected, first, at SessionStart"),
        "protectedChip": T("protected"), "protectedTitle": T("a protected memory is not retired"),
        "editTitle": t("Edit"), "failed": t("Operation failed"), "network": t("Network error"),
        "csrf": t("Session expired (the server restarted) — reload the page and try again."),
    }
    # "Edit first": the form of the Memories page (same ids, so the same styles and the same
    # POST /memories), in a wider modal that has the copies' texts beside it.
    types = "".join(f"<option>{h(ty)}</option>" for ty in mem.TYPES)
    scopes = "".join(f'<option value="{h(s)}">' for s in known_scopes())
    modal = (
        f'<div class="modal-overlay" id="dp-edit" aria-hidden="true"><div class="modal dp-editmodal" role="dialog" '
        f'aria-modal="true" aria-labelledby="dp-edit-title">'
        f'<div class="modal-head"><h3 id="dp-edit-title"></h3>'
        f'<button type="button" class="modal-close" data-close aria-label="{h(t("close"))}">&times;</button></div>'
        f'<div class="dp-editgrid"><form id="mem-form"><input type="hidden" name="action" value="edit">'
        f'<input type="hidden" name="id" id="m-id" value="">'
        f'<div class="form-row"><label>{t("Type")} <select name="type" id="m-type">{types}</select></label>'
        f'<label>{t("Scope")} <small>{t("global or project:slug")}</small>'
        f'<input type="text" name="scope" id="m-scope" list="scopes"></label>'
        f'<label>Confidence <input type="text" name="confidence" id="m-confidence"></label></div>'
        f'<label>Summary <input type="text" name="summary" id="m-summary" required></label>'
        f'<label class="dp-editbody">Body <textarea name="body" id="m-body" required></textarea></label>'
        f'<div class="modal-err" id="m-err" style="display:none"></div>'
        f'<div class="form-actions"><button class="btn btn-primary" id="m-submit" type="submit">{t("Save")}</button>'
        f'<button type="button" class="btn btn-ghost" data-close>{t("Cancel")}</button></div></form>'
        f'<div class="dp-editside" id="dp-edit-side"></div></div></div></div>'
        f'<datalist id="scopes">{scopes}</datalist>')
    if lang() == "ro":
        guide = (
            '<p>Memorii active de acelasi tip care spun acelasi lucru. Asemanarea vine din vectorii indexului '
            'semantic, iar pragul se schimba din Setari. Digest-urile nu apar aici, fiindca isi repeta sursele '
            'intentionat.</p><h4>Actiuni</h4>'
            '<p><b>Retrage</b>: copia devine superseded in favoarea celei care ramane, cu textul ei intreg, iar '
            'legaturile si ancorele ei trec la cea care ramane. O gasesti apoi la &bdquo;rezolvate&rdquo; si, din '
            'memoria pastrata, prin &bdquo;Vezi lantul&rdquo;.</p>'
            '<p>O regula critica nu se retrage in favoarea unei memorii care nu e regula critica: ramane ea, ori ii '
            'scoti intai marcajul cu <code>mem.py unpin</code>. La un grup intre proiecte, memoria retrasa iese din '
            'listele proiectului ei: daca trebuie sa ramana in amandoua, alege &bdquo;Nu sunt dubluri&rdquo; ori muta '
            'intai memoria pastrata pe <code>global</code>, din &bdquo;Editeaza intai&rdquo;.</p>'
            '<p><b>Nu sunt dubluri</b>: se noteaza pe memoriile insesi, iar grupul nu mai apare.</p>'
            '<p><b>Editeaza intai</b>: deschide memoria care ramane in editor, cu textul copiei alaturi, ca sa muti '
            'ce merita inainte sa retragi.</p>'
            '<p><b>Anuleaza</b>: orice rand rezolvat se ia inapoi, si peste o saptamana, din &bdquo;rezolvate&rdquo;. '
            'O retragere se anuleaza cat timp memoria pastrata e activa. Ce a primit ea atunci se da inapoi exact '
            'cat timp nota retragerii mai exista in <code>staging/</code>; fara nota, copiile revin la fel, iar '
            'memoria pastrata ramane cu ce a primit.</p><h4>Taste</h4>'
            '<p><b>J</b> / <b>K</b> urmatorul / precedentul &middot; <b>S</b> schimba care ramane &middot; '
            '<b>Enter</b> retrage &middot; <b>N</b> nu sunt dubluri &middot; <b>E</b> editeaza intai</p>')
    else:
        guide = (
            '<p>Active memories of one type that say the same thing. Likeness comes from the vectors of the '
            'semantic index, and the threshold is changed from Settings. Digests are left out: they repeat their '
            'sources on purpose.</p><h4>Actions</h4>'
            '<p><b>Retire</b>: the copy is superseded by the memory that stays, with its whole text, and its links '
            'and anchors move to the one that stays. It is then found under &ldquo;settled&rdquo; and, from the '
            'kept memory, through &ldquo;View chain&rdquo;.</p>'
            '<p>A critical rule is not retired in favour of a memory that is not one: it is the one that stays, or '
            'you unpin it first with <code>mem.py unpin</code>. In a group across projects, the retired memory leaves '
            'its project\'s lists: if both projects need it, choose &ldquo;Not duplicates&rdquo; or first move the '
            'kept memory to <code>global</code>, from &ldquo;Edit first&rdquo;.</p>'
            '<p><b>Not duplicates</b>: written on the memories themselves; the group is not offered again.</p>'
            '<p><b>Edit first</b>: opens the memory that stays in the editor, with the copy\'s text beside it, to '
            'move what is worth keeping before you retire.</p>'
            '<p><b>Undo</b>: any settled row can be taken back, a week later too, from &ldquo;settled&rdquo;. A '
            'retirement can be taken back while the kept memory is active. What that memory was given is returned '
            'exactly while the note of the retirement is still in <code>staging/</code>; without the note the '
            'copies come back all the same, and the kept memory keeps what it was given.</p><h4>Keys</h4>'
            '<p><b>J</b> / <b>K</b> next / previous &middot; <b>S</b> swap which one stays &middot; '
            '<b>Enter</b> retire &middot; <b>N</b> not duplicates &middot; <b>E</b> edit first</p>')
    aside = f'<aside class="help"><h3>{t("Duplicates")}</h3>{guide}</aside>'
    js = ("<script>\nvar CSRF = " + json.dumps(_CSRF) + ";\nvar LANG = " + json.dumps(lang()) + ";\nvar TXT = "
          + json.dumps(txt, ensure_ascii=False) + ";\n" + _DUP_JS_BODY + "</script>")
    return layout(t("Duplicates") + " — mem0ry4ai", "dups", '  <div id="dp-page"></div>', aside, modal + js)


ROUTES_HTML = {"/": page_index, "/projects": page_projects, "/project": page_project,
               "/inject": page_inject, "/git": page_git, "/queue": page_queue, "/links": page_links,
               "/memories": page_memories, "/claude-md": page_claudemd, "/about": page_about,
               "/settings": page_settings, "/working": page_working, "/duplicates": page_duplicates}
ROUTES_JSON = {"/poll": endpoint_poll, "/record": endpoint_record, "/duplicates/data": endpoint_duplicates}


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "mem0ry4ai"

    def log_message(self, *a):
        pass  # quiet

    def _remember_url(self, path, qs):
        """Keep the current path+query on the request context so a link can override ONE parameter
        instead of replacing the whole query string. The language switcher used a bare `?lang=xx`,
        which wiped every filter on the page — and on /project it wiped the ?slug that identifies
        the project. Any future 'same page, one thing different' link must use `self_url`."""
        _ctx.path = path
        _ctx.qs = {k: list(v) for k, v in qs.items()}

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
        self._remember_url(path, qs)
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
            _ctx.status = 200                     # a page may override it (e.g. an unknown project)
            body = ROUTES_HTML[path](qs)
            if body is None:
                return self._redirect("/")
            return self._send_html(body, getattr(_ctx, "status", 200))
        self._send_html(f"<h1>404</h1><p>{h(path)} — not found. <a href=\"/\">Dashboard</a></p>", 404)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        self._set_lang(urllib.parse.parse_qs(parsed.query))
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b""
        if parsed.path == "/api/propose":
            # Uniform machine-ingestion channel (Vercel eve 'channels' idea, kept local): any agent or
            # script POSTs JSON to PROPOSE a memory. It goes to the human REVIEW QUEUE, never straight to
            # the store — so it's the same low-trust path as the LLM extractor. CSRF-exempt on purpose
            # (a script has no browser token); safe because the server binds 127.0.0.1 and this only
            # touches the reviewed queue. Body redacted + injection-scanned before it lands.
            try:
                d = json.loads(raw or b"{}")
            except Exception:
                return self._send_json({"ok": False, "error": "invalid JSON body"})
            typ = (d.get("type") or "").strip()
            scope = (d.get("scope") or "global").strip()
            summary = (d.get("summary") or "").strip()
            body = (d.get("body") or "").strip()
            if typ not in mem.TYPES or not summary or not body:
                return self._send_json({"ok": False, "error": f"need type in ({'|'.join(mem.TYPES)}), summary, body"})
            if mem.redact.enabled():
                summary = mem.redact.redact(summary)[0]
                body = mem.redact.redact(body)[0]
            existing = queue_load()
            if any(r.get("status", "pending") == "pending" and r.get("type") == typ
                   and r.get("summary") == summary and r.get("body") == body for r in existing):
                return self._send_json({"ok": True, "qid": None, "status": "duplicate",
                                        "note": "an identical pending proposal is already queued"})
            inj = mem.redact.scan_injection(summary + "\n" + body)
            rec = {"qid": mem.gen_id(), "type": typ, "scope": scope, "summary": summary, "body": body,
                   "confidence": d.get("confidence", 0.7), "source": (d.get("source") or "http:propose"),
                   "transcript": None, "extracted_at": mem.now_ts(), "status": "pending"}
            if inj:
                rec["injection"] = inj
            queue_save(existing + [rec])
            return self._send_json({"ok": True, "qid": rec["qid"], "status": "pending",
                                    "review": "/queue", "injection_flag": inj or None})
        form = urllib.parse.parse_qs(raw.decode("utf-8")) if raw else {}
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
                    if not ok:
                        return self._send_json({"ok": False, "qid": qid,
                                                "error": t("already reviewed, or no such candidate"),
                                                "remaining": len(queue_pending())})
                elif action == "reject":
                    ok = queue_remove(qid)
                    if not ok:
                        return self._send_json({"ok": False, "qid": qid,
                                                "error": t("already reviewed, or no such candidate"),
                                                "remaining": len(queue_pending())})
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
            elif action == "threshold":
                # the threshold is read live from os.environ on every suggested_links() call, so this
                # takes effect on the next page render — no server reload. Also persist it for restarts.
                try:
                    v = max(0.5, min(0.95, float(form.get("value", [""])[0])))
                except ValueError:
                    return self._send_json({"ok": False, "error": "invalid"})
                os.environ["MEM_SUGGEST_THRESHOLD"] = f"{v:.2f}"
                set_local_env("MEM_SUGGEST_THRESHOLD", f"{v:.2f}")
                return self._send_json({"ok": True, "value": f"{v:.2f}"})
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
        if parsed.path == "/working":
            if not csrf_ok(form.get("csrf", [""])[0]):
                return self._send_json({"ok": False, "error": "CSRF"})
            action = form.get("action", [""])[0]
            rid = (form.get("id", [""])[0] or "").strip()
            try:
                if action == "promote":
                    res = mem.promote_memory(rid)
                    if res is None:
                        return self._send_json({"ok": False, "error": "not found"})
                    if res is False:
                        return self._send_json({"ok": False, "error": "not a working note"})
                    return self._send_json({"ok": True})
                if action == "dismiss":
                    # Same guard promote has three lines up. Without it this route supersedes ANY
                    # record whose id lands here — including one just promoted to durable — because
                    # supersede_memory does not care what it is retiring.
                    rec = mem.get_record(rid)
                    if not rec:
                        return self._send_json({"ok": False, "error": t("not found")})
                    if rec["meta"].get("status") != "working":
                        return self._send_json({"ok": False, "error": t("not a working note")})
                    rel = mem.supersede_memory(rid, "", "dismissed working note")
                    return self._send_json({"ok": rel is not None,
                                            **({} if rel is not None else {"error": t("not found")})})
                return self._send_json({"ok": False, "error": "unknown action"})
            except Exception as e:
                return self._send_json({"ok": False, "error": str(e)})
        if parsed.path == "/settings":
            if not csrf_ok(form.get("csrf", [""])[0]):
                return self._send_json({"ok": False, "error": "CSRF"})
            action = form.get("action", [""])[0]
            if action == "reset":   # clear every override -> in-code defaults govern again
                file_keys = _local_env_keys()
                if not _write_local_env({}, removes=[s["key"] for s in SETTINGS]):
                    return self._send_json({"ok": False, "error": t(
                        "could not write .mem-local.env (permissions? disk full?) — nothing was reset")})
                for s in SETTINGS:
                    if s["key"] in file_keys:        # only clear keys we actually had in the file —
                        os.environ.pop(s["key"], None)  # don't nuke a genuine shell export
                return self._send_json({"ok": True})
            if action != "save":
                return self._send_json({"ok": False, "error": "unknown action"})
            # validate EVERYTHING first; write nothing if any field is bad (atomic save)
            vals, errors, restart, clamped = {}, [], [], []
            for s in SETTINGS:
                if s["key"] not in form:
                    continue
                raw = form.get(s["key"], [""])[0]
                val, err = validate_setting(s, raw)
                # A value pulled into range is not the value the user typed. Saying nothing here is
                # how "I set it to 99999" becomes "…and it quietly became 10".
                if not err and val is not None and raw.strip() and val != raw.strip():
                    clamped.append(f'{s["key"]}: {raw.strip()} -> {val}')
                if err:
                    errors.append(f'{s["key"]}: {err}')
                elif val != os.environ.get(s["key"], s["default"]):
                    vals[s["key"]] = val
                    if s["apply"] == "restart web server":
                        restart.append(s["key"])
            if errors:
                return self._send_json({"ok": False, "error": "; ".join(errors)})
            # Report the write, do not assume it. _write_local_env swallows OSError and returns
            # False; discarding that told the user "saved" while only THIS process changed, so the
            # CLI, the MCP server and the next restart all kept reading the old value.
            if vals and not _write_local_env(vals):
                return self._send_json({"ok": False, "error": t(
                    "could not write .mem-local.env (permissions? disk full?) — nothing was saved")})
            for k, v in vals.items():
                os.environ[k] = v                   # live for web-read knobs + the injection preview
            note = (t("restart the web server to apply") + ": " + ", ".join(restart)) if restart else ""
            if clamped:
                note = ((note + " · ") if note else "") + t("adjusted to the allowed range") + ": " + "; ".join(clamped)
            # echo the effective stored values so the client reflects any clamp (e.g. 999999 -> 32000)
            return self._send_json({"ok": True, "saved": len(vals), "values": vals, "note": note})
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
            if action in ("add", "edit"):   # same injection flag the CLI/MCP write paths surface
                inj = mem.redact.scan_injection(form.get("summary", [""])[0] + "\n" + form.get("body", [""])[0])
                if inj:
                    resp["warning"] = ("Prompt-injection-like phrasing (" + ", ".join(inj)
                                       + "). Memories are injected into context — double-check it is legitimate.")
            if action in ("add", "edit", "supersede", "rescope"):
                rec = next((r for r in mem.all_records() if r["id"] == rid), None)
                if rec:
                    resp["html"] = render_row(rec)
                    resp["status"] = rec["meta"].get("status", "active")
                    resp["scope"] = rec["meta"].get("scope", "")
            return self._send_json(resp)
        if parsed.path == "/duplicates":
            if not csrf_ok(form.get("csrf", [""])[0]):
                return self._send_json({"ok": False, "error": "CSRF"})
            action = form.get("action", [""])[0]
            keep = (form.get("keep", [""])[0] or "").strip()
            ids = [i.strip() for i in (form.get("ids", [""])[0] or "").split(",") if i.strip()]
            if not all(_ID_RE.match(i) for i in ids + [keep] if i):
                return self._send_json({"ok": False, "error": "invalid id"})
            resp = {"ok": True}
            try:
                if action == "retire":        # `ids` are retired as copies of `keep`
                    resp["retired"] = mem.retire_duplicates(keep, ids)
                elif action == "distinct":    # `ids` are not duplicates of one another
                    mem.mark_distinct(ids)
                elif action == "restore":     # every copy retired in favour of `keep` is active again
                    resp["restored"], resp["exact"] = mem.restore_duplicates(keep)
                elif action == "alike":       # "not duplicates" is taken back for `ids`
                    mem.unmark_distinct(ids)
                else:
                    return self._send_json({"ok": False, "error": "unknown action"})
            except ValueError as e:
                # refused, with the store as it was -- usually because it changed under a page left
                # open. The reason goes back with what is true now, so the page stops showing the rest.
                resp = {"ok": False, "error": _dups_refusal(e)}
            except Exception as e:
                return self._send_json({"ok": False, "error": str(e)})
            resp["data"] = duplicates_payload()
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


def _write_local_env(updates, removes=()):
    """One atomic, locked read-modify-write of .mem-local.env: apply `updates` (key->value, replacing in
    place or appending) and drop every key in `removes`. Temp-file + os.replace = never a torn file;
    mem._locked() serializes concurrent web threads (no lost update). An 'export ' prefix the user
    hand-wrote on a replaced line is preserved."""
    p = mem.LOCAL_ENV
    removes = set(removes)
    try:
        with mem._locked():
            try:
                with open(p, encoding="utf-8") as f:
                    existing = f.readlines()
            except OSError:
                existing = []
            out, seen = [], set()
            for line in existing:
                s = line.strip()
                exp = s.startswith("export ")
                base = s[7:].strip() if exp else s
                if base and not base.startswith("#") and "=" in base:
                    k = base.partition("=")[0].strip()
                    if k in removes:
                        continue
                    if k in updates:
                        out.append(f'{"export " if exp else ""}{k}={updates[k]}\n')
                        seen.add(k)
                        continue
                out.append(line if line.endswith("\n") else line + "\n")
            for k, v in updates.items():
                if k not in seen:
                    out.append(f"{k}={v}\n")
            tmp = p + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.writelines(out)
            os.replace(tmp, p)
        return True
    except OSError:
        return False


def set_local_env(key, value):
    """Persist a single KEY=value in .mem-local.env (atomic). Used by the /links threshold control."""
    return _write_local_env({key: value})


def del_local_env(key):
    """Remove KEY's line from .mem-local.env so the in-code default governs again (reset-to-default)."""
    return _write_local_env({}, removes=(key,))


def _load_local_env():
    """Per-machine overrides from the gitignored .mem-local.env next to the code. Single source of
    truth = mem.load_local_env (already run when mem was imported); re-run here is idempotent."""
    mem.load_local_env()


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


def _write_pid_file():
    """Record which process is the server, so uninstall.py can find it: it runs detached, and
    nothing else knows. Best-effort — a data dir that cannot be written to must not keep the UI
    from starting."""
    path = os.path.join(mem.DATA, ".web-server.pid")
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"{os.getpid()}\n")
        return path
    except OSError:
        return None


def _remove_pid_file(path):
    """Take the pid file away on the way out — unless another server has claimed it since, in which
    case it is that server's to remove. A file left behind names a pid the system will hand to
    some other process sooner or later."""
    if not path:                            # never written
        return
    try:
        with open(path, encoding="utf-8") as f:
            mine = f.read().strip() == str(os.getpid())
        if mine:
            os.remove(path)
    except OSError:
        pass


def _exit_cleanly_on_signals():
    """SIGTERM is how a detached server gets stopped, and its default action skips every cleanup
    Python has. Turn it into an ordinary exit so serve() unwinds. A signal the launcher asked to
    have ignored (nohup does that to SIGHUP) stays ignored."""
    def bye(signum, frame):
        raise SystemExit(0)

    for name in ("SIGTERM", "SIGHUP"):
        sig = getattr(signal, name, None)
        try:
            if sig is not None and signal.getsignal(sig) == signal.SIG_DFL:
                signal.signal(sig, bye)
        except (ValueError, OSError):       # not the main thread
            pass


def serve(host=None, port=None):
    _load_local_env()
    # Loopback by default: the UI has no authentication, so binding it to the network is a decision
    # the operator makes on purpose, never one they inherit.
    host = host or os.environ.get("MEM_WEB_HOST", "127.0.0.1")
    port = int(port or os.environ.get("MEM_WEB_PORT", "8841"))
    httpd = Server((host, port), Handler)
    reload_on = os.environ.get("MEM_NO_RELOAD", "").strip().lower() not in ("1", "true", "yes")
    if reload_on:
        _reload_watcher()
    # flush: stdout is block-buffered when it is not a terminal, so under a service manager --
    # `docker logs`, launchd, systemd -- this line otherwise never appears and there is no way to
    # tell a server that started from one that hung.
    print(f"mem0ry4ai web UI on http://{host}:{port}/  (data: {mem.DATA})"
          + ("  · auto-reload on" if reload_on else ""), flush=True)
    _exit_cleanly_on_signals()
    # Only now that the port is ours: a second `serve` that lost the race for it fails above, and
    # must not replace the pid of the server that is actually listening.
    pid_file = _write_pid_file()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        _remove_pid_file(pid_file)


if __name__ == "__main__":
    serve(port=sys.argv[1] if len(sys.argv) > 1 else None)
