#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""UserPromptSubmit hook (Claude Code): offer the memories this prompt is about.

Reads stdin JSON {prompt, session_id, cwd, ...}; when recall.py finds memories that the prompt's
own wording points at, prints them as `additionalContext`, which Claude Code adds next to the
prompt. When the prompt names, in full, a project the session has not worked in yet, brief.py adds
that project's brief. Most prompts produce no output at all -- that is the intended common case.

Unlike the SessionStart hook this one imports the data layer: it needs the index and the egress
rules, and re-implementing either here would be the kind of second copy that drifts. Everything is
inside one try, so ANY error => exit 0 with no output. A prompt must never fail to send over this.
"""
import json
import os
import sys

HOOK_DIR = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HOOK_DIR)


def main():
    try:
        data = json.load(sys.stdin)
        prompt = data.get("prompt") or ""
        head = prompt.lstrip()[:1]
        # A slash command is an instruction to the harness, and '<' opens the wrappers Claude Code
        # puts around turns it starts itself (task notifications, command output). Neither is the
        # user asking something. Decided before anything is imported, so they cost nothing.
        if not head or head in "/<":
            return 0
        sys.path.insert(0, PROJ)
        import recall
        shown = []
        text = recall.recall(prompt, session=data.get("session_id"), cwd=data.get("cwd"), shown=shown)
        note = recall.notice(shown) if text else ""     # for the user: which memories, which project
        try:
            # After the recall, so that what it just offered is not repeated in the brief. And on
            # its own account: a brief that cannot be built must not take the recall down with it.
            import brief
            b_text, b_note = brief.on_prompt(prompt, session=data.get("session_id"), cwd=data.get("cwd"))
        except Exception:
            b_text, b_note = "", ""
        text = "\n\n".join(x for x in (text, b_text) if x)
        note = "\n".join(x for x in (note, b_note) if x)
        if text:
            payload = {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                              "additionalContext": text}}
            if note:
                payload["systemMessage"] = note
            json.dump(payload, sys.stdout, ensure_ascii=False)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
