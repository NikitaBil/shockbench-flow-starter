"""Diagnostic counts identify records, unique requests and immutable replay inputs."""

import importlib
import json


diagnostics = importlib.import_module("examples.11_allocator_diagnostics")


def test_budget_records_are_not_reported_as_unique_needs():
    event = dict(
        week=1,
        need_id="n",
        slot_id=2,
        stage="eta",
        reason="queue_eta_forecast_budget_exhausted",
        quantity=3,
        source_stock=5,
        entry_capacity=5,
        permission_observed=True,
        fleet_weight=0,
        fleet_remaining=0,
        calls_before=16,
        calls_after=16,
    )
    events = [event, dict(event), dict(event, slot_id=3), dict(event, week=2)]
    report = diagnostics.summarize(events)
    assert report["budget_denial_records"] == 4
    assert report["budget_denial_unique_week_needs"] == 2
    assert report["budget_denial_unique_week_need_slots"] == 3
    assert report["budget_denials_with_dispatchable_resources"] == 4
    assert report["executed_forecasts"] == 0


def test_diagnostic_freeze_never_edits_input_params(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "agent.py").write_text("class Agent: pass\n")
    original = '{"allocation_enabled": false, "queue_eta_enabled": false}\n'
    (source / "params.json").write_text(original)
    source_hash = diagnostics.fingerprint(source)
    observed_params = []

    def replay(folder, *args):
        observed_params.append(json.loads((folder / "params.json").read_text()))
        return {"cost_usd": 0, "max_cpu_s": 0, "summary": {}}

    monkeypatch.setattr(diagnostics, "replay", replay)
    out = tmp_path / "run"
    diagnostics.main(candidate=str(source), baseline=str(source), out=str(out))
    assert observed_params == [
        {"allocation_enabled": False, "queue_eta_enabled": False},
        {"allocation_enabled": True, "queue_eta_enabled": True},
    ]
    assert (source / "params.json").read_text() == original
    assert diagnostics.fingerprint(source) == source_hash
    report = json.loads((out / "report.json").read_text())
    assert report["status"] == "completed" and not report["sources_changed_since_freeze"]
