#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

echo "Installing React map dependencies..."
npm install react-leaflet leaflet

echo "Done. You can now run: npm run dev"
