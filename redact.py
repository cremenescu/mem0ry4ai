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

Two kinds of rule. PATTERNS are formats: the whole match is the secret, wherever it
stands. RULES are a keyword followed by a value ("parola de admin e ...", "password
'...'", "token": "..."), where the regex alone cannot tell a secret from a word: each
one validates the value it caught and masks only that value, so prose keeps reading
as prose.

Opt out per call with --no-redact (mem.py) or globally with MEM_REDACT=0.
`mem.py audit` uses scan() to report (never modify) existing records.

Pattern set inspired by askqai/claude-recall (Apache-2.0), rewritten for Python; the
idea of judging the value after "password is" by its shape comes from sessionmem (MIT).
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
    ("GITHUB_TOKEN", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{40,})\b")),
    ("OPENAI_KEY", re.compile(r"\bsk-[A-Za-z0-9]{32,}\b")),
    ("SLACK_TOKEN", re.compile(r"\bxox[bpras]-[A-Za-z0-9-]{10,}\b")),
    # the prefixed keys LLM providers issue now, which hold "-" and "_" and so slip past OPENAI_KEY
    ("LLM_API_KEY", re.compile(r"\bsk-(?:proj|svcacct|admin|ant|or)-[A-Za-z0-9_\-]{20,}")),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}")),
    # crypt(3) hashes as they stand in a shadow file, an htpasswd file or a router configuration
    ("PASSWORD_HASH", re.compile(
        r"\$(?:1|5|6|apr1)\$(?:rounds=\d+\$)?[./A-Za-z0-9]{1,16}\$[./A-Za-z0-9]{22,}"
        r"|\$2[abxy]?\$\d\d\$[./A-Za-z0-9]{53}"
        r"|\$y\$[./A-Za-z0-9]+\$[./A-Za-z0-9]{1,64}\$[./A-Za-z0-9]{40,}"
        r"|\$argon2(?:id|i|d)\$[A-Za-z0-9=,+/$]{40,}")),
    ("STRIPE_KEY", re.compile(r"\b(?:sk|rk)_live_[A-Za-z0-9]{20,}")),
    ("GOOGLE_API_KEY", re.compile(r"\bAIza[A-Za-z0-9_\-]{35}(?![A-Za-z0-9_\-])")),
]


# --- rules that look at the value they caught
#
# "the password is hunter2" and "the password is required" are the same sentence to a regex: a
# keyword, a copula, a word. So each rule below pairs its regex with a function that looks at what
# the regex captured and answers with the span to mask, or None. Only that span is replaced.
# The thresholds were set against a real store and five months of transcripts: what they mask
# there is credentials, and ordinary prose about passwords stays as written.

_STRONG = frozenset("0123456789!@#$%^&")
_CLOSERS = ".,;:)]}*_~`"     # what follows a value in a sentence or in markdown; "!" and "?" stay masked
_TECH = re.compile(r"(?:base64|sha-?\d|md5|bcrypt|argon2|scrypt|pbkdf2|utf-?8|1password|ed25519|rsa-?\d|aes-?\d"
                   r"|oauth2|wpa[23]|hmac|x509|\d+fa\b|totp|ntlm|kerberos|pkcs\d|ipv[46]|http2|tls1)", re.I)
_ENV_NAME = re.compile(r"[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\Z")
_VARIABLE = re.compile(r"\$(?:\{[^}]*\}?|\([^)]*\)?|[A-Za-z_][\w:.]*)\Z")     # $NAME, ${NAME}, $(cmd), $env:NAME
# Every rule needs one of these somewhere in the text; most text has none and skips them all.
_HINT = re.compile(r"pass|pwd|parol|secret|token|key|psk|://", re.I)


def _bare(v, strict=False, config=False):
    """How many characters of a bare token are a credential, or 0 when it reads as an ordinary word.

    A credential has a letter next to a digit or one of !@#$%^&, or is four digits or more. `strict`
    is for a keyword followed by nothing but a space, where a product name (log4net, db01) is as
    likely as a password: it wants both letter cases with a digit, or a symbol, or twenty characters.
    `config` is for a value after a configuration keyword, which may be a hash or a key and so may
    start with "$" and hold "/"."""
    core = v.rstrip(_CLOSERS)
    judged = core.rstrip("!?")
    if not 4 <= len(judged) <= 200 or "[REDACTED" in v:
        return 0
    if judged[0] in "[<{(%*~" or _VARIABLE.match(judged):
        return 0
    if not config and (judged[0] in "$/.-_'\"`" or "/" in judged or "\\" in judged):
        return 0
    if _TECH.match(judged) or _ENV_NAME.match(judged):
        return 0
    if not any(c.isalpha() for c in judged):
        return len(core) if judged.isdigit() and not strict else 0
    if len(judged) < 6 or not any(c in _STRONG for c in judged):
        return 0
    if strict and len(judged) < 20:
        cased = any(c.islower() for c in judged) and any(c.isupper() for c in judged)
        symbol = any(c in "!@#$%^&" for c in judged)
        if len(judged) < 8 or not (symbol or (cased and any(c.isdigit() for c in judged))):
            return 0
    return len(core)


def _quoted(v):
    """A quoted value is taken as it stands, unless it is a mask, a placeholder, a variable or a URL.
    With a blank at either end it is no value at all: the words between two quoted phrases."""
    if v != v.strip():
        return False
    v = v.strip("'\"")
    return bool(v) and "[REDACTED" not in v and "://" not in v and any(c.isalnum() for c in v) \
        and v[0] not in "<{%" and not _VARIABLE.match(v)


def _value(strict=False, config=False):
    """Picker for a password rule: anything quoted, or a bare token shaped like a credential."""
    def pick(m):
        groups = m.re.groupindex
        for g in ("dq", "sq"):
            if g in groups and m.group(g) is not None:
                return m.span(g) if _quoted(m.group(g)) else None
        g = "bt" if "bt" in groups and m.group("bt") is not None else "bare"
        if g not in groups or m.group(g) is None:
            return None
        n = _bare(m.group(g), strict, config)
        if not n or (g == "bt" and n != len(m.group(g))):
            return None
        return m.start(g), m.start(g) + n
    return pick


# Glued to an equals sign -- an env file, a command line, a query string -- what follows the keyword
# is the value, whatever its shape ("DB_PASSWORD=hunter"). What stands there without being one: the
# keyword again or a literal of the language (password=password, password=None), a name that ends in
# the keyword (password=self.db_password), a call or a subscript, a variable, a placeholder, a path,
# a URL, a row of one character.
_NAME = re.compile(r"(?:none|null|nil|true|false|undefined)\Z"
                   r"|(?:.*[^A-Za-z])?(?:pass(?:word|wd|phrase)?|pwd|parola)\Z", re.I)
_CAMEL = re.compile(r".*[a-z](?:Pass(?:word|wd|phrase)?|Pwd|Parola)\Z")
_DRIVE = re.compile(r"[A-Za-z]:[\\/]")


def _stands_in(core):
    """Whether what is glued to the equals sign names a value instead of being one."""
    if core[0] in "[<{(%*/~" or "(" in core or "[" in core or "://" in core:
        return True
    if core.startswith(("./", "../")) or len(set(core)) == 1:
        return True
    return bool(_VARIABLE.match(core) or _NAME.match(core) or _CAMEL.match(core) or _DRIVE.match(core))


_shaped = _value()


def _assigned(m):
    """Picker for a keyword, a separator and a value: anything quoted or a bare token shaped like a
    credential, as in every other rule; and, glued to an equals sign, whatever is not a stand-in."""
    span = _shaped(m)
    if span or m.group("bare") is None or m.group("sep") != "=":
        return span
    core = m.group("bare").rstrip(_CLOSERS)
    if len(core) < 4 or _stands_in(core):
        return None
    return m.start("bare"), m.start("bare") + len(core)


def _keyed(m):
    """The value of a quoted key: one token shaped like a credential. A label, a validation rule or
    a sentence under a key named "password" is not one."""
    g = "dq" if m.group("dq") is not None else "sq"
    v = m.group(g)
    if any(c.isspace() for c in v) or _bare(v, config=True) != len(v):
        return None
    return m.span(g)


def _token(m):
    """A long opaque token: 32 hex digits or more, or 20 characters with letters and digits that do
    not read as a file name or a slug."""
    v = m.group("tok").rstrip(_CLOSERS)
    if not re.fullmatch(r"[0-9a-fA-F]{32,}", v):
        if len(v) < 20 or "/" in v or _ENV_NAME.match(v):
            return None
        if not re.search(r"[A-Za-z]", v) or not re.search(r"\d", v):
            return None
        if re.fullmatch(r"[a-z0-9]+(?:[-_.][a-z0-9]+)+", v) and not re.search(r"[0-9a-f]{12,}", v):
            return None
    return m.start("tok"), m.start("tok") + len(v)


def _opaque(m):
    """The value of a name that merely ends in "key": only 32 hex digits or more, or 40 characters
    with letters and digits. A cache key, a sort key or a UUID under such a name stays, and so does
    a public key."""
    if re.search(r"pub(?:lic)?[\"' _-]{0,2}\Z", m.string[max(0, m.start() - 10):m.start()], re.I):
        return None
    v = m.group("tok").rstrip(_CLOSERS)
    if not re.fullmatch(r"[0-9a-fA-F]{32,}", v) and not (len(v) >= 40 and re.search(r"[A-Za-z]", v)
                                                         and re.search(r"\d", v)):
        return None
    return m.start("tok"), m.start("tok") + len(v)


def _url_password(m):
    v = m.group("pw")
    if v[0] in "$<{*%[" or v.lower() in ("pass", "password", "passwd", "parola", "pw", "xxx", "secret", "token"):
        return None
    return m.span("pw")


def _named(group):
    return lambda m: m.span(group)


_VAL = (r"(?:\"(?P<dq>[^\"\n]{4,128})\"|'(?P<sq>[^'\n]{4,128})'|`(?P<bt>[^`\s]{4,128})`"
        r"|(?P<bare>[^\s'\"`]{4,200}))")
_BARE = r"(?P<bare>[^\s'\"`]{4,200})"
_KW = r"(?:pass(?:word|wd|phrase)|pwd|parol(?:a|\u0103|ei))"
# ... and, right before its separator, the short form that ends a name: DB_PASS, $pass, ?pass, --pass=
_KW_NAME = r"(?:" + _KW + r"|(?<=[_.$?&])pass|(?<=-)pass(?==))"
_WORDS0 = r"(?:[ \t]+[^\s:=.,;()\[\]`'\"]{1,30}){0,4}?"      # "of the admin account", "bazei de date"
_WORDS1 = r"(?:[ \t]+[^\s:=.,;()\[\]`'\"]{1,30}){1,4}?"
_IS = r"(?:is|was|e|este|era)"
_TOKEN_VAL = r"['\"`]?(?P<tok>[A-Za-z0-9_\-+/=.]{20,200})"
_KEY = (r"(?:(?:[\w.\-]*[_.\-])?pass|[\w.\-]*(?:password|passwd|passphrase|pwd|parola|secret|token|psk"
        r"|api[_-]?key|private[_-]?key|preshared[_-]?key))")

RULES = [
    # "password: x", "DB_PASS=x", "parola: x", "passphrase = x" -- a keyword, a separator, a value.
    # Only the value is masked, and a bare one only when it is shaped like a credential or glued to
    # an equals sign: "1Password: the Acme vault" and "NOPASSWD: /usr/bin/x" stay as written.
    ("PASSWORD", re.compile(_KW_NAME + r"(?P<sep>[ \t]*[:=][ \t]*)" + _VAL, re.I), _assigned),
    # "Parola de admin: x", "password for the vault = x"
    ("PASSWORD", re.compile(r"\b" + _KW + _WORDS1 + r"[ \t]*[:=][ \t]*" + _BARE, re.I), _value()),
    # "the database password is x", "parola de admin e x"
    ("PASSWORD", re.compile(r"\b" + _KW + _WORDS0 + r"[ \t]+" + _IS + r"[ \t]+" + _VAL, re.I), _value()),
    # "cu parola x", "with the password x"
    ("PASSWORD", re.compile(r"\b(?:cu|with)[ \t]+(?:the[ \t]+)?(?:parola|password)[ \t]+" + _VAL, re.I), _value()),
    # "password": "x", 'db_pass' => 'x' -- JSON, YAML and PHP arrays, where the key is quoted too
    ("SECRET_VALUE", re.compile(r"(?<![\w])[\"']" + _KEY + r"[\"'][ \t]*(?::|=>?)[ \t]*"
                                r"(?:\"(?P<dq>[^\"\n]{4,512})\"|'(?P<sq>[^'\n]{4,512})')", re.I), _keyed),
    # configuration and command syntax: a keyword, a space, a quoted token
    ("PASSWORD", re.compile(r"(?:password|passwd|passphrase|parola|secret|psk)[ \t]+"
                            r"(?:\"(?P<dq>[^\"\s]{6,200})\"|'(?P<sq>[^'\s]{6,200})')", re.I), _value()),
    # ... or a bare one, after a keyword that only configuration and command lines use
    ("PASSWORD", re.compile(r"(?<![\w-])(?:encrypted-password|plaintext-password|pre-shared-secret"
                            r"|--pass(?:word|wd|phrase)?)[ \t]+" + _BARE, re.I), _value(config=True)),
    # ... or a bare one after "password" alone, when it can hardly be a word
    ("PASSWORD", re.compile(r"\b(?:parola|password|passwd)[ \t]+" + _BARE, re.I), _value(strict=True)),
    # WireGuard: the private and the preshared key (a public key is not a secret)
    ("WIREGUARD_KEY", re.compile(r"\b(?:private[ _-]?key|preshared[ _-]?key|psk)[ \t]*[:=]?[ \t]*['\"]?"
                                 r"(?P<key>[A-Za-z0-9+/]{43}=)", re.I), _named("key")),
    # otpauth://totp/Label?secret=x -- a TOTP seed is short enough to pass for a word
    ("TOTP_SECRET", re.compile(r"\botpauth://[^\s\"'`]{0,200}?[?&]secret=(?P<key>[A-Za-z2-7]{16,})", re.I),
     _named("key")),
    # "X-Auth-Token: x", "client_secret=x", "tokenul: x"
    ("TOKEN", re.compile(r"(?:token|secret)(?:ul)?[\"']?[ \t]*[:=][ \t]*" + _TOKEN_VAL, re.I), _token),
    # an API key whose alphabet API_KEY above does not know ("." and "+" and "/")
    ("API_KEY", re.compile(r"(?:api[_-]?key|apikey|access[_-]?key)[\"']?[ \t]*[:=][ \t]*" + _TOKEN_VAL, re.I),
     _token),
    # "key": "x", Local key: x, ?key=x -- any name ending in "key", when the value is long and opaque
    ("SECRET_KEY", re.compile(r"key[\"']?[ \t]*(?::|=>?)[ \t]*[\"'`]?(?P<tok>[A-Za-z0-9_\-+=.]{32,200})", re.I),
     _opaque),
    # "the api key is x", "tokenul este x"
    ("TOKEN", re.compile(r"(?:token(?:ul)?|secret(?:ul)?|api[ _-]?key|apikey|cheia[ \t]+api)" + _WORDS0
                         + r"[ \t]+" + _IS + r"[ \t]+" + _TOKEN_VAL, re.I), _token),
    # scheme://user:password@host
    ("URL_PASSWORD", re.compile(r"\b[a-z][a-z0-9+.\-]{1,20}://[^\s:/@]{0,64}:(?P<pw>[^\s@/]{1,128})@(?=[^\s@/])",
                                re.I), _url_password),
]


def _spans(text, rx, pick):
    """The spans this rule would mask in text: what its regex caught and its picker accepted."""
    last = 0
    for m in rx.finditer(text):
        span = pick(m)
        if span and span[0] >= last and span[1] > span[0]:
            last = span[1]
            yield span


def _mask(text, label, rx, pick):
    """`text` with every value this rule accepts replaced by its mask, and how many there were."""
    out, last, n = [], 0, 0
    for a, b in _spans(text, rx, pick):
        out.append(text[last:a])
        out.append(f"[REDACTED:{label}]")
        last, n = b, n + 1
    out.append(text[last:])
    return "".join(out), n


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
    text = text or ""
    labels = [label for label, rx in PATTERNS if rx.search(text)]
    if _HINT.search(text):
        for label, rx, pick in RULES:
            if label not in labels and next(_spans(text, rx, pick), None):
                labels.append(label)
    return labels


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
    counts = {}
    out = text or ""
    for label, rx in PATTERNS:
        out, n = rx.subn(f"[REDACTED:{label}]", out)
        if n:
            counts[label] = counts.get(label, 0) + n
    if _HINT.search(out):
        for label, rx, pick in RULES:
            out, n = _mask(out, label, rx, pick)
            if n:
                counts[label] = counts.get(label, 0) + n
    return out, list(counts.items())


def describe(found):
    """'API_KEY x2, PASSWORD' — human-readable summary of redact() results."""
    return ", ".join(f"{label} x{n}" if n > 1 else label for label, n in found)
