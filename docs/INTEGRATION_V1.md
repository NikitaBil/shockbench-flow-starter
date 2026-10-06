# Integration V1: Nikita's handoff

Follow-up: `docs/CONTRACTS_V1.md` describes the implemented handoff interfaces,
optional decision pipeline and final action gate. The original measurements
below refer to V1 before those additional files; repeat Linux checks for the
new submission hash. Real state/needs/allocation modules are not wired yet.

State/needs and an optional conditional V3 queue forecast are now implemented
in the shared submission folder: see `docs/ANALYTICS_V1.md`. They are tested
through an injected pipeline but are not enabled by `build_pipeline()` until
the real allocator is ready. The default policy remains unchanged.

This step connects Vitya's static network to the existing heuristic. It does
not add inventory planning, dynamic routing, or risk responses. Those policy
changes must be evaluated separately.

## Sources and ownership

- Vitya's four files are imported unchanged from local `origin/network-delivery`
  at commit `1f683f10256a5ef69cef6e8cea0dadcac9f25622`: `network.py`, its tests,
  the route example, and `NETWORK_V1.md`.
- The local Git merge could not write `.git/ORIG_HEAD` in the Codex sandbox,
  even after requesting access. File integration is present; the branch
  history has not been merged and no new commit or push was made.
- Nikita owns `agent.py`, the frozen baseline, integration regressions, and
  `examples/09_team_evaluate.py`.
- The baseline is a snapshot of Nikita's team_agent before this integration.
  Do not tune it in place. Make a separate candidate for experiments.

## Current Agent interface

The scorer loads `agent.py` from the submission directory, where the sibling
`network.py` is available. The import is `from network import StaticNetwork`,
not a repository-only import such as `from agents.team_agent.network ...`.

`Agent(config)` creates `self.network` once. Its nominal entry-edge capacities
and ordered chokepoint positions replace the heuristic's duplicated topology
lookups. `act(observation)` keeps the same action mask and closure multipliers.

- `agent.network.routes[s]`: static route for action slot `s`.
- `agent.network.slots_from[(node, commodity)]`: dispatch options.
- `agent.network.slots_to[(node, commodity)]`: options ending at that node.
- `agent.network.transit_progress(edge, lane)`: where a visible shipment
  arrives and which route segments remain.
- `agent.cap`, `agent.power`, `agent.through`: current baseline policy inputs.

The network object is not reconstructed each week. No inventory forecast is
claimed yet, and action parity is expected on identical observations.

## Next module contracts to agree with Markiyan and Vitya

The table below records the original proposed ownership. State/planning now
have V1-compatible implementations; allocation still needs its implementation
and activation. Confirm these interfaces with the team before final wiring:

| Output | Required information | Owner |
| --- | --- | --- |
| StateSnapshot | week, stock, backlog, visible pipeline/queues/WIP, forecast, observed/unknown flags | Markiyan |
| DeliveryNeed | node, commodity, quantity, due week, priority, reason | Markiyan |
| CurrentNetwork | observed constraints/costs/transit, separate from nominal and unknown values | Vitya |
| AllocationResult | flows in action-slot order, unmet requests, resource usage, reasons | Vitya |

Stock and pipeline rows use `config.layout`; action positions use `action_slots`.
An unknown lane must not be inferred to be an off-lane shipment. Pipeline
arrival on the current edge is not necessarily arrival at the final destination.
Backlog is an obligation, not stock. Final integration must pass projections
to the need planner rather than calculate and discard them.

## Reproduce in Ubuntu/WSL

Run from Nikita's working repository, not the detached review worktree:

```bash
cd /mnt/c/Users/nikit/projects/shockbench-flow-starter
export UV_PROJECT_ENVIRONMENT="$HOME/.venvs/shockbench-flow-starter"
uv sync --locked

# Focused static-network, integration, and evidence-recorder checks.
uv run pytest tests/test_network.py tests/test_team_agent.py tests/test_team_evaluate.py -q -o cache_dir=/tmp/shockbench-integration-pytest

# Server-style isolated execution: Linux is required by this runner version.
uv run sbf check team_agent --task=small
uv run sbf check team_agent --task=full

# Fast recorder smoke test, not a leaderboard result.
uv run python examples/09_team_evaluate.py --task=small --quick --episodes=4

# Action-only integration check; also works on native Windows.
uv run python examples/09_team_evaluate.py --task=small --mode=parity --episodes=1

# Full paired comparison on the proposed validation root.
uv run python examples/09_team_evaluate.py --task=small --entropy=67890 --episodes=16 --cpu_budget=True
```

The full run may be slow while references are first computed. The recorder
prints its run folder before computation and saves `result.json` with status
`running`, `completed`, or `failed`. Exact submission hashes, settings, costs,
fallbacks, interval, elapsed time and the comparison are retained, alongside
`report.txt` on success. It refuses an existing result file and detects source
changes during a run. Do not edit the candidate or baseline during evaluation.

A confidence interval crossing zero yields `insufficient_evidence`, not a
claim of improvement. Exact zero difference and zero interval are labelled
`equal_on_evaluated_episodes`; this is scoped to the evaluated scenarios.
Quick score mode is always labelled as a smoke test. Score mode requires Linux
because this kit imports the Unix runner even without CPU metering. Use
`--cpu_budget=True` in Linux for metered comparisons. On Windows, parity mode
checks actions on identical observations and records raw replay costs, not RSS
or a measured policy improvement. The same policy seed is used for that check;
it is not a substitute for the scorer's salted seeds or CPU/wall-clock rules.

The proposed validation root is 67890; use a separate training root such as
12345 for tuning and reserve root 0/dev for occasional confirmation. Initial
Small dev measurements reported by Nikita before integration: RSS 0.4004,
90% interval 0.2937-0.5018, 0 fallbacks over 1040 weeks. These are historical
baseline observations, not a fresh measurement of this integration.

## Local validation evidence (2026-10-05)

- Focused tests: 23 passed, 1 skipped. The skipped server extractor requires
  `os.fchmod`, unavailable in the Windows Python 3.12 validation runtime.
- Ruff checks and formatting checks passed.
- Complete action-parity regressions passed for Tiny, Small and Full.
- Saved parity replays on root 67890: Small 52/52 matching weekly actions,
  Full 104/104, maximum flow difference 0 in both runs.
- `sbf pack` completed and the ZIP passed static validation. Portable
  extraction and official agent loading passed in the regression tests.
- Validation used a separate Python 3.12 environment with locked project
  dependencies; Nikita's Windows and WSL environments were not replaced.
- No new RSS result or isolated CPU-budget success is claimed. Codex could
  not launch WSL (`Wsl/Service/E_ACCESSDENIED`); run the two Linux checks above.

## Acceptance

- All static-network regressions pass, including real Tiny/Small/Full tables.
- Every action matches the baseline over a complete episode of each task.
- Scalar and per-slot fractions, blocked slots and hidden closure values keep
  the same baseline behavior.
- The packed submission includes and loads its sibling network module.
- A real recorder smoke run completes and saves evidence.
- Linux isolated checks pass on Small and Full before claiming server-style
  readiness. Local Windows checks do not replace these.

The representation-only step is not expected to improve RSS. Future policy
changes must be compared against the frozen baseline or a frozen prior winner.
