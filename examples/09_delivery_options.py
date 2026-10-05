"""Run V2/V3 as observers beside unchanged heuristic on native Small/Full.

    uv run python examples/09_delivery_options.py --task=small
    uv run python examples/09_delivery_options.py --task=full

Unit-quantity route probes are diagnostics, not actual DeliveryNeed or flows.
"""

import json
import sys
import time
from dataclasses import asdict
from pathlib import Path

import fire
import gymnasium as gym
from shockbench_flow_gym import agent_config_from_reset

from sbf_starter import env_id
from sbf_starter.agents import load


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agents.team_agent.delivery import DeliveryEvaluator  # noqa: E402
from agents.team_agent.network import NetworkTracker, StaticNetwork  # noqa: E402


def main(task: str = "tiny", episode: int = 0, entropy: int = 12345, out: str | None = None) -> None:
    env = gym.make(env_id(task), entropy=entropy)
    try:
        obs, info = env.reset(seed=0, options={"episode": episode})
        config = agent_config_from_reset(env, obs, info)
        policy = load("heuristic")(config)
        start_wall, start_cpu = time.perf_counter(), time.process_time()
        network = StaticNetwork(config)
        tracker = NetworkTracker(config, network)
        evaluator = DeliveryEvaluator(config, network)
        init_cpu, init_wall = time.process_time() - start_cpu, time.perf_counter() - start_wall
        rows, samples, total_cents = [], [], 0
        done = False
        while not done:
            start_wall, start_cpu = time.perf_counter(), time.process_time()
            snapshot = tracker.update(obs)
            options = []
            for destination, commodity in network.slots_to:
                options.extend(
                    evaluator.options(
                        snapshot,
                        destination,
                        commodity,
                        quantity=1.0,
                        due_week=min(snapshot.week + 7, snapshot.horizon),
                    )
                )
            cpu = time.process_time() - start_cpu + (init_cpu if not rows else 0)
            wall = time.perf_counter() - start_wall + (init_wall if not rows else 0)
            budget = 4.0 if task == "full" else 2.0
            if cpu > budget:
                raise RuntimeError(f"V2/V3 CPU {cpu}s exceeds {budget}s in week {snapshot.week}")
            rows.append(
                {
                    "week": snapshot.week,
                    "observer_cpu_s": cpu,
                    "observer_wall_s": wall,
                    "options": len(options),
                    "unconfirmed_permissions": sum(not o.permission_observed for o in options),
                    "unknown_completion": sum(o.estimated_completion_week is None for o in options),
                }
            )
            if len(rows) == 1:
                samples = [asdict(o) for o in options]
            # The observer never decides or alters the policy's action.
            obs, _, terminated, truncated, info = env.step(policy.act(obs))
            total_cents += int(info["reward_cents"])
            done = terminated or truncated
        report = {
            "kind": "native_network_delivery_observer_not_rss",
            "task": task,
            "episode": episode,
            "entropy": entropy,
            "policy": "heuristic",
            "route_count": len(network.routes),
            "policy_cost_usd": -total_cents / 100,
            "weeks": rows,
            "max_observer_cpu_s": max(row["observer_cpu_s"] for row in rows),
            "max_observer_wall_s": max(row["observer_wall_s"] for row in rows),
            "first_week_unit_quantity_probes": samples,
        }
        folder = Path(out or f"outputs/09_delivery_options/{time.strftime('%Y-%m-%d_%H-%M-%S')}_{task}")
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(
            f"{task}: {len(rows)} weeks, {len(network.routes)} routes; observer max CPU "
            f"{report['max_observer_cpu_s']:.6f}s, wall {report['max_observer_wall_s']:.6f}s"
        )
        print("Includes V1/V2/V3 initialization in week 1; these are local observer timings, not server certification.")
        print(f"heuristic cost ${report['policy_cost_usd']:,.2f}; no RSS or strategy improvement claimed")
        print(f"report: {(folder / 'report.json').resolve()}")
    finally:
        env.close()


if __name__ == "__main__":
    fire.Fire(main)
