#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Offline memory extraction from session transcripts, using a local LLM (Ollama).

Reads a Claude Code transcript (JSONL), condenses it (user+assistant text only),
and asks the model for memory candidates (type/scope/summary/body/confidence) as JSON.

Usage:
  python3 consolidate.py --transcript <path.jsonl> [--dry-run]   # one transcript
  python3 consolidate.py [--dry-run]                             # all unprocessed staged sessions
  python3 consolidate.py --write                                 # queue candidates for review

IMPORTANT: candidates are NEVER written to the store directly. Small models are noisy and
over-confident (we measured ~1.0 confidence on everything), so everything goes through the
human review queue in the web UI.
"""
import argparse
import hashlib
import json
import os
import sys
import time

PROJ = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJ)
import llm  # noqa: E402
import redact  # noqa: E402


def _data_dir():
    """Same resolution as mem.py: MEM_DATA_DIR > plugin-safe default > code dir."""
    d = os.environ.get("MEM_DATA_DIR")
    if d:
        return os.path.abspath(os.path.expanduser(d))
    if f"{os.sep}.claude{os.sep}plugins{os.sep}" in PROJ + os.sep:
        return os.path.join(os.path.expanduser("~"), ".mem0ry4ai")
    return PROJ


DATA = _data_dir()

MAX_DIGEST = 14000  # transcript characters sent to the model

CAND_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "type": {"type": "string", "enum": ["gotcha", "fact", "decision", "command", "preference", "todo", "status"]},
            "scope": {"type": "string"},
            "summary": {"type": "string"},
            "body": {"type": "string"},
            "confidence": {"type": "number"},
        },
        "required": ["type", "scope", "summary", "body", "confidence"],
    },
}

SYSTEM = (
    "You are a LONG-TERM memory extractor for a coding agent. You receive a USER/ASSISTANT "
    "transcript. Extract ONLY DURABLE, GENERALIZABLE knowledge useful in FUTURE sessions. "
    "Do NOT summarize what happened in the session.\n\n"
    "Types (pick the RIGHT one, do not default to 'command'):\n"
    "- gotcha: technical trap + cause + fix. E.g. 'when the web server runs as daemon, the data "
    "dir needs chmod 777 or PHP cannot write sqlite'.\n"
    "- decision: architecture decision + WHY. E.g. 'chose markdown+git as source of truth instead "
    "of a DB, because it is auditable and has no fragility'.\n"
    "- fact: stable infrastructure fact. E.g. 'server X = 10.0.0.5, web runs as www-data'.\n"
    "- command: a concrete reusable command.\n"
    "- preference: a user preference. E.g. 'the user wants no emoji in UI'.\n\n"
    "REJECT (these are NOT memories) — examples:\n"
    "- 'Updated the changelog' / 'Created file X' (what was done, not a lesson)\n"
    "- one-off step-by-step instructions for ephemeral tasks\n"
    "- thanks, plans, status chatter, questions, speculation.\n\n"
    "Rules: 'scope' = 'global' if valid across projects, else 'project:<slug>' (the given slug). "
    "'summary' = one short line. 'body' = 1-4 concrete sentences. "
    "'confidence' = 0..1: ephemeral/task-like < 0.4, clear and certain lesson > 0.8. "
    "If nothing durable, return []. Respond with valid JSON ONLY."
)


def digest_transcript(path):
    """Concatenate user+assistant text (no thinking/tool noise/images/sidechains)."""
    parts = []
    for line in open(path, encoding="utf-8"):
        try:
            o = json.loads(line)
        except Exception:
            continue
        if o.get("type") not in ("user", "assistant") or o.get("isSidechain"):
            continue
        msg = o.get("message") or {}
        role = msg.get("role")
        content = msg.get("content")
        texts = []
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "text" and b.get("text"):
                    texts.append(b["text"])
        text = "\n".join(texts).strip()
        if text:
            parts.append(f"{(role or '?').upper()}: {text}")
    digest = "\n\n".join(parts)
    if len(digest) > MAX_DIGEST:
        digest = digest[-MAX_DIGEST:]  # keep the recent part (conclusions)
    # transcripts routinely contain .env reads / curl headers / passwords —
    # redact BEFORE the text reaches the model, so secrets never enter the pipeline
    if redact.enabled():
        digest, found = redact.redact(digest)
        if found:
            print(f"  redacted from transcript: {redact.describe(found)}")
    return digest


def project_slug(cwd):
    return os.path.basename(os.path.normpath(cwd)) if cwd else ""


def extract(path, slug):
    digest = digest_transcript(path)
    if not digest.strip():
        return [], 0
    prompt = f"Current project (slug): {slug or 'unknown'}\n\nTranscript:\n{digest}\n\nExtract the memories."
    cands = llm.generate_json(SYSTEM, prompt, CAND_SCHEMA)
    return cands, len(digest)


def staging_path():
    return os.path.join(DATA, "staging", "sessions.jsonl")


def queue_path():
    return os.path.join(DATA, "staging", "queue.jsonl")


def gen_qid():
    return time.strftime("%Y%m%d") + "-" + hashlib.sha1(os.urandom(8)).hexdigest()[:6]


def queue_candidates(cands, source_session, transcript):
    """Write candidates to the review queue (NOT the store — they await human approval)."""
    os.makedirs(os.path.dirname(queue_path()), exist_ok=True)
    n = 0
    with open(queue_path(), "a", encoding="utf-8") as f:
        for c in cands:
            # safety net: the digest is already redacted, but the model may rephrase
            # something resembling a secret — redact candidates too before queueing
            summary = (c.get("summary") or "").strip()
            body = (c.get("body") or "").strip()
            if redact.enabled():
                summary, _ = redact.redact(summary)
                body, _ = redact.redact(body)
            rec = {
                "qid": gen_qid(),
                "type": c.get("type"), "scope": c.get("scope") or "global",
                "summary": summary,
                "body": body,
                "confidence": c.get("confidence"),
                "source": f"llm:session:{source_session}" if source_session else "llm",
                "transcript": transcript,
                "extracted_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "status": "pending",
            }
            if not rec["summary"] or not rec["body"]:
                continue
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            n += 1
    return n


def mark_processed(session_id):
    """Mark a session as processed in staging/sessions.jsonl (atomic rewrite)."""
    p = staging_path()
    if not os.path.isfile(p) or not session_id:
        return
    lines = []
    for line in open(p, encoding="utf-8"):
        line = line.rstrip("\n")
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except Exception:
            lines.append(line)
            continue
        if r.get("session_id") == session_id:
            r["processed"] = True
            line = json.dumps(r, ensure_ascii=False)
        lines.append(line)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, p)


def unprocessed_sessions():
    p = staging_path()
    if not os.path.isfile(p):
        return []
    out = []
    seen = set()   # dedup by session_id (staging may hold several SessionEnd entries per session)
    for line in open(p, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        r = json.loads(line)
        if r.get("processed"):
            continue
        sid = r.get("session_id")
        if sid in seen:
            continue
        tp = r.get("transcript_path")
        if tp and os.path.exists(tp):
            seen.add(sid)
            out.append(r)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--transcript", help="a specific transcript .jsonl")
    ap.add_argument("--slug", default="", help="project slug (for scope); default = from staged cwd")
    ap.add_argument("--write", action="store_true", help="queue candidates + mark sessions processed")
    ap.add_argument("--dry-run", action="store_true", help="print only, write nothing (default without --write)")
    a = ap.parse_args()

    if not llm.ollama_up():
        sys.exit("Ollama is not responding (start the Ollama app). Capture stays manual.")

    jobs = []
    if a.transcript:
        jobs.append({"transcript_path": a.transcript, "cwd": "", "slug": a.slug})
    else:
        sess = unprocessed_sessions()
        print(f"{len(sess)} unprocessed sessions with a valid transcript.\n")
        jobs = sess

    for j in jobs:
        tp = j["transcript_path"]
        slug = j.get("slug") or project_slug(j.get("cwd", ""))
        print(f"=== {os.path.basename(tp)} (slug={slug or '-'}) ===")
        try:
            cands, dlen = extract(tp, slug)
        except Exception as e:
            print(f"  extraction ERROR: {e}\n")
            continue
        print(f"  digest {dlen} chars -> {len(cands)} candidates:")
        for c in cands:
            print(f"    [{c.get('type')}] ({c.get('scope')}) conf={c.get('confidence')}  {c.get('summary')}")
            body = (c.get("body") or "").strip().replace("\n", " ")
            print(f"        {body[:140]}")
        if a.write and cands:
            n = queue_candidates(cands, j.get("session_id", ""), tp)
            mark_processed(j.get("session_id", ""))
            print(f"  -> {n} candidates queued for review (staging/queue.jsonl)")
        print()

    if a.write:
        print("Candidates are queued. Review them in the web UI: approve / edit / reject.")
    else:
        print("[dry-run] nothing written. Add --write to queue them for review.")


if __name__ == "__main__":
    main()
