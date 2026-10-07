"""Weekly reports must align scenarios, preserve missing data and avoid mutations."""

import copy
import importlib.util
import json
from pathlib import Path

import pytest

from sbf_starter.team_timeline import build_timeline, episode_timeline, execution_totals, render_timeline, weekly_pair


def record(week=1, cost=10, shed=1, served=8):
    return {
        "week": week,
        "step_cost_usd": cost,
        "salvage_usd": 0,
        "requested_flows": [{"slot_id": 0, "commodity": "fuel", "unit": "GWh", "requested_quantity": 5}],
        "outcomes": {
            "cost_components_usd": {"shortage": cost - 2, "shed": 2},
            "shed_gwh": [{"node": "grid", "quantity": shed}],
            "sinks": [
                {
                    "node": "market",
                    "commodity": "chip",
                    "unit": "items",
                    "demand": 10,
                    "served": served,
                    "lost": 10 - served,
                }
            ],
            "executed_flows": [
                {"slot_id": 0, "commodity": "fuel", "unit": "GWh", "executed_quantity": 4},
                {"slot_id": 1, "commodity": "fuel", "unit": "GWh", "executed_quantity": None},
            ],
        },
        "allocation": {
            "reasons": [{"code": "allocated_current_resources"}, {"code": "eta_late"}],
            "unmet_needs": [{"reason": "stock,eta,eta"}],
        },
    }


def write_diagnostics(folder, candidate=None, baseline=None):
    folder.mkdir()
    candidate = candidate or [record()]
    baseline = baseline or [record(cost=8)]
    summary = {
        "status": "completed",
        "submissions_unchanged": True,
        "settings": {"task": "small", "entropy": 123, "seed": 0},
        "submission_sha256": {"candidate": "a", "baseline": "b"},
    }
    for label, records in (("candidate", candidate), ("baseline", baseline)):
        summary[label] = [
            {"episode": 0, "weeks": len(records), "cost_usd": sum(row["step_cost_usd"] for row in records)}
        ]
        (folder / f"{label}-0.jsonl").write_text("".join(json.dumps(row) + "\n" for row in records), encoding="utf-8")
    (folder / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    return summary


def test_weekly_delta_directions_and_overlapping_reasons():
    row = weekly_pair(record(cost=15, shed=3, served=4), record(cost=10, shed=1, served=8))
    assert row["extra_cost_usd"] == 5
    assert row["grids"][0]["extra_shed_gwh"] == 2
    assert row["sinks"][0]["extra_served"] == -4
    assert row["sinks"][0]["extra_lost"] == 4
    assert row["candidate_unmet_reason_counts"] == {"stock": 1, "eta": 1}
    assert row["estimated_late_assignment_fraction"] == 1


def test_thresholds_worst_week_and_cumulative_include_salvage():
    candidate = [record(1, cost=100.001), record(2, cost=150), record(3, cost=-40)]
    candidate[2]["salvage_usd"] = 100
    baseline = [record(1, cost=100), record(2, cost=100), record(3, cost=10)]
    result = episode_timeline(candidate, baseline, threshold_usd=1)
    assert result["first_week_above_threshold"]["cost"] == 2
    assert result["first_cumulative_cost_above_threshold_week"] == 2
    assert result["worst_cost_weeks"][0]["week"] == 2
    assert result["extra_total_cost_usd"] == pytest.approx(0.001)
    assert result["weeks"][2]["extra_salvage_usd"] == 100
    assert result["onset_context_weeks"] == [1, 2, 3]


def test_hidden_positive_execution_unknown_zero_request_explicit_inference():
    row = record()
    result = execution_totals(row)[0]
    assert result["executed"] == 4 and result["zero_request_inferences"] == 1
    row["outcomes"]["executed_flows"][0]["executed_quantity"] = None
    result = execution_totals(row)[0]
    assert result["executed"] is None
    assert result["unknown_positive_request_slots"] == 1


def test_commodities_with_same_unit_never_combined():
    row = record()
    row["outcomes"]["executed_flows"][1]["commodity"] = "other_fuel"
    groups = execution_totals(row)
    assert [group["commodity"] for group in groups] == ["fuel", "other_fuel"]


def test_unknown_node_and_component_never_fabricated_zero():
    a, b = record(), record()
    a["outcomes"]["shed_gwh"][0]["quantity"] = None
    a["outcomes"]["cost_components_usd"]["shed"] = None
    a["outcomes"]["sinks"][0]["served"] = None
    result = episode_timeline([a], [b])
    assert result["grid_totals"][0]["extra_shed_gwh"] is None
    assert result["sink_totals"][0]["extra_served"] is None
    assert result["first_week_above_threshold"]["shed"] is None


@pytest.mark.parametrize("weeks", [[1, 1], [1, 3], [2, 1]])
def test_duplicate_gapped_or_reordered_weeks_rejected(weeks):
    with pytest.raises(ValueError, match="weeks"):
        episode_timeline([record(week) for week in weeks], [record(1), record(2)])


def test_different_episode_lengths_rejected():
    with pytest.raises(ValueError, match="lengths"):
        episode_timeline([record()], [record(1), record(2)])


def test_duplicate_sinks_rejected():
    a = record()
    a["outcomes"]["sinks"] *= 2
    with pytest.raises(ValueError, match="duplicate"):
        weekly_pair(a, record())


def test_scenario_demand_mismatch_rejected():
    a = record()
    a["outcomes"]["sinks"][0]["demand"] = 20
    with pytest.raises(ValueError, match="demand"):
        weekly_pair(a, record())


def test_old_traces_need_outcomes_not_new_evaluation():
    a = record()
    del a["outcomes"]
    with pytest.raises(ValueError, match="schema v2"):
        weekly_pair(a, record())


def test_nonfinite_values_rejected():
    a = record(cost=float("nan"))
    with pytest.raises(ValueError, match="finite"):
        weekly_pair(a, record())


def test_readonly_real_file_report_and_source_hashes(tmp_path):
    folder = tmp_path / "diagnostics"
    write_diagnostics(folder)
    before = {path.name: path.read_bytes() for path in folder.iterdir()}
    result = build_timeline(folder, threshold_usd=1)
    assert len(result["source_file_sha256"]) == 3
    assert result["episodes"][0]["extra_total_cost_usd"] == 2
    assert "No new score" in render_timeline(result)
    assert before == {path.name: path.read_bytes() for path in folder.iterdir()}


@pytest.mark.parametrize("case", ["failed", "changed", "episode", "cost", "weeks"])
def test_invalid_summary_rejected(tmp_path, case):
    folder = tmp_path / "diagnostics"
    summary = write_diagnostics(folder)
    if case == "failed":
        summary["status"] = "failed"
    elif case == "changed":
        summary["submissions_unchanged"] = False
    elif case == "episode":
        summary["baseline"][0]["episode"] = 1
    elif case == "cost":
        summary["baseline"][0]["cost_usd"] += 1
    else:
        summary["baseline"][0]["weeks"] += 1
    (folder / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    with pytest.raises(ValueError):
        build_timeline(folder)


@pytest.mark.parametrize("threshold", [True, -1, float("inf")])
def test_invalid_threshold_rejected(threshold):
    with pytest.raises(ValueError, match="threshold"):
        build_timeline("unused", threshold)


def test_cli_refuses_existing_source_and_submission_outputs(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "timeline_example", Path(__file__).resolve().parents[1] / "examples" / "14_team_timeline.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source = tmp_path / "diagnostics"
    write_diagnostics(source)
    with pytest.raises(FileExistsError):
        module.main(str(source), out=str(source))
    with pytest.raises(ValueError, match="source"):
        module.main(str(source), out=str(source / "new"))
    agent = tmp_path / "agent"
    agent.mkdir()
    (agent / "agent.py").write_text("# frozen\n", encoding="utf-8")
    with pytest.raises(ValueError, match="submission"):
        module.main(str(source), out=str(agent / "report"))


def test_input_records_not_mutated():
    a, b = record(), record(cost=8)
    before = copy.deepcopy((a, b))
    episode_timeline([a], [b])
    assert before == (a, b)
