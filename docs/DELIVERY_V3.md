# V3 — delivery options for existing action slots

Vitya's network/delivery track. `agents/team_agent/delivery.py` evaluates
routes from V1 using V2's weekly snapshot. It returns options and uncertainty
labels; it does not allocate cargo or change the team's Agent. Runtime imports
are standard library and NumPy only, including the imported network helper.

## API and team integration

```python
network = StaticNetwork(config)
tracker = NetworkTracker(config, network)
delivery = DeliveryEvaluator(config, network)

# Once per week, before processing all needs:
snapshot = tracker.update(observation)
options = delivery.options(
    snapshot,
    destination_node=need.destination_node,
    commodity_id=need.commodity_id,
    quantity=need.quantity,
    due_week=need.due_week,
    source_nodes=eligible_source_nodes,  # optional IDs from the state module
    queue_work_at_arrival=projected_queue_work,  # optional map (node, pool) -> qty
)
```

`need` above is illustrative. The API takes primitive fields until Nikita and
Markiyan agree the shared DeliveryNeed contract. The destination is the final
node of the delivery slot, not an intermediate chokepoint. Source IDs are
optional filters, not proof that stock is available there. Quantities use the
commodity's unit from config. No graph IDs or Small/Full sizes are hard-coded.

Only existing edge/lane action slots ending at `(destination, commodity)` are
considered. A route prohibited according to V2 is excluded before ranking.
Estimated legal permission remains explicitly unconfirmed. A legal lane with
a downstream closure remains an option: dispatch into its first edge and
completion of its full delivery are separate questions.

Options are sorted by transport cost per unit, then no-wait arrival and slot ID.
This deterministic ordering is a comparison aid, not a policy deciding whether
a late cheap route is better than an expensive timely route. V4 will allocate
using the team's needs and agreed priority; V5 will add future restrictions.

## Full-route transport cost

For commodity `k`, the option uses every edge of the lane, including the
chokepoint continuation edges that are released automatically:

```text
freight_per_unit = sum(graph_now.c[e] for e in route.edges)
tariff_per_unit = sum(graph_now.tariff[e, k] * customs_value[k] for e in route.edges)
war_risk_per_unit = sum(public_war_cost[chokepoint, k][current_class])
transport_cost_per_unit = freight_per_unit + tariff_per_unit + war_risk_per_unit
transport_cost = quantity * transport_cost_per_unit
```

`customs_value` is config's `static.commodities.v`; a tariff rate is converted
to USD per commodity unit. War premiums use the public node attribute
`war_risk_cost` and observed class codes 0/1/2, mapped through config's
chokepoint observation order. Each chokepoint premium is paid on continuation
out of that chokepoint. All future traversal prices persist at snapshot values
for this estimate; actual prices can change before traversal.

`cost_observed` means that all inputs to this price were observed this week.
It does not guarantee future prices. Unobserved inputs retain V2's explicit
history/nominal/derived labels and appear in `uncertain_fields`. This cost
excludes queue holding, inventory, shortage, disposal and production costs.

## Time and delay conditions

`no_wait_arrival_week = snapshot.week + sum(graph_now.tau[e])`.
The simulator joins chokepoint arrivals to the lot book before that week's
default release: an uncongested chokepoint adds no extra week. A direct edge
with tau zero can arrive in the dispatch week. The current benchmark's edge
transit times are fixed; the exposed tau field is still read rather than assumed.

`estimated_completion_week` concerns the complete requested quantity, under
fixed capacities and without competing future traffic or fleet clipping. It
adds an approximate batching delay `ceil(quantity / snapshot_throughput) - 1`.
For each chokepoint, supplied queue work adds an approximate extra delay:

```text
max(0, ceil((ahead + quantity) / kappa) - ceil(quantity / kappa))
```

Queue input must describe work ahead of this cargo at its projected arrival,
before that week's release, in the matching tb/ct pool. Current queue stock
alone does not establish this forecast. An explicit zero is a no-queue
assumption. Missing work for any route chokepoint leaves completion unknown;
zero throughput also leaves it unknown. No reopening date is invented.
Multiple bottlenecks, FIFO priorities, commodity conversions, overlapping
streams and competing arrivals require a richer queue forecast later. These
formulas are planning approximations, not a simulation of every cargo lot.

The result includes `queue_delay_weeks`, `late_weeks` when completion and due
week are known, and `beyond_horizon` when the computed date exceeds `T`.
If completion is unknown, beyond_horizon uses the no-wait date; false then
does not guarantee arrival within the episode. `delay_flags` identify current
closures, zero capacities, missing queue work, estimated data, batching and
deadline/horizon conflicts. They are conditions, not delay probabilities.

`dispatchable_now` indicates estimated legal permission and positive first-edge
capacity only. Stock and shared fleet capacity can still prevent dispatch.
`snapshot_throughput` does not reserve later edges or shared chokepoint pools.
Also, graph_now is the instantaneous snapshot; executed flows use weekly
realised marks. V4 must measure requested/executed differences instead of
treating these snapshot estimates as guaranteed execution quantities.

## Validation and CPU

```text
uv run pytest tests/test_network.py tests/test_current_network.py tests/test_delivery.py -q
uv run python examples/09_delivery_options.py --task=small
uv run python examples/09_delivery_options.py --task=full
```

Focused tests cover full freight, ad-valorem tariffs, war class/index mapping,
sanctions, zero-transit edges, unknown queues, closure, batching, deadline and
horizon labels, invalid inputs, and top-level imports beside agent.py. A native
event-free Small simulation isolates one unit on a sea lane and one on a
zero-transit edge, verifying estimated no-wait dates against actual arrivals.
V1/V2 tests also verify real Tiny/Small/Full route mappings and snapshots.
Final focused run: 24 tests passed; Ruff lint and formatting checks passed.

The example observes unchanged heuristic actions throughout an episode and
probes existing routes with quantity one. These probes are diagnostics, not
actual needs. JSON reports go to gitignored `outputs/09_delivery_options/`.
Initialization is counted in week one. Local runs on 2026-10-05, episode 0,
entropy 12345:

| Task | Weeks | Static slots | Maximum V1/V2/V3 observer CPU | Budget for entire Agent |
| --- | ---: | ---: | ---: | ---: |
| Small | 52 | 108 | 0.046875 s | 2 s |
| Full | 104 | 395 | 0.140625 s | 4 s |

The example checks its own observer CPU against those budgets. These are
local module measurements, not server certification or a timing bound for an
arbitrary number of DeliveryNeeds. Group needs by destination/commodity to use
the precomputed slot index and profile the integrated Agent, including all
other modules. No all-pairs graph search is performed.

The repository-wide `uv run pytest -n 3 --maxfail=3 --tb=line` run stopped with
nine failures and four passes: all nine failures were existing scoring/check
runner imports of Unix-only `fcntl`. Official scoring remains unavailable on
this Windows environment. The benchmark and locked dependencies were not
patched. The helper folder is not yet a standalone submission Agent.

## Hypothesis and paired evaluation after integration

V2 hypothesis: using confirmed current permissions/capacities with explicit
blackout estimates reduces prohibited/clipped requests. Compare with a frozen
V1 policy while only enabling current-constraint response.

V3 hypothesis: full-route transport cost and explicit arrival/deadline estimates
improve delivery choices. Compare with frozen V2 while only enabling this
option-ranking response, keeping needs, forecasting and allocation unchanged.

On a host where the official runner works, Nikita can evaluate each change:

```text
uv run sbf compare team_agent <frozen_V1_or_V2_agent> --task=small --entropy=12345 --episodes=16 --cpu_budget=True
uv run sbf compare team_agent <frozen_V1_or_V2_agent> --task=full --entropy=12345 --episodes=4 --cpu_budget=True
```

These initial sample sizes are a smoke comparison, not sufficient evidence for
a small benefit. Expand the paired sample if its interval includes zero, then
confirm on held-out root 0. Inspect cost, paired RSS interval, prohibited
requests, requested/executed clipping, completed deliveries and whole-Agent
CPU. Run `sbf check` on the integrated Agent for both tasks. The observer's
unchanged heuristic costs match the prior native episode runs; no V2/V3 RSS
improvement is claimed before integration and paired evaluation.
