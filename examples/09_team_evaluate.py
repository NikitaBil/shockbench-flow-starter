"""Record a paired comparison with settings and exact submission fingerprints.

uv run python examples/09_team_evaluate.py --task=small --quick --episodes=4
uv run python examples/09_team_evaluate.py --task=small --entropy=67890 --episodes=16 --cpu_budget=True
"""

import hashlib
import json
import platform
import tempfile
import time
from datetime import datetime
from pathlib import Path

import fire
from shockbench_flow_agent.submission import build_submission

from sbf_starter import check_task, scoring
from sbf_starter.agents import load, resolve


def fingerprint(folder: Path) -> str:
    with tempfile.TemporaryDirectory(prefix="sbf-team-fingerprint-") as tmp:
        archive = Path(build_submission(folder, Path(tmp) / "agent.zip"))
        return hashlib.sha256(archive.read_bytes()).hexdigest()


def replay_parity(candidate: Path, baseline: Path, task: str, episodes: int | list[int], entropy: int) -> dict:
    """Compare actions on the same observed trajectory, without computing RSS."""
    import gymnasium as gym
    import numpy as np
    from shockbench_flow_gym import agent_config_from_reset

    from sbf_starter import env_id

    if isinstance(episodes, bool) or not isinstance(episodes, (int, list)):
        raise ValueError("parity mode needs a positive episode count or a list of episode indices")
    indices = list(range(episodes)) if isinstance(episodes, int) else episodes
    if not indices or any(isinstance(n, bool) or not isinstance(n, int) or n < 0 for n in indices):
        raise ValueError("parity mode needs at least one nonnegative episode index")
    env = gym.make(env_id(task), entropy=entropy)
    rows = []
    try:
        for episode in indices:
            observation, info = env.reset(seed=0, options={"episode": episode})
            config = agent_config_from_reset(env, observation, info)
            reference, current = load(baseline)(config), load(candidate)(config)
            weeks, matching, reward_sum, max_delta, done = 0, 0, 0.0, 0.0, False
            while not done:
                action, expected = current.act(observation), reference.act(observation)
                same = action.keys() == expected.keys() and all(
                    np.array_equal(action[key], expected[key]) for key in action
                )
                matching += int(same)
                max_delta = max(max_delta, float(np.max(np.abs(action["flows"] - expected["flows"]))))
                observation, reward, terminated, truncated, _ = env.step(action)
                reward_sum += reward
                weeks += 1
                done = terminated or truncated
            rows.append(
                {
                    "episode": episode,
                    "weeks": weeks,
                    "matching_action_weeks": matching,
                    "max_flow_delta": max_delta,
                    "candidate_trace_cost_usd": -reward_sum,
                }
            )
            print(f"episode {episode}: {matching}/{weeks} actions match; max flow delta {max_delta:g}", flush=True)
    finally:
        env.close()
    return {"identical_actions": all(row["weeks"] == row["matching_action_weeks"] for row in rows), "episodes": rows}


def main(
    task: str = "small",
    agent: str = "team_agent",
    against: str = "baseline",
    episodes: str | int | list[int] = 16,
    entropy: int = 67890,
    quick: bool = False,
    cpu_budget: bool = False,
    n_jobs: int = 1,
    mode: str = "score",
    out: str | None = None,
) -> None:
    """Compare folders on identical episodes; save result.json and report.txt.

    The default root is a proposed validation set, not the public dev split.
    Use a separate root when tuning. Quick results are explicitly labelled.
    Score mode requires Linux in this kit version, even without CPU metering.
    Parity mode replays equal observations without RSS or isolated timing.
    """
    check_task(task)
    if mode not in ("score", "parity"):
        raise ValueError("mode must be score or parity")
    candidate, baseline = resolve(agent).resolve(), resolve(against).resolve()
    if not candidate.is_dir() or not baseline.is_dir():
        raise ValueError("use agent names or submission folders, not individual files or zips")
    if candidate == baseline:
        raise ValueError("candidate and baseline must be different submission folders")
    before = {"candidate": fingerprint(candidate), "baseline": fingerprint(baseline)}
    folder = Path(out or f"outputs/09_team_evaluate/{datetime.now():%Y-%m-%d_%H-%M-%S_%f}_{task}")
    if (folder / "result.json").exists():
        raise FileExistsError("this run folder already contains result.json; choose a new output folder")
    folder.mkdir(parents=True, exist_ok=True)
    summary = {
        "status": "running",
        "settings": {
            "task": task,
            "agent": str(candidate),
            "against": str(baseline),
            "episodes": episodes,
            "entropy": entropy,
            "quick": quick,
            "cpu_budget": cpu_budget,
            "n_jobs": n_jobs,
            "mode": mode,
        },
        "submission_sha256": before,
        "scoring_mode": ("quick_smoke" if quick else "full") if mode == "score" else "action_parity",
        "comparison": None,
        "parity": None,
        "conclusion": None,
    }

    def save() -> None:
        (folder / "result.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")

    save()
    if mode == "score":
        print(f"Preparing {task} comparison; first reference computations may take several minutes.", flush=True)
    else:
        print(f"Replaying {task} for action parity; no RSS or server CPU measurement.", flush=True)
    print(f"Run folder: {folder.resolve()}", flush=True)
    started = time.perf_counter()
    try:
        if mode == "parity":
            if quick or cpu_budget:
                raise ValueError("quick and cpu_budget apply to score mode, not action parity")
            parity = replay_parity(candidate, baseline, task, episodes, entropy)
            summary["parity"] = parity
            conclusion = (
                "identical_actions_on_replayed_observations" if parity["identical_actions"] else "different_actions"
            )
            report = json.dumps(parity, indent=2)
        else:
            if platform.system() == "Windows":
                raise RuntimeError(
                    "score mode imports the Unix runner; use Ubuntu/WSL, or --mode=parity for action checks"
                )
            comparison = scoring.compare(
                str(candidate),
                str(baseline),
                task,
                episodes,
                entropy=entropy,
                quick=quick,
                cpu_budget=cpu_budget,
                n_jobs=n_jobs,
            )
            summary["comparison"] = scoring.as_dict(comparison)
            lo, hi = comparison.interval if comparison.interval is not None else (None, None)
            if lo is None or hi is None:
                conclusion = "insufficient_evidence"
            elif lo > 0:
                conclusion = "better"
            elif hi < 0:
                conclusion = "worse"
            elif comparison.diff == 0 and lo == 0 and hi == 0:
                conclusion = "equal_on_evaluated_episodes"
            else:
                conclusion = "insufficient_evidence"
            report = str(comparison)
        after = {"candidate": fingerprint(candidate), "baseline": fingerprint(baseline)}
        if after != before:
            summary.update(comparison=None, parity=None)
            raise RuntimeError("submission files changed during evaluation; freeze them and repeat this comparison")
        summary.update(status="completed", conclusion=conclusion)
        (folder / "report.txt").write_text(report + "\n", encoding="utf-8")
        print(report)
        print(f"Conclusion: {conclusion}")
        if quick:
            print("QUICK smoke test: these are not leaderboard numbers or confirmation of policy improvement.")
    except Exception as exc:
        summary.update(status="failed", error={"type": type(exc).__name__, "message": str(exc)})
        raise
    finally:
        summary["elapsed_seconds"] = round(time.perf_counter() - started, 3)
        save()
    print(f"Saved: {(folder / 'result.json').resolve()}")


if __name__ == "__main__":
    fire.Fire(main)
