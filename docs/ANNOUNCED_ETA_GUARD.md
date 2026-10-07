# Network/delivery V5.1: announced-prohibition ETA guard

This is one optional experiment on `network-delivery`, based on synchronized
parent `58d2128`. It does not complete all of V5 (waiting for reopening versus
detouring remains separate), and it does not implement the planner's terminal
replenishment or wafer/fuel consumption policy.

## Hypothesis and scope

Avoid assigning cargo using a FIFO completion estimate whose assumed next-edge
releases contradict an observed announced prohibition. This may prevent
stranded shipments, but it may also lose useful deliveries because announcements
can be decoys. It must therefore remain opt-in pending paired evaluation.

The public simulator checks the next edge's prohibition at queue release in
`shockbench_flow/dynamics/chokepoint.py`. Cargo already on an edge is not stopped
by subsequently prohibiting that traversed edge. Public
`information/messages.py` explicitly documents that `pending_prohibitions` comes
from shown publications and does not distinguish real threads from decoys.
Neither an announcement nor the absence of one is certain future permission.

The implementation uses only:

- observed `pending_prohibitions.edge`, `.k`, `.effective_week` rows and their
  individual `.observed` masks;
- the existing action-slot routes in `StaticNetwork`;
- observed `graph_now.tau` for the cheap entry-time check;
- existing queue/pipeline state and joint `QueueForecast.visits` for projected
  first/full releases of each commodity onto its next edge.

Padding and partly hidden rows are ignored. Duplicate rows use the earliest
announced effective week; row order does not affect the result. Announcements
outside the future episode horizon are ignored. No slots or future inventory
are invented, and no forecast reserves future resources.

## Behavior

Set this JSON in an experimental submission folder:

```json
{
  "allocation_enabled": true,
  "queue_eta_enabled": true,
  "announced_eta_guard_enabled": true
}
```

The new flag defaults to **false** and requires the first two flags. Agent and
integration changes only wire this parameter to our Allocator; the team
contracts, StateBuilder, NeedPlanner and QueueForecaster are unchanged.

If even the observed transit-only entry to a future edge is at or after its
announced effective week, the conditional estimate is rejected before a FIFO
call. Otherwise the normal FIFO forecast runs, and its queue visits are checked.
A release that crosses the announced effective week, including a batch's last
release, invalidates the estimate. A partially released competitor that extends
into that announced period also leaves the joint estimate unresolved. All joint
cargo is checked: a competing shipment's invalid releases can change FIFO shares.

The reason is `queue_eta_announced_prohibition_conflict` and ETA is **unknown**.
This means the current conditional forecast is unsuitable under the announced
scenario; it does not assert the real route will be permanently unavailable.
There is no known lifting date in this feed, so this experiment does not predict
reopening or recompute the queue under timed bans. The allocator may select an
existing alternative, otherwise it reports unmet need. Normal release remains
in effect, with no overrides.

Budgets stay at **16 FIFO forecasts per allocation, 24 weeks lookahead**. Parsing
is once per allocation; checks reuse the existing route/forecast data. CPU must
still be measured on both Small and Full.

## Reproducing the isolated experiment

`examples/12_announced_eta_guard.py` copies the current submission into new
`before` and `after` folders. The only parameter difference is the new flag.
It preserves existing params, enables the allocator and queue ETA in both,
records folder fingerprints, verifies inputs stayed unchanged, and measures
Agent initialization in week 1. It records complete actions, costs, unmet
reasons and CPU; it does not compute RSS.

```text
uv run python examples/12_announced_eta_guard.py --task=small --entropy=67890 --episodes=0
uv run python examples/12_announced_eta_guard.py --task=full --entropy=67890 --episodes='[0,1]'
```

Use the resulting submission folders for the teammate's official paired check
(replace the example output path with the actual generated directory):

```text
uv run sbf compare outputs/12_announced_eta_guard/RUN/after outputs/12_announced_eta_guard/RUN/before --task=small --entropy=12345 --episodes=64 --cpu_budget=True
uv run sbf compare outputs/12_announced_eta_guard/RUN/after outputs/12_announced_eta_guard/RUN/before --task=full --entropy=12345 --episodes=64 --cpu_budget=True
```

Do not use `quick`. Tune on root 12345; confirm separately on root 67890 with
16 paired episodes and on the dev root. An interval containing zero gives no
evidence to promote the flag. Compare against the shipped heuristic separately
to establish whether the whole agent improves on baseline.

These copies are the synchronized current policy, **not** the teammate's
original frozen candidate/baseline from October 6. Those exact folders remain
unavailable locally; their metadata is in `TEAM_FROZEN_RUN_2026-10-06.json`.

## Verification

The new tests cover effective-week equality, passed first legs, commodity and
route matching, queue-delayed last batch, a conflicting competitor, masked rows,
unknown transit, duplicate ordering, no alternative, disabled behavior, flag
validation, and real Tiny/Small/Full observations and actions.

Code commit: `76b2f839253f661e975cc2bebd5ac18fd95565ad`.

The complete native episodes at root 67890, replay seed 0 gave:

| Task / episode | Weeks | Cost before and after, USD | Changed action weeks | Maximum CPU before / after, s |
| --- | ---: | ---: | ---: | ---: |
| Small / 0 | 52 | 5,061,382,976,051.70 | 0 | 0.3125 / 0.3125 |
| Full / 0 | 104 | 15,378,353,408,753.03 | 0 | 0.890625 / 1.0000 |
| Full / 1 | 104 | 14,441,462,050,782.08 | 0 | 0.4375 / 0.484375 |

No candidate forecast triggered the announced-conflict guard in these runs.
An independent replay of the recorded actions checked actual feed exposure:
Small/0 had 5 observed pending-row records in weeks 45–49; Full/0 had none;
Full/1 had 11 records in weeks 3–5 and 11–14. These are repeated per-week rows,
not counts of distinct events. Thus real announcements were present in two
runs, but did not invalidate the evaluated shipment forecasts. The synthetic
tests exercise the conflict behavior. Broader paired evaluation is needed to
measure its real policy effect. **Keep the flag disabled.**

`uv run pytest -n 3 -q`: **265 passed, 13 failed, 7 skipped**. The 13 failures
are the previously known Windows limitations: 12 depend on Unix `fcntl`, one
asserts Unix `.env` mode 0600. All 19 newly added tests passed; Ruff and format
checks passed for all changed Python files. The Small opt-in submission passed
the official archive/import checks (15 files); isolated timing stopped on
`import fcntl`. Local process CPU stayed below Small 2 s / Full 4 s limits.

Initialization is counted in week 1. The tests and part of native evaluation
ran concurrently in separate processes on this workstation; these measurements
do not certify the server's performance or establish a precise CPU overhead.

Measured results are recorded in the accompanying
`ANNOUNCED_ETA_GUARD_EVIDENCE.json`. Do not interpret native cost differences or
local process CPU as official RSS or server CPU certification.

## Remaining work

1. Teammate: evaluate this flag independently with the paired commands above;
   preserve frozen originals and return RSS delta/interval plus CPU results.
2. Network/delivery V5.2: compare waiting for an evidenced reopening against
   existing detours. Agree the time-dependent queue interpretation with the
   state/forecast owner before adding it; no arbitrary reopening schedule.
   Update: the prototype and availability audit are in
   `CLOSURE_WAIT_EXPERIMENT.md`. Standard has `chi=false`, so observed end dates
   cannot drive this policy on the leaderboard; a remaining-duration model is
   still needed. The known-date flag stays disabled.
3. Planner owner: terminal upstream replenishment, wafer raw requirements,
   and actual fuel consumption remain the main unresolved shortage/shed leads
   from the previous audit. This announcement experiment does not fix them.
   Delivery will verify the resulting needs against legal slots, current
   stock/capacity and ETA in a separate integrated experiment.
