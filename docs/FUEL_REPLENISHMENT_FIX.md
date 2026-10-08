# Fuel replenishment regression fix

## Evidence And Scope

Frozen candidate `nikita-candidate-20261006-140843` generated no terminal fuel
requests and no marine LNG/crude dispatch requests in either diagnostic episode.
Producer stocks accumulated while terminals depleted. This fix closes that
missing planning stage; it does not claim the entire score regression is solved.

`NeedPlanner` now propagates net grid fuel needs to feeder terminals using only
existing action slots. It creates `fuel_replenishment` needs for Vitya's unchanged
allocator; it does not invent a combined source-to-grid slot or dispatch through
multiple ordinary edges in the same week.

## Accounting

- Grid stock is netted by the existing planner before upstream projection.
- Terminal stock/import inventory is shared once across consumer requests.
  Alternative feeders divide coverage rather than each ordering the full need.
- Observed live pipeline/queue quantities reserve import inventory. Their
  forecast calendar pieces are not counted again. Calendar-only imports are
  supported; WIP and future external supply are not terminal import inventory.
- Unknown ETA offsets the volume already ordered, not deadline coverage. The
  original grid needs remain unmet until actual coverage becomes available.
  Pending imports are not dispatchable stock or guaranteed on-time arrivals.
- Import due dates subtract nominal downstream transit and one week for the
  pre-dispatch-stock rule, clamped to the current week. Original overdue
  consumer deadlines are unchanged; current-week imports can still be late.
- Unknown feeder stock stays unknown. No observed zero or guaranteed ETA is
  fabricated to increase dispatch.

`fuel_replenishment_enabled` is validated as a boolean. It defaults to true in
the planner and forecast presets; `baseline_conservative` disables it. The
production Agent still keeps allocation disabled by default. Candidate builder
accepts `--fuel_replenishment=True/False` for controlled comparisons.

## WSL Verification

Codex performed static Ruff checks, not pytest or scoring. Nikita runs these
blocks in the VS Code WSL terminal; stop at the first failure.

```bash
cd /mnt/c/Users/nikit/projects/shockbench-flow-starter
export UV_PROJECT_ENVIRONMENT="$HOME/.venvs/shockbench-flow-starter"
uv sync --locked
uv run pytest tests/test_team_analytics.py tests/test_team_candidate.py tests/test_team_diagnose.py tests/test_team_agent.py tests/test_team_contracts.py tests/test_allocation.py tests/test_delivery.py tests/test_network.py tests/test_current_network.py -n 3 -q

FIX="outputs/fuel-fix-$(date +%Y%m%d-%H%M%S)"
uv run python examples/11_team_candidate.py --preset=forecast_bom --shortage_cost_model=False --fuel_replenishment=True --out="$FIX"
uv run sbf check "$FIX/candidate" --task=small
uv run sbf check "$FIX/candidate" --task=full

uv run python examples/12_team_diagnose.py --agent="$FIX/candidate" --against="$FIX/baseline" --task=small --entropy=67890 --episodes=2 --out="$FIX/diagnostics"
cat "$FIX/diagnostics/report.txt"

uv run python examples/09_team_evaluate.py --agent="$FIX/candidate" --against="$FIX/baseline" --task=small --entropy=67890 --episodes=16 --cpu_budget=True --out="$FIX/small-compare"
```

Inspect traces for terminal `fuel_replenishment` needs, nonzero marine requests,
and preserved observed/estimated/unknown provenance. Compare costs with the old
frozen candidate and baseline; do not infer success from tests alone.

If Small warrants a broader run:

```bash
uv run python examples/09_team_evaluate.py --agent="$FIX/candidate" --against="$FIX/baseline" --task=full --entropy=67890 --episodes=16 --cpu_budget=True --out="$FIX/full-compare"
```

Entropy 67890 is now a debugging/tuning root, not independent evidence of
generalization. Final promotion needs an untouched validation root.

## Remaining Limitations

This is fuel-chain replenishment, not a generic multi-stage production planner.
Deterministic feeder choice and the existing forecast horizon are not optimized
for long voyages or closure recovery. Unknown-ETA inbound reservations can
postpone replacement orders even for delayed cargo; expediting needs a separate
explicit model, not pretending that uncertain cargo meets a deadline.

The existing BOM `w_scr/tau` and shortage-cost unit assumptions are unchanged.
Keep shortage_cost_model disabled in this comparison to isolate the fix. No
baseline files, old frozen candidates, Git history or remote branches are changed
by this patch. The needs change belongs to analytics when the team publishes it.
