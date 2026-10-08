"""The diagnostic harness must observe decisions without changing the policy."""

import importlib.util
import json
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

import numpy as np
import pytest


ROOT = Path(__file__).resolve().parents[1]


def example(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "examples" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


diagnose = example("12_team_diagnose")


def test_tap_calls_once_and_preserves_identity():
    class Builder:
        calls = 0
        issues = ("unknown",)

        def build(self, observation, network):
            self.calls += 1
            assert observation is obs and network is net
            return output

    obs, net, output = object(), object(), object()
    target = Builder()
    tap = diagnose.Tap(target, "build")
    assert tap.build(obs, net) is output
    assert tap.last is output
    assert target.calls == 1
    assert tap.issues == target.issues


def test_plain_handles_numpy_dataclasses_and_readonly_mappings():
    @dataclass
    class Quantity:
        value: float | None
        source: str

    value = MappingProxyType(
        {
            "observed": Quantity(np.float64(3), "observed"),
            "unknown": Quantity(None, "unknown"),
            "array": np.array([1, 2]),
        }
    )
    result = diagnose.plain(value)
    assert json.loads(json.dumps(result, allow_nan=False)) == {
        "observed": {"value": 3, "source": "observed"},
        "unknown": {"value": None, "source": "unknown"},
        "array": [1, 2],
    }


def test_flows_use_config_slot_commodity_and_keep_units_separate():
    config = {
        "static": {
            "action_slots": {"k": [1, 0, 1]},
            "commodities": {"id": ["fuel", "chips"]},
            "units": {"fuel": "GWh", "chips": "units"},
        }
    }
    rows = diagnose.flow_rows({"flows": np.array([4.0, 2.0, 0.0])}, config)
    assert [(row["commodity"], row["unit"], row["requested_quantity"]) for row in rows] == [
        ("chips", "units", 4.0),
        ("fuel", "GWh", 2.0),
    ]


def test_outcomes_are_post_step_visible_quantities_with_config_indices():
    config = {
        "static": {
            "action_slots": {"k": [0, 1]},
            "nodes": {"id": ["market", "grid"]},
            "commodities": {"id": ["fuel", "chip"]},
            "units": {"fuel": "GWh", "chip": "items"},
        },
        "layout": {"demands": [(0, 1)], "grids": [1], "cost_components": ["shed", "shortage"]},
    }
    obs = {}
    values = {
        "last_week.clip.requested": [10, 2],
        "last_week.clip.executed": [6, 1],
        "last_week.sinks.demand": [4],
        "last_week.sinks.served": [3],
        "last_week.sinks.lost": [1],
        "last_week.shed.qty": [5],
        "last_week.cost_components": [100, 200],
    }
    for key, value in values.items():
        obs[key] = np.array(value)
        obs[f"{key}.observed"] = np.array([1])
    obs["last_week.clip.executed.observed"] = np.array([1, 0])
    result = diagnose.outcomes(obs, config)
    assert result["executed_flows"][0]["executed_quantity"] == 6
    assert result["executed_flows"][1]["executed_quantity"] is None
    assert result["sinks"][0]["commodity"] == "chip"
    assert result["cost_components_usd"] == {"shed": 100, "shortage": 200}
    assert result["shed_gwh"] == [{"node": "grid", "quantity": 5}]


@pytest.mark.parametrize("episodes", [True, 0, -1, "2"])
def test_bad_episode_count_rejected(episodes):
    with pytest.raises(ValueError, match="positive integer"):
        diagnose.main("unused", episodes=episodes)


def test_real_candidate_replay_is_readonly(tmp_path, monkeypatch):
    from sbf_starter.agents import load

    monkeypatch.chdir(ROOT)
    frozen = tmp_path / "frozen"
    example("11_team_candidate").main(preset="baseline_conservative", queue_eta=False, out=str(frozen))
    before = diagnose.fingerprint(frozen / "candidate")
    trace = tmp_path / "trace.jsonl"
    result = diagnose.run_episode(load(frozen / "candidate"), "tiny", 67890, 0, trace)
    rows = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
    assert result["weeks"] == len(rows) > 0
    assert all(row["state"] is not None and row["allocation"] is not None for row in rows)
    assert result["cost_usd"] == pytest.approx(sum(row["step_cost_usd"] for row in rows))
    assert all(row["outcomes"] is not None and row["stage_cpu_seconds"] is not None for row in rows)
    component_total = sum(sum(row["outcomes"]["cost_components_usd"].values()) for row in rows)
    assert result["cost_usd"] == pytest.approx(component_total - sum(row["salvage_usd"] for row in rows))
    assert diagnose.fingerprint(frozen / "candidate") == before


def test_main_keeps_lazy_imports_isolated_across_policies_and_episodes(tmp_path, monkeypatch):
    candidate, baseline = tmp_path / "candidate", tmp_path / "baseline"
    for folder, label in ((candidate, "candidate"), (baseline, "baseline")):
        folder.mkdir()
        (folder / "agent.py").write_text(
            "class Agent:\n"
            "    def __init__(self, config):\n"
            "        from diagnostic_helper import LABEL\n"
            "        self.label = LABEL\n"
            "    def act(self, observation):\n"
            "        from diagnostic_helper import LABEL\n"
            "        return LABEL\n",
            encoding="utf-8",
        )
        (folder / "diagnostic_helper.py").write_text(f"LABEL = {label!r}\n", encoding="utf-8")
    seen = []

    def replay(agent_class, task, entropy, episode, trace_path):
        agent = agent_class({})
        expected = "candidate" if trace_path.name.startswith("candidate-") else "baseline"
        assert agent.label == expected
        assert agent.act({}) == expected
        seen.append((expected, episode))
        return {
            "cost_usd": 0,
            "zero_flow_weeks": 0,
            "weeks": 1,
            "reason_counts": {},
            "unmet_reason_counts": {},
            "planner_issue_counts": {},
        }

    monkeypatch.setattr(diagnose, "run_episode", replay)
    output = tmp_path / "diagnostics"
    diagnose.main(str(candidate), against=str(baseline), episodes=2, out=str(output))
    assert seen == [("candidate", 0), ("baseline", 0), ("candidate", 1), ("baseline", 1)]
    summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    assert summary["status"] == "completed"
    assert summary["submissions_unchanged"] is True
