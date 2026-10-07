"""Closure waiting versus detour: one-change native ablation, not official RSS."""

import hashlib
import json
import shutil
import time
from collections import Counter
from pathlib import Path

import fire
import gymnasium as gym
import numpy as np
from shockbench_flow_gym import agent_config_from_reset

from sbf_starter import env_id
from sbf_starter.agents import load


ROOT = Path(__file__).resolve().parents[1]


def fingerprint(folder):
    digest = hashlib.sha256()
    for path in sorted(folder.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts:
            digest.update(path.relative_to(folder).as_posix().encode() + b"\0" + path.read_bytes())
    return digest.hexdigest()


def replay(folder, task, entropy, episode, weeks):
    env = gym.make(env_id(task), entropy=entropy)
    try:
        obs, info = env.reset(seed=0, options={"episode": episode})
        config = agent_config_from_reset(env, obs, info)
        cls = load(folder)
        start = time.process_time()
        agent = cls(config)
        initialization = time.process_time() - start
        rows, cents, done = [], 0, False
        while not done and (weeks == 0 or len(rows) < weeks):
            start = time.process_time()
            action = agent.act(obs)
            cpu = time.process_time() - start + (initialization if not rows else 0)
            assert action["flows"].shape == env.action_space["flows"].shape
            assert np.all(np.isfinite(action["flows"])) and np.all(action["flows"] >= 0)
            allocation = agent.last_allocation
            reasons = Counter(reason.code for reason in allocation.reasons)
            usage = [r.message for r in allocation.reasons if r.code == "queue_eta_forecast_usage"]
            unmet = Counter(n.reason for n in allocation.unmet_needs)
            closure_rows = int(
                np.count_nonzero(
                    (obs["closure_end.chokepoint.observed"] == 1) & (obs["closure_end.end_week.observed"] == 1)
                )
            )
            closed = int(np.count_nonzero((obs["graph_now.open.observed"] == 1) & (obs["graph_now.open"] == 0)))
            obs, _, terminated, truncated, info = env.step(action)
            cents += int(info["reward_cents"])
            rows.append(
                {
                    "week": len(rows) + 1,
                    "cpu_s": cpu,
                    "flows": action["flows"].tolist(),
                    "reasons": dict(reasons),
                    "unmet_reasons": dict(unmet),
                    "forecast_usage": usage,
                    "observed_closure_end_rows": closure_rows,
                    "observed_fully_closed_chokepoints": closed,
                }
            )
            done = terminated or truncated
        return {
            "cost_usd": -cents / 100,
            "complete_episode": bool(done),
            "max_cpu_s": max(row["cpu_s"] for row in rows),
            "weeks": rows,
        }
    finally:
        env.close()


def main(task="tiny", entropy=67890, episodes=(0,), weeks=0, source="team_agent", out=None):
    if weeks < 0:
        raise ValueError("weeks must be nonnegative; zero means the full episode")
    if isinstance(episodes, int):
        episodes = (episodes,)
    folder = Path(out or f"outputs/13_closure_wait/{time.strftime('%Y-%m-%d_%H-%M-%S')}_{task}")
    folder.mkdir(parents=True, exist_ok=False)
    source = Path(source) if Path(source).is_dir() else ROOT / "agents" / source
    source_hash = fingerprint(source)
    hashes = {}
    for label, enabled in (("before", False), ("after", True)):
        target = folder / label
        shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__"))
        path = target / "params.json"
        params = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
        params.update(
            allocation_enabled=True,
            queue_eta_enabled=True,
            announced_eta_guard_enabled=False,
            closure_wait_enabled=enabled,
        )
        path.write_text(json.dumps(params, indent=2) + "\n", encoding="utf-8")
        hashes[label] = fingerprint(target)
    report = {
        "kind": "native_one_change_ablation_not_rss",
        "status": "running",
        "task": task,
        "entropy": entropy,
        "replay_seed": 0,
        "weeks_requested": weeks,
        "source_hash": source_hash,
        "frozen_folder_hashes": hashes,
        "episodes": [],
    }
    report_path = folder / "report.json"
    print(f"ablation: {report_path.resolve()}", flush=True)
    try:
        for episode in episodes:
            pair = {"episode": episode}
            for label in ("before", "after"):
                pair[label] = replay(folder / label, task, entropy, episode, weeks)
                print(
                    f"episode {episode} {label}: ${pair[label]['cost_usd']:,.2f}; "
                    f"max CPU {pair[label]['max_cpu_s']:.6f}s",
                    flush=True,
                )
            pair["changed_action_weeks"] = [
                a["week"]
                for a, b in zip(pair["before"]["weeks"], pair["after"]["weeks"], strict=True)
                if a["flows"] != b["flows"]
            ]
            pair["cost_delta_usd"] = pair["after"]["cost_usd"] - pair["before"]["cost_usd"]
            report["episodes"].append(pair)
        if hashes != {label: fingerprint(folder / label) for label in hashes}:
            raise RuntimeError("frozen ablation inputs changed")
        if source_hash != fingerprint(source):
            raise RuntimeError("source changed during ablation")
        report["status"] = "completed"
    except Exception as exc:
        report.update(status="failed", error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    fire.Fire(main)
