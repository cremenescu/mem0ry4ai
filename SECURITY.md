# Security Policy

## Supported Versions

Only the latest release on the `main` branch receives security fixes. While in `v0.x`-alpha, expect rapid iteration — pin a specific version if you need stability.

| Version | Supported |
|---------|-----------|
| latest `v0.x`-alpha | yes |
| anything older | no |

## Reporting a Vulnerability

**Do not open a public issue for security problems.**

Email [razvan@cremenescu.ro](mailto:razvan@cremenescu.ro) with:

- A clear description of the problem.
- Steps to reproduce, ideally with a minimal proof of concept.
- The affected version (commit SHA or release tag).
- Your assessment of impact (context hijacking, data exposure, code execution, etc.).

You should expect a first response within 7 days. If the issue is confirmed, a fix will be prepared on a private branch before public disclosure. You'll be credited in the release notes unless you ask otherwise.

## What this software is, for threat-modelling purposes

mem0ry4ai stores durable knowledge as markdown files in a git repository on your machine, and feeds
a relevant slice of it into an AI agent's context at the start of every session. Two properties
follow, and they shape everything below:

1. **Stored memories are re-injected into a trusted position, every session.** A memory is not inert
   data — it is text that will sit in an agent's context alongside the user's own instructions,
   repeatedly, for as long as it exists. That makes the write path a security boundary.
2. **The store is plain files under version control.** Anything written there is recoverable from
   git history even after it is removed, so a credential that lands in the store is a credential you
   must rotate, not merely delete.

## In scope

- **Stored prompt injection.** Any way for a low-trust write (most realistically an agent's MCP
  `memory_add`, since the store is shared between agents) to forge structure that the SessionStart
  injection or the MCP server treats as trusted: forging a record delimiter to create a second
  record, escalating a memory to `priority: critical`, or forging one of the injection's own section
  headings. Two rounds of hardening have gone into this; new bypasses are wanted.
- **Egress bypass.** Any path by which a memory classified `private` reaches a model, or a
  `redacted` memory's body does. Every surface is supposed to resolve this through one function,
  `emit_for()`; a surface that reads records directly and skips it is a vulnerability. (One such
  path — fetching by id over MCP — was found and fixed in v0.17.0.)
- **Redaction bypass.** Credential shapes that `redact.py` fails to catch on the write path, given
  that the store is git-versioned and often mirrored publicly. It knows a set of formats and judges
  the value that follows a keyword by its shape; it is known to be incomplete — a password made
  only of letters, after a colon or in a sentence, is left alone, because nothing in its shape
  tells it from a word. Concrete misses beyond that are useful reports, and so is ordinary prose
  that it masks.
- **The web UI**, which binds to loopback by default and has no login: CSRF on state-changing
  endpoints, path traversal, template injection, or anything that lets a page in the user's browser
  drive it or read it. The server answers only to the names it knows (`localhost`, `127.0.0.1`,
  `[::1]` on its own port, plus `MEM_WEB_ALLOWED_HOSTS`) and refuses a `POST` whose `Origin` is not
  the host it was addressed to; a way around either check — DNS rebinding included — is in scope.
- **The MCP server**: malformed or hostile JSON-RPC over stdio causing crashes, unbounded resource
  use, or writes the caller should not be able to make (note `MEM_MCP_WRITE=0` exists to make it
  read-only).
- **`/api/propose`**, the HTTP ingestion endpoint: anything that gets content past review into the
  store itself, rather than into the queue, and anything that lets a caller without the key in
  `staging/.propose-key` — a web page above all — queue a proposal.
- Code execution from parsing a hostile store file, transcript, or API payload.

## Not in scope

- **The store is not encrypted.** Anyone who can read your disk can read your memories. This is a
  deliberate trade: plain markdown is the reason the store can be diffed, reviewed and recovered
  with ordinary tools. If your threat model includes disk access, use full-disk encryption.
- **The user's own agent acting on the user's own machine.** Memories describe real work; an agent
  reading them and then doing something with the user's privileges is the product working.
- **Anything requiring an attacker who already has local code execution as the user**, since they
  can simply read `store/`.
- **Optional local services you point it at** (Ollama, a local LLM endpoint). Their security is
  theirs; report to them. What *is* in scope is us sending them something we should not.
- **Memories you deliberately marked `open` reaching a model.** That is what `open` means. Use
  `--tier private` for what must not travel.
