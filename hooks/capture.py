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


def _data_dir():
    """Same resolution as mem.py: MEM_DATA_DIR > plugin-safe default > code dir."""
    d = os.environ.get("MEM_DATA_DIR")
    if d:
        return os.path.abspath(os.path.expanduser(d))
    if f"{os.sep}.claude{os.sep}plugins{os.sep}" in PROJ + os.sep:
        return os.path.join(os.path.expanduser("~"), ".mem0ry4ai")
    return PROJ


DATA = _data_dir()
STAGING = os.path.join(DATA, "staging")


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
    ev = data.get("hook_event_name")
    if ev in ("SessionEnd", "PreCompact"):
        # Flush at BOTH boundaries (compaction protocol): a long session that compacts mid-way still
        # gets the memories written so far committed + embedded, so nothing is lost at the boundary and
        # search/suggestions stay fresh — without relying on advance notice before compaction.
        auto_commit_store("pre-compaction checkpoint" if ev == "PreCompact" else "end of session")
        auto_embed()
    return 0


def auto_embed():
    """Best-effort: refresh the semantic vectors at session end so search/link-suggestions
    reflect memories written this session, without the user running `mem.py embed` by hand.
    Launched DETACHED so it never blocks the session; incremental (only changed records) and a
    no-op in milliseconds when no embedder is up. Silent on any error."""
    import subprocess
    try:
        mem = os.path.join(PROJ, "mem.py")
        if not os.path.exists(mem):
            return
        env = dict(os.environ, MEM_DATA_DIR=DATA)
        subprocess.Popen([sys.executable, mem, "embed"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         env=env, start_new_session=True)
    except Exception:
        pass


def auto_commit_store(reason="end of session"):
    """Automatic checkpoint: commit store/ at a session boundary (no human action required).

    Memories written during the session land in git history without the user pressing anything.
    `reason` distinguishes an end-of-session checkpoint from a mid-session pre-compaction one.
    Silent on any error (repo lock from another session, ownership, etc.).
    """
    import subprocess
    try:
        base = ["git", "-C", DATA, "-c", f"safe.directory={DATA}"]
        if not os.path.isdir(os.path.join(DATA, ".git")):
            # standalone data dir (plugin install / MEM_DATA_DIR): it gets its own repo,
            # so the memory timeline works there too
            if not os.path.isdir(os.path.join(DATA, "store")):
                return
            subprocess.run(["git", "init", "-q", DATA], capture_output=True, timeout=10)
            gi = os.path.join(DATA, ".gitignore")
            if not os.path.exists(gi):
                with open(gi, "w", encoding="utf-8") as f:
                    # derived dbs (FTS index + embeddings) are regenerable + churn on every checkpoint
                    f.write("staging/\nstore/.index.db*\nstore/.embed.db*\n.web-server.pid\n.web-server.log\n")
        # -uall: list untracked files individually ('?? store/' alone would yield an empty label)
        r = subprocess.run(base + ["status", "--porcelain", "-uall", "store"],
                           capture_output=True, text=True, timeout=10)
        if r.returncode != 0 or not r.stdout.strip():
            return
        # one commit PER FILE (= per scope): global never mixes with individual projects
        files = sorted({l[3:].strip() for l in r.stdout.splitlines() if l.strip()})
        for f in files:
            if "/projects/" in f:
                label = f.rsplit("/", 1)[-1].removesuffix(".md")
            elif f.endswith("global.md"):
                label = "global"
            else:
                label = f.rsplit("/", 1)[-1]
            subprocess.run(base + ["add", f], capture_output=True, timeout=10)
            subprocess.run(base + ["-c", "commit.gpgsign=false",
                                   "-c", "user.name=mem0ry4ai hook",
                                   "-c", "user.email=hook@mem0ry4ai.local",
                                   "commit", "-m",
                                   f"store: checkpoint {label} ({reason})",
                                   "--", f],
                           capture_output=True, timeout=15)
    except Exception:
        pass


if __name__ == "__main__":
    sys.exit(main())
