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
- Your assessment of impact (data exposure, code execution, sandbox escape, etc.).

You should expect a first response within 7 days. If the issue is confirmed, a fix will be prepared on a private branch before public disclosure. You'll be credited in the release notes unless you ask otherwise.

## Scope

mem0ry4ai is a non-sandboxed terminal — by design, the shell it spawns inherits full user privileges. The following are **NOT** vulnerabilities:

- The shell can read/write any file the user can read/write.
- Arbitrary commands typed by the user execute with the user's privileges.
- The app does not run in the macOS App Sandbox.

The following **ARE** in scope:

- Code execution triggered by **untrusted terminal output** (escape sequence injection, OSC handling bugs, etc.).
- Crashes triggered by hostile remote shells (over SSH).
- Issues in our `ShellIntegration` writer (`~/.zshrc` corruption, command injection via filename, etc.).
- Memory safety bugs in `LocalTerminal.swift` / `PuttyTerminalView`.
