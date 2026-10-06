#!/usr/bin/env bash
# Run in Ubuntu/WSL; snapshot both submissions before the expensive comparison.
set -euo pipefail

if [[ "$(uname -s)" != "Linux" ]]; then
    printf 'Use Linux/Ubuntu WSL: the official runner requires Unix APIs.\n' >&2
    exit 1
fi
cd "$(dirname "${BASH_SOURCE[0]}")/.."
out="${1:-outputs/team_integration/$(date +%Y-%m-%d_%H-%M-%S_%N)}"
agent="${AGENT:-team_agent}"
against="${AGAINST:-baseline}"
entropy="${ENTROPY:-67890}"
episodes="${EPISODES:-16}"
n_jobs="${N_JOBS:-1}"
if [[ -e "$out" ]]; then
    printf 'Output already exists; choose a new run directory: %s\n' "$out" >&2
    exit 1
fi
mkdir -p "$out"
out="$(realpath "$out")"
printf 'Run folder: %s\nComparing %s against %s; entropy=%s, episodes=%s\n' "$out" "$agent" "$against" "$entropy" "$episodes"
printf 'Full reference computation can take a long time. No upload is performed.\n'
printf 'agent=%s\nagainst=%s\nentropy=%s\nepisodes=%s\nn_jobs=%s\ncpu_budget=True\n' "$agent" "$against" "$entropy" "$episodes" "$n_jobs" > "$out/settings.txt"
uv run pytest tests/test_network.py tests/test_team_agent.py tests/test_team_contracts.py \
    tests/test_team_check_script.py tests/test_team_evaluate.py -q \
    -o "cache_dir=$out/pytest-cache" | tee "$out/tests.txt"
uv run sbf pack "$agent" --out="$out/candidate.zip"
uv run sbf pack "$against" --out="$out/baseline.zip"
sha256sum "$out/candidate.zip" "$out/baseline.zip" > "$out/sha256.txt"
for task in small full; do
    uv run sbf check "$out/candidate.zip" --task="$task" | tee "$out/check_$task.txt"
    uv run sbf compare "$out/candidate.zip" "$out/baseline.zip" --task="$task" \
        --entropy="$entropy" --episodes="$episodes" --cpu_budget=True \
        --n_jobs="$n_jobs" --out="$out/compare_$task.json" | tee "$out/compare_$task.txt"
done
printf 'Completed. Tests, CPU checks, paired scores and exact ZIP snapshots: %s\n' "$out"
