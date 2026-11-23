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
import re
import shutil
import signal
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
MEM = os.path.join(HERE, "mem.py")
PID_FILE = ".web-server.pid"
NOT_RUNNING = "is not running"


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
                if "mem0ry4ai" in cmd or "session_start.py" in cmd or "capture.py" in cmd \
                        or "prompt_recall.py" in cmd or "tool_recall.py" in cmd:
                    found.append((event, cmd))
    return found


def _setting(key, data):
    """One setting, resolved the way mem.py resolves it: the environment first, then the per-machine
    .mem-local.env the web UI saves to. Parsed here rather than imported, for the reason above."""
    v = os.environ.get(key)
    if v:
        return v
    path = os.path.join(data if os.environ.get("MEM_DATA_DIR") else HERE, ".mem-local.env")
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("export "):
                    line = line[7:].strip()
                k, sep, val = line.partition("=")
                if sep and k.strip() == key:
                    return val.strip().strip('"').strip("'")
    except OSError:
        pass
    return None


def _read_pid(path):
    """The pid recorded in a pid file, or None when the file holds anything else.

    Zero and negative numbers are refused here, before anything can use them: to kill(2) they are
    not processes but "my whole process group" and "everything I am allowed to signal"."""
    try:
        with open(path, encoding="utf-8") as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        return None
    return pid if pid > 0 else None


def _inspect(pid):
    """(alive, command line) of a pid. The command line is None when the process exists but this
    machine gives no way to read it — a reason to leave it alone, never a reason to guess."""
    if os.name == "nt":
        # NOT os.kill(pid, 0): on Windows any signal other than the two console events goes to
        # TerminateProcess, so the customary "is it alive?" probe is itself a kill.
        probes = (
            ["wmic", "process", "where", f"ProcessId={pid}", "get", "CommandLine", "/value"],
            ["powershell", "-NoProfile", "-Command",
             f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine"],
        )
        for argv in probes:
            try:
                r = subprocess.run(argv, capture_output=True, text=True, timeout=20,
                                   creationflags=_NO_WINDOW)
            except (OSError, subprocess.SubprocessError):
                continue
            if r.returncode != 0:
                continue
            out = r.stdout.strip()
            if argv[0] == "wmic":
                out = out.partition("CommandLine=")[2].strip()
            return (True, out) if out else (False, None)
        try:                                # tasklist knows whether it runs, not what it runs
            r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
                               capture_output=True, text=True, timeout=20, creationflags=_NO_WINDOW)
            return (f'"{pid}"' in r.stdout, None)
        except (OSError, subprocess.SubprocessError):
            return (True, None)             # cannot even tell: treated as alive and unreadable
    try:
        os.kill(pid, 0)
    except PermissionError:
        pass                                # alive, and someone else's
    except (OSError, OverflowError):
        return (False, None)
    try:
        r = subprocess.run(["ps", "-ww", "-p", str(pid), "-o", "command="],
                           capture_output=True, text=True, timeout=10)
        if r.returncode == 0 and r.stdout.strip():
            return (True, r.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    try:                                    # a ps without -p (busybox): Linux has the answer in /proc
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            cmd = f.read().replace(b"\0", b" ").decode("utf-8", "replace").strip()
        return (True, cmd or None)
    except OSError:
        return (True, None)


def _cwd_of(pid):
    """Working directory of a running process, to place a script it was given by relative path."""
    try:
        return os.readlink(f"/proc/{pid}/cwd")                      # Linux
    except OSError:
        pass
    try:                                                            # macOS, BSD
        r = subprocess.run(["lsof", "-b", "-w", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
                           capture_output=True, text=True, timeout=10)
        for line in r.stdout.splitlines():
            if line.startswith("n"):
                return line[1:]
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def _same_file(a, b):
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def _own_server(pid):
    """Is this pid the web server of THIS copy of the code? -> (verdict, what was found).

    True only for a live process whose command line is this install's `mem.py serve`. False for
    anything that is positively something else, a dead pid included. None when it could not be
    checked. Only True is ever signalled: a pid by itself proves nothing — pid files outlive their
    process, the number gets reused, and a port is served by whoever bound it first. Another clone
    of this project running its own `mem.py serve` is not ours to stop either.
    """
    alive, cmd = _inspect(pid)
    if not alive:
        return False, NOT_RUNNING
    if cmd is None:
        return None, "is running, but its command line cannot be read on this machine"
    shown = cmd if len(cmd) <= 100 else cmd[:97] + "..."
    flags = re.IGNORECASE if os.name == "nt" else 0
    # The way the hook starts it: an absolute path, matched whole so a space inside it is harmless.
    for path in {MEM, os.path.realpath(MEM)}:
        if re.search(re.escape(path) + r"[\"']?\s+serve(?=\s|$)", cmd, flags):
            return True, shown
    # Started by hand: `python3 mem.py serve`, `./mem.py serve`. The path only means something
    # relative to that process's working directory.
    m = re.search(r"(?:^|\s)[\"']?(\S*mem\.py)[\"']?\s+serve(?=\s|$)", cmd, flags)
    if not m:
        return False, f"is another process ({shown})"
    script = m.group(1)
    if not os.path.isabs(script):
        cwd = _cwd_of(pid)
        if not cwd:
            return None, f"runs a mem.py that cannot be placed, its path is relative ({shown})"
        script = os.path.join(cwd, script)
    if not _same_file(script, MEM):
        return False, f"is the server of another install ({shown})"
    return True, shown


def _listeners(port):
    """Pids listening on a local TCP port, or None when nothing on this machine can tell."""
    found, asked = [], False
    if os.name == "nt":
        probes = (["netstat", "-ano", "-p", "TCP"],)
    else:
        # -b: lsof otherwise stat()s every open file, and hangs with a stalled network mount.
        probes = (["lsof", "-b", "-w", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
                  ["ss", "-Hltnp", f"sport = :{port}"])
    for argv in probes:
        try:
            r = subprocess.run(argv, capture_output=True, text=True, timeout=20,
                               creationflags=_NO_WINDOW)
        except (OSError, subprocess.SubprocessError):
            continue
        asked = True
        for line in r.stdout.splitlines():
            if argv[0] == "lsof":
                m = re.fullmatch(r"\s*(\d+)\s*", line)
            elif argv[0] == "ss":
                m = re.search(r"pid=(\d+)", line)
            else:
                m = re.match(r"\s*TCP\s+\S+:%d\s+\S+\s+LISTENING\s+(\d+)\s*$" % port, line)
            if m and int(m.group(1)) not in found:
                found.append(int(m.group(1)))
        if found:
            break
    return found if asked else None


def _terminate(pid):
    """Ask a server that passed _own_server to exit; True once it is gone."""
    if os.name == "nt":
        r = subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True,
                           creationflags=_NO_WINDOW)
        return r.returncode == 0
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    deadline = time.time() + 5
    while time.time() < deadline:
        # "No longer ours" rather than "no longer there": until its parent collects it, a process
        # that has exited still answers kill(pid, 0).
        if _own_server(pid)[0] is False:
            return True
        time.sleep(0.1)
    return False


def stop_web_server(dry):
    """Stop the server the SessionStart hook launches. It is detached, so nothing else will.

    The pid file says where to look; it is not believed. Whatever it names, and whatever listens on
    the port when the file is missing or wrong, has to pass _own_server before it is signalled.
    Everything that does not pass is reported and left alone.
    """
    data = _data_dir()
    pid_file = os.path.join(data, PID_FILE)
    try:
        port = int(_setting("MEM_WEB_PORT", data) or 8841)
    except ValueError:
        port = 8841
    said = []

    def say(msg):
        print(("  web server : " if not said else "               ") + msg)
        said.append(msg)

    target = how = None
    stale = False
    pid = _read_pid(pid_file)
    if pid is not None:
        ok, found = _own_server(pid)
        if ok:
            target, how = pid, "from the pid file"
        else:
            stale = ok is False
            say(f"the pid file names pid {pid}, which {found}"
                + ("" if found == NOT_RUNNING else " — left alone"))
    elif os.path.exists(pid_file):
        stale = True
        say("the pid file does not hold a pid")

    if target is None:
        listeners = _listeners(port)
        if listeners is None:
            say(f"no way on this machine to see what listens on port {port} — nothing signalled")
        for lp in listeners or []:
            if lp == pid:
                continue                    # already judged, and reported, above
            ok, found = _own_server(lp)
            if ok:
                target, how = lp, f"found listening on port {port}"
                break
            say(f"port {port} is served by pid {lp}, which {found} — left alone")

    if target is None:
        if not said:
            say(f"not running (no pid file, nothing listening on port {port})")
    elif dry:
        say(f"would stop pid {target} ({how})")
    else:
        try:
            if _terminate(target):
                say(f"stopped (pid {target}, {how})")
                stale = True                # whatever the file says now, that server is gone
            else:
                say(f"asked pid {target} to stop ({how}), but it is still running")
        except OSError as e:
            say(f"could not stop pid {target} ({e})")

    if stale and os.path.exists(pid_file):
        if dry:
            say("would remove the stale pid file")
        else:
            try:
                os.remove(pid_file)
                say("removed the stale pid file")
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
