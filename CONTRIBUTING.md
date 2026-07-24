# Contributing to mem0ry4ai

Thanks for your interest. mem0ry4ai is a small, focused project — keep contributions in the same spirit.

## License

mem0ry4ai is **GPL-2.0-or-later**. By submitting a PR you agree your contribution is licensed under the same terms.

If you copy code from elsewhere, it must be GPL-compatible (MIT, BSD, Apache-2.0, public domain). Credit it in the Acknowledgements section of the README.

## The one hard rule: stdlib only

The whole point of this project is that it installs by cloning it. **No `pip install`, no SDK, no
vendored dependency** — Python 3.9+ standard library, and that is it. Optional integrations (Ollama
for embeddings, a local LLM for consolidation) must degrade silently to a working keyword-only setup
when they are absent. A PR that adds a runtime dependency will be declined regardless of how good
the feature is; there is almost always a stdlib way, and if there genuinely isn't, open an issue
first so we can discuss whether the feature is worth the exception.

The corollary: the markdown store is the source of truth. Anything in SQLite is a derived index that
must be safe to delete and regenerate.

## Issues

Use the issue templates. Before opening:

- Search existing issues (open + closed).
- Reproduce on a clean clone at the latest release.
- Include: OS and Python version (`python3 -V`), what you ran, the output, expected vs actual.

Redact your own store contents before pasting — memories tend to contain things you did not mean to
share.

## Pull Requests

1. Open an issue first for non-trivial changes — saves wasted work if the direction isn't right.
2. One logical change per PR. Don't mix refactors with feature additions.
3. Match the existing style: 4-space indent, comments in English, no emoji in code or output.
   Comments explain *why*, not *what* — the code already says what.
4. Run the guard suite before submitting (see below). If you touch a rule it covers, run the canary too.
5. Include an SPDX header in new files:
   ```python
   # SPDX-License-Identifier: GPL-2.0-or-later
   ```

## Running it from a clone

```bash
git clone https://github.com/cremenescu/mem0ry4ai.git
cd mem0ry4ai
python3 mem.py list                # the CLI works immediately, no build step
python3 mem.py serve               # web UI on http://127.0.0.1:8841
python3 mem.py mcp                 # MCP server (stdio)
```

Point `MEM_DATA_DIR` at a scratch directory while developing so you never test against your real
store:

```bash
export MEM_DATA_DIR=/tmp/mem0ry4ai-dev
git init "$MEM_DATA_DIR"
```

## Tests

```bash
python3 tests/test_guards.py             # 34 checks, isolated store, ~30s
python3 tests/test_guards.py --canary    # removes each guard in turn; the suite must notice
tools/bench_recall.py                    # retrieval quality: Recall@k + MRR, no LLM judge
```

If you add a guard for a rule that must hold, add it to `MUTATIONS` in `tests/test_guards.py` as
well. A check that still passes when the thing it checks is deleted reports green and protects
nothing — the canary exists to catch exactly that, and it has already caught one of ours.

If you change ranking, run `tools/bench_recall.py` before and after. Note the caveat in its
docstring: the bundled fixture is small enough to measure ordering rather than discrimination, so
decide parameters with `--store` against a store of realistic size.

## Areas that need help

- **Windows**: the code paths exist and are guarded, but they get far less real-world use than
  macOS and Linux. Bug reports from actual Windows use are valuable.
- **Redaction patterns**: `redact.py` is keyword-driven and therefore misses formats it was not
  told about (high-entropy tokens, credentials embedded in URLs). A format/entropy fallback is a
  well-scoped, self-contained contribution.
- **Client coverage**: the MCP server is hand-rolled against the spec. Reports of what does or does
  not work in a client we have not tried are useful, especially around resources and prompts.
- **Translations**: the web UI is EN/RO. The string table is in one place.

## What's out of scope

- **Runtime dependencies** — see above.
- **A hosted or multi-user service.** This is a local-first tool for one person's machine. Sync
  between *your own* machines is a legitimate feature request; a server other people log into is a
  different product.
- **Sending memories anywhere by default.** Nothing may phone home, and no feature may make the
  store leave the machine without the user explicitly asking for it.
- **Auto-deleting memories.** Superseding keeps history; deleting throws away the "what did we
  believe, and when" trail that is the reason for a git-backed store.

## Questions

Open a discussion or email [razvan@cremenescu.ro](mailto:razvan@cremenescu.ro).
