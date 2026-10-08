"""Matched native costs and CPU evidence. This is deliberately NOT an RSS scorer.

Independent trajectories on equal generated episodes; no private state reaches
either actor. Official CPU fallback, policy-seed salting and references require
the standard Linux scorer. Default runs one Tiny pair, never a tuning search.
"""

import hashlib
import json
import re
import tempfile
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

import fire
import gymnasium as gym
import numpy as np
from scipy import stats
from shockbench_flow_agent.submission import build_submission
from shockbench_flow_gym import agent_config_from_reset

from sbf_starter import check_task, env_id
from sbf_starter.agents import load, resolve


def fingerprint(folder):
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(build_submission(folder, Path(tmp) / "policy.zip"))
        return hashlib.sha256(path.read_bytes()).hexdigest()


def replay(folder, task, entropy, episode):
    env = gym.make(env_id(task), entropy=entropy)
    timings, costs, invalid, recovery = [], Counter(), Counter(), Counter()
    positive_invalid, ignored_zero, whole_week_failures = 0, 0, 0
    total_cents, weeks = 0, 0
    try:
        obs, info = env.reset(seed=0, options={"episode": episode})
        config = agent_config_from_reset(env, obs, info)
        # Loading/imports occur before the measured constructor, as in the kit.
        cls = load(folder)
        started = time.process_time()
        actor = cls(config)
        constructor = time.process_time() - started
        done = False
        while not done:
            started = time.process_time()
            action = actor.act(obs)
            timings.append(time.process_time() - started + (constructor if weeks == 0 else 0))
            for key in action:
                if not env.action_space[key].contains(action[key]):
                    raise ValueError(f"malformed {key} at week {weeks + 1}")
            helper = getattr(actor, "production_residual", None)
            if helper is not None:
                recovery[helper.last["status"]] += 1
            obs, _, terminated, truncated, step_info = env.step(action)
            total_cents -= int(step_info["reward_cents"])
            rec = env.unwrapped.core.trajectory.records[-1]  # offline outcome evidence only
            costs.update(rec.costs.as_dict())
            invalid.update(rec.invalid)
            whole_week_failures += int("fallback" in step_info)
            for reason in rec.invalid:
                masked_slot = re.search(r"slot (\d+) masked this week", reason)
                if masked_slot and reason.startswith("overrides["):
                    quantity = action.get("override_qty", ())[int(masked_slot.group(1))]
                    ignored_zero += int(quantity == 0)
                    positive_invalid += int(quantity > 0)
                else:
                    positive_invalid += 1  # conservatively retain unclassified reasons
            weeks += 1
            done = terminated or truncated
        budget = 4 if task == "full" else 2
        return {"episode": episode, "weeks": weeks, "cost_usd": total_cents / 100,
                "cost_components_usd": dict(costs), "salvage_usd": step_info["salvage_cents"] / 100,
                "constructor_cpu_s": constructor, "week1_cpu_s": timings[0],
                "max_cpu_s": max(timings), "median_cpu_s": float(np.median(timings)),
                "cpu_budget_s": budget,
                "would_exceed_budget_weeks": [i + 1 for i, x in enumerate(timings) if x > budget],
                "invalid_entry_counts": dict(invalid), "whole_week_failures": whole_week_failures,
                "positive_or_unclassified_invalid_entries": positive_invalid,
                "ignored_zero_masked_override_entries": ignored_zero,
                "recovery_status_counts": dict(recovery)}
    finally:
        env.close()


def main(agent="team_agent_vitya_v1", against="team_agent", task="tiny", entropy=202610081,
         episodes=1, out=None):
    check_task(task)
    indices = list(range(episodes)) if type(episodes) is int else episodes
    if not isinstance(indices, list) or not indices or any(type(i) is not int or i < 0 for i in indices):
        raise ValueError("episodes must be a positive count or a list of nonnegative IDs")
    candidate, baseline = resolve(agent).resolve(), resolve(against).resolve()
    if candidate == baseline:
        raise ValueError("candidate and baseline must differ")
    output = Path(out or f"outputs/16_primary_native_screen/{datetime.now():%Y-%m-%d_%H-%M-%S_%f}").resolve()
    if output.exists() or any(folder == output or folder in output.parents for folder in (candidate, baseline)):
        raise ValueError("choose a fresh output outside both submissions")
    output.mkdir(parents=True)
    result = {"status": "running", "task": task, "entropy": entropy, "episode_ids": indices,
              "replay_seed": 0, "quick": False, "official_cpu_budget_enforced": False,
              "rss": None, "rss_interval": None, "candidate": [], "baseline": [],
              "sha256": {"candidate": fingerprint(candidate), "baseline": fingerprint(baseline)},
              "params": {label: json.loads((folder / "params.json").read_text())
                         for label, folder in (("candidate", candidate), ("baseline", baseline))},
              "limitations": ["Native cost screen, NOT isolated sbf compare or certified RSS.",
                              "Same generated episodes; independent policy trajectories; no salted policy seed.",
                              "CPU constructor included; native metering does not enforce fallback.",
                              "Diagnostic invalid entries include ignored zero masked overrides; inspect reasons.",
                              "USD intervals are unnormalized and cannot be converted to RSS without references."]}

    def save():
        (output / "result.json").write_text(json.dumps(result, indent=2, allow_nan=False) + "\n", encoding="utf-8")

    try:
        for episode in indices:
            for label, folder in (("candidate", candidate), ("baseline", baseline)):
                row = replay(folder, task, entropy, episode)
                result[label].append(row)
                save()
                print(f"{label} {episode}: cost={row['cost_usd']:.2f}; max CPU={row['max_cpu_s']:.3f}s", flush=True)
        deltas = np.array([a["cost_usd"] - b["cost_usd"] for a, b in zip(result["candidate"], result["baseline"])])
        mean = float(deltas.mean())
        interval = None
        if len(deltas) > 1:
            margin = float(stats.t.ppf(.95, len(deltas) - 1) * stats.sem(deltas))
            interval = [mean - margin, mean + margin]
        result.update(status="completed", paired_cost_delta_usd=mean,
                      paired_cost_delta_90pct_t_interval_usd=interval,
                      candidate_cost_mean_usd=float(np.mean([a["cost_usd"] for a in result["candidate"]])),
                      baseline_cost_mean_usd=float(np.mean([a["cost_usd"] for a in result["baseline"]])),
                      wins=int(np.count_nonzero(deltas < 0)), ties=int(np.count_nonzero(deltas == 0)))
        print(json.dumps({k: v for k, v in result.items() if k.startswith("paired_")}), flush=True)
    except Exception as exc:
        result.update(status="incomplete", error={"type": type(exc).__name__, "message": str(exc)})
        raise
    finally:
        save()


if __name__ == "__main__":
    fire.Fire(main)
