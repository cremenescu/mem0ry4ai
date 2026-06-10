# mem0ry4ai

Memorie persistenta, local-first, pentru agenti de cod — construita pentru [Claude Code](https://claude.com/claude-code).

Agentul uita totul intre sesiuni. mem0ry4ai rezolva asta: capteaza cunostinte durabile (capcane,
decizii, fapte, comenzi, preferinte, todos, status de proiect), le stocheaza in **markdown simplu
versionat cu git** si **injecteaza automat felia relevanta** la inceputul fiecarei sesiuni —
scoped pe proiectul in care lucrezi.

Documentatia completa: [README.md](README.md) (engleza).

## Pornire rapida

Cerinte: Python 3.9+, PHP 8+ (pentru web UI), git. Zero alte dependinte — fara Docker, fara
baza de date vectoriala, fara chei API.

```bash
git clone https://github.com/cremenescu/mem0ry4ai.git
cd mem0ry4ai

# 1. CLI
./mem.py add --type gotcha --scope global --summary "..." --body "..."
./mem.py list
./mem.py search "..."             # FTS5 ranked

# 2. Web UI (server propriu, fara Apache)
./server_web.sh                    # -> http://127.0.0.1:8841/

# 3. Integrarea cu Claude Code (hooks)
python3 hooks/install.py --target user
# reporneste Claude Code (sau /clear)
```

## Principii

- **Markdown + git = sursa de adevar.** Indexul SQLite FTS5 e derivat si regenerabil.
- **Supersedare, nu stergere.** Faptele vechi raman in istoric; git pastreaza tot.
- **Gate pe incredere.** Agentul cu context complet scrie direct; extractia batch cu LLM local
  (optionala, prin Ollama) trece printr-o coada de review umana — modelele mici sunt zgomotoase
  si supra-increzatoare (masurat).
- **Coexista cu CLAUDE.md** — nu atinge fisierele tale, namespace propriu.
- Tipurile **`todo`** si **`status`** raspund la „unde am ramas?" cand revii la un proiect.

## Licenta

GPL-2.0-or-later — vezi [LICENSE](LICENSE).
