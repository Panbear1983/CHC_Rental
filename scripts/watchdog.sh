#!/usr/bin/env bash
set -euo pipefail
REPO="/Users/peter/Desktop/Old_Projects/GitHub/CHC_Rental"
DATE=$(TZ=America/New_York date +%Y-%m-%d)
CACHE_DIR="$REPO/state/cache/$DATE"
if [[ ! -d "$CACHE_DIR" ]]; then
  echo "Watchdog: no cache for $DATE, triggering scrape and deliver"
  cd "$REPO"
  .venv/bin/python -m chc_rental.cli --root . scrape >> state/daily.log 2>&1
  .venv/bin/python -m chc_rental.cli --root . deliver --live >> state/daily.log 2>&1
else
  echo "Watchdog: cache for $DATE exists, skipping"
fi >> state/daily.log 2>&1