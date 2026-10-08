# Planner hardening handoff

Branch: `planner/final-hardening`, based on integrated `analytics` HEAD
`241fb04`. Remote refs were fetched before work. The checkout's frozen
candidate from 2026-10-06 is absent, so this report distinguishes current
code evidence from the historical diagnostic archive.

## Current flow and boundaries

`StateBuilder` parses t-1 stock/backlog, supply, pipeline, queues and WIP.
WIP is also represented once as an `ExpectedArrival`; it is not added to
`available_stock`. `NeedPlanner._requirements` creates dated published sink,
production/BOM, and projected grid-fuel requirements. `plan` nets those events
against on-hand stock and eligible dated arrivals in a shared per-pair ledger,
then emits `DeliveryNeed`s. `DecisionPipeline` validates the handoff and passes
the same state, observation and static route network to the allocator. The
planner does not create action slots, routes, or source stock.

The important pre-existing wafer path is sink forecast → OSAT package stock
coverage → OSAT throughput and BOM raw input → reachable Fab output stock and
route-specific Fab lead/transit → Fab input BOM. Shared dated stock/arrivals
are consumed once. This path is active only for the configured production
horizon and currently observed routes.

## Changes in this branch

- Added optional `fuel_replenishment_enabled`. For uncovered LNG/crude grid
  Needs, the planner selects one existing terminal coupling, requires an
  existing source→terminal slot, and emits a separately identified Need at the
  terminal. Its amount is net of terminal stock and eligible future arrivals.
  A repeated slot cannot multiply demand; direct nuclear-fuel grids do not get
  synthetic terminal legs.
- When enabled, fuel projection extends through the largest observed
  source→terminal transit plus terminal→grid transit (plus one week), so a
  source replenishment Need can appear before its receipt deadline. Terminal
  Need due weeks are backed out by the observed terminal→grid transit. The
  planner uses current observed edge transit only; hidden future disruptions
  are not used.
- `NeedPlanner.last_trace` now records each emitted Need's requirement,
  credited coverage, uncovered amount, receipt deadline, route lead lower
  bound, latest dispatch week, priority, shortage value, and reason. A
  `last_issues` entry flags a Need whose latest dispatch week is already past.
- `Agent` and `build_pipeline` accept an explicit `planner_options` mapping;
  constructor options are no longer silently ignored at that boundary. Boolean
  planner switches are validated. No allocator or action-slot code changed.

## Planner options and integration wiring

| Option | Code default | Recommended explicit value | Meaning / active | Coverage |
|---|---:|---:|---|---|
| `production_horizon` | 4 | 4 | Future demand weeks considered for production; active | `test_team_analytics`, `test_production_routes` |
| `production_enabled` | true | true | Build OSAT/Fab BOM needs; active | `test_team_analytics`, `test_nominal_bom` |
| `include_estimated_arrivals` | false | false | Credit non-observed arrival estimates; active | `test_production_routes`, `test_team_analytics` |
| `safety_stock` | true | true | Grid-only `ibar` reserve target; active | `test_team_analytics` |
| `safety_buffer_policy.input_buffer_fraction` | 0.0 | 0.0 | Fractional BOM input buffer; active when nonzero | `test_nominal_bom`, `test_team_analytics` |
| `shortage_cost_model` | false | false pending paired economics | Published sink pi/VOLL propagated with units; active | `test_team_analytics` |
| `fuel_replenishment_enabled` | false | true for candidate A/B | Add source→terminal LNG/crude stage and lead-aware forecast extension; active only when true | new fuel planner regressions |
| queue/arrival awareness | no planner switch | state builder default | Observed pipeline/queue/WIP become dated arrivals; estimated queue forecasts require a forecaster supplied to `StateBuilder` | `test_team_analytics` |
| route-aware production timing | no switch | on | Fab output coverage requires an existing route and observed edge times; active | `test_production_routes` |

Integration can opt in with:

```json
{"planner_options":{"production_horizon":4,"production_enabled":true,
 "include_estimated_arrivals":false,"safety_stock":true,
 "shortage_cost_model":false,"fuel_replenishment_enabled":true}}
```

`allocation_enabled`, queue ETA, announced ETA guard, and closure wait remain
separate allocator options and were not changed. The current `team_agent` has
no `params.json`; these planner values are not active through the default
Agent today, which still has allocation disabled.

## Findings from existing Full diagnostics

- Taiwan, Korea, and India: the archived Full/0 week-3 trace recorded LNG/crude
  grid Needs refused with `no_source_stock`, and zero upstream terminal Needs.
  These are planner chain omissions. This branch addresses that omission when
  enabled; a fresh Full replay is still required to assess sourcing/throughput.
- Nuclear fuel: prior week-3 Korea and India projections were smaller than
  their observed stock, so no Needs were emitted. This is coverage/netting,
  not proof that automatic future burn was forecast far enough. Nuclear fuel
  routes are direct source→grid and remain a separate commodity chain.
- Wafer/Fab: archived current diagnostics show empty Taiwan/Korea Fab input
  stock with no raw wafer requirement at Full weeks 1–7 for the synchronized
  planner, while the earlier frozen planner had wafer Needs in weeks 1–2.
  Existing output-stock/WIP coverage and the four-week window are plausible
  causes; no current causal replay after this change was run. Fuel changes do
  not alter the wafer/BOM path.
- Due weeks: the new trace reports a physical lower bound from currently
  observed transit and marks `latest_feasible_dispatch_week < current_week`.
  It deliberately leaves the requested receipt deadline unchanged. A genuine
  impossible-at-creation count requires a fresh Full trace.
- Critical semiconductor inputs and per-region cause counts were not measured
  in a new representative Full run; no planner-only classification is claimed
  for those cases.

## Economics and policy semantics

`priority` remains queue ordering (demand/grid 3, production 2, safety stock
1; backlog 4). It is not used as shortage value. Direct sink values are
published USD per native unit per weekly cost period. The optional grid fuel
VOLL estimate is USD/GWh per weekly period, while Fab output terms require
matching native BOM units. Unknown remains `None`; explicit zero remains `0`.
The upstream terminal Need inherits its corresponding grid Need's numeric
value because both quantities use the same fuel unit and represent the same
shortage exposure.

Safety stock is only the configured grid `ibar`, not a fixed planner buffer.
The BOM buffer defaults to zero. No Early Warning reserve logic was added.

## Tests and evaluation status

Focused planner/route/BOM/contract/agent tests: **125 passed**. The two new
fuel-chain regressions were rerun after the final fuel deadline changes:
**2 passed**. Ruff and `git diff --check` pass. `sbf check` on the final
controlled candidate copy passed archive/import validation and isolated
Small timing: 16 files; first week including init 0.043 s, median act 0.0101 s,
maximum act 0.043 s against the public 2 s/week budget.

The original frozen candidate/baseline inputs are absent. I created local
baseline/candidate copies at `outputs/planner_ab/` with identical allocator
settings and only `fuel_replenishment_enabled` different, then replayed the
same first seven weeks of episode 0 at entropy 67890. This is partial native
cost, not RSS or full-episode economics:

| Task | Baseline partial cost USD | Candidate partial cost USD | Difference | Total observed shed GWh, baseline → candidate | Needs, baseline → candidate | Max act CPU s, baseline → candidate |
|---|---:|---:|---:|---:|---:|---:|
| Small | 218,715,367,731.41 | 91,455,420,951.67 | -58.19% | 38,135.28 → 7,225.70 | 617 → 887 | 0.0379 → 0.0457 |
| Full | 353,479,129,524.44 | 195,342,597,907.03 | -44.74% | 66,518.31 → 28,042.41 | 1,300 → 1,844 | 0.0399 → 0.0450 |

Candidate fuel-replenishment Needs numbered 173 on Small and 340 on Full for
these seven weeks. The all-Need allocator `no_source_stock` record count rose
from 469 to 554 on Small and 1,532 to 1,724 on Full, so this partial result
does not establish that the new terminal Needs are consistently sourceable.
Freight and holding were not separately exported by this replay. `sbf compare`
was also attempted for Small, two episodes, entropy 67890, with CPU budget, but
remained in reference quantile generation and was stopped before agent runs.
Keep the feature opt-in until paired RSS and full-episode cost-component
evaluation confirms lower shortage/shed and higher RSS.

## Keep / reject recommendation

- **KEEP** single-use terminal inventory/arrival netting and source route
  existence checks; required for a distinct multi-stage Need.
- **KEEP** exact-slot de-duplication and topology-driven terminal selection;
  they prevent duplicated replenishment quantities.
- **KEEP** structured Need trace and impossible-deadline issue as diagnostics.
- **KEEP opt-in only** fuel projection extension and upstream replenishment
  until paired economic evaluation shows lower shortage/shed and improved RSS.
- **REJECT for now** turning on estimated arrivals, extra buffers, VOLL, or
  broad priority changes without separate measured evidence.

Unresolved: source stock availability is checked by the allocator at dispatch;
planner visibility that a route exists does not guarantee source stock or a
feasible allocation. Multi-terminal substitution and automatic fuel burn
outside the observed projection window also need Full-run validation.
