"""Native weekly provenance audit; simulator internals are offline evidence only.

No RSS or official timing is computed. Hooks call the original simulator exactly
once and are restored even on failure. The actor sees only its normal observation.
"""

import hashlib
import json
import tempfile
import time
from collections import Counter, defaultdict
from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
from datetime import datetime
from pathlib import Path

import fire
import gymnasium as gym
import numpy as np
from shockbench_flow.dynamics import sim
from shockbench_flow.dynamics.clip import fleet_totals
from shockbench_flow_agent.submission import build_submission
from shockbench_flow_gym import agent_config_from_reset

from sbf_starter import check_task, env_id
from sbf_starter.agents import load, resolve


def fingerprint(folder):
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(build_submission(folder, Path(tmp) / "agent.zip"))
        return hashlib.sha256(archive.read_bytes()).hexdigest()


def plain(value):
    if is_dataclass(value):
        return plain(asdict(value))
    if isinstance(value, np.ndarray):
        return plain(value.tolist())
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, dict):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    return value


class StageTap:
    def __init__(self, target, name, book):
        self.target, self.name, self.book = target, name, book

    def __getattr__(self, name):
        method = getattr(self.target, name)
        if name != "apply":
            return method

        def apply(*args, **kwargs):
            result = method(*args, **kwargs)
            self.book[self.name] = plain(result)
            return result

        return apply


@contextmanager
def execution_tap(book):
    original_clip, original_fleet = sim.clip_requests, sim.fleet_slack

    def clip(inst, requests, banned, cap, stock, clamps=None):
        result = original_clip(inst, requests, banned, cap, stock, clamps)
        book["before_fleet"] = result.copy()
        return result

    def fleet(inst, items, caps):
        result = original_fleet(inst, items, caps)
        book["fleet"] = {"caps": list(caps), "usage": fleet_totals(inst, items),
                         "items": list(items), "executed": result}
        return result

    sim.clip_requests, sim.fleet_slack = clip, fleet
    try:
        yield
    finally:
        sim.clip_requests, sim.fleet_slack = original_clip, original_fleet


def visible(obs, key, index):
    mask = np.broadcast_to(obs[key + ".observed"], obs[key].shape)
    return float(obs[key][index]) if mask[index] else None


def run_episode(folder, task, entropy, episode, trace_path):
    env = gym.make(env_id(task), entropy=entropy)
    stages, execution = {}, {}
    totals = defaultdict(Counter)
    costs, reasons = Counter(), Counter()
    cpu, invalid, weeks = [], 0, 0
    try:
        obs, info = env.reset(seed=0, options={"episode": episode})
        config = agent_config_from_reset(env, obs, info)
        started = time.process_time()
        actor = load(folder)(config)
        constructor = time.process_time() - started
        assert actor.pipeline is None, "audit targets the active heuristic"
        network, cover = actor.network, actor.stock_rebalancer
        for name in ("production_horizon", "route_preferences", "stock_rebalancer", "sales_dispatch",
                     "upstream_dispatch", "packaging_dispatch", "fuel_dispatch", "fuel_mpc",
                     "fuel_batch", "fuel_lookahead", "fuel_release"):
            setattr(actor, name, StageTap(getattr(actor, name), name, stages))
        stock_rows = {tuple(pair): i for i, pair in enumerate(config["layout"]["stock_slots"])}
        with trace_path.open("w", encoding="utf-8") as trace, execution_tap(execution):
            done = False
            while not done:
                started = time.process_time()
                action = actor.act(obs)
                cpu.append(time.process_time() - started)
                final = np.asarray(action["flows"], dtype=float)
                entry_used = np.bincount([r.edge_id for r in network.routes], weights=final,
                                        minlength=len(network.edge_names))
                slots = []
                for s, route in enumerate(network.routes):
                    pair = route.source_node, route.commodity_id
                    stock = visible(obs, "stock.qty", stock_rows[pair])
                    source_used = float(final[list(network.slots_from[pair])].sum())
                    edge_cap = visible(obs, "graph_now.u", route.edge_id)
                    pre = stages["production_horizon"][s]
                    preferred = stages["route_preferences"][s]
                    restored_room = max(0.0, pre - final[s])
                    # This is visible physical opportunity, NOT a proven economic benefit.
                    physical_spare = (min(restored_room, max(0.0, stock - source_used),
                                          max(0.0, edge_cap - entry_used[route.edge_id]))
                                      if stock is not None and edge_cap is not None else None)
                    coverage, _power = cover._coverage(route, obs)
                    row = {"slot": s, "source": network.node_names[route.source_node],
                           "destination": network.node_names[route.destination_node],
                           "commodity": network.commodity_names[route.commodity_id], "unit": route.unit,
                           "source_stock_observed": stock, "entry_capacity_observed": edge_cap,
                           "receiver_cover_estimated_weeks": coverage,
                           "nominal_transit_weeks_estimate": route.nominal_transit_weeks,
                           "eta_status": "unknown" if route.chokepoints else "nominal_estimate",
                           "pre_preferences": pre, "after_preferences": preferred,
                           "after_rebalance": stages["stock_rebalancer"][s],
                           "after_sales": stages["sales_dispatch"][s],
                           "after_batch": stages["fuel_batch"][s], "requested": final[s],
                           "suppressed_visible_spare": physical_spare if preferred < pre else 0.0,
                           "preferred_visible_spare": (min(max(0., preferred - final[s]),
                                                            max(0., stock - source_used),
                                                            max(0., edge_cap - entry_used[route.edge_id]))
                                                       if stock is not None and edge_cap is not None else None)}
                    slots.append(row)
                next_obs, _, terminated, truncated, step_info = env.step(action)
                rec = env.unwrapped.core.trajectory.records[-1]
                for row in slots:
                    s, name = row["slot"], row["commodity"]
                    row["before_fleet"] = execution["before_fleet"].get(s, 0.0)
                    row["executed"] = rec.executed.get(s, 0.0)
                    for field in ("requested", "executed", "suppressed_visible_spare", "preferred_visible_spare"):
                        totals[field][name] += row[field] or 0.0
                    totals["edge_stock_mask_clip"][name] += row["requested"] - row["before_fleet"]
                    totals["fleet_clip"][name] += row["before_fleet"] - row["executed"]
                fleet = execution["fleet"]
                for pool, usage, cap in zip(("tb", "ct"), fleet["usage"], fleet["caps"]):
                    reasons[pool + "_fleet_binding_weeks"] += int(usage > cap * (1 + 1e-9))
                release = []
                for s, requested in rec.override_requested.items():
                    static = config["static"]["override_slots"]
                    k = static["k"][s]
                    name = network.commodity_names[k]
                    released = rec.override_executed.get(s, 0.0)
                    release.append({"slot": s, "commodity": name, "unit": config["static"]["units"][name],
                                    "requested": requested, "executed": released})
                    totals["override_requested"][name] += requested
                    totals["override_executed"][name] += released
                costs.update({name: float(value) for name, value in vars(rec.costs).items()})
                invalid += len(rec.invalid)
                trace.write(json.dumps(plain({"week": int(obs["week"][0]), "slots": slots,
                                              "release": release, "fleet": fleet,
                                              "invalid": rec.invalid,
                                              "cost_components_usd": vars(rec.costs)}), allow_nan=False) + "\n")
                obs, done = next_obs, terminated or truncated
                weeks += 1
        return {"episode": episode, "weeks": weeks, "cost_components_usd": dict(costs),
                "total_cost_usd": sum(costs.values()) - float(step_info["salvage_cents"]) / 100,
                "salvage_usd": float(step_info["salvage_cents"]) / 100,
                "quantities_by_commodity": {k: dict(v) for k, v in totals.items()},
                "counts": dict(reasons), "invalid": invalid, "constructor_cpu_seconds": constructor,
                "max_act_cpu_seconds_with_taps": max(cpu), "first_week_cpu_with_constructor": constructor + cpu[0]}
    finally:
        env.close()


def main(agent="team_agent", task="tiny", entropy=67890, episodes=1, out=None):
    check_task(task)
    indices = list(range(episodes)) if isinstance(episodes, int) and not isinstance(episodes, bool) else episodes
    if not isinstance(indices, list) or not indices or any(type(i) is not int or i < 0 for i in indices):
        raise ValueError("episodes must be a positive count or a list of nonnegative IDs")
    folder = resolve(agent).resolve()
    output = Path(out or f"outputs/15_primary_dispatch_audit/{datetime.now():%Y-%m-%d_%H-%M-%S_%f}").resolve()
    if output.exists() or folder == output or folder in output.parents:
        raise ValueError("choose a fresh output outside the submission")
    output.mkdir(parents=True)
    result = {"status": "running", "task": task, "entropy": entropy, "episode_ids": indices,
              "seed": 0, "agent": str(folder), "sha256": fingerprint(folder), "episodes": [],
              "limitations": ["Native replay, no RSS, no isolation or CPU fallback enforcement.",
                              "Physical spare is per-slot opportunity, not additive shared-resource capacity.",
                              "Estimated coverage/transit are not guaranteed ETA or marginal total cost.",
                              "Private execution hooks are offline ground truth, never actor inputs."]}
    try:
        for episode in indices:
            row = run_episode(folder, task, entropy, episode, output / f"episode-{episode}.jsonl")
            result["episodes"].append(row)
            print(json.dumps(row), flush=True)
        result["status"] = "completed"
    except Exception as exc:
        result.update(status="failed", error={"type": type(exc).__name__, "message": str(exc)})
        raise
    finally:
        (output / "summary.json").write_text(json.dumps(result, indent=2, allow_nan=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    fire.Fire(main)
