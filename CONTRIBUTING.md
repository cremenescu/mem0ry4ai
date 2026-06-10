# Contributing to mem0ry4ai

Thanks for your interest. mem0ry4ai is a small, focused project — keep contributions in the same spirit.

## License

mem0ry4ai is **GPL-2.0-or-later**. By submitting a PR you agree your contribution is licensed under the same terms.

If you copy code from elsewhere, it must be GPL-compatible (MIT, BSD, Apache-2.0, public domain). Include attribution in [NOTICE](NOTICE).

## Issues

Use the issue templates. Before opening:

- Search existing issues (open + closed).
- Reproduce on a clean install of the latest release.
- Include: macOS version, terminal output, steps to reproduce, expected vs actual.

## Pull Requests

1. Open an issue first for non-trivial changes — saves wasted work if the direction isn't right.
2. One logical change per PR. Don't mix refactors with feature additions.
3. Match the existing code style (4-space indent, Swift idioms, comments in Romanian or English — both are fine).
4. Run a Release build locally before submitting: `./build/package.sh test`.
5. Include a SPDX header in new `.swift` files:
   ```swift
   // SPDX-License-Identifier: GPL-2.0-or-later
   // Copyright (c) YYYY Your Name
   ```

## Building from source

```bash
brew install xcodegen
xcodegen generate
xcodebuild -project mem0ry4ai.xcodeproj -scheme mem0ry4ai -configuration Debug \
    -derivedDataPath .build-xcode build
open .build-xcode/Build/Products/Debug/mem0ry4ai.app
```

## Areas that need help

- Additional themes
- bash/fish equivalents of the zsh integration in `App/ShellIntegration.swift`
- Tab cycling UX improvements
- Apple Developer ID signing + notarization (requires paid Apple Dev account)

## What's out of scope

- Mac App Store target — sandbox cripples local shells (`tcsetpgrp` blocked). For a MAS-friendly fork, see the separate `VTerm` project (private).
- SSH/RDP/Telnet protocols embedded — that's `mRemoteNXT`.

## Questions

Open a discussion or email [razvan@cremenescu.ro](mailto:razvan@cremenescu.ro).
