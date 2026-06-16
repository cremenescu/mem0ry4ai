# mem0ry4ai

Memorie persistenta, local-first, pentru agenti de cod — construita pentru [Claude Code](https://claude.com/claude-code).

**Pagina proiectului:** [cremenescu.ro/ro/mem0ry4ai](https://cremenescu.ro/ro/mem0ry4ai/)

Agentul uita totul intre sesiuni. mem0ry4ai rezolva asta: capteaza cunostinte durabile (capcane,
decizii, fapte, comenzi, preferinte, todos, status de proiect), le stocheaza in **markdown simplu
versionat cu git** si **injecteaza automat felia relevanta** la inceputul fiecarei sesiuni —
scoped pe proiectul in care lucrezi.

Documentatia completa: [README.md](README.md) (engleza).

## Pornire rapida

Cerinte: **Python 3.9+ si git — atat.** Fara PHP, fara Docker, fara baza de date vectoriala, fara
chei API, fara `pip install`. CLI-ul, hook-urile si web UI-ul sunt toate Python stdlib pur, deci
ruleaza la fel pe **macOS, Linux si Windows** (nativ, fara WSL — pe Windows: `py mem.py serve`).

```bash
# Varianta plugin (o comanda; datele traiesc in ~/.mem0ry4ai, repo git propriu)
claude plugin marketplace add cremenescu/mem0ry4ai
claude plugin install mem0ry4ai@mem0ry4ai

# SAU git clone (datele raman in clona ta)
git clone https://github.com/cremenescu/mem0ry4ai.git
cd mem0ry4ai

# 1. CLI
./mem.py add --type gotcha --scope global --summary "..." --body "..."
./mem.py list
./mem.py search "..."             # FTS5 ranked + nudge pe recenta
./mem.py search "..." --since 2026-05-01
./mem.py audit                    # raport secrete in store (read-only, nu modifica)
./mem.py embed                    # optional: vectori pt cautare semantica (necesita Ollama + all-minilm)
./mem.py resume --scope project:x # briefing "unde am ramas": status + todo-uri ready + recente

# 2. Web UI (server Python stdlib — fara PHP, fara Apache)
./mem.py serve                     # -> http://127.0.0.1:8841/  (Windows: py mem.py serve)

# 3. Integrarea cu Claude Code (hooks)
python3 hooks/install.py --target user
# reporneste Claude Code (sau /clear)
```

## Principii

- **Markdown + git = sursa de adevar.** Indexul SQLite FTS5 e derivat si regenerabil.
- **Supersedare bi-temporala, nu stergere.** Faptele vechi raman in istoric; supersedarea retine
  separat **cand** au incetat sa fie valabile (`invalidated`) si **de ce** (`invalid-reason`),
  distinct de `created` (valabil-de-la). Istoric „ce-am crezut si cand", nu doar o piatra de mormant.
- **Gate pe incredere.** Agentul cu context complet scrie direct; extractia batch cu LLM local
  (optionala, prin Ollama) trece printr-o coada de review umana — modelele mici sunt zgomotoase
  si supra-increzatoare (masurat).
- **Coexista cu CLAUDE.md** — nu atinge fisierele tale, namespace propriu.
- **Redactare de secrete pe orice cale de scriere** (chei API, token-uri, parole, chei private)
  + `mem.py audit` pentru ce e deja in store. Dezactivabil cu `--no-redact` / `MEM_REDACT=0`.
- **Reguli critice + buget de injectare**: `mem.py pin <id>` marcheaza regulile care conditioneaza
  actiunile agentului — intra GARANTAT, primele, cu tot cu body; restul umple `MEM_INJECT_BUDGET`
  (default 8000 bytes), iar orice taietura e anuntata explicit. Injectarea se taie singura,
  determinist — niciodata harness-ul, orb.
- Tipurile **`todo`** si **`status`** raspund la „unde am ramas?" cand revii la un proiect; tipul
  **`procedural`** tine runbook-uri reutilizabile (pasi de release, o procedura de recuperare).
  Campul optional **`files`** leaga o memorie de fisierele pe care le priveste (indexat + chips in UI).
- **Relatii intre memorii**: `mem.py link` (related-to, bidirectional in UI) si `mem.py block` +
  `mem.py ready` pentru todo-uri (ce poti ataca acum, fara blocaj deschis). Legate deliberat, nu automat.
  Pagina **Legaturi** arata **sugestii semantice** (cele mai apropiate perechi nelegate, confirmate
  sau respinse de tine) deasupra unui graf force-directed (SVG, fara dependinte) + lista.
- **Cautare hibrida (optionala).** Implicit keyword-first (FTS5 + nudge pe recenta), zero dependinte.
  Cu Ollama + un embedder mic (`all-minilm`), cautarea fuzioneaza scorul keyword cu similaritate
  cosinus pe vectori locali — „auth token expiry" gaseste „JWT TTL", si scoate la suprafata potriviri
  semantice chiar cand nimic nu prinde keyword. Embedder-ul e DOAR pentru regasire (nu decide, nu scrie
  -> nu atinge gate-ul de incredere); fara Ollama, fallback tacut la keyword. Fara bifa de tinut minte:
  pagina Memorii **auto-detecteaza** embedder-ul si arata un beculet — **verde** = LLM-ul local e pornit
  (keyword + semantic), **gri** = revine la cautare clasica. `mem.py search` afiseaza modul folosit;
  `--no-semantic` forteaza keyword.
- **Anti-duplicare.** `mem.py add` **avertizeaza (nu blocheaza)** cand exista deja o memorie de acelasi
  tip foarte similara — arata cele mai apropiate + comanda `supersede` gata de rulat — ca sa contopesti
  in loc sa dublezi. Memoriile noi se **embededeaza + commit-uiesc automat la fiecare granita** (final de
  sesiune SI inainte de compactare), deci nimic nu se pierde cand contextul se comprima fara preaviz, iar
  cautarea/sugestiile raman la zi fara `mem.py embed` manual. (`MEM_DUP_CHECK=0` dezactiveaza dup-warning-ul.)
- **Web UI** = dashboard (status sistem) + Memorii (lista filtrabila) + Proiecte (sumar per proiect)
  + Legaturi (graf), navigare consistenta + breadcrumb pe toate paginile.

## Licenta

GPL-2.0-or-later — vezi [LICENSE](LICENSE).
