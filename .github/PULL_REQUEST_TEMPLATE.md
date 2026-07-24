## Summary
<!-- 1-3 sentences: what does this PR do and why. -->

## Related issue
<!-- Closes #N, or "no issue — trivial" -->

## Changes
- 
- 
- 

## Testing
<!-- How did you verify this works? -->
- [ ] `python3 tests/test_guards.py` passes
- [ ] `python3 tests/test_guards.py --canary` passes (required if you touched a rule it covers,
      and add your new guard to `MUTATIONS` if you added one)
- [ ] `tools/bench_recall.py` run before/after, if this touches ranking or the embedder
- [ ] Exercised the surfaces this affects: CLI / SessionStart hook / MCP server / web UI
- [ ] Tested against a scratch store (`MEM_DATA_DIR=/tmp/...`), not a real one

## License
- [ ] My contribution is licensed under GPL-2.0-or-later (matches the project).
- [ ] New files include the SPDX header (`# SPDX-License-Identifier: GPL-2.0-or-later`).
- [ ] Any imported third-party code is GPL-compatible and credited in the README.

## Output
<!-- Paste the relevant command output. If the web UI changed, a screenshot. -->
