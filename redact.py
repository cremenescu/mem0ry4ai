#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Secret redaction — keeps credentials out of the store.

The store is plain markdown versioned by git: a secret that lands there is hard to
remove retroactively (it stays in git history). So every write path redacts by
default: `mem.py add` / `mem.py propose` and the consolidate.py extraction pipeline
(transcripts routinely contain .env reads, curl headers, passwords).

Values are replaced with [REDACTED:<label>] — the memory still says WHAT kind of
secret was used, only the value is gone. A memory tool should remember "the command
used a Bearer token", not the token itself.

Opt out per call with --no-redact (mem.py) or globally with MEM_REDACT=0.
`mem.py audit` uses scan() to report (never modify) existing records.

Pattern set inspired by askqai/claude-recall (Apache-2.0), rewritten for Python.
"""
import os
import re

PATTERNS = [
    ("API_KEY", re.compile(
        r"(?:api[_-]?key|apikey|secret[_-]?key|access[_-]?key)\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{16,}", re.I)),
    ("BEARER_TOKEN", re.compile(r"\bBearer\s+[A-Za-z0-9_\-.=]{20,}")),
    ("AWS_KEY", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("PRIVATE_KEY", re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]+?-----END [A-Z ]*PRIVATE KEY-----")),
    ("PASSWORD", re.compile(r"(?:password|passwd|pwd)\s*[:=]\s*['\"][^'\"]{4,}['\"]", re.I)),
    ("GITHUB_TOKEN", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("OPENAI_KEY", re.compile(r"\bsk-[A-Za-z0-9]{32,}\b")),
    ("SLACK_TOKEN", re.compile(r"\bxox[bpras]-[A-Za-z0-9-]{10,}\b")),
]


def enabled():
    """Redaction is on unless MEM_REDACT is set to 0/false/no/off."""
    return os.environ.get("MEM_REDACT", "1").lower() not in ("0", "false", "no", "off")


def scan(text):
    """Labels of secret patterns found in text (report only, nothing modified)."""
    return [label for label, rx in PATTERNS if rx.search(text or "")]


def redact(text):
    """Replace secret values with [REDACTED:<label>]. Returns (text, found).

    `found` is a list of (label, count) for everything that was replaced.
    """
    found = []
    out = text or ""
    for label, rx in PATTERNS:
        out, n = rx.subn(f"[REDACTED:{label}]", out)
        if n:
            found.append((label, n))
    return out, found


def describe(found):
    """'API_KEY x2, PASSWORD' — human-readable summary of redact() results."""
    return ", ".join(f"{label} x{n}" if n > 1 else label for label, n in found)
