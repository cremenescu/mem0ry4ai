#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""PostToolUse / PostToolUseFailure hook (Claude Code): what the store knows about the place a
tool just touched.

Reads stdin JSON {tool_name, tool_input, session_id, cwd, ...}. The first time a session touches a
path inside a project, brief.py answers with that project's brief; the first time it touches a
file that memories are anchored to, with those memories. Either is printed as `additionalContext`,
which Claude Code puts next to the tool's result. Nearly every call prints nothing at all.

Registered for the tools in MATCHER -- the ones whose input names a place on disk -- on both
events, because a call that fails fires the second INSTEAD of the first, and a failed first command
in a project is exactly when its gotchas are wanted. Everything is inside one try, so ANY error =>
exit 0 with no output: a tool call must never fail over this.
"""
import json
import os
import sys

HOOK_DIR = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HOOK_DIR)
# Exact tool names, '|'-separated: the form Claude Code matches as a list, not as a regex.
MATCHER = "Read|Edit|Write|MultiEdit|NotebookEdit|Bash|Grep|Glob"


def main():
    try:
        data = json.load(sys.stdin)
        if data.get("tool_name") not in MATCHER.split("|"):
            return 0                    # registered more widely than meant: still only these
        sys.path.insert(0, PROJ)
        import brief
        text, note = brief.on_tool(data)
        if text:
            payload = {"hookSpecificOutput": {"hookEventName": data.get("hook_event_name") or "PostToolUse",
                                              "additionalContext": text}}
            if note:
                payload["systemMessage"] = note     # for the user: which project, which file
            json.dump(payload, sys.stdout, ensure_ascii=False)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
