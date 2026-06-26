#!/usr/bin/env bash
set -euo pipefail

# Optional: download OpenStreetMap data for local routing/tile experiments.
# The current map tab does NOT require this file; it uses browser map tiles.
# This is useful later if you want to run OSRM or generate local map tiles.

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
MAP_DIR="${MAP_DIR:-$ROOT_DIR/data/maps}"
OSM_URL="${OSM_URL:-https://download.geofabrik.de/europe/netherlands-latest.osm.pbf}"
TARGET="$MAP_DIR/netherlands-latest.osm.pbf"

mkdir -p "$MAP_DIR"

echo "Downloading Netherlands OSM extract from: $OSM_URL"
echo "Target: $TARGET"

if command -v curl >/dev/null 2>&1; then
  curl -L --fail --continue-at - --output "$TARGET" "$OSM_URL"
elif command -v wget >/dev/null 2>&1; then
  wget -c -O "$TARGET" "$OSM_URL"
else
  echo "Please install curl or wget first." >&2
  exit 1
fi

echo "Downloaded: $TARGET"
echo "Next step for local routing would be to preprocess it with OSRM, e.g. osrm-extract/osrm-partition/osrm-customize."
