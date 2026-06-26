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
