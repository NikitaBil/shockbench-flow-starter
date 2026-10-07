# V5.2: conditional closure waiting prototype

**Decision: keep `closure_wait_enabled=false`.** The installed benchmark's
leaderboard regime, `standard`, has `chi=false`: it does not reveal closure end
dates. This prototype therefore cannot improve standard-regime RSS using the
date feed. V5.2's competitive waiting policy still needs an uncertainty-aware
remaining-duration model. No artificial end dates were supplied in evaluation.

The availability constraint is stated in public
`shockbench_flow/information/theta.py` (`Standard.chi`) and the leaderboard
regime is documented in `docs/GUIDE.md`. The installed package's
`information/view.py` defines visible closure ends as the week when an active
event ends, with one row per event. Several events can affect one chokepoint.
The simulator joins arrivals to the queue before releasing cargo, and checks
the next edge's current prohibition at release. An already traversed edge does
not stop cargo retroactively.

## Implemented mechanism

This is one opt-in experiment based on parent `ad18bd5`. It tests dispatching
now onto a legal route and waiting at its chokepoint against dispatching now
via a detour. It does not implement postponing dispatch at the source.

Code commit: `818d7be2d6f0e9285cc8e6b290d36a353e6e5579` on `network-delivery`,
authored by Victor Danylenko.

```json
{
  "allocation_enabled": true,
  "queue_eta_enabled": true,
  "announced_eta_guard_enabled": false,
  "closure_wait_enabled": true
}
```

The flag defaults to false, requires the allocator and queue ETA, and remains
independent of the earlier announced-prohibition guard. No team contracts,
NeedPlanner or StateBuilder logic changed. Agent/integration only forward the
flag. QueueForecaster accepts an optional delivery-owned pool-throughput
schedule; its existing default persistence model remains in effect otherwise.

Fields and data:

- `closure_end.chokepoint`, `closure_end.end_week`, and both observed masks;
- `graph_now.open`, pool `kappa`, live `tau`, `war_risk` and observed masks;
- configured StaticNetwork routes, commodity pools/native units, nominal
  `k_c * mu`, and per-war-class `queue_holding` costs from static instance nodes;
- current observed stock, entry capacity, permissions, fleet slack;
- existing queue/pipeline lots, proposed cargo, need priority/deadline and
  supplied per-unit/per-week shortage penalty.

An observed closed chokepoint is eligible for waiting only with a known active
end date and observed pool throughput. Multiple active events use their
**latest** end date. An unknown overlapping end invalidates that node's
calendar. A live row with hidden node invalidates the calendar because it could
overlap any known node. Padding, hidden dates and stale dates never become zero
waiting time.

Before the last known end, current pool throughput persists. At and after that
week, the conditional schedule restores nominal `k_c * mu`, without multiplying
openness twice. This also supports observed partial closures; intermediate
improvements between overlapping events are not inferred. Edge/fleet capacities,
transit, war-risk and bans persist at the existing forecast assumptions. Future
unannounced events are unknown. The result is an **estimated conditional ETA**.

The full existing FIFO engine includes competing lots and all proposed cargo;
the last piece's completion determines ETA. A lower-priority addition cannot
invalidate an earlier selected shipment's reported ETA. Stock and capacity are
spent only on current dispatch; no future stock or resources are reserved.
Standard release continues, with no overrides.

Candidate ranking retains timely-first ordering, followed by transport plus
an upper bound on queue holding cost and the supplied lateness penalty, then
deterministic tie-breaks. The holding bound assumes the whole proposed quantity
remains until each forecast cohort's last release; actual partially released
cargo can cost less. War-risk class persists for this estimate. This is a
conservative comparison coefficient, not a calibrated expected episode cost.

Diagnostics include `closure_route_comparison` with feasible alternatives'
slots, ETA and transport/queue cost, and `wait_for_announced_reopening` with
dates, completion and holding bound. Arrival after the deadline remains late;
unknown ETA remains unknown. No known-date ETA is invented when the feed is
absent.

Budgets remain **16 FIFO calls per allocation, 24 weeks lookahead**. Candidates
without stock, current capacity or permission are rejected first. A known
transit/closure lower bound past the episode horizon is also rejected before
forecasting; that lower bound is never labelled a timely ETA.

## Native one-change comparison

`examples/13_closure_wait.py` creates new frozen `before`/`after` copies,
enables allocator and queue ETA in both, disables the announced guard in both,
and changes only `closure_wait_enabled`. Fingerprints verify that source and
copies stayed unchanged. Initialization counts in week 1. Recorded rows include
actions, costs, CPU, reasons and closure-feed exposure.

```text
uv run python examples/13_closure_wait.py --task=small --entropy=67890 --episodes='[0,1,2,3]'
uv run python examples/13_closure_wait.py --task=full --entropy=67890 --episodes='[0,1]'
```

All six episodes completed (52 weeks on Small, 104 on Full), replay seed 0:

| Task / episode | Cost before and after, USD | Changed action weeks | Maximum CPU after, s | Known end rows / fully closed node-week records |
| --- | ---: | ---: | ---: | ---: |
| Small / 0 | 5,061,382,976,051.70 | 0 | 0.109375 | 0 / 1 |
| Small / 1 | 5,092,788,237,368.59 | 0 | 0.093750 | 0 / 1 |
| Small / 2 | 5,133,691,243,341.59 | 0 | 0.078125 | 0 / 2 |
| Small / 3 | 5,020,180,266,989.67 | 0 | 0.093750 | 0 / 1 |
| Full / 0 | 15,378,353,408,753.03 | 0 | 0.375000 | 0 / 5 |
| Full / 1 | 14,441,462,050,782.08 | 0 | 0.171875 | 0 / 0 |

No standard-regime waiting decisions were triggered. The known-date behavior
is demonstrated by controlled tests, not these leaderboard-regime trajectories.
There is **no measured improvement**; chi=false explains the zero effect.
Measurements used local process CPU and some tests/runs overlapped in separate
processes. They show local headroom to Small 2 s / Full 4 s, not exact overhead
or server certification. This is not RSS and does not reproduce the unavailable
October 6 frozen submissions. Compact evidence is saved in
`CLOSURE_WAIT_EVIDENCE.json`; complete reports remain in ignored outputs.

## Checks and evaluation decision

The 31 new tests cover quiet/urgent choices, reopening before arrival and at
arrival, overlapping closures, hidden data, multiple chokepoints, FIFO backlog,
full batches, holding costs, shared stock, priority/determinism, the interaction
with the announced-ban guard, protected selected ETA, horizon/budget pruning,
disabled behavior, flag validation, packed imports/actions on Tiny/Small/Full,
and the installed Standard regime's unavailable dates.

Full repository run: **293 passed, 13 failed, 7 skipped**. Three subsequently
added tests are covered by the final new test-file run: **31 passed**.
The 13 failures remain the known Windows limits: 12 Unix `fcntl` dependency
failures and one Unix `.env` mode-0600 assertion. Ruff checks cover all changed
Python files. Small opt-in `sbf check` passed archive/import checks (16 files);
isolated timing failed at `import fcntl`.

The teammate can independently verify the standard-regime no-op with:

```text
uv run sbf compare outputs/13_closure_wait/RUN/after outputs/13_closure_wait/RUN/before --task=small --entropy=67890 --episodes=16 --cpu_budget=True
uv run sbf compare outputs/13_closure_wait/RUN/after outputs/13_closure_wait/RUN/before --task=full --entropy=67890 --episodes=16 --cpu_budget=True
```

Replace RUN with the actual copied directory; do not use quick. This is an
integrity check, with expected zero policy difference under standard. A larger
tuning sweep for the known-date flag has no purpose while chi remains false.

## Required before a competitive waiting policy

The state/risk owner needs to provide a bounded remaining-closure forecast from
available openness history, Early Warnings and permitted announcements, with
provenance and uncertainty. An estimated date must not be written into an
observed closure_end row. Delivery then compares conditional waiting scenarios
against legal detours, includes backlog and waiting/shortage costs, and keeps
unknown outcomes explicit. That is a separate change, not a claimed completion
of the standard-regime V5.2 policy.

Evaluate that future policy separately using paired `sbf compare` on Small and
Full with CPU budgets, root 12345 for development, then independent root 67890
and dev-root confirmation. The existing fuel/wafer/terminal planner issues
remain higher-priority leads for improving the whole agent's RSS.
