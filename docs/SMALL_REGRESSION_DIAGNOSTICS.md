# Small regression diagnostics

The frozen run `nikita-candidate-20261006-140843` scored -1.9408 against
baseline 0.4900 on 16 validation episodes at entropy 67890. No naive-rule
substitutions were reported. This is evidence against promoting that candidate,
not a failure of the old baseline. The baseline and frozen candidate must stay
unchanged while diagnosing the regression.

## Replay

Run in the VS Code WSL terminal:

```bash
cd /mnt/c/Users/nikit/projects/shockbench-flow-starter
export UV_PROJECT_ENVIRONMENT="$HOME/.venvs/shockbench-flow-starter"
uv sync --locked
uv run pytest tests/test_team_diagnose.py -q

RUN="outputs/nikita-candidate-20261006-140843"
DIAG="$RUN/diagnostics-$(date +%Y%m%d-%H%M%S)"
uv run python examples/12_team_diagnose.py --agent="$RUN/candidate" --against="$RUN/baseline" --task=small --entropy=67890 --episodes=2 --out="$DIAG"
```

Start with episodes 0 and 1; the evaluation report shows both regress strongly.
This is a diagnostic replay, not a new RSS measurement or official CPU check.
The policies have independent trajectories from matching reset seeds; later
observations need not match. The scoring runner's salted policy_seed is not
reproduced. Do not substitute this replay's cost for the official comparison.

## Recorded Evidence

- `summary.json`: cost, zero-request weeks, quantities by commodity, reason and
  issue counts, submission hashes, source-preservation check and limitations.
- `report.txt`: compact cost/zero-flow comparison and frequent candidate reasons.
- `candidate-0.jsonl`, etc.: pre-action stock/backlog, observed/estimated/unknown
  quantities, pipeline, queues, arrivals, needs, unmet requests, resource usage,
  allocation messages, requested action flows and subsequent step cost.

Requested flows are not guaranteed actual shipments. Do not sum quantities of
different commodities/units. Local constructor/act timings include diagnostic
wrappers and do not replace isolated `sbf check` CPU measurements.

## Hypotheses To Check

1. `delivery_eta_unknown` together with `queue_eta_*` and zero requested flows:
   uncertain forecasts might be blocking supply, rather than merely ranking it.
   `queue_eta_forecast_budget_exhausted` identifies exhausted candidate-search
   budget; `queue_eta_inputs_unknown` identifies missing forecast inputs.
2. `current_resources_exhausted_or_unknown`: inspect source quantities and
   `automatic_release_fleet_bound` messages; separate truly empty stocks from
   conservative fleet reservations or hidden stock.
3. `no_permitted_delivery_slot` or `unconfirmed_permission`: check the requested
   destination/commodity and network permission evidence.
4. Planner `stock:*:unknown_coverage` or production issues: some requirements
   may be suppressed before the allocator sees them. Inspect the weekly needs.
5. Positive dispatches but high later backlog: investigate delivery timing,
   production-input coverage and duplicated replenishment across weeks.

Reason counts are symptoms, not proof of causation. Select an early failing
week, connect its unmet need to the source stock and route rejection, then add
a focused regression test before changing policy. Do not manufacture observed
stock or guaranteed ETA to obtain a better score.

No policy changes, upload, promotion or replay execution are performed by adding
this harness. Tests and replay are run by Nikita in WSL.
