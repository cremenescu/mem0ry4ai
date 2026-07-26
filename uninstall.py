#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Undo a mem0ry4ai install — on macOS, Linux and Windows alike.

The rule this whole script is built around: **uninstalling the software never deletes the
memories.** They are the thing you spent months accumulating; the code is a `git clone` away. So
the store is left exactly where it is and its location is printed, and removing it takes a separate
flag and a typed confirmation. An uninstaller that quietly takes the data with it is a data-loss
bug wearing a helpful face.

What it does undo, because these are things the installer added to YOUR system and you cannot be
expected to remember them:

  1. the Claude Code hooks, in both user and project settings.json
  2. the scheduled maintenance job (launchd on macOS)
  3. the running web server it started

Usage:
  python3 uninstall.py                 # show exactly what would be removed, change nothing
  python3 uninstall.py --yes           # do it
  python3 uninstall.py --yes --delete-memories   # ...and destroy the store (asks you to type it)
"""
import argparse
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def _data_dir():
    """Where the memories are — resolved the same way mem.py does, without importing it.

    Importing mem would be tidier, but an uninstaller has to work on a broken install too: half a
    clone, a missing dependency, a python that no longer matches. It should never be the thing that
    cannot run.
    """
    d = os.environ.get("MEM_DATA_DIR")
    if d:
        return os.path.abspath(os.path.expanduser(d))
    home_store = os.path.expanduser("~/.mem0ry4ai")
    if os.path.isdir(os.path.join(home_store, "store")):
        return home_store
    return HERE


def settings_paths():
    out = [os.path.expanduser("~/.claude/settings.json")]
    proj = os.path.join(os.getcwd(), ".claude", "settings.json")
    if proj not in out:
        out.append(proj)
    return out


def find_hooks(path):
    """Which mem0ry4ai hooks are registered in one settings.json. Read-only."""
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    found = []
    for event, entries in (data.get("hooks") or {}).items():
        for entry in entries if isinstance(entries, list) else []:
            for h in (entry.get("hooks") or []):
                cmd = str(h.get("command", ""))
                if "mem0ry4ai" in cmd or "session_start.py" in cmd or "capture.py" in cmd:
                    found.append((event, cmd))
    return found


def stop_web_server(dry):
    """Stop the server the SessionStart hook launches. It is detached, so nothing else will."""
    data = _data_dir()
    pid_file = os.path.join(data, ".web-server.pid")
    pid = None
    if os.path.exists(pid_file):
        try:
            pid = int(open(pid_file).read().strip())
        except (OSError, ValueError):
            pid = None
    if pid is None:
        print("  web server : no pid file — nothing recorded as running")
        return
    if dry:
        print(f"  web server : would stop pid {pid}")
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True,
                           creationflags=_NO_WINDOW)
        else:
            os.kill(pid, 15)
        print(f"  web server : stopped (pid {pid})")
    except (OSError, ProcessLookupError) as e:
        print(f"  web server : could not stop pid {pid} ({e}) — it may already be gone")
    try:
        os.remove(pid_file)
    except OSError:
        pass


def remove_hooks(dry):
    any_found = False
    for path in settings_paths():
        hooks = find_hooks(path)
        if not hooks:
            continue
        any_found = True
        label = "user" if path.startswith(os.path.expanduser("~/.claude")) else "project"
        if dry:
            print(f"  hooks      : would remove {len(hooks)} from {path}")
            for event, cmd in hooks:
                print(f"               {event}: {cmd[:88]}")
            continue
        r = subprocess.run([sys.executable, os.path.join(HERE, "hooks", "install.py"),
                            "--target", label, "--uninstall"],
                           capture_output=True, text=True, creationflags=_NO_WINDOW)
        ok = r.returncode == 0
        print(f"  hooks      : {'removed from' if ok else 'FAILED on'} {path}")
        if not ok:
            print("               " + (r.stderr or r.stdout).strip()[:200])
    if not any_found:
        print("  hooks      : none registered")


def remove_scheduled_job(dry):
    # The scheduled job is launchd, which exists only on macOS. Claiming elsewhere that something
    # "would be removed" is the same lie as claiming it was: it describes work that cannot happen.
    if sys.platform != "darwin":
        print("  maintenance: no scheduled job on this platform (launchd is macOS only)")
        return
    script = os.path.join(HERE, "mem_maintenance.py")
    if not os.path.exists(script):
        print("  maintenance: mem_maintenance.py not present")
        return
    plist = os.path.expanduser("~/Library/LaunchAgents/ro.vtun.mem0ry4ai-maintenance.plist")
    if not os.path.exists(plist):
        print("  maintenance: no scheduled job installed")
        return
    if dry:
        print("  maintenance: would remove the scheduled job")
        return
    r = subprocess.run([sys.executable, script, "uninstall"], capture_output=True, text=True,
                       creationflags=_NO_WINDOW)
    print("  maintenance: " + ((r.stdout or r.stderr).strip().splitlines() or ["done"])[0])


def report_leftovers(data):
    print()
    print("Left in place, on purpose:")
    print(f"  your memories        {data}")
    if os.environ.get("MEM_DATA_DIR"):
        # Tell people how to clear it on the machine they are actually on, not on the one this
        # paragraph was first written for.
        how = ('setx MEM_DATA_DIR ""  (or clear it in System > Environment Variables)'
               if os.name == "nt" else
               "remove the MEM_DATA_DIR line from your shell profile (~/.zshrc, ~/.bashrc, ...)")
        print(f"  MEM_DATA_DIR is set and points there. To stop pointing at it: {how}")
    print(f"  the code             {HERE}")
    print("  Remove the code with a plain directory delete once you are done — it is a clone,")
    print("  and deleting it does not touch the store above unless the store is inside it.")
    if os.path.commonpath([os.path.abspath(data), HERE]) == HERE:
        print()
        print("  NOTE: your store is INSIDE the code folder. Deleting the folder WOULD delete your")
        print("  memories. Move the store elsewhere first, or back it up, before removing anything.")


def confirm_destroy(data):
    print()
    print(f"About to permanently delete every memory in: {data}")
    print("This cannot be undone from here. Type the word DELETE to confirm.")
    try:
        return input("> ").strip() == "DELETE"
    except (EOFError, KeyboardInterrupt):
        return False


def main():
    ap = argparse.ArgumentParser(description="Undo a mem0ry4ai install. Never deletes memories "
                                             "unless you explicitly ask twice.")
    ap.add_argument("--yes", action="store_true", help="actually make the changes (default: preview)")
    ap.add_argument("--delete-memories", action="store_true",
                    help="also destroy the store — requires --yes and a typed confirmation")
    a = ap.parse_args()

    data = _data_dir()
    dry = not a.yes

    print("mem0ry4ai uninstall" + ("  (preview — nothing will change)" if dry else ""))
    print(f"  code       : {HERE}")
    print(f"  memories   : {data}")
    print()
    stop_web_server(dry)
    remove_hooks(dry)
    remove_scheduled_job(dry)

    if a.delete_memories:
        if dry:
            print()
            print(f"  memories   : WOULD DELETE {data} (re-run with --yes to be asked for real)")
        elif confirm_destroy(data):
            try:
                shutil.rmtree(data)
                print(f"  memories   : deleted {data}")
            except OSError as e:
                print(f"  memories   : could NOT delete {data}: {e}")
        else:
            print("  memories   : left alone (not confirmed)")
    else:
        report_leftovers(data)

    if dry:
        print()
        print("Nothing was changed. Re-run with --yes to apply.")


if __name__ == "__main__":
    main()
