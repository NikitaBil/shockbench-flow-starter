# V4 — current-week volume allocation

This stage implements Vitya's allocation track using the team's existing
state and needs modules. It changes one main idea: dispatch is assigned
sequentially to actual needs instead of requesting every nominal slot capacity.
The hypothesis is fewer stock/entry-edge/fleet clipped requests without double
spending a shared resource. Better RSS is not assumed.

## Branch integration on 2026-10-06

All remote branches were fetched and merged into `network-delivery`:

- `main` at f60ba83 supplies the project context;
- `integration` at 48acf87 supplies Agent, V1 contracts, state, needs,
  conditional queue forecaster, frozen baseline and integration tests;
- `analytics` at 141589a supplies original experiments, risk analysis,
  project context and roadmap;
- our fdc632e supplies the newer V2/V3 network and delivery modules.

Both teammates independently added team_agent files. Conflicting Agent,
contracts, state and needs use the integration branch versions, which have
mask-aware quantities and tests of real wire layouts. Our newer network V2,
V3 and network regressions are retained. Analytics' original implementation
also remains under agents/mine and in its merged Git history. In particular,
its priority convention (lower number first) is not mixed with the canonical
contract's higher-priority-first convention.

The imported experimental `risk.py` depended on the older RiskState class.
That class is preserved in `risk_contracts.py`; it does not replace canonical
V1 contracts. This analyzer is not enabled by V4. Its message semantics and
uncalibrated scores still require the risk owner's validation before V5.

## Inputs, output and activation

`Allocator(config, network).allocate(state, needs, observation, network)`
implements the agreed Allocator protocol. It uses `StateSnapshot.available_stock`,
pipeline and queue metadata plus canonical DeliveryNeed fields. Ordering is
descending priority, then earlier due_week, then stable need_id. It returns
AllocationResult with float64 flows in action-slot order, explicit unmet
quantities, current-resource usage and diagnostic reasons.

The normal team_agent remains the frozen heuristic behavior. Set
`{"allocation_enabled": true}` in its submission folder's params.json to
wire StateBuilder, NeedPlanner and Allocator through DecisionPipeline. False
or omission keeps the heuristic; non-boolean settings are rejected. All module
objects are created once per episode. The default planner's forecast and
production assumptions are unchanged; queue forecast and legacy risk are not
silently activated with allocation.

## Resource semantics

Only observed, nonnegative pre-dispatch stock is spent. Unknown stock remains
unknown in the input and yields no spendable budget. Ordinary arrivals, supply
lift and production happen after current dispatch in simulator 0.1.2, so their
same-week quantities are not added. This is verified against the installed
simulator and agrees with the state builder's availability_mode.

Current edge, prohibition and permission data come from one V2 update per
week; V3 ranks existing delivery slots by full-route transport cost. Both
current and estimated prohibitions can exclude a route. Permission must be
confirmed through visible action_mask or route prohibition fields before
dispatch. Hidden capacities can use V2's explicitly labelled estimates; no
zero-filled blackout value is treated as an observed capacity.

For each assignment the allocator reduces:

- remaining quantity of that need;
- the one shared `(source, commodity)` stock balance;
- the one shared first-edge capacity across lanes and commodities;
- additional detour fleet budget in the relevant tb/ct pool, weighted by
  the public extra transit-week terms.

When an option is exhausted the next existing option is tried. Full lane cost
is used, but downstream edge and chokepoint capacity are not reserved today.
An entry is permitted into a currently closed lane; reopening is not invented
and completion uncertainty is reported. Comparing waiting with a costly
alternative using shortage costs is V5, separate from V4.

Automatic chokepoint releases also consume current detour fleet. Their current
usage is bounded conservatively using visible queued cargo and inbound cargo
arriving this week, current outgoing caps and pool throughput. Incomplete
metadata/visibility uses an outgoing-capacity upper bound rather than assuming
empty queues. This may reserve too much fleet and leave needs unmet. Residual
fleet is reported as ResourceUsage kind fleet_pool (tb index 0, ct index 1),
in native-unit weeks with estimated limit provenance. It is not a guarantee
about weekly realised marks or hidden future disruption.

Routes whose earliest possible arrival is after T are excluded. Overdue needs
remain requests; deadlines otherwise only affect need ordering in this stage.
V4 does not invent an upstream DeliveryNeed for a terminal or a multi-action
path. If an input need has no existing final delivery slot or no stocked source,
it is explicitly unmet. Intermediate replenishment must be addressed in the
needs/allocator handoff before promoting this experimental policy.

## Reproducible validation

```text
uv run pytest tests/test_allocation.py tests/test_network.py tests/test_current_network.py tests/test_delivery.py tests/test_team_agent.py tests/test_team_contracts.py tests/test_team_analytics.py tests/test_team_evaluate.py tests/test_team_check_script.py -n 3
uv run python examples/10_network_allocation.py --task=small
uv run python examples/10_network_allocation.py --task=full
```

The native example freezes two complete submission folders with allocation
enabled/disabled. Each runs its own full trajectory on the same scenario;
this is not an action-parity replay. It records exact cent-based costs,
observed requested/executed clipping per slot, unmet counts, source fingerprints
and whole-Agent CPU including initialization in week one. It refuses existing
output directories and saves failure status as well as success. Reports and
frozen candidates are under gitignored outputs/10_network_allocation.

Unit tests check shared stock and edges, priority, fallback, sanctions,
unknown stock, blackout permission, direct zero-transit edges, episode horizon,
estimated capacity labels, and detour fleet shared with current automatic
releases. Existing Small/Full topology, state, contract and baseline-parity
regressions remain applicable. Runtime uses only standard library and NumPy.
Bounds and mappings are config-driven; there is no hard-coded network size or
all-pairs route search. Options are cached per destination/commodity per week.

Official paired evaluation of the frozen candidate versus frozen baseline:

```text
uv run sbf compare <run_folder>/candidate <run_folder>/baseline --task=small --entropy=12345 --episodes=16 --cpu_budget=True
uv run sbf compare <run_folder>/candidate <run_folder>/baseline --task=full --entropy=12345 --episodes=4 --cpu_budget=True
```

These initial counts are smoke samples; expand paired evidence as resources
allow, then confirm on root 67890 and occasionally root 0/dev. Check the
integrated candidate with sbf check on both networks. The Unix-only fcntl
runner still prevents official scoring/isolation on this Windows host;
native CPU and raw costs are not RSS or server certification. Default policy
promotion requires this evidence plus resolution of unmet upstream needs.

## Local results, 2026-10-06

The focused network and team integration suite passed 131 tests, with three
Unix Bash orchestration tests skipped. This includes V4 requests executing
without stock/edge/fleet clipping in an event-free simulator week on Tiny,
Small and Full, default-policy full-episode parity, and a packed opt-in
candidate loading its real pipeline. Lint/format checks cover the changed
active modules and new tests/example, not the imported legacy experiments.

Native frozen runs used episode 0, entropy 12345, complete separate trajectories:

| Task | Baseline cost, trillion USD | V4 cost, trillion USD | Baseline / V4 clipped slot-weeks | V4 maximum CPU |
| --- | ---: | ---: | ---: | ---: |
| Small | 2.964621 | 5.099488 | 3624 / 3 | 0.062500 s |
| Full | 5.335792 | 15.148120 | 34902 / 9 | 0.140625 s |

CPU includes initialization and all active Agent modules, not just allocation.
Very short baseline calls are below this CPU clock's resolution. Lower clipping
does not imply lower total cost: V4 costs more in both these smoke scenarios.
It therefore remains disabled by default. These are two native episodes, not
paired RSS evidence. Native clipping only compares slots whose requested and
executed values are both observed.

One verified input gap: on Small week one, the planner supplies 126 needs,
90 remain at least partly unmet, and none of the eight terminal fuel pairs
has a replenishment need. Source -> terminal -> grid requires multiple action
weeks; the allocator receives grid requests but cannot replenish their
terminal sources from an absent terminal request. This is a concrete handoff
gap, not proof that it alone explains the full cost regression. The needs
owner should supply upstream quantities/deadlines, or agree an explicit
network-based upstream expansion stage with the allocation owner. Arrival at
an ordinary intermediate node in week t becomes dispatchable there in t+1.
Existing in-flight cargo and any upstream requests must not be counted twice.

Official `sbf check team_agent --task=small` passes ZIP/import static checks
but its isolated execution fails importing fcntl. `sbf compare` for the frozen
Small candidate/baseline fails at the same runner import. Neither check is
reported as an isolated execution/scoring success. No benchmark or dependency
patch, upload, or changes to remote main/integration/analytics were made.

The repository-wide pytest run stopped with 5 failures, 26 passes and 3 skips:
four failures were fcntl imports, and the existing Codabench token-file test
assumes Unix mode 0600, which Windows does not report. These platform failures
are separate from the passing focused suite; no real Codabench credentials or
uploads were used by that mocked test.
