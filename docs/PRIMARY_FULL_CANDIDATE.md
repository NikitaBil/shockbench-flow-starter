# Primary Full Policy

The participant selected the actor with native Full RSS 0.781480 on the untouched
root 20261013 as the preferred candidate for local verification.

Status: promoted to `agents/team_agent` at the participant's explicit request.
The primary agent now loads the verified policy parameters by default.
The immutable `team_agent_0781480` copy and previous frozen checkpoints remain
unchanged. This promotion does not upload a submission or publish a Git branch.

- Primary name: `team_agent`, parameters: `agents/team_agent/params.json`.
- Immutable named copy: `agents/team_agent_0781480`.
- ZIP: `outputs/team_agent_0781480.zip`.
- Source: `outputs/best-full-20261008/candidate`.
- Submission SHA-256:
  `cc7c0cf6712cd16f7b0c538b3930fa6b96f8055d9aebb287dd2105b3576e1d51`.
- Parameters: `agents/team_agent_0781480/params.json`.
- Original evidence: `outputs/best-full-20261008/result.json`.

The immutable copy has the same checked ZIP SHA as the original winner,
including all helper modules and parameters: 24 files, 288626 zipped bytes.
The primary directory retains disabled development options (closed-route staging
and WIP forecasting), so its archive is not byte-identical to the frozen copy.
Their parameters match; neither of these experimental options is enabled.
Primary archive: 25 files, SHA-256
`efa7c00d1d7ac112f269950d020860ed839ed141bd2d60dcc2d7f24361016dbe`.
`.gitattributes` preserves submission bytes across Windows/Linux checkouts.

The 0.781480 result belongs to 32 specific Full episodes, with eight per harm
level, standard information and non-quick references. It is not the first 32
episodes of the root and is not a guarantee for other roots.
Native 90% interval: [0.752458, 0.808544].
The participant reproduced Full RSS 0.7815 in WSL on this root and episode set,
with 0 fallback weeks and 0 CPU-overrun weeks. That run reported 2674 ignored
action entries; their cause has not yet been established.
The immutable copy passed participant-run isolated checks on Small and Full:
maximum CPU 0.524 s / 0.375 s against budgets 2 s / 4 s, respectively.
The promoted primary archive has a different hash and needs its own Linux check.

## Participant Verification In WSL

```bash
cd /mnt/c/Users/nikit/Documents/Codex/2026-10-03/g/work/reviews/integration-ready-20261007
export UV_PROJECT_ENVIRONMENT="$HOME/.venvs/shockbench-copy-0781480"
uv sync --locked

uv run --locked sbf check team_agent --task=small
uv run --locked sbf check team_agent --task=full

# Standard repo evaluation: 20 dev episodes, 5 per harm level; no manual list.
uv run --locked sbf evaluate team_agent --task=full --cpu_budget=True --n_jobs=1 --out="outputs/primary_full_dev_$(date +%Y%m%d-%H%M%S).json"

# Optional reproduction of the original 0.781480 result, not a new holdout.
EPISODES="[0,1,2,3,4,5,6,7,8,9,10,11,13,15,16,19,23,25,26,30,39,40,44,45,52,56,59,64,67,72,127,131]"
RESULT="outputs/team_agent_0781480_verify_$(date +%Y%m%d-%H%M%S).json"
uv run --locked sbf evaluate team_agent_0781480 --task=full --entropy=20261013 --episodes="$EPISODES" --quick=False --cpu_budget=True --n_jobs=1 --out="$RESULT"
```

The first non-quick reference computation can take a long time.
Check the appropriate primary/frozen SHA above. Record the actual Linux score,
interval, CPU and fallback counts; results on another root can differ.
Do not interpret the CLI's suggested upload command as authorization to submit.

## Complete Project Verification

The full unfiltered Windows pytest run collected 827 cases:
785 passed, 34 failed, 8 skipped. All seven checks of this exact frozen copy
passed. The failed cases are 29 Unix-extractor failures (`os.fchmod`),
four Unix-runner failures (`fcntl`) and one POSIX-permission assertion.
The project is not fully green on Windows; a full Linux run remains necessary.

The copied candidate was freshly replayed on all original 32 Full episodes and
reproduced RSS 0.7814801685292412 with exact integer-cent episode costs.
Its SHA is unchanged, native invalid/fallback/CPU-overrun counts are zero,
and maximum measured local CPU was 0.171875 s. This predates the promotion.
Whole-project Ruff separately reported 21 historical/duplicated lint findings.

Complete evidence:
`outputs/project-full-0781480-20261008-31495812/REPORT.md`,
`outputs/project-full-0781480-20261008-31495812/pytest.xml`,
`outputs/project-full-0781480-20261008-31495812/report.json`.

## Promotion Verification

`tests/test_primary_full_candidate.py` checks the immutable hash, matching
parameters, and exact weekly action parity on Tiny/Small/Full at roots 0 and
20261013. Neutral heuristic tests now explicitly use neutral parameters rather
than assuming the production agent has no `params.json`.

The previous working primary directory was preserved at
`outputs/promotion-0781480-20261008-123106/previous-team-agent`.
Promotion verification completed:

- Focused integration/module tests: 450 passed, 1 existing Linux-only skip.
- Clean staged Git snapshot: 14 passed, 1 existing Linux-only skip.
- Both submission hashes reproduced exactly from that staged Git snapshot.
- Fresh primary Full replay: RSS 0.7814801685292412 on the original 32 episodes;
  every episode cost matches the immutable winner exactly in integer cents.
- Maximum native CPU: 0.296875 s; native rejected-positive-action, fallback and
  locally over-budget week counters are zero. This is not isolated certification.
- Scoped Ruff and `git diff --cached --check` passed.

Evidence is local under `outputs/promotion-0781480-20261008-123106/`:
`pytest.xml`, `staged-pytest.xml` and `full-replay/result.json`.
The previous working-directory backup is local and not committed.
