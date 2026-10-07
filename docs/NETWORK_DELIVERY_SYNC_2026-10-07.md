# Network/delivery synchronization after main PR #2

Code merge: `fc8435ea8ba56b9914d0b4b0c1701862f96a68e2` on `network-delivery`.
Author: Victor Danylenko. Imported `origin/main=50f3f46`, including Markiyan's
`a94297b`. The earlier `03c266e` was already integrated.

The merge was conflict-free. It changed only `agents/team_agent/needs.py`,
`state.py`, and added `tests/test_production_routes.py`. Our allocation,
delivery ETA budget/filter/order changes and diagnostic hook were preserved.
Contracts, deployment settings and the default heuristic were not changed.

## What the imported fix does

- Shared dated Fab output inventory is consumed once across receiving OSATs.
- Output stock only covers consumers reachable through configured routes.
- Remaining production can spill into alternative reachable Fabs with spare
  capacity, rather than relying solely on the initially selected Fab.
- Each Fab uses its own route transit and production lead time.
- Masked pipeline arrival time remains unknown; unknown routes do not acquire
  a fabricated zero remaining transit.

This is a planner/state correctness change, not proof of a higher RSS.

## Exact teammate run metadata

The user supplied the original frozen run's root, parameters and submission
hashes. They are preserved in
[TEAM_FROZEN_RUN_2026-10-06.json](TEAM_FROZEN_RUN_2026-10-06.json).

Important distinctions:

- Frozen directory: `outputs/11_team_candidate/2026-10-06_14-49-25_762846/`.
  It is **not present in this local checkout** and `outputs/` is gitignored.
- Its Git HEAD was `48acf87` (`integration`) with uncommitted changes.
  Git checkout alone cannot reproduce those submissions.
- Frozen candidate `params.json` includes `planner_options`,
  `fuel_replenishment_enabled=true`, and `queue_forecast_enabled=false`.
  The current merged Agent does not read/wire these options; current
  NeedPlanner has no `fuel_replenishment_enabled` argument. Therefore copying
  that JSON onto the current agent would not reproduce its semantics.
- Baseline params were absent; reported built-in defaults are `fraction=1.0`
  and `closure_power=1.0`. Matching defaults do not prove matching source.
- Root/entropy `67890`, replay seed `0`; original diagnostics Full 0–1,
  Small v2 0–3; RSS separately evaluated on 16 episodes, no quick mode,
  with CPU budget.
- Reported SHA-256 values identify packed submissions. They are distinct from
  example 11's folder fingerprints. Until the original folders/archives are
  transferred, neither expected submission hash is locally verified.

No frozen folders were modified or synthesized as exact substitutes. No new
fuel policy or parameter wiring was added during synchronization. Those
features need their actual source or a separately agreed implementation.

## Post-merge verification

```text
uv run pytest tests/test_allocation.py tests/test_allocator_diagnostics.py tests/test_production_routes.py tests/test_team_analytics.py tests/test_nominal_bom.py tests/test_team_agent.py tests/test_team_contracts.py tests/test_current_network.py tests/test_delivery.py tests/test_network.py -n 3 -q
```

Result: **198 passed**. Includes imported production-route/unknown-ETA tests,
allocator regressions, and real nominal Tiny/Small/Full/packed-agent checks.

Native smoke runs used the current copied `team_agent` with
`allocation_enabled=true`, `queue_eta_enabled=true`, the default NeedPlanner,
and the shipped heuristic as a separate baseline. Root `67890`, seed `0`,
episode `0`, **seven weeks only**:

| Task | Candidate partial cost USD | Heuristic partial cost USD | Candidate maximum CPU s |
| --- | ---: | ---: | ---: |
| Small | 215,999,122,743.13 | 121,801,980,989.88 | 0.109375 |
| Full | 353,444,438,668.85 | 208,776,970,182.12 | 0.359375 |

CPU includes initialization and diagnostic collection. These are partial
native costs, not RSS, original frozen reproduction, or server CPU certification.
They also do not isolate the effect of `a94297b`; a paired pre/post test is
needed for that claim. Candidate still costs more in these smoke runs.

Current Full wafer requests are zero in weeks 1–5, about 142,087.23 units in
week 6, then zero in week 7. Thus the previous root-12345 observation of zero
wafer across seven weeks must not be generalized to this root/version.
Week 3 still has no raw wafer requirements at the listed Fabs. Current nucfuel
requests are zero in weeks 1–5, 25,900.25 GWh in week 6, 36,726 GWh in week 7.

Reports and frozen smoke copies remain local under:

- `outputs/11_allocator_diagnostics/2026-10-07_main_sync_small_67890_e0_7/`
- `outputs/11_allocator_diagnostics/2026-10-07_main_sync_full_67890_e0_7/`

The official isolated runner/`sbf compare` retains the known Windows `fcntl`
limitation. `sbf check` on the opt-in Small smoke copy passed archive/import
validation (15 files); its isolated timing phase failed at `import fcntl`.
The teammate performs official paired evaluation locally.

## Next handoff

Transfer the original frozen `candidate` and `baseline` folders (or their
exact submission archives). Verify the two supplied submission hashes before
any replay. Keep those inputs unchanged. Evaluate the synchronized candidate
separately; investigate terminal replenishment, wafer requirements and
consumption forecasts without silently treating ignored params as active.
