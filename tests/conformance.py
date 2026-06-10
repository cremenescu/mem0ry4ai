#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Conformance test: the Python parser (mem.py) and the PHP parser (web/lib.php) must
produce EXACTLY the same result on the same store. Catches format drift between the two
engines (the risk of having parsing logic in two places).

Run before committing parser changes: python3 tests/conformance.py
Exit 0 = identical; 1 = drift; 2 = runtime error.
"""
import json
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def find_php():
    """PHP binary: MEM_PHP env override, otherwise whatever is on PATH."""
    env = os.environ.get("MEM_PHP")
    if env and os.path.exists(env):
        return env
    c = shutil.which("php")
    if c:
        return c
    sys.exit("FAIL: no php binary found (install php or set MEM_PHP=/path/to/php)")

def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT)
    if r.returncode != 0:
        print(f"FAIL: command failed: {' '.join(cmd)}", file=sys.stderr)
        print(r.stderr, file=sys.stderr)
        sys.exit(2)
    return r.stdout

def main():
    php = find_php()
    py_data = json.loads(run(["./mem.py", "list", "--status", "all", "--json"]))
    php_data = json.loads(run([php, "tests/php_dump.php"]))

    key = lambda x: x["id"]
    py_data.sort(key=key)
    php_data.sort(key=key)

    if py_data == php_data:
        print(f"PASS: {len(py_data)} records, the Python and PHP parsers are identical.")
        return 0

    print("FAIL: the Python and PHP parsers differ.")
    if len(py_data) != len(php_data):
        print(f"  record count: python={len(py_data)} php={len(php_data)}")
    for a, b in zip(py_data, php_data):
        if a != b:
            print(f"  record {a.get('id')}:")
            for k in a:
                if a.get(k) != b.get(k):
                    print(f"    field '{k}': python={a.get(k)!r}  php={b.get(k)!r}")
            break
    return 1

if __name__ == "__main__":
    sys.exit(main())
