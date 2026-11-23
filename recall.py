#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Per-prompt recall: the few memories a prompt is about, offered before the model answers it.

SessionStart chooses memories by working directory. That is the wrong key for anyone who starts
sessions at a monorepo root -- 170 of 175 sessions on the store this was built for. The directory
says nothing there; the prompt says everything: "do it like in billing-api", "you know how, it
goes through the bastion", "we reverse-engineered something like this before". Each of those was
a memory that existed, in another project's scope, that nothing brought up.

Keyword only, on purpose. This runs on EVERY prompt, so it must cost tens of milliseconds and
work with no embedder running; and a prompt names things (a project, a host, a file, a tool) far
more often than it paraphrases them.

Precision over recall, also on purpose. A memory this misses costs what it cost before the hook
existed. A wrong one is pushed, unasked, into a context that then carries it -- possibly on a
prompt that was "ok, thanks". So the gate is on RARE shared vocabulary, not on how many words
match: common words match everything and say nothing.

What is kept about a prompt: sizes and counts (`prompt_log`), and which of its words -- only words
that already occur in the store -- were used in which session (`prompt_vocab`). Never the text: it
is the user's, and the transcript already holds it. The word table is what tells conversation from
topic. "You know", "I think", "that thing" are rare in memories, which are written in a technical
register, so rarity alone makes them look precious; but they turn up in a fifth of all sessions,
while "ldap" or "bluetooth" turn up in the few sessions that were about them. Spread across
sessions is the signal, and it is learned from the user, in whatever language they write.
"""
import math
import os
import re
import sqlite3
import time
import unicodedata

import mem

# Function words, folded (no diacritics, lower case). Deliberately short: the corpus does the real
# work -- a word that appears in a quarter of all memories is dropped whatever it is (see _weigh),
# which adapts to the store's language without a list for each one. This only saves the lookups.
STOP = frozenset("""
a ai al ale am ar are as asa asta astea acum aici acolo acest aceasta aceste acel acea aia ala alea
alt alta alte altceva apoi asupra atat atunci au ba bine ca cam cand care cat cate catre ce cea cei
cel cele ceva chiar ci cine cu cum da daca dar de deci deja din doar dupa e ea ei el ele era este eu
face fac faci facem facut fara fi fie fiind foarte fost hai iar ii il imi in inca insa intr intre
iti la le li lor lui ma mai mda mea mei mele meu mi mie mult multe ne nici nimic niste noi nu o ok
or ori pai pana pe pentru peste poate pot poti prea prin putea sa sau se si sunt spre sub ta tale
te ti tine toata toate tot toti totul totusi trebuie tu un una unde unei unui unul uite va vad vezi
voi vom vor vreau vrei zi zic zice zis
the and for are but not you all can had her was one our out has have this that with from they will
would there their what about which when make like just into than then them these some could other
its also how any may should does did been being were your yours ours here where why who whom
please thanks thank yes sure okay
""".split())

# Longest first. Romanian articles and plurals, plus the English plural: enough that "routerele",
# "routerului" and "routers" all look up "router". Verbs are left alone; a prefix handles those.
_SUFFIXES = ("urilor", "urile", "ului", "ilor", "elor", "ele", "ile", "uri", "ul", "le", "ii",
             "ei", "ea", "a", "e", "i", "s")
_TOKEN = re.compile(r"[^\W_]+")          # the split FTS5's unicode61 tokenizer makes: '_' and '-' divide

# The user saying, in so many words, "you should already know this". Every complaint about a
# forgotten memory found in six months of transcripts carried one of these -- "ai uitat", "stii deja
# cum", "am mai facut", "ca pana acum" -- so it is the one moment when offering a memory on thinner
# evidence is what was asked for. Matched on folded text; Romanian and English, since that is what
# the UI speaks. MEM_PROMPT_CUES replaces the pattern; "-" turns this path off. (A bare "ca la X" /
# "like in X" is NOT here: it is ordinary speech nine times in ten. Naming a project is handled
# separately and exactly, in _named_scopes.)
_CUES = (r"\b(?:ai uitat|n-?ai tinut minte|stii (?:deja|ca|cum)|(?:ti|v)-?am (?:zis|spus|cerut|aratat)|"
         r"am zis deja|am mai (?:facut|discutat|vorbit|avut|zis|folosit|rezolvat|patit|lucrat)|"
         r"ca pana acum|ca data trecuta|cum am (?:facut|stabilit|zis|vorbit|procedat)|"
         r"am (?:stabilit|hotarat|decis)|(?:vezi|cauta|uita-te) in memori|"
         r"iti (?:amintesti|aduci aminte)|"
         r"remember|as (?:before|usual|we did)|like (?:we did|last time|before)|"
         r"we (?:already|previously)|last time|you (?:forgot|already know))\b")
_CODE_LINE = re.compile(r"^\s*(?:[\[\]{}()<>$#>|+\-*=:;/\\]|\d{2,}[-:./]\d|[A-Za-z_][\w.]*\s*[=({\[]|\S+\.\S+:\d)")


def _int(key, default):
    try:
        return int(os.environ.get(key, default))
    except (TypeError, ValueError):
        return int(default)


def _float(key, default):
    try:
        v = float(os.environ.get(key, default))
        return v if v == v and abs(v) != float("inf") else float(default)
    except (TypeError, ValueError):
        return float(default)


def fold(s):
    """Lower-cased, diacritics removed -- how the FTS index stores it, and how people type it."""
    return "".join(c for c in unicodedata.normalize("NFKD", (s or "").lower())
                   if not unicodedata.combining(c))


def stem(word):
    """The part of a word worth matching on: one inflection suffix removed, never below 4 chars."""
    for suf in _SUFFIXES:
        if word.endswith(suf) and len(word) - len(suf) >= 4:
            return word[:-len(suf)]
    return word


def has_cue(prompt):
    """Whether the prompt itself says a memory is expected (see _CUES)."""
    pat = os.environ.get("MEM_PROMPT_CUES") or _CUES
    if pat.strip() in ("-", "0"):
        return False
    try:
        return re.search(pat, fold(prompt[:1500])) is not None
    except re.error:
        return False


def _is_prose(line):
    """A line a person wrote, as opposed to one they pasted: mostly letters, and not shaped like
    a log entry, a shell command, an assignment or a path with a line number."""
    s = line.strip()
    if not s or len(s) > 400 or _CODE_LINE.match(s):
        return False
    return sum(c.isalpha() or c.isspace() for c in s) >= 0.8 * len(s)


def own_words(prompt):
    """The part of a prompt the user wrote: what precedes a paste, and what follows it.

    A pasted log or code block has thousands of words and matches something in any store -- and
    mostly in English, where every ordinary word ("permission", "denied", "object") looks rare to
    a store written in another language. People put their request above the paste or below it."""
    head, tail = _int("MEM_PROMPT_HEAD", "500"), 200
    lines = prompt.splitlines()
    if all(_is_prose(l) or not l.strip() for l in lines):
        return prompt if len(prompt) <= head + tail else prompt[:head] + " " + prompt[-tail:]
    top = []
    for l in lines:
        if l.strip() and not _is_prose(l):
            break
        top.append(l)
    bottom = []
    for l in reversed(lines):
        if l.strip() and not _is_prose(l):
            break
        bottom.append(l)
    return (" ".join(top)[:head] + " " + " ".join(reversed(bottom))[-tail:]).strip()


def prompt_terms(prompt):
    """The words of a prompt that could identify a memory, in order, each once."""
    text = own_words(prompt)
    out, seen = [], set()
    for w in _TOKEN.findall(fold(text)):
        if len(w) < 3 or w in STOP or w.isdigit() and len(w) < 3:
            continue
        s = stem(w) if len(w) >= 5 else w
        if s in seen or s in STOP:
            continue
        seen.add(s)
        out.append((w, s))
    return out


def _named_scopes(words, records):
    """Project scopes the prompt names: one of its words is a part of the project's name that at
    most two projects share ("billing" for billing-api; not "api", "web" or the client's name).

    This is the cross-project case the hook exists for -- "do it like in billing-api", said while
    working somewhere else. A memory from the named project is then offered on the lenient terms."""
    owners = {}
    for r in records:
        scope = r["meta"].get("scope", "")
        if scope.startswith("project:"):
            for tok in set(_TOKEN.findall(fold(scope.split(":", 1)[1]))):
                if len(tok) >= 3 and not tok.isdigit():
                    owners.setdefault(tok, set()).add(scope)
    named = set()
    for w in words:
        scopes = owners.get(w, ())
        if 0 < len(scopes) <= 2:
            named |= scopes
    return named


def _fts_token(word, st):
    """How one term is asked of the index: exact when short, by prefix on its stem otherwise."""
    return f'"{word}"' if len(word) < 5 else f'"{st}"*'


def _matches(st, word, tokens):
    if len(word) < 5:
        return word in tokens
    return any(t.startswith(st) for t in tokens)


def _spread(stems):
    """{stem: fraction of known sessions whose prompts used it}. {} until enough sessions are known
    for a fraction to mean something -- then every word counts in full, and only the gate protects."""
    if not stems:
        return {}
    try:
        con = mem._access_con()
        try:
            total = con.execute("SELECT count(DISTINCT session) FROM prompt_vocab").fetchone()[0]
            if total < _int("MEM_PROMPT_LEARN_MIN", "15"):
                return {}
            marks = ",".join("?" * len(stems))
            rows = con.execute(f"SELECT term, count(*) FROM prompt_vocab WHERE term IN ({marks}) "
                               f"GROUP BY term", list(stems)).fetchall()
        finally:
            con.close()
        return {t: c / total for t, c in rows}
    except Exception:
        return {}


def _topicality(frac):
    """1.0 for a word that belongs to few sessions, 0.0 for one that belongs to most of them."""
    lo, hi = _float("MEM_PROMPT_SPREAD_LO", "0.08"), _float("MEM_PROMPT_SPREAD_HI", "0.20")
    if frac <= lo:
        return 1.0
    if frac >= hi:
        return 0.0
    return (hi - frac) / (hi - lo)


def _remember_vocab(session, stems):
    if not session or not stems:
        return
    try:
        con = mem._access_con()
        try:
            con.executemany("INSERT OR IGNORE INTO prompt_vocab (term, session) VALUES (?, ?)",
                            [(s, session) for s in stems])
            con.commit()
        finally:
            con.close()
    except Exception:
        pass


def _idf(n, df):
    return math.log(1.0 + (n - df + 0.5) / (df + 0.5))


def _weigh(con, terms):
    """([(word, stem, weight)], known_stems, rare): the terms that can discriminate, heaviest first.

    weight = how rare the word is among memories x how topical it is among prompts. Three kinds
    drop out: words the store has never seen (no memory can be about them), words in more than a
    quarter of it (this store's stopwords, in whatever language it is in), and words spread across
    most sessions (conversation). `known_stems` is every stem the store knows, for learning.

    `rare` is the weight at which a word counts as rare HERE: found in at most 4% of the store
    (never fewer than two memories). Stated as a share, not a number, because the same word is
    rare in a store of two thousand memories and ordinary in one of thirty -- a fixed threshold
    tuned on the large one would simply never fire on the small one."""
    n = con.execute("SELECT count(*) FROM mem").fetchone()[0] or 1
    ceiling = max(3, int(n * _float("MEM_PROMPT_COMMON", "0.25")))
    rare = _idf(n, max(2, int(n * _float("MEM_PROMPT_RARE", "0.04"))))
    seen = []
    for word, st in terms[:60]:
        try:
            df = con.execute("SELECT count(*) FROM mem WHERE mem MATCH ?",
                             (_fts_token(word, st),)).fetchone()[0]
        except sqlite3.OperationalError:
            continue
        if df > 0:
            seen.append((word, st, df))
    spread = _spread([st for _, st, _ in seen])
    out = []
    for word, st, df in seen:
        if df > ceiling:
            continue
        w = _idf(n, df) * _topicality(spread.get(st, 0.0))
        if w > 0.5:
            out.append((word, st, w))
    out.sort(key=lambda x: -x[2])
    return out[:_int("MEM_PROMPT_TERMS", "24")], [st for _, st, _ in seen], rare


def find(prompt, exclude=(), before=None, session=None):
    """Ranked candidates for a prompt: [(score, record, matched_words)], best first, gate applied.

    `exclude` is a set of ids not to offer (already shown this session). `before` restricts to
    memories created earlier than a timestamp -- for replaying old prompts honestly, so a prompt
    is not credited with finding the memory that was written because of it."""
    info = {"terms": 0, "candidates": 0, "top": 0.0, "cue": has_cue(prompt)}
    terms = prompt_terms(prompt)
    if not terms:
        return [], info
    if mem.index_stale() and not mem.build_index():
        return [], info
    con = sqlite3.connect(mem.index_path())
    try:
        weighed, known, rare = _weigh(con, terms)
        _remember_vocab(session, known)
        info["terms"] = len(weighed)
        if not weighed:
            return [], info
        match = " OR ".join(_fts_token(w, s) for w, s, _ in weighed)
        rows = con.execute("SELECT id FROM mem WHERE mem MATCH ? "
                           "ORDER BY bm25(mem, 0.0, 4.0, 1.0) LIMIT 80", (match,)).fetchall()
    except sqlite3.OperationalError:
        return [], info
    finally:
        con.close()
    info["candidates"] = len(rows)
    by_id = {r["id"]: r for r in mem.all_records()}
    strong = rare                                      # weight from which a term is "rare" here
    # Two rare words in a title, each counted double, come to about four times `rare`; the floor
    # sits a little under that, so such a pair passes and a rare word plus a filler does not.
    floor = _float("MEM_PROMPT_MIN_SCORE", "0") or 3.4 * rare
    title = _int("MEM_PROMPT_TITLE", "140")           # how much of a summary counts as its title
    need = _int("MEM_PROMPT_NEED", "2")               # rare words that must match ...
    need_title = _int("MEM_PROMPT_NEED_TITLE", "2")   # ... and how many of them in the title
    named = _named_scopes({w for w, _ in terms}, by_id.values())
    out = []
    for (rid,) in rows:
        r = by_id.get(rid)
        if not r or rid in exclude:
            continue
        m = r["meta"]
        if m.get("status", "active") != "active" or m.get("type") == "profile" \
                or m.get("priority") == "critical":
            continue                    # retired or scratch; or already injected at session start
        if before and (m.get("created") or "") >= before:
            continue
        scope = m.get("scope", "")
        label = scope.split(":", 1)[1] if scope.startswith("project:") else ""
        summary = mem.record_summary(r)
        s_tok = set(_TOKEN.findall(fold(summary[:title])))
        t_tok = set(_TOKEN.findall(fold(summary[title:])))       # the tail of a long summary
        l_tok = set(_TOKEN.findall(fold(label)))
        b_tok = set(_TOKEN.findall(fold(r.get("body", "") + " " + (m.get("files") or ""))))
        score, hit, n_rare, n_title, n_label, n_tail = 0.0, [], 0, 0, 0, 0
        for word, st, w in weighed:
            in_s = _matches(st, word, s_tok)
            in_l = not in_s and _matches(st, word, l_tok)
            in_t = not in_s and not in_l and _matches(st, word, t_tok)
            if not (in_s or in_l or in_t or _matches(st, word, b_tok)):
                continue
            score += w * (2.0 if in_s or in_l else 1.0)
            hit.append(word)
            if w >= strong:
                n_rare += 1
                n_title += in_s
                n_label += in_l
                n_tail += in_t
        lenient = info["cue"] or scope in named
        if lenient:
            n_title += n_tail            # asked for: anywhere in the summary line will do
        # The project's name stands in for ONE title word, never for all of them.
        n_title = n_title + min(n_label, 1) if n_title else 0
        # The gate. A memory is offered only when words that are RARE here match it, at least one
        # of them in its title -- the first stretch of the summary, since summaries have grown into
        # paragraphs and "somewhere in the summary" says little. That is what "this memory is about
        # what you just named" looks like. Replaying 6,000 real prompts showed why each part is
        # needed: several middling words in a BODY match any pasted log or code block ("properties,
        # identity, first, new"), one rare word plus a common one is a coincidence ("dry", "run"),
        # and a project's name alone drags in whichever of its memories happen to sort first.
        # Lenient when it was asked for -- the prompt says a memory is expected, or names this
        # memory's project: then one rare word in the title is enough, given a second match.
        want, want_title = (1, 1) if lenient else (need, need_title)
        if n_rare < want or n_title < want_title or len(hit) < 2 or score < floor:
            continue
        out.append((score, r, hit))
    out.sort(key=lambda x: x[1]["meta"].get("created") or "", reverse=True)   # newer wins a tie
    out.sort(key=lambda x: -x[0])
    if out:
        # Company for the best match has to be near it: a fifth result at a third of the score is
        # padding, and padding is what teaches the reader to skip the whole block.
        keep = out[0][0] * _float("MEM_PROMPT_NEAR", "0.6")
        out = [x for x in out if x[0] >= keep]
    info["top"] = round(out[0][0], 2) if out else 0.0
    return out, info


def _mem_cmd(cwd):
    here = os.path.join(mem.ROOT, "mem.py")
    try:
        rel = os.path.relpath(here, cwd) if cwd else here
        return rel if not rel.startswith("../../") else here
    except Exception:
        return here


def _log(session, chars, info, injected, ms, outcome):
    try:
        con = mem._access_con()
        try:
            con.execute("INSERT INTO prompt_log (ts, session, chars, terms, candidates, injected, "
                        "top_score, ms, outcome) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (mem.now_ts(), session, chars, info.get("terms", 0), info.get("candidates", 0),
                         injected, info.get("top", 0.0), ms, outcome))
            con.commit()
        finally:
            con.close()
    except Exception:
        pass


def _short(title, n=60):
    """A title cut for a one-line notice. Summaries have grown into paragraphs -- 278 active ones
    pass 300 characters, one reaches 1,318 -- and the notice lists up to five of them."""
    s = " ".join((title or "").split())
    return s if len(s) <= n else s[:n - 1].rstrip() + "…"


def notice(shown):
    """The one line the USER is shown when a prompt pulled memories in: how many, and which.

    The ids and the project of each are the point. The gate works on rare vocabulary and has no
    notion of domain, so its mistakes are collisions between projects -- a word that is rare in
    the store and means two different things. Nobody could see those: the recall went to the model
    and nowhere else."""
    if not shown or os.environ.get("MEM_RECALL_NOTICE", "1").strip().lower() in ("0", "no", "false", "off"):
        return ""
    n = len(shown)
    if os.environ.get("MEM_UI_LANG") == "ro":
        de = " de" if n >= 20 and not 1 <= n % 100 <= 19 else ""
        head = f"mem0ry4ai · {n}{de} {'memorie' if n == 1 else 'memorii'} la acest prompt: "
    else:
        head = f"mem0ry4ai recall · {n} {'memory' if n == 1 else 'memories'} for this prompt: "
    return head + " · ".join(f"{rid} [{where}] {_short(title)}" for rid, where, title in shown)


def recall(prompt, session=None, cwd=None, log=True, shown=None):
    """The text to add to the context for this prompt, or "" (the usual answer).
    `shown`, when given, is filled with (id, project, summary) of what the text lists."""
    t0 = time.time()
    prompt = prompt or ""
    done = lambda info, n, outcome: log and _log(session, len(prompt), info, n,
                                                 int((time.time() - t0) * 1000), outcome)
    if os.environ.get("MEM_PROMPT_RECALL", "1").strip().lower() in ("0", "no", "false", "off"):
        return ""
    if len(prompt.strip()) < _int("MEM_PROMPT_MIN_CHARS", "12"):
        done({}, 0, "short")
        return ""
    ranked, info = find(prompt, exclude=mem.pushed_this_session(session),
                        session=session if log else None)
    if not ranked:
        done(info, 0, "no-terms" if not info["terms"] else "nothing-relevant")
        return ""
    lines, ids = [], []
    for score, r, hit in ranked:
        # The egress choke point, on the freshly parsed record: a private memory never reaches a
        # model, and that must not depend on how the candidates happened to be found.
        summary = mem.emit_for(r, mem.DEST_AGENT)[0]
        if summary is None:
            continue
        m = r["meta"]
        scope = m.get("scope", "")
        where = scope.split(":", 1)[1] if scope.startswith("project:") else scope
        lines.append(f"- [{r['id']}] {m.get('type', '?')} · {where} — {summary}")
        ids.append(r["id"])
        if shown is not None:
            shown.append((r["id"], where, summary))
        if len(ids) >= _int("MEM_PROMPT_MAX", "5"):
            break
    if not ids:
        done(info, 0, "withheld")
        return ""
    if log:
        mem.log_access(ids, "prompt", session)
    done(info, len(ids), "injected")
    return ("Memories that may bear on this prompt (mem0ry4ai recall: matched on its wording, not "
            f"verified — read one with `{_mem_cmd(cwd)} get <id>` before relying on it). "
            "A memory records what was true when it was written: if it names a file, function or "
            "flag, check that it still exists.\n"
            + "\n".join(lines))


def _typed(d):
    """The text of one transcript line when it is something the user typed, else None.

    Narrower than "a user turn": a transcript also files under `user` the compaction summaries the
    model wrote, skill bodies, command output and tool results. Those are long, technical and not
    the user's words -- counted as prompts they make every technical term look conversational."""
    if d.get("type") != "user" or d.get("isMeta") or d.get("isSidechain") or d.get("isCompactSummary"):
        return None
    content = (d.get("message") or {}).get("content")
    if isinstance(content, list):
        content = " ".join(b.get("text", "") for b in content
                           if isinstance(b, dict) and b.get("type") == "text")
    if not isinstance(content, str):
        return None
    text = content.strip()
    if not text or text[0] in "<[/" or text.startswith("This session is being continued"):
        return None
    return text


def typed_prompts(transcript_path):
    """What the user actually typed in one Claude Code transcript, message by message."""
    import json
    try:
        with open(transcript_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                text = _typed(d)
                if text is not None:
                    yield text
    except OSError:
        return


_OPENED = re.compile(r"mem\.py[\"']?\s+get\s+[\"']?(\d{8}-[0-9a-z]{6})")


def _local(ts):
    """A transcript's UTC timestamp as the local 'YYYY-MM-DD HH:MM:SS' the store writes."""
    import datetime
    try:
        return datetime.datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone() \
            .strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return ""


def turns(transcript_path):
    """One transcript as turns: (message id, local time, typed prompt, [ids of the memories the
    agent then OPENED -- `mem.py get` or the MCP memory_get -- before the next typed prompt]).

    Opening a memory is the one thing in a transcript that says, without anybody judging it, that
    this memory was needed after that prompt."""
    import json
    cur = None
    try:
        with open(transcript_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                # Cheap rejections before the parse, on substrings that do not depend on how the
                # writer spaced its JSON: a tool's result (the bulk of a transcript) is never
                # something typed, and an assistant line matters only if it opens a memory.
                if '"tool_use_id"' in line:
                    continue
                if '"user"' not in line and not (
                        '"assistant"' in line and ("mem.py" in line or "memory_get" in line)):
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if d.get("isSidechain"):
                    continue
                text = _typed(d)
                if text is not None:
                    if cur:
                        yield cur
                    cur = (d.get("uuid") or "", _local(d.get("timestamp") or ""), text, [])
                elif d.get("type") == "assistant" and cur:
                    for b in (d.get("message") or {}).get("content") or []:
                        if not isinstance(b, dict) or b.get("type") != "tool_use":
                            continue
                        inp = b.get("input") if isinstance(b.get("input"), dict) else {}
                        if str(b.get("name", "")).endswith("memory_get"):
                            m = re.match(r"\d{8}-[0-9a-z]{6}", str(inp.get("id", "")))
                            if m:
                                cur[3].append(m.group(0))
                        else:
                            cur[3].extend(_OPENED.findall(str(inp.get("command", ""))))
    except OSError:
        return
    if cur:
        yield cur


def cases_path():
    return os.path.join(mem.DATA, "eval", "recall_cases.json")


def build_cases(n=150, data_dir=None):
    """Pick the benchmark from the transcripts on disk: {"built", "lookups": [...], "quiet": [...]}.

    A lookup is a typed prompt after which the agent went and opened a memory that already existed
    at that moment. Nobody labelled it; the agent's own next move did. The quiet set is prompts
    after which it opened nothing. Both are picked evenly across time, the same way on every run,
    and they hold pointers (transcript, message id) and memory ids -- never the prompt's text,
    which is the user's and stays where it was written."""
    import sessions
    created = {r["id"]: (r["meta"].get("created") or "")[:19] for r in mem.all_records()}
    least = _int("MEM_PROMPT_MIN_CHARS", "12")
    lookups, quiet = [], []
    for ptr in sessions._iter_pointers(data_dir or mem.DATA):
        tpath, sid = ptr.get("transcript_path"), ptr.get("session_id")
        for uuid, ts, text, opened in turns(tpath):
            if not uuid or not ts or len(text) < least:
                continue
            # Only a memory that was already there: one written after the prompt, perhaps because
            # of it, is not something any recall could have offered.
            expect = sorted({i for i in opened if created.get(i) and created[i] < ts})
            row = {"session": sid, "path": tpath, "uuid": uuid, "ts": ts}
            if expect:
                lookups.append(dict(row, expect=expect))
            elif not opened:
                quiet.append(row)

    def spread(rows):
        rows.sort(key=lambda r: (r["ts"], r["uuid"]))
        if len(rows) <= n:
            return rows
        step = len(rows) / n
        return [rows[int(i * step)] for i in range(n)]
    return {"built": mem.now_ts(), "lookups": spread(lookups), "quiet": spread(quiet)}


def evaluate(cases):
    """Score the recall on a benchmark. Each prompt is replayed as of its own moment (`before`),
    nothing is logged and nothing is learned.

    Two numbers matter. `found` of `lookups`: how often the recall would have put the memory in
    front of the agent before it had to go looking. `silent` of `quiet`: how often it says nothing
    when nothing was needed. A change that raises the first by lowering the second has only
    become louder."""
    status = {r["id"]: r["meta"].get("status", "active") for r in mem.all_records()}
    top = _int("MEM_PROMPT_MAX", "5")
    texts = {}

    def prompt(case):
        if case["path"] not in texts:
            texts[case["path"]] = {u: t for u, _, t, _ in turns(case["path"])}
        return texts[case["path"]].get(case["uuid"])

    out = dict.fromkeys(("lookups", "found", "spoke", "offered", "retired", "unreadable",
                         "quiet", "silent", "noise"), 0)
    parse = mem.all_records
    snapshot = parse()
    mem.all_records = lambda: snapshot          # hundreds of replays, one parse of the store
    try:
        for case in cases.get("lookups", []):
            text = prompt(case)
            if text is None:
                out["unreadable"] += 1          # the transcript is gone
                continue
            # A memory retired since is not held against the recall: it would not offer it today.
            expect = [i for i in case.get("expect", []) if status.get(i) == "active"]
            if not expect:
                out["retired"] += 1
                continue
            got = [r["id"] for _, r, _ in find(text, before=case["ts"])[0][:top]]
            out["lookups"] += 1
            out["found"] += any(i in got for i in expect)
            out["spoke"] += bool(got)
            out["offered"] += len(got)
        for case in cases.get("quiet", []):
            text = prompt(case)
            if text is None:
                out["unreadable"] += 1
                continue
            got = find(text, before=case["ts"])[0][:top]
            out["quiet"] += 1
            out["silent"] += not got
            out["noise"] += len(got)
    finally:
        mem.all_records = parse
    return out


def _report(cases, s):
    pct = lambda a, b: f"{100 * a / b:.0f}%" if b else "-"
    skipped = s["retired"] + s["unreadable"]
    lines = [f"recall benchmark (built {cases.get('built', '?')})",
             f"lookups: {s['lookups']} prompts after which the agent went and opened a memory",
             f"  the recall would have offered that memory:  {s['found']} of {s['lookups']} ({pct(s['found'], s['lookups'])})",
             f"  it offered anything at all:                 {s['spoke']} of {s['lookups']}, "
             f"{(s['offered'] / s['spoke'] if s['spoke'] else 0):.1f} memories each",
             f"quiet:   {s['quiet']} prompts after which it opened nothing",
             f"  the recall stays silent:                    {s['silent']} of {s['quiet']} ({pct(s['silent'], s['quiet'])})"]
    if skipped:
        lines.append(f"skipped: {s['retired']} whose memory was retired since, {s['unreadable']} whose transcript is gone")
    return "\n".join(lines)


def learn(data_dir=None):
    """Warm the word/session table from the transcripts already on disk. Returns (sessions, words).

    Without it the hook learns as it goes and needs a couple of weeks of prompts before it can tell
    conversation from topic. The transcripts hold years of them. Idempotent: a session already
    learned is overwritten with the same rows."""
    import sessions
    data_dir = data_dir or mem.DATA
    if mem.index_stale() and not mem.build_index():
        return 0, 0
    idx = sqlite3.connect(mem.index_path())
    known_cache, n_sessions, n_rows = {}, 0, 0
    try:
        for ptr in sessions._iter_pointers(data_dir):
            sid, path = ptr.get("session_id"), ptr.get("transcript_path")
            stems = set()
            for text in typed_prompts(path):
                for word, st in prompt_terms(text):
                    if st not in known_cache:
                        try:
                            known_cache[st] = idx.execute(
                                "SELECT 1 FROM mem WHERE mem MATCH ? LIMIT 1",
                                (_fts_token(word, st),)).fetchone() is not None
                        except sqlite3.OperationalError:
                            known_cache[st] = False
                    if known_cache[st]:
                        stems.add(st)
            if stems:
                _remember_vocab(sid, sorted(stems))
                n_sessions += 1
                n_rows += len(stems)
    finally:
        idx.close()
    return n_sessions, n_rows


if __name__ == "__main__":
    import sys
    if sys.argv[1:2] == ["learn"]:
        s, w = learn()
        print(f"learned the prompt vocabulary of {s} sessions ({w} word/session pairs)")
    elif sys.argv[1:2] == ["eval"]:
        # `eval` scores the recall on the saved benchmark; `eval --build` picks the benchmark first.
        # A benchmark is only worth something while it stays the same, so an existing one is kept
        # unless --force says otherwise.
        import json
        flags = sys.argv[2:]
        where = cases_path()
        if "--build" in flags:
            if os.path.exists(where) and "--force" not in flags:
                sys.exit(f"{where} exists: a benchmark is kept as it is (--force replaces it)")
            os.makedirs(os.path.dirname(where), exist_ok=True)
            with open(where, "w", encoding="utf-8") as f:
                json.dump(build_cases(), f, indent=1)
        if not os.path.exists(where):
            sys.exit("no benchmark yet: python3 recall.py eval --build")
        with open(where, encoding="utf-8") as f:
            cases = json.load(f)
        score = evaluate(cases)
        print(json.dumps(score) if "--json" in flags else _report(cases, score))
    else:
        print(recall(" ".join(sys.argv[1:]) or sys.stdin.read(), log=False) or "(nothing)")
