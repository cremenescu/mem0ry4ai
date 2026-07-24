#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Reproducible recall benchmark — does the ranking actually find the right memory?

Every ranking change so far (bge-m3 over all-minilm, the RRF fusion, the summary rerank) was argued
from a handful of hand-run queries. This turns that into a number anyone can reproduce: seed a
throwaway store with fixture memories, ask paraphrased questions whose answer is known, and measure
where the intended record lands.

    tools/bench_recall.py                      # fixture store, keyword + hybrid
    tools/bench_recall.py --no-semantic        # keyword only (no embedder needed)
    tools/bench_recall.py --fixture mine.json --store ~/.mem0ry4ai   # your own store, no seeding

The shape of this — an in-repo fixture, Recall@k, and deliberately no LLM judge — follows mnema's
recall benchmarks (https://github.com/MerlijnW70/mnema).

Reported: Recall@1, Recall@5, Recall@10 and MRR over every query. No LLM judge anywhere — the
expected id is written down in the fixture, so the number cannot drift with a model's mood.
(Spelled out on purpose: the compact "@1/@5/@10" form turns into @mentions of unrelated GitHub
users the moment it lands in a release note or an issue.)

CAVEAT, learned the hard way: the in-repo fixture holds 14 memories, so every target is already in
the candidate set and the benchmark measures ORDERING, not discrimination. Sweeping the dense
retriever's weight on it showed a clean monotone win (R@1 0.785 -> 0.896) that did not exist on a
real 799-record store (flat at 0.55, no trend). Use it to catch regressions; decide ranking
parameters with --fixture/--store against a store of realistic size.

Fixture format (a list): {"summary", "body", "type", "scope", "queries": [...]}, where each query
must retrieve THAT memory. With --fixture + --store, use {"expect": "<record id>", "queries": [...]}
instead, and nothing is seeded or written.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)
FIXTURE = os.path.join(HERE, "bench_fixture.json")


def _load_mem(data_dir):
    """Import mem.py with MEM_DATA_DIR already set — it resolves the store at import time."""
    os.environ["MEM_DATA_DIR"] = data_dir
    sys.path.insert(0, PROJ)
    for mod in ("mem", "llm", "redact"):
        sys.modules.pop(mod, None)
    import mem  # noqa: E402
    return mem


def seed(mem, entries):
    """Write the fixture memories into the (empty) store and return query -> expected id."""
    expect = {}
    for e in entries:
        rid = mem.add_memory(e.get("type", "gotcha"), e.get("scope", "project:bench"),
                             e["summary"], e["body"], source="bench", redact_secrets=False)
        for q in e["queries"]:
            expect[q] = rid
    return expect


def rank_of(mem, query, target, semantic):
    """1-based position of `target` in the ranking for `query`; None if absent."""
    ids, mode = mem.hybrid_search(query, allow_semantic=semantic)
    if ids is None:
        return None, "substring"
    ids = [i for i in ids]
    return (ids.index(target) + 1 if target in ids else None), mode


def measure(mem, expect, semantic):
    ranks, modes = [], set()
    for q, target in expect.items():
        r, mode = rank_of(mem, q, target, semantic)
        ranks.append((q, target, r))
        modes.add(mode)
    n = len(ranks) or 1
    at = lambda k: sum(1 for _, _, r in ranks if r is not None and r <= k) / n
    mrr = sum((1.0 / r) for _, _, r in ranks if r) / n
    return {"n": len(ranks), "r@1": at(1), "r@5": at(5), "r@10": at(10), "mrr": mrr,
            "mode": "+".join(sorted(modes)), "ranks": ranks}


def report(title, m, verbose):
    print(f"\n{title}  ({m['n']} queries, mode: {m['mode']})")
    print(f"  Recall@1  {m['r@1']:.3f}")
    print(f"  Recall@5  {m['r@5']:.3f}")
    print(f"  Recall@10 {m['r@10']:.3f}")
    print(f"  MRR       {m['mrr']:.3f}")
    missed = [(q, r) for q, _, r in m["ranks"] if r is None or r > 5]
    if missed and verbose:
        print(f"  outside top-5 ({len(missed)}):")
        for q, r in missed:
            print(f"    rank {r if r else '-':>3}  {q}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--fixture", default=FIXTURE, help="fixture JSON (default: the in-repo one)")
    ap.add_argument("--store", help="run against an existing store instead of seeding a temp one "
                                    "(the fixture must then carry `expect` ids)")
    ap.add_argument("--no-semantic", action="store_true", help="keyword only; skip the embedder run")
    ap.add_argument("--quiet", action="store_true", help="scores only, no per-query misses")
    ap.add_argument("--min-mrr", type=float, help="exit 1 if the best MRR is below this (CI gate)")
    a = ap.parse_args()

    with open(a.fixture, encoding="utf-8") as f:
        entries = json.load(f)

    tmp = None
    try:
        if a.store:
            mem = _load_mem(os.path.abspath(os.path.expanduser(a.store)))
            expect = {q: e["expect"] for e in entries for q in e["queries"] if e.get("expect")}
            if not expect:
                sys.exit("--store needs a fixture with `expect` ids (see the module docstring)")
        else:
            tmp = tempfile.mkdtemp(prefix="mem-bench-")
            subprocess.run(["git", "init", "-q", tmp], check=True)
            subprocess.run(["git", "-C", tmp, "config", "commit.gpgsign", "false"], check=True)
            mem = _load_mem(tmp)
            expect = seed(mem, entries)
        mem.build_index()

        kw = measure(mem, expect, semantic=False)
        report("keyword only (FTS5 + recency)", kw, not a.quiet)
        best = kw["mrr"]

        if not a.no_semantic:
            r = mem.embed_index()
            if r is None:
                print("\nhybrid: skipped — no embedder (start Ollama, or pass --no-semantic)")
            else:
                hy = measure(mem, expect, semantic=True)
                report(f"hybrid (+ {os.environ.get('MEM_EMBED_MODEL', 'bge-m3')})", hy, not a.quiet)
                best = max(best, hy["mrr"])
                d = hy["r@1"] - kw["r@1"]
                print(f"\nsemantic delta: Recall@1 {d:+.3f}, MRR {hy['mrr'] - kw['mrr']:+.3f}")

        if a.min_mrr is not None and best < a.min_mrr:
            sys.exit(f"\nFAIL: best MRR {best:.3f} < --min-mrr {a.min_mrr}")
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
