---
name: Feature request
about: Suggest a new feature or improvement
title: "[feature] "
labels: enhancement
---

**The problem**
What user need or workflow is not currently served well?

**Proposed solution**
What you'd like to see. Be concrete — sketches / CLI shape / API ideas welcome.

**Alternatives considered**
What other approaches did you think about, and why this one?

**Scope check**
- [ ] Works with the Python standard library only — no `pip install`, no SDK, no vendored dependency.
      (An optional local service is fine, as long as everything still works when it is absent.)
- [ ] Keeps the markdown store as the source of truth; anything in SQLite stays derived and disposable.
- [ ] Sends memories nowhere by default, and nothing phones home.
- [ ] Does not delete memories — superseding keeps the "what did we believe, and when" history.

**Additional context**
References, related issues, screenshots.
