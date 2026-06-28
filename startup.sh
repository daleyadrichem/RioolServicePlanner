#!/usr/bin/env bash
set -e

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

FRONTEND_DIR="$ROOT_DIR/frontend"
BACKEND_DIR="$ROOT_DIR/backend"

echo "Starting application from: $ROOT_DIR"

# Optional setup flags
RUN_INSTALL=false
RUN_INIT_DB=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --install)
      RUN_INSTALL=true
      shift
      ;;
    --init-db)
      RUN_INIT_DB=true
      shift
      ;;
    --setup)
      RUN_INSTALL=true
      RUN_INIT_DB=true
      shift
      ;;
    *)
      echo "Unknown option: $1"
      echo "Usage: ./start.sh [--install] [--init-db] [--setup]"
      exit 1
      ;;
  esac
done

cleanup() {
  echo ""
  echo "Stopping all processes..."
  trap - INT TERM EXIT
  kill 0
}

trap cleanup INT TERM EXIT

if [ "$RUN_INSTALL" = true ]; then
  echo "Installing frontend dependencies..."
  cd "$FRONTEND_DIR"
  npm install

  echo "Syncing backend dependencies..."
  cd "$BACKEND_DIR"
  uv sync
fi

if [ "$RUN_INIT_DB" = true ]; then
  echo "Creating backend .env file..."
  cat > "$BACKEND_DIR/.env" <<'EOF'
DATABASE_URL="sqlite:///./riool_service.db"
TECHNICIANS_CONFIG_PATH="./config/technicians_config.json"
TICKET_SCENARIOS_CONFIG_PATH="./config/ticket_scenarios_config.json"
LOCATIONS_CONFIG_PATH="./config/locations_config.json"
EOF

  echo "Initializing database..."
  cd "$BACKEND_DIR"
  uv run python src/riool_service/database_initializer/initialize_database.py
fi

echo "Starting frontend..."
cd "$FRONTEND_DIR"
npm run dev &
FRONTEND_PID=$!

echo "Starting backend API..."
cd "$BACKEND_DIR"
uv run python -m uvicorn riool_service.api.main:app --reload &
API_PID=$!

echo "Starting simulator worker..."
cd "$BACKEND_DIR"
uv run python src/riool_service/services/simulator_service/worker.py &
SIMULATOR_PID=$!

echo "Starting planning AI worker..."
cd "$BACKEND_DIR"
uv run python src/riool_service/services/planning_ai/worker.py &
PLANNING_PID=$!

echo ""
echo "All services started."
echo "Frontend PID:      $FRONTEND_PID"
echo "Backend API PID:   $API_PID"
echo "Simulator PID:     $SIMULATOR_PID"
echo "Planning AI PID:   $PLANNING_PID"
echo ""
echo "Press Ctrl+C to stop everything."

wait