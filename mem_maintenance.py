#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Scheduled local memory hygiene for mem0ry4ai — run by launchd (macOS), non-destructive by default.

Borrows Vercel eve's *scheduled maintenance* idea, kept LOCAL (launchd, not cloud). Periodically:
  1. reindex   — rebuild the FTS5 index (regenerable artifact)
  2. re-embed  — refresh semantic vectors IF a local Ollama is up (skipped otherwise)
  3. consolidate — refresh dedupe PROPOSALS on the `mem-consolidation` git branch (NEVER auto-merged;
                   you review with `git diff` and merge yourself — same review gate as `mem.py consolidate`)
  4. working notes — REPORT stale scratch notes; only DELETE them when explicitly opted in
                     (MEM_MAINT_PRUNE_WORKING=1), and even then it's logged + git-reversible
Every mutating step is review-gated or opt-in, so the scheduler never silently rewrites durable memory.

Usage:
  python3 mem_maintenance.py run                 # do one maintenance pass (what launchd calls)
  python3 mem_maintenance.py install [--interval SECONDS]   # install the launchd job (default 6h)
  python3 mem_maintenance.py uninstall
  python3 mem_maintenance.py status
"""
import argparse
import datetime
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import mem  # noqa: E402  — the data layer (DATA/STORE, build_index, embed_index, records, delete)

LABEL = "ro.vtun.mem0ry4ai-maintenance"
PLIST = os.path.expanduser(f"~/Library/LaunchAgents/{LABEL}.plist")
DEFAULT_INTERVAL = 21600   # 6 hours — maintenance is not urgent
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def log_path():
    return os.path.join(mem.DATA, "staging", "maintenance.log")


def _log(msg):
    line = f"{datetime.datetime.now().isoformat(timespec='seconds')}  {msg}"
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(log_path()), exist_ok=True)
        with open(log_path(), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _git(*args, check=False):
    """Run git in the store repo, non-interactively. Signing is force-disabled on commits so a machine
    with commit.gpgsign=true (and no local override) can't hang the unattended job on a GPG passphrase."""
    return subprocess.run(["git", "-C", mem.DATA, *args], capture_output=True, text=True,
                          creationflags=_NO_WINDOW)


def step_reindex():
    try:
        ok = mem.build_index()
        _log(f"reindex: {'ok' if ok else 'FTS5 unavailable — skipped'}")
    except Exception as e:
        _log(f"reindex: ERROR {e}")


def step_embed():
    try:
        import llm
        if not llm.embedder_up():
            _log("embed: skipped (no local Ollama)")
            return
        res = mem.embed_index()
        _log(f"embed: {res}" if res else "embed: no embedder")
    except Exception as e:
        _log(f"embed: ERROR {e}")


def step_prune_working():
    """Report stale working (scratch) notes; delete them only when opted in. Git keeps history either way."""
    days = int(os.environ.get("MEM_MAINT_WORKING_DAYS", "30"))
    cutoff = (datetime.datetime.now() - datetime.timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        stale = [r for r in mem.all_records()
                 if r["meta"].get("status") == "working" and (r["meta"].get("created") or "")[:19] < cutoff]
    except Exception as e:
        _log(f"working-notes: ERROR {e}")
        return 0
    if not stale:
        _log("working-notes: none stale")
        return 0
    ids = ", ".join(r["id"] for r in stale)
    if os.environ.get("MEM_MAINT_PRUNE_WORKING", "0") == "1":
        n = 0
        for r in stale:
            try:
                if mem.delete_memory(r["id"]):
                    n += 1
            except Exception:
                pass
        _log(f"working-notes: PRUNED {n} older than {days}d (git-reversible): {ids}")
        return n
    _log(f"working-notes: {len(stale)} stale >{days}d — REPORT only (set MEM_MAINT_PRUNE_WORKING=1 to prune): {ids}")
    return 0


def step_commit_store():
    """Commit any store/*.md changes (e.g. pruned notes) so the tree is clean before consolidate."""
    if not os.path.isdir(os.path.join(mem.DATA, ".git")):
        return
    st = _git("status", "--porcelain", "store")
    if not st.stdout.strip():
        return
    _git("add", "store")
    _git("-c", "commit.gpgsign=false", "commit", "-m", "maintenance: automated store hygiene")
    _log("commit: store changes committed")


def step_consolidate():
    """Refresh dedupe proposals on the mem-consolidation branch (review-gated; never merges to main)."""
    if not os.path.isdir(os.path.join(mem.DATA, ".git")):
        _log("consolidate: skipped (store not a git repo)")
        return
    if _git("status", "--porcelain").stdout.strip():
        _log("consolidate: skipped (working tree not clean)")
        return
    # don't clobber a proposal branch you haven't reviewed/merged yet (consolidate force-resets it)
    if _git("rev-parse", "--verify", "-q", "mem-consolidation").returncode == 0:
        ahead = _git("rev-list", "--count", "mem-consolidation", "^HEAD").stdout.strip()
        if ahead and ahead != "0":
            _log(f"consolidate: skipped ({ahead} unreviewed commit(s) on mem-consolidation — merge or discard first)")
            return
    r = subprocess.run([sys.executable, os.path.join(HERE, "mem.py"), "consolidate"],
                       capture_output=True, text=True, creationflags=_NO_WINDOW)
    out = (r.stdout or "").strip().splitlines()
    head = next((l for l in out if "cluster" in l.lower() or "clean" in l.lower()), out[0] if out else "(no output)")
    _log(f"consolidate: {head}")


def cmd_run(_a):
    _log("=== maintenance pass start ===")
    step_reindex()
    step_embed()
    step_prune_working()
    step_commit_store()
    step_consolidate()
    _log("=== maintenance pass done ===")


PLIST_TMPL = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>{label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{python}</string>
        <string>{script}</string>
        <string>run</string>
    </array>
    <key>StartInterval</key><integer>{interval}</integer>
    <key>RunAtLoad</key><false/>
    <key>StandardOutPath</key><string>{log}</string>
    <key>StandardErrorPath</key><string>{log}</string>
</dict>
</plist>
"""


def cmd_install(a):
    if os.name != "posix" or sys.platform != "darwin":
        sys.exit("install: launchd is macOS-only (on Linux use cron/systemd-timer to call `mem_maintenance.py run`)")
    os.makedirs(os.path.dirname(PLIST), exist_ok=True)
    os.makedirs(os.path.dirname(log_path()), exist_ok=True)
    with open(PLIST, "w", encoding="utf-8") as f:
        f.write(PLIST_TMPL.format(label=LABEL, python=sys.executable,
                                  script=os.path.join(HERE, "mem_maintenance.py"),
                                  interval=a.interval, log=log_path()))
    subprocess.run(["launchctl", "unload", PLIST], capture_output=True)   # idempotent reinstall
    r = subprocess.run(["launchctl", "load", PLIST], capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"install: launchctl load failed: {r.stderr.strip()}")
    print(f"installed {LABEL}  (every {a.interval}s)  -> {PLIST}\n  logs: {log_path()}")
    print("  run once now:  python3 mem_maintenance.py run")


def cmd_uninstall(_a):
    subprocess.run(["launchctl", "unload", PLIST], capture_output=True)
    if os.path.exists(PLIST):
        os.remove(PLIST)
        print(f"uninstalled {LABEL} (plist removed)")
    else:
        print(f"{LABEL} was not installed")


def cmd_status(_a):
    installed = os.path.exists(PLIST)
    print(f"launchd job: {'INSTALLED' if installed else 'not installed'}  ({PLIST})")
    if installed:
        r = subprocess.run(["launchctl", "list", LABEL], capture_output=True, text=True)
        print("  loaded" if r.returncode == 0 else "  (plist present but not loaded — `launchctl load`)")
    if os.path.exists(log_path()):
        tail = open(log_path(), encoding="utf-8").read().splitlines()[-6:]
        print("  recent log:")
        for l in tail:
            print("   ", l)


def main():
    p = argparse.ArgumentParser(prog="mem_maintenance.py", description="scheduled local memory hygiene")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run").set_defaults(func=cmd_run)
    pi = sub.add_parser("install"); pi.add_argument("--interval", type=int, default=DEFAULT_INTERVAL); pi.set_defaults(func=cmd_install)
    sub.add_parser("uninstall").set_defaults(func=cmd_uninstall)
    sub.add_parser("status").set_defaults(func=cmd_status)
    a = p.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
