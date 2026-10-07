# Active dispatch upgrade (network-delivery, 2026-10-07)

These changes run in the existing allocation path (`allocation_enabled: true`,
`queue_eta_enabled: true`); they do not require privileged closure dates.
The default heuristic and the frozen candidate/baseline remain unchanged.
No measured RSS improvement is claimed. The teammate runs regressions and
paired Small/Full scoring; syntax and lint are checked locally.

## Stage 1: budget-aware search

Hypothesis: spending FIFO forecasts on provably inferior alternatives causes
later, lower-ranked needs to lose access to ETA computation unnecessarily.
For each need, a feasible incumbent bounds the search. An alternative is
skipped only if its optimistic ranking cannot beat the incumbent, including
quantity and stable slot tie-breaks. Observed transit is a lower bound, not a
completion estimate; hidden transit uses this week as the optimistic bound.
Each allocation rebuilds the comparison with remaining stock/edge/fleet.

Fields: existing action slots, graph_now.tau and visibility, route prices,
DeliveryNeed due/priority/quantity/shortage_cost_per_unit_usd, current stock,
edge and fleet ledger. Need ordering and FIFO budget (16 calls, 24 weeks)
are unchanged. One less forecast cannot guarantee lower operating cost.

Evaluation: pack the parent and this stage separately with identical allocation
and queue-ETA params, then `uv run sbf compare STAGE1 PARENT --task=small
--episodes=16 --entropy=67890 --cpu_budget=True`, and repeat for Full. Do not
use `--quick`. Check forecast usage and budget-denied unique week/need/slot
counts, cost components, realized arrivals, and week-1/max-week CPU. Different
network shapes come from config; no fixed commodity or node IDs are introduced.

## Stage 2: dispatch feasibility is separate from ETA certification

Hypothesis: an exhausted computation budget or a truncated 24-week forecast
window should not stop replenishment along otherwise observable open routes.
The old implementation conflated these cutoffs with physical infeasibility.
Unknown ETA is now eligible as a last resort after known in-horizon delivery
options, only for `queue_eta_forecast_budget_exhausted`, or
`queue_eta_completion_unresolved` when the simulated window ends before T.

This fallback requires observed stock, confirmed permission and observed
positive edge/queue rates and transit along the whole route. It does not bypass
missing own-state inputs, known bans, closure, a detected delay to an earlier
selected shipment, or completion failure after simulating through episode end.
The empty-queue batch lower bound must fit within T. With the announcement guard
enabled, a pending ban on a future leg requires a forecast and blocks fallback.

Each chosen shipment consumes the same current stock/entry/fleet ledger and
enters subsequent joint forecasts as a proposal. Neither pipeline arrivals nor
future queue capacity are spendable current resources. No release overrides.
Because unforecast cargo can affect earlier joint estimates, their completion
claims are conservatively withdrawn for all already selected sea slots. Final
assignment traces and AllocationResult reasons report unknown timing, not
on-time coverage; air/direct timing does not become unknown merely because a
sea proposal was added. This conservative invalidation does not create a new
forecast or reserve a queue.

Fields: existing pipeline/queue quantities and metadata/quality, stock.qty
visibility, graph_now.u/tau/open/kappa and masks, permitted action slots,
forecast cutoff reasons and T. No closure_end visibility is required.
Budget stays 16 calls and 24 weeks. Extra dispatch may increase freight, queueing
or excess inventory: the score must judge whether avoided shortages outweigh
these costs. Fewer *reported* late assignments after ETA withdrawal is not
evidence of faster arrivals. Count unknown assignments separately and measure
realized inventory, shortage/shed, arrival timing and cost.

Evaluation: same paired command as Stage 1, comparing STAGE2 against STAGE1
with identical params and entropy, Small and Full, 16 episodes each and CPU
budget. Inspect budget-denied unique needs, actual quantities dispatched and
received, unknown timing counts, and all cost components. Then compare the
combined candidate with the original frozen benchmark when those exact folders
are available. Never rewrite the frozen baseline or infer it from Git HEAD.

## Stage 3: recover sooner when shortage valuation is missing

Hypothesis: among late deliveries, choosing cheap slow transport when the
planner supplies no marginal shortage cost unnecessarily prolongs shortage.
`None` now means missing valuation, rather than zero damage. Timely options
still rank first. Known late options with missing valuation rank by completion
delay, then transport plus holding cost, volume and stable slot ID. Explicit
zero remains zero; supplied USD/native-unit/week penalties retain the existing
cost-plus-delay comparison. Unknown options remain behind known in-horizon
options and use the no-queue lower bound for ordering only, never certification.
Forecast scheduling follows the same fastest-first rule for nominally late,
unpriced needs. Agreed inter-need priority/deadline/ID order is unchanged.

Fields: DeliveryNeed due and optional shortage valuation, candidate completion
or unknown flag, route transit/rates and transport/holding cost. No new field,
commodity exception, planner rule or budget increase. Faster transport can
increase freight; this is an explicit tradeoff for teammate evaluation, not a
claim that every episode improves.

Evaluation: compare STAGE3 against STAGE2, then combined against the matched
parent and frozen references, with the same Small/Full paired commands. Measure
total RSS and shortage/shed *and* freight/holding. Track realized delivery delay;
do not confuse nominal bounds or withdrawn ETA labels with actual arrivals.

## Regression handoff

Run `uv run pytest tests/test_allocation.py tests/test_dispatch_search.py
tests/test_dispatch_uncertainty.py tests/test_dispatch_lateness.py
tests/test_announced_delivery_eta.py tests/test_closure_delivery.py
tests/test_allocator_diagnostics.py`, followed by the integration/state/planner
regressions and the normal suite on Linux. Run `sbf check` on Tiny/Small/Full
and compare with CPU metering. These regression files were written here but
their execution and RSS evaluation are delegated to the teammate as requested.

Reproduce immutable, enabled agent ZIPs from the stage commits using
`uv run python scripts/prepare_dispatch_comparison.py`. The printed output
directory contains `before.zip`, `search.zip`, `dispatch.zip`, `candidate.zip`,
the corresponding agent folders and `manifest.json` with commit IDs and packed
SHA-256. This uses the benchmark's submission packer and `check_zip` archive
validation (not timed execution), does not execute an agent,
and refuses existing output directories. All stages use identical existing
`allocation_enabled=true, queue_eta_enabled=true` params. No new feature flag
must be enabled. Planner defaults are identical between stages; the original
uncommitted frozen candidate is a separate reference and is not reconstructed.

| Variant | Agent commit | Compare against |
| --- | --- | --- |
| before | `0e9b92b` | matched current parent, allocation enabled |
| search | `f39133b` | before |
| dispatch | `727c6d9` | search |
| candidate | `a8ab160` | dispatch, then before and exact frozen references |

From the generated directory, run these as single-line commands (Linux):

```sh
uv run sbf compare search.zip before.zip --task=small --episodes=16 --entropy=67890 --cpu_budget=True
uv run sbf compare dispatch.zip search.zip --task=small --episodes=16 --entropy=67890 --cpu_budget=True
uv run sbf compare candidate.zip dispatch.zip --task=small --episodes=16 --entropy=67890 --cpu_budget=True
uv run sbf compare candidate.zip before.zip --task=small --episodes=16 --entropy=67890 --cpu_budget=True
```

Alternatively pass full generated ZIP paths from the repository root. Repeat
with `--task=full`; use the exact original frozen candidate/baseline as additional
comparisons. Root 0 is a separate confirmation after deciding on changes, not
the basis for tuning. Check the paired RSS interval, per-episode differences,
cost components and CPU rather than using a successful dispatch as score proof.

The allocator cannot fulfill a request the planner never emits. Wafer input
coverage, projected nuclear fuel consumption and missing terminal upstream
needs remain separate planner investigations. These changes spend existing
needs' dispatch resources better; they do not fabricate needs, sources or routes.
