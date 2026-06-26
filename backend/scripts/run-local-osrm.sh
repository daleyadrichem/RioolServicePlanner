#!/usr/bin/env bash
set -euo pipefail

# Optional helper for later local road routing experiments.
# Requires Docker and a preprocessed OSRM dataset. This script intentionally
# does not preprocess automatically because the Netherlands extract is large.

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
OSRM_DIR="${OSRM_DIR:-$ROOT_DIR/data/osrm}"
OSRM_FILE="${OSRM_FILE:-netherlands-latest.osrm}"
PORT="${PORT:-5000}"

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker is required to run OSRM locally." >&2
  exit 1
fi

if [ ! -f "$OSRM_DIR/$OSRM_FILE" ]; then
  echo "Missing $OSRM_DIR/$OSRM_FILE" >&2
  echo "Preprocess an .osm.pbf file with OSRM first, then rerun this script." >&2
  exit 1
fi

docker run --rm -t -i \
  -p "$PORT:5000" \
  -v "$OSRM_DIR:/data" \
  ghcr.io/project-osrm/osrm-backend \
  osrm-routed --algorithm mld "/data/$OSRM_FILE"
