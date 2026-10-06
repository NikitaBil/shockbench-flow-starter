"""Inspect V1 route mappings; no agent policy or scoring runner is required.

uv run python examples/08_network_routes.py --task=small
uv run python examples/08_network_routes.py --task=full
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


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agents.team_agent.network import StaticNetwork  # noqa: E402


def main(task: str = "tiny", out: str | None = None) -> None:
    """Export every slot's complete static route and shared capacity resources."""
    env = gym.make(env_id(task), entropy=12345)
    try:
        obs, info = env.reset(seed=0, options={"episode": 0})
        config = agent_config_from_reset(env, obs, info)
        wall_start, cpu_start = time.perf_counter(), time.process_time()
        network = StaticNetwork(config)
        cpu = time.process_time() - cpu_start
        wall = time.perf_counter() - wall_start
        rows = []
        for route in network.routes:
            row = asdict(route)
            row.update(
                commodity=network.commodity_names[route.commodity_id],
                node_names=[network.node_names[n] for n in route.nodes],
                edge_names=[network.edge_names[e] for e in route.edges],
                lane_name=None if route.lane_id is None else network.lane_names[route.lane_id],
                shared_edges={
                    str(e): network.edges_to_slots[e] for e in route.edges if len(network.edges_to_slots[e]) > 1
                },
                shared_chokepoint_pools={
                    f"{c}/{route.pool}": network.chokepoint_pools_to_slots[c, route.pool]
                    for c in route.chokepoints
                    if len(network.chokepoint_pools_to_slots[c, route.pool]) > 1
                },
            )
            rows.append(row)
        report = {
            "kind": "v1_static_routes_not_policy_evaluation",
            "task": task,
            "instance_id": config["static"]["instance_id"],
            "instance_hash": config["static"]["instance_hash"],
            "slot_count": len(rows),
            "init_cpu_s": cpu,
            "init_wall_s": wall,
            "routes": rows,
        }
        folder = Path(out or f"outputs/08_network_routes/{time.strftime('%Y-%m-%d_%H-%M-%S')}_{task}")
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "routes.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(f"{task}: {len(rows)} slots; V1 build CPU {cpu:.6f}s, wall {wall:.6f}s")
        for label, predicate in (
            ("direct", lambda row: row["lane_id"] is None and row["nominal_transit_weeks"] == 0),
            ("lane", lambda row: row["lane_id"] is not None),
        ):
            sample = next((row for row in rows if predicate(row)), None)
            if sample:
                print(
                    f"{label}: slot {sample['slot_id']}, {sample['commodity']}: "
                    f"{' -> '.join(sample['node_names'])}; nominal edge transit {sample['nominal_transit_weeks']} weeks"
                )
        print(f"full route table: {(folder / 'routes.json').resolve()}")
    finally:
        env.close()


if __name__ == "__main__":
    fire.Fire(main)
