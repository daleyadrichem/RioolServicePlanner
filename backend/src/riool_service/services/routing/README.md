# Routing service

This module uses OSRM's HTTP API to calculate driving times between ticket locations.

## Configuration

By default it uses the public OSRM demo server:

```bash
OSRM_BASE_URL=https://router.project-osrm.org
OSRM_PROFILE=driving
OSRM_TIMEOUT_SECONDS=15
```

For a later standalone/professional setup you can point `OSRM_BASE_URL` to your own OSRM instance without changing the planner code.

## Endpoints

### Full route matrix

```http
POST /routing/tickets/matrix
Content-Type: application/json

{
  "ticket_ids": [1, 2, 3],
  "refresh_cache": false
}
```

Returns every selected ticket-to-ticket travel time in minutes and distance in km.

### Source/destination route matrix

```http
POST /routing/tickets/matrix-between
Content-Type: application/json

{
  "source_ticket_ids": [1, 2],
  "destination_ticket_ids": [3, 4, 5],
  "refresh_cache": false
}
```

Returns only the requested source-to-destination travel times in minutes and distances in km. Internally, the OSRM table request uses explicit `sources` and `destinations` indices, so the planner does not need to calculate a full NxN matrix when it only needs selected pairs.

## Cache

Routes are cached in the existing `route_cache` table for 30 days. The cache is directed because travel time from A to B can differ from B to A.
