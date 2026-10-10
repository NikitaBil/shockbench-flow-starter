"""Regressions for the v15 receiver constraints and production-planner wiring."""

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from agents.team_agent_receiver_overflow_v15.receiver_overflow import ReceiverOverflow


ROOT = Path(__file__).resolve().parents[1]
AGENT = ROOT / "agents" / "team_agent_receiver_overflow_v15"


def test_receiver_overflow_shares_storage_across_arrival_cohorts():
    pair = (2, 1)
    model = ReceiverOverflow.__new__(ReceiverOverflow)
    model.enabled = True
    model.horizon = 3
    model.last_limited = ()
    model.stocks = {pair: 0}
    model.inputs = {pair: (10.0, 0.0)}
    model.groups = {(pair, 1): (0,), (pair, 2): (1,)}
    model.network = SimpleNamespace(edge_head=())
    observation = {
        "week": np.array([1]),
        "stock.qty": np.array([0.0]),
        "stock.qty.observed": np.array([1]),
    }
    for field in ("qty", "edge", "k", "arrival_week"):
        observation[f"pipeline.{field}"] = np.zeros(0)
        observation[f"pipeline.{field}.observed"] = np.zeros(0, dtype=np.int8)

    actual = model.apply(np.array([10.0, 10.0]), observation)

    # The first cohort fills storage. With zero production, none of the later
    # cohort can be admitted; independent per-delay budgets admitted both.
    np.testing.assert_allclose(actual, [10.0, 0.0])
    assert model.last_limited == ({"pair": pair, "arrival_week": 3, "removed": 10.0},)


def test_v15_keeps_unproven_planner_opt_in_and_tracks_full_forecast_horizon():
    params = json.loads((AGENT / "params.json").read_text())

    assert params["allocation_enabled"] is False
    assert params["queue_eta_enabled"] is False
    assert params["queue_forecast_enabled"] is False
    assert params["planner_options"]["production_horizon"] == 8
    assert params["planner_options"]["include_estimated_arrivals"] is False


def test_queue_forecast_arrivals_are_selective_and_require_forecaster(monkeypatch):
    monkeypatch.syspath_prepend(str(AGENT))
    spec = importlib.util.spec_from_file_location("v15_needs_test", AGENT / "needs.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    planner = module.NeedPlanner.__new__(module.NeedPlanner)
    planner.include_estimated_arrivals = False
    planner.include_queue_forecast_arrivals = True
    queue_arrival = SimpleNamespace(
        source="estimated", source_kind="queue", quantity=SimpleNamespace(source="estimated")
    )
    wip_arrival = SimpleNamespace(source="estimated", source_kind="wip", quantity=SimpleNamespace(source="estimated"))

    assert planner._arrival_is_eligible(SimpleNamespace(queue_forecast=object()), queue_arrival)
    assert not planner._arrival_is_eligible(SimpleNamespace(queue_forecast=None), queue_arrival)
    assert not planner._arrival_is_eligible(SimpleNamespace(queue_forecast=object()), wip_arrival)


def test_recovery_does_not_reserve_late_baseline_cargo_against_early_window(monkeypatch):
    import scipy.optimize

    monkeypatch.syspath_prepend(str(AGENT))
    spec = importlib.util.spec_from_file_location("v15_recovery_test", AGENT / "production_recovery.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    class Route:
        def __init__(self, slot_id, source_node, edge_id, tau):
            self.slot_id = slot_id
            self.source_node = source_node
            self.destination_node = 1
            self.commodity_id = 0
            self.edge_id = edge_id
            self.edges = (edge_id,)
            self.lane_id = None
            self.nominal_freight_per_unit = 0.0
            self.tau = tau

    routes = (Route(0, 0, 0, 1), Route(1, 2, 1, 5))
    target = (1, 0)
    model = SimpleNamespace(
        production_times={target: 0},
        penalties={target: 100.0},
        production_rate=lambda _pair, _obs: 1.0,
    )
    recovery = module.ProductionRecovery.__new__(module.ProductionRecovery)
    recovery.enabled = True
    recovery.cover = 4.0
    recovery.last_diagnostics = {}
    recovery.delay = SimpleNamespace(
        begin=lambda _obs: None,
        route_delay=lambda route, _obs, nominal: {"delay_weeks": route.tau if route.tau else nominal},
    )
    recovery.models = [SimpleNamespace(_arrivals=lambda _obs: {})]
    recovery.slots = (0,)
    recovery.targets = {target: model}
    recovery.stocks = {(0, 0): 0, target: 1, (2, 0): 2}
    recovery.network = SimpleNamespace(
        routes=routes,
        slots_from={(0, 0): (0,), (2, 0): (1,)},
        edges_to_slots=((0,), (1,)),
        commodity_names=("raw",),
    )
    recovery.horizon = 10
    recovery.storage = {target: 20.0}
    recovery.values = np.array([1.0])
    recovery.cover_model = SimpleNamespace(production_rate=lambda _pair, _obs: 1.0)
    observed = np.ones(2, dtype=np.int8)
    observation = {
        "week": np.array([1]),
        "stock.qty": np.array([10.0, 0.0, 10.0]),
        "stock.qty.observed": np.ones(3, dtype=np.int8),
        "graph_now.u": np.array([100.0, 100.0]),
        "graph_now.u.observed": observed,
        "graph_now.tau": np.array([1.0, 5.0]),
        "graph_now.tau.observed": observed,
        "graph_now.c": np.zeros(2),
        "graph_now.c.observed": observed,
        "graph_now.tariff": np.zeros((2, 1)),
        "graph_now.tariff.observed": np.ones((2, 1), dtype=np.int8),
    }
    captured = {}

    def fake_linprog(_objective, *, A_ub, b_ub, bounds, method):
        captured["A_ub"] = A_ub
        captured["b_ub"] = b_ub
        return SimpleNamespace(success=False, status=1)

    monkeypatch.setattr(scipy.optimize, "linprog", fake_linprog)
    recovery.apply(np.array([0.0, 5.0]), np.array([10.0, 5.0]), observation)

    # The baseline route arrives five weeks later, so it must not consume the
    # early one-week receiver window's five units of headroom.
    assert captured["b_ub"][-1] == 5.0
    assert captured["A_ub"][-1, 0] == 1.0
