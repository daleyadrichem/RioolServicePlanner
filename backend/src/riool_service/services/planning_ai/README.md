# Planning AI

Initial planning module for the Riool Service Planner.

Algorithm:

1. Select all feasible candidate tickets without using medium deadlines as a hidden priority rule.
2. Fetch the complete travel-time matrix internally through the OSRM provider/cache.
3. Build multiple starting plans with semi-random route seeds.
   - This avoids the common mistake where mechanics living close together all stay in the same area.
   - Some starts deliberately let a mechanic claim a farther cluster first.
4. Fill each start plan with cheapest feasible insertion.
5. Improve each plan with local search:
   - move ticket to another mechanic;
   - swap tickets between mechanics;
   - reorder tickets within a mechanic route.
6. Keep the best scoring solution.

Medium and low tickets share the same planning class. Medium receives only a tiny
score tie-breaker that is weighed against travel time, and deadline misses are
scored softly instead of blocking otherwise efficient route choices.

## Incremental planning worker

New tickets and simulator-injected tickets are not planned inline by the API or
simulator worker. Run the incremental planning worker next to the FastAPI process:

```bash
python -m riool_service.services.planning_ai.worker
```

For verbose diagnostics, the worker now defaults to DEBUG logging when started
through this module. You can also set it explicitly:

```bash
python -m riool_service.services.planning_ai.worker --log-level DEBUG
# or
PLANNING_WORKER_LOG_LEVEL=DEBUG python -m riool_service.services.planning_ai.worker
```

The worker polls for open tickets that are not present in the latest completed
planning run and inserts one ticket at a time with the incremental replanner. This
path starts from the latest persisted plan, does not run the full multi-start
optimizer, and does not request a full OSRM matrix. It reuses cached existing
legs and only fetches the row/column from and to the incoming ticket location.

Urgent incremental tickets are restricted to the active planning day, but the
initial planner's protected 6-hour route-work cap is removed for this urgent
insert path so the urgent ticket can be placed today and non-urgent work can be
moved to later days with the configured same-day reschedule penalty.
