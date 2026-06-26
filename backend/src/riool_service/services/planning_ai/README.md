# Planning AI

Initial planning module for the Riool Service Planner.

Algorithm:

1. Select candidate tickets with the earliest SLA pressure.
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

Low-priority tickets are treated as useful fillers: they are inserted when they fit
well into the route and do not cause SLA, workday, or non-urgent capacity issues.
