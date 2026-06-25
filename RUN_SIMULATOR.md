# Run backend, worker and frontend

## Backend API

From the unzipped project root:

```bash
cd backend_src/src
python -m uvicorn riool_service.api.main:app --reload
```

Docs:

```text
http://127.0.0.1:8000/docs
```

## Simulator worker

Open a second terminal. Use the same folder and database environment as the API:

```bash
cd backend_src/src
python -m riool_service.services.simulator_service.worker
```

The worker checks the `simulation_state` table every second. When status is `RUNNING`, it advances `current_simulation_time` using `speed_multiplier` and moves due rows from `simulation_tickets` into `tickets`.

## Frontend

Open a third terminal:

```bash
cd frontend_src
npm install
npm run dev
```

Usually available at:

```text
http://localhost:5173
```

## Simulator flow

1. Select a scenario.
2. Click `Tickets genereren`.
3. Click `Start simulatie`.
4. Keep the worker running. The frontend polls `/simulator/state` and `/simulator/injections` every second.
5. Use `Pauze`, `Stop simulatie`, and speed buttons to update the backend state.
