# V1 — static network and delivery routes

Owner: Vitya, network/delivery track. This implements roadmap task V1 only.
The module is `agents/team_agent/network.py`; the team Agent and common
contracts are integrated by Nikita. It is a helper module, not a standalone
submission folder. Existing starter policies are unchanged.

## Inputs and outputs

Construct `StaticNetwork(config)` once per episode. It reads:

- `static.nodes.id` and `static.commodities.id/pool`;
- `static.edges.id/tail/head/K/u0/tau0/c0`;
- `static.lanes.id/edges/chokepoints`;
- `static.action_slots.edge/k/lane`, `static.units`;
- `layout.chokepoints` and `spaces.action.flows.shape`.

No weekly observation, demand forecast, inventory, pipeline quantities,
Early Warnings, solver or file access is used. The runtime module imports
only the standard library and keeps static structures in memory.

`network.routes[s]` is a frozen `SlotRoute` for action position `s`:

| Field | Meaning |
| --- | --- |
| `slot_id`, `commodity_id` | Position in flows and index in static.commodities |
| `source_node`, `destination_node` | Integer node indices, in config order |
| `edge_id`, `lane_id` | Action's entry edge and lane; None for a direct edge |
| `edges`, `nodes` | Complete ordered directed path, including both endpoint nodes |
| `chokepoints` | Ordered node indices of the route's chokepoints |
| `chokepoint_positions` | Positions of those nodes in layout.chokepoints, for observations |
| `pool`, `unit` | Cargo throughput pool tb/ct and quantity unit |
| `nominal_transit_weeks` | Sum of static edge transit times, excluding queue waiting |
| `nominal_freight_per_unit` | Sum of static freight per unit on all route edges |

All numeric IDs refer to config indices. The `node_names`, `edge_names`,
`commodity_names` and `lane_names` tuples are for explanations. Names and
Small slot numbers never determine routing behavior.

A lane action dispatches on its first edge. Later lane edges are part of the
same shipment's route; they are not additional flows entries. A route ending
at a terminal does not also deliver to the grid connected to that terminal.
That requires a separate action slot.

## Adjacency and shared resources

- `out_edges[node]` / `in_edges[node]`: directed adjacency. These include grid
  coupling edges, which remain topology but never become cargo action routes.
- `slots_from[(node, commodity)]` / `slots_to[(node, commodity)]`: existing
  action options by source or final destination; missing keys mean no options.
- `edges_to_slots[edge]`: action slots sharing that physical edge. The edge's
  nominal joint capacity is `edge_capacity[edge]`, not capacity per slot.
- `chokepoints_to_slots[node]`: routes exposed to the same physical chokepoint.
- `chokepoint_pools_to_slots[(node, pool)]`: routes sharing the same throughput
  pool. Tankers and containers have distinct pools but share physical disruption
  exposure at a chokepoint.
- `shared_resources(a, b)`: common ordered edges, chokepoints and throughput pools.

These are resource memberships, not capacity reservations. V2 will supply
current constraints; V4 will allocate volumes. Downstream lane capacity is
not reserved for the future merely because it appears in this table.

## Pipeline handoff to Markiyan

Use `network.transit_progress(edge_id, lane_id)` for a known, visible shipment
after resolving its indices and observed masks in StateSnapshot. It returns:

- `arrival_node`: head of the currently traversed edge;
- `destination_node`: end of the full lane, or head of a confirmed direct edge;
- `remaining_edges`: edges after the current edge;
- `remaining_chokepoints`: chokepoints still to be cleared after that arrival,
  including the arrival node if it is a chokepoint;
- `remaining_nominal_transit_weeks`: sum of remaining edge times;
- `reaches_destination`: whether the current edge's arrival finishes this route.

`pipeline.arrival_week` applies to `arrival_node`. It is not a guaranteed final
delivery week when `remaining_edges` is nonempty. Queues, dynamic transit times
and operation ordering need separate treatment in later tasks.

Do not pass padded rows or turn hidden values into index 0. Pass `lane_id=None`
only when the shipment is confirmed off a lane. The observation's unobserved
lane field alone does not establish that fact during a blackout. The helper
rejects negative/out-of-range indices, edges outside a given lane, and lane-less
shipments at chokepoints instead of guessing a destination.

## Two checked examples

| Network | Slot | Commodity | Complete route | Nominal edge transit |
| --- | ---: | --- | --- | ---: |
| Small | 27 | lng | term_tw → grid_tw | 0 weeks |
| Full | 55 | lng | term_tw → grid_tw | 0 weeks |
| Small / Full | 0 | lng | src_qa_lng → chk_hormuz → chk_malacca → term_tw | 3 weeks |

The direct grid route shares its edge capacity with another commodity slot.
The sea lane shares chokepoints and/or edges with other routes. Its cargo
arriving on `sea.tb.src_qa_lng.chk_hormuz` reaches Hormuz first, not term_tw.
The examples are explanatory; the implementation contains no network names
or fixed slot counts.

## Reproduce and evaluate

```text
uv run pytest tests/test_network.py -q
uv run python examples/08_network_routes.py --task=small
uv run python examples/08_network_routes.py --task=full
```

The example exports every route and shared-resource membership to
`outputs/08_network_routes/<time>_<task>/routes.json`, with local construction
CPU and wall time. It also runs on native Windows without importing the Unix
scoring runner. Very short calls can fall below the CPU clock's resolution.

Seven tests cover shared-entry lanes, direct zero-transit routes, config and
observation index alignment, separate throughput pools, intermediate pipeline
arrivals, malformed paths and real Tiny/Small/Full tables. Static construction
is linear in topology size plus total route length; shared-resource queries
only inspect the two selected paths. No weekly all-pairs route search is needed.

Local validation on 2026-10-05: all seven focused tests and Ruff checks passed.
Exporting Small's 108 routes took 0.003114 s wall time; Full's 395 routes took
0.010256 s. CPU readings were below the local clock's resolution, not proof
of zero CPU use. The broader `uv run pytest -n 3 --maxfail=3 --tb=line` run
stopped with eight failures in the existing Unix runner's `fcntl` import and
five passes. Those runner checks remain unverified on this Windows machine.

V1 changes representations, not policy. A standalone `sbf compare` cannot
measure this module's benefit until Nikita integrates it into an Agent. At that
point compare the integrated representation-only version with a frozen prior
policy on identical scenarios, expecting unchanged actions and costs:

```text
uv run sbf compare team_agent <frozen_previous_agent> --task=small --entropy=12345 --episodes=16 --cpu_budget=True
uv run sbf compare team_agent <frozen_previous_agent> --task=full --entropy=12345 --episodes=4 --cpu_budget=True
```

There is no claim of RSS improvement for V1. An isolated server check of the
integrated Agent is still needed; local topology timings are only its share of
the Small 2 s / Full 4 s budgets, which include episode initialization.

## Next track task

V2: read action_mask and observed graph_now constraints, keeping current,
nominal and unknown values distinct. DeliveryNeed, forecasts and risk models
remain Markiyan's inputs; the Agent/act orchestration and scoring remain
Nikita's responsibilities.
