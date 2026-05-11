#!/bin/sh
set -eu

INTERVAL_SECONDS="${SCRAPER_INTERVAL_SECONDS:-86400}"
ONLY="${SCRAPER_ONLY:-bologna}"

while true; do
  echo "Starting scheduled scraper: ${ONLY}"

  if [ "$ONLY" = "all" ]; then
    python -m scraper.run_all || echo "Scraper run failed; will retry after interval."
  else
    python -m scraper.run_all --only "$ONLY" || echo "Scraper run failed; will retry after interval."
    python -m scraper.run_all --only clean || echo "Cleaner run failed; will retry after interval."
  fi

  echo "Scheduled scraper finished. Sleeping ${INTERVAL_SECONDS} seconds."
  sleep "$INTERVAL_SECONDS"
done
