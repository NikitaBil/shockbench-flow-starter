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
