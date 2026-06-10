#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-or-later
# mem0ry4ai — standalone web server (php -S), no Apache or other web-server dependency.
# Idempotent: if the server is already listening on the port, exits immediately
# (safe to call from the SessionStart hook on every session).
#
#   ./server_web.sh           start (if not running) -> http://127.0.0.1:8841/
#   ./server_web.sh status    show state
#   ./server_web.sh stop      stop the server
#
# Also started AUTOMATICALLY by the SessionStart hook, so the web UI is always
# up while you work with Claude Code.

set -u
DIR="$(cd "$(dirname "$0")" && pwd)"
PORT="${MEM_WEB_PORT:-8841}"
HOST="127.0.0.1"
PIDFILE="$DIR/.web-server.pid"
LOGFILE="$DIR/.web-server.log"

find_php() {
    # MEM_PHP env override, otherwise whatever is on PATH
    if [ -n "${MEM_PHP:-}" ] && [ -x "$MEM_PHP" ]; then echo "$MEM_PHP"; return 0; fi
    command -v php 2>/dev/null && return 0
    return 1
}

is_up() {
    nc -z "$HOST" "$PORT" >/dev/null 2>&1
}

case "${1:-start}" in
  status)
    if is_up; then
        echo "UP  http://$HOST:$PORT/  (pid $(cat "$PIDFILE" 2>/dev/null || echo '?'))"
    else
        echo "DOWN"
    fi
    ;;
  stop)
    # kill master + workers (PHP_CLI_SERVER_WORKERS) — the pidfile alone is not enough
    pkill -f "php -S $HOST:$PORT" 2>/dev/null && echo "stopped (php -S on $PORT)" || echo "nothing running on $PORT"
    rm -f "$PIDFILE"
    ;;
  start|*)
    if is_up; then
        exit 0   # already up — idempotent, silent exit (called by the hook on every session)
    fi
    PHP="$(find_php)" || { echo "php not found (install it or set MEM_PHP=/path/to/php)"; exit 1; }
    # PHP_CLI_SERVER_WORKERS: without it the built-in server is single-threaded
    cd "$DIR" || exit 1
    PHP_CLI_SERVER_WORKERS=4 nohup "$PHP" -S "$HOST:$PORT" -t "$DIR/web" \
        >> "$LOGFILE" 2>&1 &
    echo $! > "$PIDFILE"
    disown 2>/dev/null || true
    for _ in 1 2 3 4 5; do is_up && break; sleep 0.2; done
    if is_up; then
        echo "started: http://$HOST:$PORT/  (pid $(cat "$PIDFILE"), log $LOGFILE)"
    else
        echo "failed to start — see $LOGFILE"; exit 1
    fi
    ;;
esac
