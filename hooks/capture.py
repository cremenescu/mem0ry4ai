#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""PreCompact + SessionEnd hook (Claude Code): record a pointer to the session transcript
in staging/, for later consolidation (offline LLM extraction).

Fire-and-forget: output does not matter, only the side effect of logging the session.
PreCompact receives transcript_path directly; SessionEnd does not, so we derive it from
session_id + cwd. Any error => silent exit 0 (never breaks the session).
"""
import glob
import json
import os
import sys
import time

HOOK_DIR = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HOOK_DIR)
STAGING = os.path.join(PROJ, "staging")


def derive_transcript(cwd, session_id):
    """Claude Code transcripts live at ~/.claude/projects/<cwd-slug>/<session_id>.jsonl."""
    if not session_id:
        return None
    home = os.path.expanduser("~")
    slug = cwd.replace("/", "-") if cwd else ""
    cand = os.path.join(home, ".claude", "projects", slug, f"{session_id}.jsonl")
    if os.path.exists(cand):
        return cand
    hits = glob.glob(os.path.join(home, ".claude", "projects", "*", f"{session_id}.jsonl"))
    return hits[0] if hits else None


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0
    sid = data.get("session_id", "")
    cwd = data.get("cwd", "")
    tp = data.get("transcript_path") or derive_transcript(cwd, sid)
    rec = {
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "event": data.get("hook_event_name", ""),
        "reason": data.get("reason", ""),
        "session_id": sid,
        "cwd": cwd,
        "transcript_path": tp,
        "processed": False,   # consolidation marks this True after extraction
    }
    try:
        os.makedirs(STAGING, exist_ok=True)
        with open(os.path.join(STAGING, "sessions.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
