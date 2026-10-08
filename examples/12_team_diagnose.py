"""Replay frozen policies independently and record observed decision diagnostics."""

import hashlib
import json
import tempfile
import time
from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from datetime import datetime
from pathlib import Path

import fire
import gymnasium as gym
import numpy as np
from shockbench_flow_agent import unload_agent
from shockbench_flow_agent.submission import build_submission
from shockbench_flow_gym import agent_config_from_reset

from sbf_starter import check_task, env_id
from sbf_starter.agents import load, resolve


def fingerprint(folder):
    with tempfile.TemporaryDirectory(prefix="sbf-diagnose-") as tmp:
        archive = Path(build_submission(folder, Path(tmp) / "agent.zip"))
        return hashlib.sha256(archive.read_bytes()).hexdigest()


def plain(value):
    if is_dataclass(value):
        return {field.name: plain(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, np.ndarray):
        return plain(value.tolist())
    if isinstance(value, np.generic):
        return plain(value.item())
    if isinstance(value, Mapping):
        return {str(key): plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [plain(item) for item in value]
    return value


class Tap:
    """Observe module output without changing arguments, results or call count."""

    def __init__(self, target, method):
        self.target, self.method, self.last = target, method, None
        self.cpu_seconds = 0.0

    def __getattr__(self, name):
        attribute = getattr(self.target, name)
        if name != self.method:
            return attribute

        def call(*args, **kwargs):
            started = time.process_time()
            try:
                self.last = attribute(*args, **kwargs)
            finally:
                self.cpu_seconds += time.process_time() - started
            return self.last

        return call


def quantity_rows(mapping, config):
    nodes, commodities = config["static"]["nodes"]["id"], config["static"]["commodities"]["id"]
    return [
        {
            "node_id": node,
            "node": nodes[node],
            "commodity_id": commodity,
            "commodity": commodities[commodity],
            "unit": config["static"]["units"][commodities[commodity]],
            "quantity": plain(quantity),
        }
        for (node, commodity), quantity in sorted(mapping.items())
    ]


def flow_rows(action, config):
    rows = []
    for slot, quantity in enumerate(action["flows"]):
        if quantity <= 0:
            continue
        commodity = config["static"]["action_slots"]["k"][slot]
        name = config["static"]["commodities"]["id"][commodity]
        rows.append(
            {
                "slot_id": slot,
                "commodity_id": commodity,
                "commodity": name,
                "unit": config["static"]["units"][name],
                "requested_quantity": float(quantity),
            }
        )
    return rows


def observed_values(observation, key):
    """Keep hidden values as None, including scalar visibility masks."""
    values = np.asarray(observation[key])
    seen = np.broadcast_to(np.asarray(observation[f"{key}.observed"]), values.shape)
    return [float(value) if visible else None for value, visible in zip(values.flat, seen.flat)]


def outcomes(observation, config):
    """Post-step public last_week fields, aligned with the action just taken."""
    static, layout = config["static"], config["layout"]
    requested = observed_values(observation, "last_week.clip.requested")
    executed = observed_values(observation, "last_week.clip.executed")
    flows = []
    for slot, (request, execution) in enumerate(zip(requested, executed)):
        commodity = static["commodities"]["id"][static["action_slots"]["k"][slot]]
        flows.append(
            {
                "slot_id": slot,
                "commodity": commodity,
                "unit": static["units"][commodity],
                "requested_quantity": request,
                "executed_quantity": execution,
            }
        )
    sinks = []
    values = {name: observed_values(observation, f"last_week.sinks.{name}") for name in ("demand", "served", "lost")}
    for row, (node, commodity_id) in enumerate(layout["demands"]):
        commodity = static["commodities"]["id"][commodity_id]
        sinks.append(
            {
                "node": static["nodes"]["id"][node],
                "commodity": commodity,
                "unit": static["units"][commodity],
                **{name: data[row] for name, data in values.items()},
            }
        )
    costs = observed_values(observation, "last_week.cost_components")
    shed = observed_values(observation, "last_week.shed.qty")
    return {
        "executed_flows": flows,
        "sinks": sinks,
        "cost_components_usd": dict(zip(layout["cost_components"], costs)),
        "shed_gwh": [
            {"node": static["nodes"]["id"][node], "quantity": value} for node, value in zip(layout["grids"], shed)
        ],
    }


def run_episode(agent_class, task, entropy, episode, trace_path):
    env = gym.make(env_id(task), entropy=entropy)
    summary = {
        "episode": episode,
        "weeks": 0,
        "zero_flow_weeks": 0,
        "cost_usd": 0.0,
        "requested_by_commodity": {},
        "reason_counts": {},
        "unmet_reason_counts": {},
        "planner_issue_counts": {},
        "state_issue_counts": {},
        "act_cpu_seconds": 0.0,
    }
    reasons, unmet, planner_issues, state_issues = Counter(), Counter(), Counter(), Counter()
    requested = defaultdict(float)
    try:
        observation, info = env.reset(seed=0, options={"episode": episode})
        config = agent_config_from_reset(env, observation, info)
        started = time.process_time()
        agent = agent_class(config)
        summary["constructor_cpu_seconds"] = time.process_time() - started
        pipeline = getattr(agent, "pipeline", None)
        builder = planner = allocator = None
        if pipeline is not None:
            builder = Tap(pipeline.state_builder, "build")
            planner = Tap(pipeline.need_planner, "plan")
            allocator = Tap(pipeline.allocator, "allocate")
            pipeline.state_builder, pipeline.need_planner, pipeline.allocator = builder, planner, allocator
        with trace_path.open("w", encoding="utf-8") as trace:
            done = False
            while not done:
                if builder is not None:
                    for tap in (builder, planner, allocator):
                        tap.cpu_seconds = 0.0
                started = time.process_time()
                action = agent.act(observation)
                cpu = time.process_time() - started
                summary["act_cpu_seconds"] += cpu
                flows = flow_rows(action, config)
                for row in flows:
                    requested[row["commodity"]] += row["requested_quantity"]
                summary["zero_flow_weeks"] += int(not flows)
                record = {
                    "week": int(observation["week"][0]),
                    "requested_flows": flows,
                    "act_cpu_seconds": cpu,
                    "state": None,
                    "needs": None,
                    "allocation": None,
                    "stage_cpu_seconds": None,
                }
                allocation = getattr(agent, "last_allocation", None)
                if builder is not None:
                    state = builder.last
                    record["state"] = {
                        "available_stock": quantity_rows(state.available_stock, config),
                        "backlog": quantity_rows(state.backlog, config),
                        "pipeline": plain(state.pipeline),
                        "queues": plain(state.queues),
                        "arrivals": plain(state.arrivals),
                        "issues": list(state.issues),
                    }
                    record["needs"] = plain(planner.last)
                    record["planner_issues"] = list(planner.last_issues)
                    planner_issues.update(planner.last_issues)
                    state_issues.update(state.issues)
                    stage_cpu = {
                        "state": builder.cpu_seconds,
                        "needs": planner.cpu_seconds,
                        "allocation": allocator.cpu_seconds,
                    }
                    stage_cpu["other"] = max(0.0, cpu - sum(stage_cpu.values()))
                    record["stage_cpu_seconds"] = stage_cpu
                if allocation is not None:
                    record["allocation"] = {
                        "unmet_needs": plain(allocation.unmet_needs),
                        "resource_usage": plain(allocation.resource_usage),
                        "reasons": plain(allocation.reasons),
                    }
                    reasons.update(reason.code for reason in allocation.reasons)
                    unmet.update(reason for item in allocation.unmet_needs for reason in item.reason.split(","))
                observation, reward, terminated, truncated, step_info = env.step(action)
                record["outcomes"] = outcomes(observation, config)
                record["step_cost_usd"] = -int(step_info["reward_cents"]) / 100
                record["salvage_usd"] = int(step_info.get("salvage_cents", 0)) / 100 if terminated else 0.0
                summary["cost_usd"] += record["step_cost_usd"]
                summary["weeks"] += 1
                trace.write(json.dumps(record, allow_nan=False) + "\n")
                done = terminated or truncated
    finally:
        env.close()
    summary.update(
        requested_by_commodity=dict(requested),
        reason_counts=dict(reasons),
        unmet_reason_counts=dict(unmet),
        planner_issue_counts=dict(planner_issues),
        state_issue_counts=dict(state_issues),
    )
    return summary


def main(agent, against="baseline", task="small", entropy=67890, episodes=2, out=None):
    """Diagnostics only: no RSS, official CPU enforcement, upload or policy edits."""
    check_task(task)
    if isinstance(episodes, bool) or not isinstance(episodes, int) or episodes < 1:
        raise ValueError("episodes must be a positive integer")
    candidate, baseline = resolve(agent).resolve(), resolve(against).resolve()
    if not candidate.is_dir() or not baseline.is_dir() or candidate == baseline:
        raise ValueError("agent and against must be different submission folders")
    output = Path(out or f"outputs/12_team_diagnose/{datetime.now():%Y-%m-%d_%H-%M-%S_%f}").resolve()
    if output.exists():
        raise FileExistsError("choose a new diagnostic output directory")
    if candidate in output.parents or baseline in output.parents:
        raise ValueError("diagnostics must remain outside submission folders")
    before = {"candidate": fingerprint(candidate), "baseline": fingerprint(baseline)}
    output.mkdir(parents=True)
    result = {
        "trace_schema_version": 2,
        "status": "running",
        "settings": {
            "task": task,
            "entropy": entropy,
            "episodes": episodes,
            "seed": 0,
            "candidate": str(candidate),
            "baseline": str(baseline),
        },
        "submission_sha256": before,
        "candidate": [],
        "baseline": [],
        "limitations": [
            "Independent trajectories, not identical weekly observations.",
            "Requested flows are not confirmed shipments; different native units are not summed.",
            "Replay is local, not isolated scoring; timings include diagnostic taps, not official CPU checks.",
            "No RSS or confidence interval is computed; scorer policy_seed salting is not reproduced.",
        ],
    }
    report = []
    try:
        folders = {"candidate": candidate, "baseline": baseline}
        for episode in range(episodes):
            for label in ("candidate", "baseline"):
                # The official loader unloads the previous submission, including
                # its lazy imports. Keep just one policy loaded through its replay.
                agent_class = load(folders[label])
                summary = run_episode(agent_class, task, entropy, episode, output / f"{label}-{episode}.jsonl")
                result[label].append(summary)
                line = (
                    f"{label} episode {episode}: cost {summary['cost_usd']:,.0f} USD; "
                    f"zero requested flows {summary['zero_flow_weeks']}/{summary['weeks']} weeks"
                )
                report.append(line)
                print(line, flush=True)
                if label == "candidate":
                    for key in ("unmet_reason_counts", "reason_counts", "planner_issue_counts"):
                        line = f"  {key}: {Counter(summary[key]).most_common(12)}"
                        report.append(line)
                        print(line, flush=True)
        result["status"] = "completed"
    except Exception as exc:
        result.update(status="failed", error={"type": type(exc).__name__, "message": str(exc)})
        raise
    finally:
        unload_agent()
        after = {"candidate": fingerprint(candidate), "baseline": fingerprint(baseline)}
        result["submissions_unchanged"] = before == after
        if before != after:
            result["status"] = "invalid_sources_changed"
        (output / "summary.json").write_text(json.dumps(result, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        (output / "report.txt").write_text("\n".join(report) + "\n", encoding="utf-8")
    if before != after:
        raise RuntimeError("submission files changed during diagnostics; results are not valid")
    print(f"Diagnostics saved: {output}")


if __name__ == "__main__":
    fire.Fire(main)
