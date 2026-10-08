# Nikita: candidate integration and evaluation

## Scope

This is Nikita's integration/evaluation task, not a rewrite of the teammates'
planning or allocation algorithms. The local `integration` checkout received
the relevant team modules from `network-delivery` at `5ac577a`, which includes
Markiyan's analytics at `1bfb7ff`. Git metadata writes were denied in this
Codex session: this was a file import, not a Git merge or remote push.

`agents/baseline/agent.py` is unchanged. The historical Small dev score reported
by Nikita was 0.4004 (90% interval 0.2937 to 0.5018, no naive substitutions).
That observation is not a new measurement of this candidate. The production
`team_agent` still uses its prior heuristic unless explicitly enabled.

## Implemented

- `Agent` passes validated `planner_options` to the pipeline factory.
- `build_pipeline` creates `StateBuilder`, `NeedPlanner` and Vitya's `Allocator`
  once per episode. `DecisionPipeline` retains the existing order and strict
  action/diagnostic validation; exceptions do not silently select a heuristic.
- `queue_eta_enabled` selects Vitya's bounded candidate-route ETA estimates.
  Independently, `queue_forecast_enabled` adds conditional arrival estimates
  for existing cargo to the snapshot. Neither means guaranteed future delivery.
- `examples/11_team_candidate.py` packages and freezes two standalone submission
  folders. Only the candidate receives opt-in parameters. Baseline bytes/hash
  must remain identical; existing output directories are never overwritten.
- `manifest.json` is outside both submissions. It records params, source and
  frozen archive hashes, Git commit/branch/status, and cached upstream refs.
  Dirty Git status is preserved, not labelled as a clean commit. It does not
  contain a fabricated score or CPU measurement.
- `tests/test_team_candidate.py` checks preparation, actual module wiring,
  baseline preservation, disabled default behavior and invalid settings.
  Existing contract tests cover order, malformed actions and module failures.

## Decisions Still Requiring Evidence

The planner presets are experimental. In the installed simulator, `grid.voll`
is USD/GWh, not USD/MWh. Markiyan's current indirect cost proxy multiplies it
by 1000 and a fuel share. Do not call this a calibrated shortage cost; the
candidate builder permits `--shortage_cost_model=False` to isolate allocation.

Likewise, `fab.w_scr` is a disruption scrap-window length, not a BOM loss
fraction. The current planner uses `1 + w_scr/tau` as an input buffer. That
teammate formula is preserved here and needs a separate analytics correction
or explicitly justified experiment before promotion. Passing tests alone
does not prove these planning assumptions or score improvements.

## WSL Verification Owned by Nikita

Run from the main working repository, not an earlier detached review:

```bash
cd /mnt/c/Users/nikit/projects/shockbench-flow-starter
export UV_PROJECT_ENVIRONMENT="$HOME/.venvs/shockbench-flow-starter"
uv sync --locked
uv run pytest tests/test_team_candidate.py tests/test_team_contracts.py tests/test_team_agent.py tests/test_team_analytics.py tests/test_allocation.py tests/test_delivery.py tests/test_network.py tests/test_current_network.py -n 3 -q

RUN="outputs/nikita-candidate-$(date +%Y%m%d-%H%M%S)"
uv run python examples/11_team_candidate.py --preset=forecast_bom --shortage_cost_model=False --out="$RUN"
uv run sbf check "$RUN/candidate" --task=small
uv run sbf check "$RUN/candidate" --task=full

# Smoke only; no leaderboard/skill-improvement claim.
uv run python examples/09_team_evaluate.py --agent="$RUN/candidate" --against="$RUN/baseline" --task=small --quick --episodes=2 --cpu_budget=True --out="$RUN/small-smoke"

# Paired evaluation on the proposed validation root; do not tune on it.
uv run python examples/09_team_evaluate.py --agent="$RUN/candidate" --against="$RUN/baseline" --task=small --entropy=67890 --episodes=16 --cpu_budget=True --out="$RUN/small-compare"
uv run python examples/09_team_evaluate.py --agent="$RUN/candidate" --against="$RUN/baseline" --task=full --entropy=67890 --episodes=16 --cpu_budget=True --out="$RUN/full-compare"
```

The checks target the frozen, enabled candidate, not the disabled default.
Use a new run folder for another preset. Keep both frozen folders unchanged
during comparisons. The recorder saves settings, hashes, comparison data,
interval, conclusion and elapsed time; an interval crossing zero is
insufficient evidence, not improvement. CPU must fit 2s/week Small and
4s/week Full, including first-week construction.

No pytest, benchmark evaluation, upload or policy promotion was executed for
these new integration edits by Codex; Nikita runs the commands above. Static
Ruff checks on Nikita's changed Python files pass.
