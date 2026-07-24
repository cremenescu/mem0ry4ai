---
name: Bug report
about: Something is broken or behaves unexpectedly
title: "[bug] "
labels: bug
---

**Describe the bug**
What did you expect to happen vs what actually happened?

**To reproduce**
Steps to reproduce the behavior:
1. ...
2. ...
3. ...

**Environment**
- OS: (e.g. macOS 15.2 / Ubuntu 24.04 / Windows 11)
- Python version: (`python3 -V`)
- mem0ry4ai version: (release tag, or `git rev-parse --short HEAD`)
- Install: (git clone / Claude Code plugin)
- Which surface: (CLI / SessionStart hook / MCP server / web UI) — for MCP, which client
- Embedder: (Ollama model, or none — `mem.py search` prints the mode it actually used)

**Output**
The command you ran and what it printed. For the web UI, the terminal running `mem.py serve`.
For the MCP server, its **stderr** — stdout carries the protocol stream and will not contain errors.

**Please redact your store**
Memories tend to contain things you did not mean to share. Trim or replace real content before
pasting; a minimal reproduction against a scratch store is ideal:
`MEM_DATA_DIR=/tmp/mem-repro python3 mem.py ...`

**Additional context**
Anything else that might be relevant.
