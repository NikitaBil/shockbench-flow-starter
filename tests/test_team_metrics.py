"""Offline metrics preserve units, missing data and paired evidence."""

import copy
import json

import pytest

from sbf_starter.team_metrics import (
    build_report,
    comparison_metrics,
    diagnostic_metrics,
    matching_sources,
    percentile,
    render_report,
    trace_metrics,
)


def trace_record():
    return {
        "week": 2,
        "act_cpu_seconds": 0.2,
        "step_cost_usd": 90,
        "salvage_usd": 10,
        "stage_cpu_seconds": {"state": 0.01, "needs": 0.04, "allocation": 0.15},
        "requested_flows": [{"commodity_id": 0, "commodity": "fuel", "unit": "GWh", "requested_quantity": 10}],
        "state": {
            "available_stock": [
                {
                    "node": "terminal",
                    "commodity_id": 0,
                    "commodity": "fuel",
                    "unit": "GWh",
                    "quantity": {"value": None, "source": "unknown"},
                }
            ],
            "backlog": [
                {
                    "node": "market",
                    "commodity_id": 1,
                    "commodity": "chip",
                    "unit": "items",
                    "quantity": {"value": 100, "source": "observed"},
                }
            ],
            "arrivals": [{"commodity_id": 0, "quantity": {"value": 4, "source": "observed"}, "arrival_week": None}],
        },
        "needs": [{"need_id": "n1", "commodity_id": 0, "quantity": 10, "due_week": 1}],
        "allocation": {
            "unmet_needs": [{"need_id": "n1", "remaining_quantity": 4, "reason": "stock,eta_unknown"}],
            "reasons": [{"code": "allocated_current_resources"}, {"code": "eta_late"}],
            "resource_usage": [
                {"kind": "edge", "resource_index": 0, "pool": None, "unit": "GWh", "used": 10, "limit": 10}
            ],
        },
        "outcomes": {
            "executed_flows": [{"commodity": "fuel", "unit": "GWh", "requested_quantity": 10, "executed_quantity": 6}],
            "sinks": [{"commodity": "chip", "unit": "items", "demand": 100, "served": 80, "lost": 20}],
            "cost_components_usd": {"shortage": 80, "freight": 20},
            "shed_gwh": [{"node": "grid", "quantity": 3}],
        },
    }


def score_result():
    score = {
        "rss": 0.8,
        "interval": [0.76, 0.85],
        "quick": False,
        "fallback_weeks": 0,
        "cost_usd": 100,
        "per_episode": [
            {"episode": 0, "stratum": 1, "J_policy_cents": 10000, "J_naive_cents": 20000, "J_clairvoyant_cents": 5000}
        ],
    }
    baseline = copy.deepcopy(score)
    baseline.update(rss=0.5, cost_usd=130)
    baseline["per_episode"][0]["J_policy_cents"] = 13000
    return {
        "status": "completed",
        "settings": {"task": "small", "entropy": 123, "cpu_budget": True},
        "submission_sha256": {"candidate": "a", "baseline": "b"},
        "comparison": {"a": score, "b": baseline, "difference": 0.3, "interval": [0.2, 0.4]},
    }


def test_unit_safe_quantities_missing_stock_and_salvage():
    metrics = trace_metrics([trace_record()], 0.1)
    rows = {row["commodity"]: row for row in metrics["commodity_metrics"]}
    assert rows["fuel"]["execution_fraction"] == 0.6
    assert rows["fuel"]["planned_unmet_fraction"] == 0.4
    assert rows["fuel"]["overdue_need_quantity"] == 10
    assert rows["chip"]["demand_service_fraction"] == 0.8
    assert rows["chip"]["execution_fraction"] is None
    assert rows["fuel"]["unmet_reason_quantity/stock"] == 4
    assert rows["fuel"]["unmet_reason_quantity/eta_unknown"] == 4
    stock = next(row for row in metrics["node_metrics"] if row["field"] == "available_stock")
    assert stock["mean_known_quantity"] is None and stock["unknown_records"] == 1
    assert metrics["cost_reconciliation_residual_usd"] == 0
    assert metrics["cpu_local"]["first_week_with_constructor_seconds"] == pytest.approx(0.3)
    assert metrics["resource_metrics"][0]["saturated_records"] == 1
    assert metrics["transport_metrics"][0]["unknown_timing_records"] == 1
    assert metrics["shed_by_grid"][0]["observed_shed_gwh"] == 3


def test_old_trace_missing_outcomes_are_unknown_not_zero():
    record = trace_record()
    del record["outcomes"]
    del record["stage_cpu_seconds"]
    metrics = trace_metrics([record])
    assert metrics["cost_components_usd"] is None
    assert metrics["cost_reconciliation_residual_usd"] is None
    assert metrics["outcome_weeks"] == 0
    assert metrics["cpu_local"]["stages"] == {}
    assert all(row["execution_fraction"] is None for row in metrics["commodity_metrics"])


def test_partial_visibility_never_fills_hidden_execution_or_costs():
    record = trace_record()
    record["outcomes"]["executed_flows"][0]["executed_quantity"] = None
    record["outcomes"]["cost_components_usd"]["freight"] = None
    metrics = trace_metrics([record])
    assert metrics["commodity_metrics"][1]["execution_fraction"] is None
    assert metrics["cost_components_usd"] == {"shortage": 80}
    # Missing component prevents full cost reconciliation.
    assert metrics["cost_reconciliation_residual_usd"] is None


def test_empty_and_single_percentiles():
    assert percentile([], 0.95) is None
    assert percentile([2], 0.95) == 2
    assert percentile([0, 10], 0.95) == 9.5


def test_target_gate_only_proposes_holdout_not_leaderboard_promotion():
    result = comparison_metrics(score_result())
    assert result["gate"] == "candidate_for_holdout_validation"
    assert result["score_gap_to_target"] == 0
    assert result["episode_win_fraction"] == 1
    assert result["worst_episodes"][0]["extra_cost_usd"] == -30


@pytest.mark.parametrize("case", ["quick", "cpu", "fallback", "score", "interval", "difference"])
def test_bad_evidence_blocks_target_gate(case):
    result = score_result()
    candidate = result["comparison"]["a"]
    if case == "quick":
        candidate["quick"] = True
    elif case == "cpu":
        result["settings"]["cpu_budget"] = False
    elif case == "fallback":
        candidate["fallback_weeks"] = 1
    elif case == "score":
        candidate["rss"] = 0.7
    elif case == "interval":
        candidate["interval"] = [0.7, 0.9]
    else:
        result["comparison"]["interval"] = [-0.1, 0.4]
    assert comparison_metrics(result)["gate"] == "do_not_promote"


def test_failed_results_not_scored():
    assert comparison_metrics({"status": "failed"})["usable"] is False
    assert comparison_metrics({"status": "running", "comparison": None})["usable"] is False


def test_mismatching_reference_costs_rejected():
    result = score_result()
    result["comparison"]["b"]["per_episode"][0]["J_naive_cents"] += 1
    with pytest.raises(ValueError, match="references"):
        comparison_metrics(result)


def test_nonfinite_score_cannot_pass_target_gate():
    result = score_result()
    result["comparison"]["a"]["rss"] = float("nan")
    with pytest.raises(ValueError, match="nonfinite"):
        comparison_metrics(result)


def test_resource_unknown_and_zero_limits_never_divide_by_zero():
    record = trace_record()
    record["allocation"]["resource_usage"] += [
        {"kind": "stock", "resource_index": 0, "unit": "GWh", "used": 0, "limit": None},
        {"kind": "edge", "resource_index": 1, "unit": "GWh", "used": 1, "limit": 0},
    ]
    metrics = trace_metrics([record])
    resources = {(row["kind"], row["index"]): row for row in metrics["resource_metrics"]}
    assert resources["stock", 0]["unknown_limit_records"] == 1
    assert resources["stock", 0]["mean_utilization"] is None
    assert resources["edge", 1]["zero_limit_records"] == 1
    assert resources["edge", 1]["over_limit_records"] == 1


def test_sources_linked_only_for_equal_hashes_task_and_root():
    score = comparison_metrics(score_result())
    diagnostic = {
        "usable": True,
        "settings": {"task": "small", "entropy": 123},
        "submission_sha256": {"candidate": "a", "baseline": "b"},
    }
    assert matching_sources(score, diagnostic)
    diagnostic["submission_sha256"]["candidate"] = "other"
    assert not matching_sources(score, diagnostic)


def test_offline_report_does_not_change_inputs(tmp_path):
    result_path = tmp_path / "result.json"
    result_path.write_text(json.dumps(score_result()), encoding="utf-8")
    diagnostic_folder = tmp_path / "diagnostics"
    diagnostic_folder.mkdir()
    record = trace_record()
    trace = diagnostic_folder / "candidate-0.jsonl"
    trace.write_text(json.dumps(record) + "\n", encoding="utf-8")
    summary = {
        "status": "completed",
        "submissions_unchanged": True,
        "settings": {"task": "small", "entropy": 123},
        "submission_sha256": {"candidate": "a", "baseline": "b"},
        "candidate": [{"episode": 0, "weeks": 1, "cost_usd": 90}],
        "baseline": [],
    }
    (diagnostic_folder / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    before = trace.read_bytes(), result_path.read_bytes()
    result = build_report([result_path], [diagnostic_folder])
    text = render_report(result)
    assert "0.7500" in text and "not guarantees" in text
    assert result["diagnostics"][0]["matching_score_sources"] == [str(result_path.resolve())]
    assert before == (trace.read_bytes(), result_path.read_bytes())
    summary["candidate"][0]["weeks"] = 2
    (diagnostic_folder / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    with pytest.raises(ValueError, match="trace and summary"):
        diagnostic_metrics(diagnostic_folder)


@pytest.mark.parametrize("target", [True, -1, 2, float("nan")])
def test_invalid_targets(target):
    with pytest.raises(ValueError, match="target"):
        build_report(["unused"], target=target)
