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
