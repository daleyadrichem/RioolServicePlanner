# Simulator service

The simulator uses a separate worker process.

Run the API in terminal 1:

```bash
cd backend_src/src
python -m uvicorn riool_service.api.main:app --reload
```

Run the simulator worker in terminal 2:

```bash
cd backend_src/src
python -m riool_service.services.simulator_service.worker
```

The frontend controls the simulation through the API. The worker reads the `simulation_state` table, advances the simulation clock, and injects due `simulation_tickets` into the real `tickets` table.
