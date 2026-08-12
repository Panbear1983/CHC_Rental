#!/usr/bin/env bash
#
# chc.sh — one terminal entry point for the CHC Rental dashboard and daily job.
#
# Wraps the venv binaries so you never have to remember the venv path or the
# --root flag, and gives the terminal direct control over the background
# launchd job. Run it from anywhere; it resolves its own repo location.
#
#   ./chc.sh                 launch the owner dashboard (TUI)   [default]
#   ./chc.sh run [--live]    plan today's push (add --live to send)
#   ./chc.sh check           verify the bot can reach each allowlisted person
#   ./chc.sh prune           delete state past its retention window
#   ./chc.sh logs [-f]       show the daily job log (-f to follow)
#   ./chc.sh job status      is the hourly launchd job loaded?
#   ./chc.sh job start|stop  load / unload the hourly job
#   ./chc.sh job run         run the hourly job once, right now
#   ./chc.sh help            this message
#
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$REPO/.venv/bin/python"
TUI="$REPO/.venv/bin/chc-rental-tui"
CLI="$REPO/.venv/bin/chc-rental"

LAUNCHD_LABEL="com.chcrental.daily"
LAUNCHD_PLIST="$HOME/Library/LaunchAgents/${LAUNCHD_LABEL}.plist"
DOMAIN="gui/$(id -u)"

die() { echo "chc: $*" >&2; exit 1; }

usage() { sed -n '2,18p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

[[ -x "$PY" ]] || die "venv not found at $REPO/.venv — create it, then: $PY -m pip install -e ."

cmd="${1:-dashboard}"
[[ $# -gt 0 ]] && shift || true

case "$cmd" in
  dashboard|dash|tui)
    exec "$TUI" --root "$REPO" "$@"
    ;;
  run|check|prune|init)
    exec "$CLI" --root "$REPO" "$cmd" "$@"
    ;;
  logs)
    log="$REPO/state/daily.log"
    [[ -f "$log" ]] || die "no log yet at $log (the job has not run)"
    if [[ "${1:-}" == "-f" ]]; then exec tail -f "$log"; else exec tail -n 40 "$log"; fi
    ;;
  job)
    action="${1:-status}"; [[ $# -gt 0 ]] && shift || true
    case "$action" in
      status)
        if launchctl print "${DOMAIN}/${LAUNCHD_LABEL}" >/dev/null 2>&1; then
          echo "job ${LAUNCHD_LABEL}: LOADED (runs hourly)"
        else
          echo "job ${LAUNCHD_LABEL}: not loaded"
        fi
        ;;
      start)
        [[ -f "$LAUNCHD_PLIST" ]] || die "plist not installed at $LAUNCHD_PLIST (copy scripts/${LAUNCHD_LABEL}.plist there)"
        launchctl bootstrap "$DOMAIN" "$LAUNCHD_PLIST" && echo "job started (loaded)"
        ;;
      stop)
        launchctl bootout "${DOMAIN}/${LAUNCHD_LABEL}" 2>/dev/null && echo "job stopped (unloaded)" || echo "job was not loaded"
        ;;
      run|kick)
        launchctl kickstart -k "${DOMAIN}/${LAUNCHD_LABEL}" && echo "job kicked off once (see: ./chc.sh logs)"
        ;;
      *)
        die "unknown job action '$action' (status|start|stop|run)"
        ;;
    esac
    ;;
  help|-h|--help)
    usage
    ;;
  *)
    echo "chc: unknown command '$cmd'" >&2
    usage
    exit 2
    ;;
esac
