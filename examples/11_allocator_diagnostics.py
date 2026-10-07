"""Trace allocator decisions on native episodes, without changing frozen inputs.

    uv run python examples/11_allocator_diagnostics.py --task=full --weeks=7

Use --weeks=0 for a complete episode. CPU includes init and trace collection;
offline requirements/serialization are outside Agent time. This is not RSS.
"""

import hashlib
import json
import shutil
import sys
import time
from collections import Counter
from dataclasses import asdict
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


def summarize(events):
    denied = [e for e in events if e["stage"] == "eta" and e["reason"] == "queue_eta_forecast_budget_exhausted"]
    assignments = [e for e in events if e["stage"] == "assignment"]
    late = [e for e in assignments if e["reason"] == "eta_late"]
    return {
        "budget_denial_records": len(denied),
        "budget_denial_unique_week_needs": len({(e["week"], e["need_id"]) for e in denied}),
        "budget_denial_unique_week_need_slots": len({(e["week"], e["need_id"], e["slot_id"]) for e in denied}),
        "budget_denials_with_dispatchable_resources": sum(
            e["quantity"] > 0
            and e["source_stock"] > 0
            and e["entry_capacity"] > 0
            and e["permission_observed"]
            and (e["fleet_weight"] == 0 or e["fleet_remaining"] > 0)
            for e in denied
        ),
        "executed_forecasts": sum(e["calls_after"] - e["calls_before"] for e in events if e["stage"] == "eta"),
        "assignment_records": len(assignments),
        "late_assignment_records": len(late),
        "late_already_overdue": sum(e["overdue_at_dispatch"] for e in late),
        "late_no_wait_after_deadline": sum(
            not e["overdue_at_dispatch"] and e["no_wait_arrival"] > e["due_week"] for e in late
        ),
        "late_queue_or_batch_delay": sum(
            not e["overdue_at_dispatch"] and e["no_wait_arrival"] <= e["due_week"] for e in late
        ),
        "reason_records": dict(Counter(e["reason"] for e in events)),
    }


def replay(folder, task, entropy, episode, weeks):
    env = gym.make(env_id(task), entropy=entropy)
    try:
        obs, info = env.reset(seed=0, options={"episode": episode})
        config = agent_config_from_reset(env, obs, info)
        cls = load(folder)
        start = time.process_time()
        agent = cls(config)
        init_cpu = time.process_time() - start
        pipeline = getattr(agent, "pipeline", None)
        captured, events = {}, []
        trace_available = pipeline is not None and hasattr(pipeline.allocator, "trace_callback")
        if pipeline is not None:
            build, plan = pipeline.state_builder.build, pipeline.need_planner.plan

            def capture_build(*args, **kwargs):
                captured["state"] = build(*args, **kwargs)
                return captured["state"]

            def capture_plan(*args, **kwargs):
                captured["needs"] = plan(*args, **kwargs)
                return captured["needs"]

            pipeline.state_builder.build, pipeline.need_planner.plan = capture_build, capture_plan
            if trace_available:
                pipeline.allocator.trace_callback = events.append
        rows, cents, done = [], 0, False
        while not done and (weeks == 0 or len(rows) < weeks):
            events.clear()
            start = time.process_time()
            action = agent.act(obs)
            cpu = time.process_time() - start + (init_cpu if not rows else 0)
            allocation = getattr(agent, "last_allocation", None)
            net = getattr(agent, "network", None)
            if net is None:
                # The shipped heuristic has no network object; load only the
                # current topology outside its timed Agent execution.
                sys.path.insert(0, str(ROOT))
                from agents.team_agent.network import StaticNetwork

                net = StaticNetwork(config)
            row = {"week": len(rows) + 1, "cpu_s": cpu, "trace": list(events)}
            row["flows"] = [
                {
                    "slot_id": r.slot_id,
                    "source": net.node_names[r.source_node],
                    "destination": net.node_names[r.destination_node],
                    "commodity": net.commodity_names[r.commodity_id],
                    "quantity": float(action["flows"][r.slot_id]),
                    "unit": r.unit,
                }
                for r in net.routes
                if action["flows"][r.slot_id] > 0
            ]
            if pipeline is not None:
                state, needs = captured["state"], captured["needs"]
                planner = pipeline.need_planner
                # Pure diagnostic re-evaluation; never call the stateful plan twice.
                reader = plan.__func__.__globals__["ObservationReader"](obs)
                issues = []
                requirements = planner._requirements(state, reader, issues, net)
                row["needs"] = [asdict(n) for n in needs]
                row["planner_issues"] = list(planner.last_issues)
                row["unmet_needs"] = [asdict(n) for n in allocation.unmet_needs]
                row["reasons"] = [asdict(r) for r in allocation.reasons]
                pairs = set(requirements) | {(n.destination_node, n.commodity_id) for n in needs}
                # Include zero fuel/wafer needs; absence is part of the diagnosis.
                pairs.update(
                    pair for pair in state.available_stock if net.commodity_names[pair[1]] in {"nucfuel", "wafer"}
                )
                row["requirements"] = []
                for pair in sorted(pairs):
                    stock = state.available_stock.get(pair)
                    reqs = requirements.get(pair, ())
                    pair_needs = [n for n in needs if (n.destination_node, n.commodity_id) == pair]
                    row["requirements"].append(
                        {
                            "destination_node": pair[0],
                            "destination": net.node_names[pair[0]],
                            "commodity_id": pair[1],
                            "commodity": net.commodity_names[pair[1]],
                            "stock": None if stock is None else asdict(stock),
                            "raw_requirements": [list(r) for r in reqs],
                            "need_ids": [n.need_id for n in pair_needs],
                            "eligible_arrivals": [
                                asdict(a)
                                for a in state.arrivals
                                if (a.destination_node, a.commodity_id) == pair
                                and a.arrival_week is not None
                                and a.quantity.value is not None
                                and (
                                    planner.include_estimated_arrivals
                                    or (a.source == "observed" and a.quantity.source == "observed")
                                )
                            ],
                            "action_slots": list(net.slots_to.get(pair, ())),
                            "classification": "emitted_need"
                            if pair_needs
                            else "no_raw_requirement"
                            if not reqs
                            else "unknown_coverage"
                            if stock is None or stock.value is None
                            else "netted_by_planner",
                        }
                    )
            obs, _, terminated, truncated, info = env.step(action)
            cents += int(info["reward_cents"])
            row["shed"] = {
                config["static"]["nodes"]["id"][node]: float(obs["last_week.shed.qty"][i])
                if obs["last_week.shed.qty.observed"][i]
                else None
                for i, node in enumerate(config["layout"]["grids"])
            }
            row["requested_executed"] = [
                {
                    "slot_id": int(s),
                    "requested": float(obs["last_week.clip.requested"][s]),
                    "executed": float(obs["last_week.clip.executed"][s]),
                }
                for s in np.flatnonzero(
                    (obs["last_week.clip.requested.observed"] == 1)
                    & (obs["last_week.clip.executed.observed"] == 1)
                    & (obs["last_week.clip.requested"] > 0)
                )
            ]
            rows.append(row)
            done = terminated or truncated
        return {
            "trace_available": trace_available,
            "cost_usd": -cents / 100,
            "complete_episode": bool(done),
            "max_cpu_s": max(r["cpu_s"] for r in rows),
            "summary": summarize([e for r in rows for e in r["trace"]]),
            "weeks": rows,
        }
    finally:
        env.close()


def main(task="tiny", entropy=12345, episode=0, weeks=7, candidate="team_agent", baseline="heuristic", out=None):
    if weeks < 0:
        raise ValueError("weeks must be nonnegative; zero means the full episode")
    folder = Path(out or f"outputs/11_allocator_diagnostics/{time.strftime('%Y-%m-%d_%H-%M-%S')}_{task}")
    folder.mkdir(parents=True, exist_ok=False)
    sources = {
        label: Path(value) if Path(value).is_dir() else ROOT / "agents" / value
        for label, value in (("candidate", candidate), ("baseline", baseline))
    }
    originals = {k: fingerprint(v) for k, v in sources.items()}
    for label, source in sources.items():
        target = folder / label
        shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__"))
        if label == "candidate":
            params_path = target / "params.json"
            params = json.loads(params_path.read_text()) if params_path.exists() else {}
            params.update(allocation_enabled=True, queue_eta_enabled=True)
            params_path.write_text(json.dumps(params, indent=2) + "\n", encoding="utf-8")
    frozen = {label: fingerprint(folder / label) for label in sources}
    report = {
        "kind": "native_diagnostic_not_rss",
        "status": "running",
        "task": task,
        "entropy": entropy,
        "episode": episode,
        "weeks_requested": weeks,
        "source_paths": {k: str(v.resolve()) for k, v in sources.items()},
        "source_hashes": originals,
        "frozen_hashes": frozen,
        "initialization_counted_in_week_one": True,
    }
    report_path = folder / "report.json"
    print(f"diagnostics: {report_path.resolve()}", flush=True)
    try:
        for label in ("baseline", "candidate"):
            report[label] = replay(folder / label, task, entropy, episode, weeks)
            print(
                f"{label}: cost ${report[label]['cost_usd']:,.2f}; CPU {report[label]['max_cpu_s']:.6f}s; "
                f"{report[label]['summary']}",
                flush=True,
            )
        if originals != {k: fingerprint(v) for k, v in sources.items()} or frozen != {
            k: fingerprint(folder / k) for k in sources
        }:
            raise RuntimeError("source or frozen inputs changed during replay")
        report["status"] = "completed"
    except Exception as exc:
        report.update(status="failed", error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    fire.Fire(main)
