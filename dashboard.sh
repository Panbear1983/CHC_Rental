#!/usr/bin/env bash
#
# dashboard.sh — the single public entry point for CHC Rental.
#
#   ./dashboard.sh                         launch the local operator dashboard (default)
#   ./dashboard.sh scrape                  update today's source cache; never send
#   ./dashboard.sh deliver [--live]        use today's cache; never scrape
#   ./dashboard.sh run [--live]            plan or send today's push
#   ./dashboard.sh check                   verify Telegram reachability
#   ./dashboard.sh prune                   prune expired state
#   ./dashboard.sh logs [-f]               show or follow the daily log
#   ./dashboard.sh alerts ...              incremental alert operations
#   ./dashboard.sh alerts-logs [-f]        show or follow the alerts log
#   ./dashboard.sh job status|start|stop|run
#   ./dashboard.sh alerts-job status|start|stop|run
#   ./dashboard.sh help
#
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$REPO/.venv/bin/python"

LAUNCHD_LABEL="com.chcrental.daily"
LAUNCHD_PLIST="$HOME/Library/LaunchAgents/${LAUNCHD_LABEL}.plist"
ALERTS_LAUNCHD_LABEL="com.chcrental.alerts"
ALERTS_LAUNCHD_PLIST="$HOME/Library/LaunchAgents/${ALERTS_LAUNCHD_LABEL}.plist"
DOMAIN="gui/$(id -u)"

die() { echo "dashboard: $*" >&2; exit 1; }

usage() { sed -n '2,14p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

[[ -x "$PY" ]] || die "venv not found at $REPO/.venv — create it, then: $PY -m pip install -e ."

if [[ $# -eq 0 ]]; then
  exec "$PY" -m chc_rental.tui.app --root "$REPO"
fi

cmd="$1"
shift

case "$cmd" in
  run|scrape|deliver|check)
    exec "$PY" -m chc_rental.cli --root "$REPO" "$cmd" --env-file "$REPO/.env" "$@"
    ;;
  prune|init)
    exec "$PY" -m chc_rental.cli --root "$REPO" "$cmd" "$@"
    ;;
  alerts)
    exec "$PY" -m chc_rental.cli --root "$REPO" alerts "$@"
    ;;
  logs)
    log="$REPO/state/daily.log"
    [[ -f "$log" ]] || die "no log yet at $log (the job has not run)"
    if [[ "${1:-}" == "-f" ]]; then exec tail -f "$log"; else exec tail -n 40 "$log"; fi
    ;;
  alerts-logs)
    log="$REPO/state/alerts.log"
    [[ -f "$log" ]] || die "no incremental log yet at $log (the alerts job has not run)"
    if [[ "${1:-}" == "-f" ]]; then exec tail -f "$log"; else exec tail -n 60 "$log"; fi
    ;;
  job)
    action="${1:-status}"
    [[ $# -gt 0 ]] && shift || true
    case "$action" in
      status)
        if launchctl print "${DOMAIN}/${LAUNCHD_LABEL}" >/dev/null 2>&1; then
          echo "job ${LAUNCHD_LABEL}: LOADED (10-minute checks; scrape and delivery separated)"
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
        launchctl kickstart -k "${DOMAIN}/${LAUNCHD_LABEL}" && echo "job kicked off once (see: ./dashboard.sh logs)"
        ;;
      *)
        die "unknown job action '$action' (status|start|stop|run)"
        ;;
    esac
    ;;
  alerts-job)
    action="${1:-status}"
    [[ $# -gt 0 ]] && shift || true
    case "$action" in
      status)
        if launchctl print "${DOMAIN}/${ALERTS_LAUNCHD_LABEL}" >/dev/null 2>&1; then
          echo "job ${ALERTS_LAUNCHD_LABEL}: LOADED (15-minute gated tick)"
        else
          echo "job ${ALERTS_LAUNCHD_LABEL}: not loaded (safe default)"
        fi
        ;;
      start)
        [[ -f "$ALERTS_LAUNCHD_PLIST" ]] || die "plist not installed at $ALERTS_LAUNCHD_PLIST (copy scripts/${ALERTS_LAUNCHD_LABEL}.plist there after canary approval)"
        launchctl enable "${DOMAIN}/${ALERTS_LAUNCHD_LABEL}"
        launchctl bootstrap "$DOMAIN" "$ALERTS_LAUNCHD_PLIST" && echo "incremental alerts job started"
        ;;
      stop)
        launchctl bootout "${DOMAIN}/${ALERTS_LAUNCHD_LABEL}" 2>/dev/null || true
        launchctl disable "${DOMAIN}/${ALERTS_LAUNCHD_LABEL}"
        echo "incremental alerts job stopped and disabled"
        ;;
      run|kick)
        launchctl kickstart -k "${DOMAIN}/${ALERTS_LAUNCHD_LABEL}" && echo "incremental alerts tick requested (see: ./dashboard.sh alerts-logs)"
        ;;
      *)
        die "unknown alerts-job action '$action' (status|start|stop|run)"
        ;;
    esac
    ;;
  help|-h|--help)
    usage
    ;;
  *)
    die "unknown command '$cmd' (run ./dashboard.sh help)"
    ;;
esac
