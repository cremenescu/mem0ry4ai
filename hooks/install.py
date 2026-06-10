#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Register/remove the mem0ry4ai hooks in Claude Code's settings.json.

Non-destructive merge (preserves any other configured hooks) + automatic backup.

Usage:
  python3 hooks/install.py [--target user|project] [--uninstall] [--dry-run]
    user    = ~/.claude/settings.json  -> memory in ALL your projects (default)
    project = <cwd>/.claude/settings.json -> current project only

Our entries are recognized by signature (the HOOK_DIR path inside the command),
so reinstalling replaces them cleanly instead of duplicating.
"""
import argparse
import json
import os
import sys
import time

HOOK_DIR = os.path.dirname(os.path.abspath(__file__))
SESSION_START = os.path.join(HOOK_DIR, "session_start.py")
CAPTURE = os.path.join(HOOK_DIR, "capture.py")
PY = sys.executable or "python3"
SIG = HOOK_DIR  # signature used to recognize our own entries


def settings_path(target):
    if target == "user":
        return os.path.expanduser("~/.claude/settings.json")
    return os.path.join(os.getcwd(), ".claude", "settings.json")


def load(path):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {}


def strip_ours(arr):
    """Drop entries whose commands point into our HOOK_DIR."""
    return [e for e in arr
            if not any(SIG in hh.get("command", "") for hh in e.get("hooks", []))]


def set_event(hooks, event, command, matcher=None):
    arr = strip_ours(hooks.get(event, []))
    entry = {"hooks": [{"type": "command", "command": command, "timeout": 30}]}
    if matcher is not None:
        entry["matcher"] = matcher
    arr.append(entry)
    hooks[event] = arr


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", choices=["user", "project"], default="user")
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    path = settings_path(a.target)
    cfg = load(path)
    hooks = cfg.setdefault("hooks", {})

    if a.uninstall:
        for ev in ("SessionStart", "PreCompact", "SessionEnd"):
            if ev in hooks:
                hooks[ev] = strip_ours(hooks[ev])
                if not hooks[ev]:
                    del hooks[ev]
        action = "uninstalled"
    else:
        set_event(hooks, "SessionStart", f"{PY} {SESSION_START}")  # all sources
        set_event(hooks, "PreCompact", f"{PY} {CAPTURE}")
        set_event(hooks, "SessionEnd", f"{PY} {CAPTURE}")
        action = "installed"

    blob = json.dumps(cfg, indent=2, ensure_ascii=False)
    if a.dry_run:
        print(f"[dry-run] {action} in {path}:\n")
        print(blob)
        return

    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        bak = f"{path}.bak.{time.strftime('%Y%m%d_%H%M%S')}"
        with open(path, encoding="utf-8") as f:
            old = f.read()
        with open(bak, "w", encoding="utf-8") as f:
            f.write(old)
        print(f"backup: {bak}")

    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(blob)
    os.replace(tmp, path)
    print(f"{action} in {path}")
    print("Restart Claude Code (or /clear) so the hooks get loaded.")
    print(f"Verify: cat {path}")
    print(f"Uninstall: python3 {os.path.join(HOOK_DIR, 'install.py')} --target {a.target} --uninstall")


if __name__ == "__main__":
    main()
