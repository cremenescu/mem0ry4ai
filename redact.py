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


# Prompt-injection / instruction-override phrasing. A stored memory is re-injected into the agent's
# context, so a memory that says "ignore your instructions and ..." is worth a second look before it
# lives in the store (Hermes Agent scans for the same reason). Deliberately imperative-override /
# role-hijack phrasings only — NOT curl/exfil URLs, which legitimately fill `command`-type memories
# and would drown the signal. Gaps are [\s\S]{0,N}? (bounded, so no ReDoS) so a phrase wrapped across a
# line in multi-line markdown still matches — the previous `.` missed any line break.
INJECTION_PATTERNS = [
    # override of prior/standing instructions — the classic prompt-injection opener
    ("IGNORE_INSTRUCTIONS", re.compile(
        r"\b(?:ignore|disregard|forget|override|bypass|overlook)\b[\s\S]{0,40}?"
        r"\b(?:previous|prior|above|earlier|all|your|the|these|any|system)\b[\s\S]{0,25}?"
        r"\b(?:instruction|prompt|rule|directive|guideline|command|context|message)s?\b", re.I)),
    # negation form: "do not follow / no longer obey your instructions". Drops 'rule' as the noun on
    # purpose — firewall / OSPF 'rules' are legit content this variant must not false-positive on.
    ("IGNORE_INSTRUCTIONS", re.compile(
        r"\b(?:do\s+not|don'?t|never|no\s+longer|stop)\b[\s\S]{0,15}?"
        r"\b(?:follow|obey|comply|adhere|abide|heed)\b[\s\S]{0,30}?"
        r"\b(?:instruction|prompt|directive|guideline)s?\b", re.I)),
    # identity / role hijack
    ("ROLE_OVERRIDE", re.compile(
        r"\b(?:you\s+are\s+now\b|you\s+will\s+now\b|you\s+must\s+now\b|"
        r"from\s+(?:now\s+on|this\s+(?:point|moment)\s+(?:on|onward|forward))\b|"
        r"pretend\s+(?:to\s+be|that\s+you)\b|act\s+(?:as|like)\s+(?:if|an?|the)\b|"
        r"you\s+are\s+(?:now\s+)?(?:dan\b|an?\s+(?:unrestricted|unfiltered|jailbroken|unbound|uncensored)))",
        re.I)),
    # extracting the system prompt / hidden instructions — verb must OPEN the sentence (optionally after
    # 'please'), so a runbook line "this command prints your system prompt" doesn't false-positive.
    ("PROMPT_LEAK", re.compile(
        r"(?:^|[.!?]\s|\n)\s*(?:please\s+)?"
        r"(?:reveal|repeat|print|show|output|disclose|leak|dump)\b[\s\S]{0,30}?"
        r"\b(?:system\s+prompt|your\s+(?:instructions|prompt|rules|system\s+message)|"
        r"initial\s+(?:prompt|instructions))\b", re.I)),
]


def enabled():
    """Redaction is on unless MEM_REDACT is set to 0/false/no/off."""
    return os.environ.get("MEM_REDACT", "1").lower() not in ("0", "false", "no", "off")


def injection_enabled():
    """Injection scanning is on unless MEM_SCAN_INJECTION is set to 0/false/no/off."""
    return os.environ.get("MEM_SCAN_INJECTION", "1").lower() not in ("0", "false", "no", "off")


def scan(text):
    """Labels of secret patterns found in text (report only, nothing modified)."""
    return [label for label, rx in PATTERNS if rx.search(text or "")]


def scan_injection(text):
    """Labels of prompt-injection / instruction-override patterns in text (report only).

    A WRITE-TIME authoring aid: it flags a memory whose phrasing looks like an instruction override,
    so a poisoned memory isn't saved unnoticed and later re-injected into the agent's context. It is a
    heuristic, NOT a guarantee and NOT an inject-time filter — the SessionStart hook / MCP instructions
    still surface stored memories verbatim, so a memory added through an unscanned path can slip past.
    Treat a hit as 'look at this', not 'this is safe because it's clean'."""
    if not injection_enabled():
        return []
    return sorted({label for label, rx in INJECTION_PATTERNS if rx.search(text or "")})


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
