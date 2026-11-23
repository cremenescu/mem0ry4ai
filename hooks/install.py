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
PROMPT_RECALL = os.path.join(HOOK_DIR, "prompt_recall.py")
TOOL_RECALL = os.path.join(HOOK_DIR, "tool_recall.py")
CAPTURE = os.path.join(HOOK_DIR, "capture.py")
PY = sys.executable or "python3"
SIG = HOOK_DIR  # signature used to recognize our own entries
# A tool call that fails fires PostToolUseFailure INSTEAD of PostToolUse -- a shell command that
# exits non-zero, for one. That is when a project's gotchas are worth the most.
TOOL_EVENTS = ("PostToolUse", "PostToolUseFailure")
EVENTS = ("SessionStart", "UserPromptSubmit") + TOOL_EVENTS + ("PreCompact", "SessionEnd")
sys.path.insert(0, HOOK_DIR)
from session_start import PARTS  # noqa: E402  -- one list of parts, not a copy that can fall behind
from tool_recall import MATCHER  # noqa: E402  -- likewise: the tools the hook is written for


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


def set_event(hooks, event, commands, timeout=30, matcher=None):
    """Replace OUR handlers for `event` with `commands` (a list: Claude Code runs the handlers of
    one event in parallel and caps each one's output separately, which is the point of having
    several). No commands = just remove ours. `matcher` narrows a tool event to the tools named."""
    arr = strip_ours(hooks.get(event, []))
    if commands:
        entry = {"hooks": [{"type": "command", "command": c, "timeout": timeout} for c in commands]}
        if matcher:
            entry = {"matcher": matcher, **entry}
        arr.append(entry)
    if arr:
        hooks[event] = arr
    else:
        hooks.pop(event, None)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", choices=["user", "project"], default="user")
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-prompt-recall", action="store_true",
                    help="do not register the per-prompt recall hook (UserPromptSubmit)")
    ap.add_argument("--no-tool-recall", action="store_true",
                    help="do not register the project-brief hook (PostToolUse, PostToolUseFailure)")
    a = ap.parse_args()

    path = settings_path(a.target)
    cfg = load(path)
    hooks = cfg.setdefault("hooks", {})

    if a.uninstall:
        for ev in EVENTS:
            set_event(hooks, ev, [])
        action = "uninstalled"
    else:
        # quote both paths so spaces (Windows: "C:\Program Files\...\python.exe" or a username with a
        # space) don't break the command; the HOOK_DIR signature is still a substring for uninstall.
        # SessionStart: one handler per part, each with its own 10,000-character ceiling.
        set_event(hooks, "SessionStart", [f'"{PY}" "{SESSION_START}" --part {p}' for p in PARTS])
        # A short timeout: this one sits between the user pressing Enter and the model starting.
        set_event(hooks, "UserPromptSubmit",
                  [] if a.no_prompt_recall else [f'"{PY}" "{PROMPT_RECALL}"'], timeout=10)
        # Shorter still: this one runs after every tool call the matcher names, and nearly always
        # has nothing to say.
        for ev in TOOL_EVENTS:
            set_event(hooks, ev, [] if a.no_tool_recall else [f'"{PY}" "{TOOL_RECALL}"'],
                      timeout=5, matcher=MATCHER)
        set_event(hooks, "PreCompact", [f'"{PY}" "{CAPTURE}"'])
        set_event(hooks, "SessionEnd", [f'"{PY}" "{CAPTURE}"'])
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
