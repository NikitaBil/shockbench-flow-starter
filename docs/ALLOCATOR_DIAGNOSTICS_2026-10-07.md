# Network/delivery: allocator audit, 2026-10-07

Branch: `network-delivery`. Author: Victor Danylenko. The allocation policy
remains opt-in: `allocation_enabled=true`, `queue_eta_enabled=true` in a **new
evaluation copy** of `params.json`. The default heuristic is still the default.

The current allocator correctly filters candidates without dispatchable stock,
permission, current edge capacity or residual fleet capacity before FIFO work.
The largest observed failures also involve needs that never reach the allocator
and replenishment of intermediate terminals. Raising the ETA budget cannot fix
those failures.

## Scope and synchronization

- Started at `5ac577a`, fetched origin and merged `origin/analytics=03c266e`
  in `3e97e48`. `origin/main=a0b48e2` and `origin/integration=48acf87` were
  already ancestors. Fetched again before delivery; these tips had not changed.
- Imported the team's BOM, dimensional cost, WIP and production timing changes.
  No additional edits to `needs.py`, `state.py`, shared contracts or presets
  were made in this delivery task.
- Preserved previous frozen candidate/baseline folders. Diagnostics use new
  copies under `outputs/11_allocator_diagnostics/`, verify their SHA-256 hashes,
  and record parameters, source paths, root and episode.
- Root `12345`, episodes 0 and 1 are **local native evidence**. The exact
  teammate frozen commit, params and root were unspecified during this audit. These runs do
  not reproduce the quoted RSS scores or the exact 3597/4082 diagnostic counts.
  Metadata was subsequently supplied; see
  [TEAM_FROZEN_RUN_2026-10-06.json](TEAM_FROZEN_RUN_2026-10-06.json) and
  [the synchronization note](NETWORK_DELIVERY_SYNC_2026-10-07.md).

## Separate changes

| Commit | Main idea | Hypothesis | Result |
| --- | --- | --- | --- |
| `c40eae0` | Optional per-candidate trace | Separate resource, topology and ETA failures without changing actions | Action/resource/reason parity test passed |
| `8618a99` | Pre-FIFO horizon filter | Observed transit already beyond T cannot benefit from an expensive forecast under its persistence assumption | Full/0 forecasts 790 → 787; identical actions and costs |
| `b4ddaef` | Forecast scheduling within each need | Spend the bounded budget on potentially timely routes before cheaper routes already late without a queue | One-forecast regression selects the timely route; Small/0 and Full/0 unchanged |

Two diagnostic fixes, `1eabda2` and `35db2ca`, respectively allow editing the
working tree after a replay has frozen its inputs and support the archived
planner's older `_requirements` signature. The initial Full/0 run finished
both trajectories but its old post-run assertion rejected the changed working
source. The frozen copies remained unchanged; `audit.json` records verification.
The completed horizon comparison independently replays the same pre-fix policy
and reproduces its actions, costs and diagnostic counts.

Topology-specific `DecisionReason` records are also returned through the
existing `AllocationResult`. `UnmetNeed.reason=no_permitted_delivery_slot`
remains compatible with existing consumers. The new codes distinguish
`no_action_slot_to_destination` from `all_delivery_slots_prohibited`.
The latter can reflect permission estimates; the detailed trace separately
reports permission visibility. An absent slot is evidence to check a need's
destination/commodity semantics, not proof by itself that the planner is wrong.

## Fields and policy criteria

- Stock: `StateSnapshot.available_stock`, sourced from `stock.qty` and its
  masks; only observed stock is spent. Existing pipeline/arrivals are not
  added to this week's dispatch stock.
- Topology: configured `action_slots`, lane edges and `StaticNetwork.slots_to`.
  A lane's final destination is distinct from an intermediate terminal.
- Current constraints: masked `action_mask`, `graph_now.prohibited`, `u`,
  `open`, `kappa.tb/ct`; current fleet bounds also account conservatively for
  automatic releases. Capacity estimates retain their provenance.
- Timing: `week`, T, masked `graph_now.tau`, queue/pipeline state and need's
  absolute `due_week`. Hidden transit is not used as a known lower bound for
  the horizon filter.
- Price: current freight, tariffs, commodity customs value, war risk and
  `shortage_cost_per_unit_usd` when the needs owner provides it.

Needs still use **higher priority → earlier due week → stable need ID**.
Within one need, forecasts are scheduled by
`(no_wait > due, transport_cost_per_unit, no_wait, slot_id)`.
This schedules work; it does not establish on-time ETA. The final greedy
candidate ranking remains known on-time / known late / unknown, then economic
cost including known lateness penalty, ETA, larger feasible quantity and slot.
Unknown ETA is rejected. Late fallback remains explicitly labelled late.
Due weeks are not shifted to conceal lateness. FIFO limits remain **16 calls
per allocate, at most 24 forecast weeks**. No future resources are reserved.

## ETA budget and late deliveries

Before the two policy fixes, with synchronized needs:

| Full episode | Budget-denial records | Unique (week, need) | Unique (week, need, slot) | Actual forecasts | Late / assignments |
| --- | ---: | ---: | ---: | ---: | ---: |
| 0 | 8860 | 1621 | 5695 | 790 | 2583 / 3849 = 67.1% |
| 1 | 3393 | 794 | 1908 | 428 | 2238 / 3598 = 62.2% |

Every budget-denial event had positive feasible quantity, current source stock,
entry capacity and dispatch permission; weighted fleet candidates also had
residual fleet capacity. Repeated greedy passes/cache hits contribute multiple
records. These are not 8860 or 3393 independent rejected requests. Candidate
events can also coexist with a later assignment through an alternative slot.

Full/0 denials by commodity: `chip_mat=3402`, `chip_le_raw=4944`,
`chip_mat_raw=472`, `wafer=42`. Full/1: `1134`, `1963`, `287`, `9` respectively.
Their need IDs, ranks, deadlines, slot IDs and resource evidence are in the
trace; compact examples are committed in `ALLOCATOR_DIAGNOSTIC_EVIDENCE.json`.

All late assignments in these Full runs had `no_wait_arrival > due_week`.
None was already overdue at dispatch; none became late solely from queue or
batch delay. Back-planned current receipt deadlines can already be impossible
from current source stock. Simply speeding up the forecast or increasing its
budget does not make those assignments timely. Small/0 has 410 late assignments
of 697: 409 already late under no-wait transit, one attributable to queue/batch
delay. A forecast remains conditional on current transit/rates persisting.

## First seven weeks Full/0: fuel and wafer

Quantities below are **requested**, not executed shipments. Nucfuel is GWh;
wafer is wafer-equivalent. Do not add these columns across commodities.

| Week | Synchronized nucfuel | Heuristic nucfuel | Synchronized wafer | Pre-merge wafer |
| --- | ---: | ---: | ---: | ---: |
| 1 | 0 | 48,768 | 0 | 648,582.83 |
| 2 | 0 | 48,768 | 0 | 202,375.95 |
| 3 | 0 | 48,768 | 0 | 0 |
| 4 | 0 | 48,768 | 0 | 0 |
| 5 | 0 | 48,768 | 0 | 0 |
| 6 | 37,701.91 | 48,768 | 0 | 0 |
| 7 | 43,597.20 | 48,768 | 0 | 0 |

The pre-merge reconstruction copies the `5ac577a` frozen folder into a new
directory and overlays only the action-preserving trace implementation from
`c40eae0`; original files are untouched. Its wafer needs exist in weeks 1–2,
with resource and ETA-budget refusals. In week 3 its raw wafer requirements
are already absent. After importing the team's timing/BOM changes, raw wafer
requirements are absent for the first seven weeks. This change precedes both
delivery fixes and must be evaluated independently by the needs owner.

Nucfuel needs are netted by the planner in weeks 1–5. Week 3 examples:

- Korea: observed grid stock 171,845.79 GWh; four raw requirements of
  3163.30 GWh for weeks 3–6; no emitted nucfuel need.
- India: observed stock 56,160 GWh; four raw requirements of 1080 GWh;
  no emitted nucfuel need.
- Taiwan has no configured nucfuel delivery slot in this Full topology;
  its fuel chain uses terminal-to-grid LNG/crude. The absence of a Taiwan
  nucfuel route is not a reason to fabricate one.

Thus the zero early nucfuel dispatch is **planner coverage**, not an allocator
budget rejection. The observed stocks cover the planner's stated requirements;
whether those requirements adequately describe automatic future consumption
still needs validation. This root's heuristic requests 48,768 GWh/week,
different from the teammate's quoted 36,726; exact scenario matching is pending.

For LNG/crude, valid grid transfer slots exist: Full slots 55/56
`term_tw → grid_tw`, 57/58 `term_kr → grid_kr`, 68/69 `term_in → grid_in`.
Week 3 traces show grid-fuel needs rejected with `no_source_stock` at these
slots. There is **no upstream need at the corresponding terminal** for that
commodity. The allocator cannot spend source inventory at a remote node as if
it were already at the terminal. Shipping source-to-terminal is a separate
action and does not immediately fulfill the final grid need.

Visible native shed for the first seven weeks: India becomes 360 GWh/week
from week 4 for the candidate versus zero for the heuristic; Korea candidate
week 5/6/7 is 922.58 / 2509.11 / 2483.40 GWh versus heuristic
1148.26 / 2509.11 / 2401.55. Taiwan is zero on both in these seven weeks.
That last observation does not contradict losses later in the teammate's
Full episodes. The committed evidence retains each week's regional values.

## `no_permitted_delivery_slot`

Full/0 and Full/1 did not emit this reason on the local root. Small/0 emitted
47 records, all because existing slots were prohibited, not because topology
was absent. For example, week 6 `safety_stock:21:2:9` has static slot 26;
current permission excludes it. The test also covers a truly absent action
slot and returns separate topology evidence without synthesizing a path.

## Verification and limits

- Allocator tests, including Tiny/Small/Full real nominal dispatch and packed
  opt-in agents, passed in the full run. Additional diagnostic tests passed.
- Whole `uv run pytest -n 3`: **226 passed, 13 failed, 7 skipped**. The 13
  failures are outside delivery: twelve require Unix `fcntl` in official
  check/evaluation examples; one checks Unix `.env` mode 0600 on Windows.
  Two newly added diagnostic tests passed separately after collection.
- Focused latest allocator/diagnostic checks cover resource prefilters,
  topology, unknown transit/ETA, fixed budget, deterministic priority/deadline/ID
  order, deadline-sensitive forecast scheduling and preserved frozen inputs:
  **43 passed**, eight real-environment/packed cases deselected in that focused
  run (those eight had already passed in the full run).
- Ruff passes for changed Python files. `sbf check team_agent --task=small`
  passes archive/import validation; isolated timing fails at `import fcntl`.
  Official `sbf compare` and server CPU validation remain with the teammate.
- Local maximum CPU, including init and trace collection: final Small/0
  **0.125 s**, Full/0 **0.5625 s**, Tiny seven-week smoke **0.140625 s**.
  These are below the local 2/4-second limits, not a server certification.
- Native full-episode costs: Full/0 **$15,277,576,301,869.13**, Small/0
  **$5,185,258,028,643.92**. Both unchanged by within-need forecast scheduling
  on this root. The heuristic Full/0 is **$5,335,791,765,444.15**. The candidate
  still needs substantial work; these fixes are not evidence of competitiveness.

## Handoff to Markiyan and paired evaluation

This document is a prepared handoff; no direct message to Markiyan was sent.

1. Add/check explicit upstream terminal replenishment needs for observed
   terminal-to-grid deficits, using existing source-to-terminal slots and
   a receipt deadline that accounts for the final transfer. Keep distinct
   intermediate/final need IDs and prevent counting the same deficit twice.
2. Explain absent wafer requirements with zero Fab input stock in weeks 1–7
   after synchronization. Week 3 `fab_tw_leading_1`, `fab_tw_mature_1` and
   `fab_kr_leading_1` are concrete examples in the evidence JSON. Check finished
   stock/WIP coverage and the four-week planning window against production plus
   transit lead times. Do not mix this with a fuel heuristic experiment.
3. Compare projected grid fuel burn with automatic production in the actual
   environment. `dynamics/sim.py` sets Fab `phat` from capacity and available
   input, then requests energy; this is independent of our grounded production
   targets. Grid rationing reads previous gas stock against `psi * ibar`;
   immediate fuel availability also matters. Validate those assumptions before
   changing fuel targets or safety reserves.
4. Check due weeks with no-wait transit already too long. Preserve late and
   unknown labels; do not shift deadlines to improve diagnostics artificially.

For Linux paired tests, export three **new** copies from commits `c40eae0`
(diagnostics only), `8618a99` (horizon prune) and `b4ddaef` (forecast order).
For example, from the repository root:

```bash
mkdir -p outputs/delivery_stages/c40eae0 outputs/delivery_stages/8618a99 outputs/delivery_stages/b4ddaef
git archive c40eae0 agents/team_agent | tar -x -C outputs/delivery_stages/c40eae0
git archive 8618a99 agents/team_agent | tar -x -C outputs/delivery_stages/8618a99
git archive b4ddaef agents/team_agent | tar -x -C outputs/delivery_stages/b4ddaef
uv run python - <<'PY'
import json
from pathlib import Path
for ref in ('c40eae0', '8618a99', 'b4ddaef'):
    path = Path('outputs/delivery_stages') / ref / 'agents/team_agent/params.json'
    params = json.loads(path.read_text()) if path.exists() else {}
    params.update(allocation_enabled=True, queue_eta_enabled=True)
    path.write_text(json.dumps(params, indent=2) + '\n')
PY
```

Evaluate each change separately with identical roots/episode lists and CPU
metering. Example for the horizon filter:

```bash
uv run sbf compare outputs/delivery_stages/8618a99/agents/team_agent outputs/delivery_stages/c40eae0/agents/team_agent --task=small --entropy=12345 --episodes=16 --cpu_budget=True --n_jobs=1 --out=outputs/delivery_stages/prune_small.json
uv run sbf compare outputs/delivery_stages/8618a99/agents/team_agent outputs/delivery_stages/c40eae0/agents/team_agent --task=full --entropy=12345 --episodes=16 --cpu_budget=True --n_jobs=1 --out=outputs/delivery_stages/prune_full.json
```

Then compare `b4ddaef` against `8618a99` with the same flags. Confirm promising
changes on root 0. Afterwards compare the complete candidate against the
team's untouched frozen baseline using its original root and parameters.
Report paired intervals, RSS, shortage/shed and CPU, not only averages.
Do not edit the original frozen candidate/baseline for any of these tests.

To reproduce local detailed traces:

```text
uv run python examples/11_allocator_diagnostics.py --task=full --entropy=12345 --episode=0 --weeks=7
uv run python examples/11_allocator_diagnostics.py --task=full --entropy=12345 --episode=1 --weeks=0
```

Full traces and copied source are local ignored outputs. Compact measured
evidence and example refusals are versioned in
[ALLOCATOR_DIAGNOSTIC_EVIDENCE.json](ALLOCATOR_DIAGNOSTIC_EVIDENCE.json).
