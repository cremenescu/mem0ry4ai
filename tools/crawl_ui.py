#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Mechanical sweep of the whole web UI: every reachable page, every link, every status code.

Deterministic on purpose — this is the half of a UI audit that does not need judgment, so it should
never cost an LLM call. Crawls from the known pages, follows every internal link, and reports:
non-200 responses, links pointing at them, whether stateful pages keep their query parameters
through the language switcher, and what a missing resource actually returns.

    tools/crawl_ui.py http://127.0.0.1:8841

Run it after touching mem_web.py. The parameter-preservation section exists because that exact
class of bug shipped three times: a link that rebuilds the query from a hand-listed set of keys
forgets whichever one was added last.
"""
import re
import sys
import urllib.parse
import urllib.request
import collections

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8888"
SEEDS = ["/", "/memories", "/projects", "/links", "/git", "/inject", "/claude-md",
         "/about", "/settings", "/working", "/queue"]

def get(url):
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception as e:
        return 0, f"ERR {e}"

seen, queue, results = set(), list(SEEDS), {}
links_from = collections.defaultdict(set)
while queue:
    path = queue.pop(0)
    if path in seen or len(seen) > 120:
        continue
    seen.add(path)
    code, html = get(BASE + path)
    results[path] = code
    if code != 200:
        continue
    for href in re.findall(r'href="([^"]+)"', html):
        href = href.replace("&amp;", "&")
        if href.startswith(("http", "mailto:", "#", "javascript:", "data:")):
            continue
        if not href.startswith("/"):
            href = "/" + href.lstrip("./")     # relative asset refs resolve against the root here
        links_from[path].add(href)
        p = urllib.parse.urlparse(href).path
        if p.startswith("/assets/"):
            continue
        # queue distinct paths, and one representative per parameterised link
        key = href if len(href) < 90 else p
        if key not in seen:
            queue.append(key)

print(f"=== {len(results)} URL-uri atinse ===")
bad = {p: c for p, c in results.items() if c != 200}
for p, c in sorted(bad.items()):
    print(f"  {c}  {p}")
print(f"  {len(results) - len(bad)} OK, {len(bad)} non-200")

print("\n=== linkuri catre pagini care nu raspund 200 ===")
broken = 0
for src, hrefs in sorted(links_from.items()):
    for hr in sorted(hrefs):
        key = hr if len(hr) < 90 else urllib.parse.urlparse(hr).path
        if results.get(key, 200) not in (200, None):
            print(f"  {src} -> {hr}  [{results.get(key)}]")
            broken += 1
print(f"  {broken} link-uri rupte")

print("\n=== pastrarea parametrilor: paginile cu stare, prin comutatorul de limba ===")
# Discovered from the running store, not hardcoded: a fixed id or slug makes this tool report a
# false failure against any store but the author's — including the public seed store.
_, _idx = get(BASE + "/memories?status=active")
_ids = re.findall(r"/memories\?id=(\d{8}-[A-Za-z0-9]+)", _idx)
_, _projidx = get(BASE + "/projects")
_slugs = re.findall(r"/project\?slug=([A-Za-z0-9._-]+)", _projidx)
STATEFUL = ["/memories?drift=1", "/memories?scope=global&type=gotcha&status=all",
            "/memories?q=pool", "/claude-md?scope=global", "/git", "/queue", "/working"]
if _ids:
    STATEFUL.append(f"/memories?id={_ids[0]}")
if _slugs:
    STATEFUL.append(f"/project?slug={_slugs[0]}")
for u in STATEFUL:
    code, html = get(BASE + u)
    if code != 200:
        print(f"  {code}  {u}")
        continue
    want = urllib.parse.parse_qs(urllib.parse.urlparse(u).query)
    m = re.search(r'<span class="lang-switch">(.*?)</span>', html, re.S)
    if not m:
        print(f"  FARA comutator  {u}")
        continue
    lost = set()
    for href in re.findall(r'href="([^"]+)"', m.group(1)):
        got = urllib.parse.parse_qs(urllib.parse.urlparse(href.replace("&amp;", "&")).query)
        for k, v in want.items():
            if got.get(k) != v:
                lost.add(k)
    print(f"  {'OK  ' if not lost else 'PIERDE ' + ','.join(sorted(lost))}  {u}")

print("\n=== coduri pentru resurse inexistente (ar trebui 404, nu 200 gol) ===")
for u in ["/nope", "/memories?id=20990101-nosuch", "/project?slug=does-not-exist",
          "/assets/nope.css", "/memories?scope=project:nonexistent"]:
    code, html = get(BASE + u)
    hint = ""
    if code == 200:
        low = html.lower()
        hint = "  (200 + stare goala)" if ("no memories" in low or "nicio memorie" in low
                                           or "empty" in low) else "  (200, continut normal?!)"
    print(f"  {code}{hint}  {u}")
