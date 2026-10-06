"""Native Allocator V1 smoke comparison: separate trajectories, not RSS.

    uv run python examples/10_network_allocation.py --task=small
    uv run python examples/10_network_allocation.py --task=full

Freeze both folders before replay; the default team_agent stays heuristic.
"""

import hashlib
import json
import shutil
import time
from pathlib import Path

import fire
import gymnasium as gym
import numpy as np
from shockbench_flow_gym import agent_config_from_reset

from sbf_starter import ROOT, env_id
from sbf_starter.agents import load


def fingerprint(folder):
    digest = hashlib.sha256()
    for path in sorted(folder.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts:
            digest.update(path.relative_to(folder).as_posix().encode())
            digest.update(b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


def replay(folder, task, entropy, episode):
    env = gym.make(env_id(task), entropy=entropy)
    try:
        obs, info = env.reset(seed=0, options={"episode": episode})
        config = agent_config_from_reset(env, obs, info)
        agent_class = load(folder)
        start = time.process_time()
        agent = agent_class(config)
        init_cpu = time.process_time() - start
        rows, cents, done = [], 0, False
        while not done:
            start = time.process_time()
            action = agent.act(obs)
            cpu = time.process_time() - start + (init_cpu if not rows else 0)
            if cpu > (4 if task == "full" else 2):
                raise RuntimeError(f"local Agent CPU budget exceeded in week {len(rows) + 1}: {cpu}s")
            allocation = getattr(agent, "last_allocation", None)
            obs, _, terminated, truncated, info = env.step(action)
            cents += int(info["reward_cents"])
            seen = (obs["last_week.clip.requested.observed"] == 1) & (obs["last_week.clip.executed.observed"] == 1)
            requested, executed = obs["last_week.clip.requested"], obs["last_week.clip.executed"]
            clipped = seen & (requested - executed > 1e-9 * np.maximum(1.0, requested))
            rows.append(
                {
                    "week": len(rows) + 1,
                    "cpu_s": cpu,
                    "nonzero_flow_slots": int(np.count_nonzero(action["flows"])),
                    "visible_clip_slots": int(np.count_nonzero(seen)),
                    "clipped_slots": int(np.count_nonzero(clipped)),
                    "clipped_requests": [
                        {"slot": int(slot), "requested": float(requested[slot]), "executed": float(executed[slot])}
                        for slot in np.flatnonzero(clipped)
                    ],
                    "unmet_needs": None if allocation is None else len(allocation.unmet_needs),
                    "reason_counts": None
                    if allocation is None
                    else {
                        code: sum(r.code == code for r in allocation.reasons)
                        for code in {r.code for r in allocation.reasons}
                    },
                }
            )
            done = terminated or truncated
        return {
            "cost_usd": -cents / 100,
            "max_cpu_s": max(row["cpu_s"] for row in rows),
            "clipped_slot_weeks": sum(row["clipped_slots"] for row in rows),
            "visible_clip_slot_weeks": sum(row["visible_clip_slots"] for row in rows),
            "weeks": rows,
        }
    finally:
        env.close()


def main(task="tiny", entropy=12345, episode=0, out=None):
    folder = Path(out or f"outputs/10_network_allocation/{time.strftime('%Y-%m-%d_%H-%M-%S')}_{task}")
    folder.mkdir(parents=True, exist_ok=False)
    baseline, candidate = folder / "baseline", folder / "candidate"
    source = ROOT / "agents" / "team_agent"
    for target, enabled in ((baseline, False), (candidate, True)):
        shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__"))
        params_path = target / "params.json"
        params = json.loads(params_path.read_text()) if params_path.exists() else {}
        params["allocation_enabled"] = enabled
        params_path.write_text(json.dumps(params, indent=2) + "\n", encoding="utf-8")
    hashes = {label: fingerprint(path) for label, path in (("baseline", baseline), ("candidate", candidate))}
    report = {
        "status": "running",
        "kind": "native_allocator_v1_separate_trajectory_smoke_not_rss",
        "task": task,
        "entropy": entropy,
        "episode": episode,
        "folder_hashes": hashes,
        "initialization_counted_in_week_one": True,
    }
    report_path = folder / "report.json"
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"frozen inputs and report: {folder.resolve()}", flush=True)
    try:
        for label, path in (("baseline", baseline), ("candidate", candidate)):
            report[label] = replay(path, task, entropy, episode)
            print(
                f"{label}: ${report[label]['cost_usd']:,.2f}; "
                f"max local CPU {report[label]['max_cpu_s']:.6f}s; "
                f"clipped slot-weeks {report[label]['clipped_slot_weeks']}",
                flush=True,
            )
        if hashes != {label: fingerprint(path) for label, path in (("baseline", baseline), ("candidate", candidate))}:
            raise RuntimeError("frozen source changed during replay")
        report["status"] = "completed"
        report["cost_difference_usd"] = report["candidate"]["cost_usd"] - report["baseline"]["cost_usd"]
        print("One-episode native evidence; no RSS, confidence interval or server CPU certification.")
    except Exception as exc:
        report["status"], report["error"] = "failed", f"{type(exc).__name__}: {exc}"
        raise
    finally:
        report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    fire.Fire(main)
