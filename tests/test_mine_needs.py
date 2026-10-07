"""Inventory projection regressions for the legacy candidate forecaster."""

import importlib
import sys
from types import SimpleNamespace

import numpy as np
import pytest

from tests.conftest import ROOT


@pytest.fixture
def mine_api(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "agents" / "mine"))
    for name in ("needs", "contracts"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    needs = importlib.import_module("needs")
    contracts = importlib.import_module("contracts")
    return SimpleNamespace(needs=needs, contracts=contracts)


def test_on_hand_pipeline_and_wip_arrivals_are_consumed_once(mine_api):
    c = mine_api.contracts
    config = {
        "T": 3,
        "static": {
            "nodes": {"id": ["sink"], "type": ["sink"]},
            "commodities": {"id": ["chip_le"], "v": [1.0]},
            "sinks": {"node": [0], "k": [0], "pi": [100.0]},
            "instance": {"nodes": []},
        },
        "layout": {"demands": [[0, 0]]},
    }
    snapshot = c.StateSnapshot(week=1, horizon_T=3)
    snapshot.stock[0, 0] = c.StockItem(0, "sink", 0, "chip_le", 2.0, True)
    snapshot.backlog[0, 0] = c.BacklogItem(0, "sink", 0, "chip_le", 0.0, True, 100.0)
    # 2 on hand + 3 pipeline units meet week 1. A separate WIP arrival meets
    # week 2. Neither may be added again to later weeks' projected coverage.
    snapshot.arrival_calendar[0, 0, 1] = c.ArrivalEntry(0, 0, 1, confirmed_qty=3.0)
    snapshot.arrival_calendar[0, 0, 2] = c.ArrivalEntry(0, 0, 2, estimated_qty=3.0)
    observation = {
        "demand_forecast.qty": np.asarray([[5.0, 3.0, 3.0]]),
        "demand_forecast.qty.observed": np.ones((1, 3), dtype=int),
    }

    needs = mine_api.needs.NeedsForecaster(config).generate_delivery_needs(observation, snapshot)

    assert [(need.due_week, need.qty, need.reason) for need in needs] == [(3, 3.0, "market_demand")]


def test_arrival_before_zero_demand_is_carried_forward_once(mine_api):
    c = mine_api.contracts
    config = {
        "T": 3,
        "static": {
            "nodes": {"id": ["sink"], "type": ["sink"]},
            "commodities": {"id": ["chip_le"], "v": [1.0]},
            "sinks": {"node": [0], "k": [0], "pi": [100.0]},
            "instance": {"nodes": []},
        },
        "layout": {"demands": [[0, 0]]},
    }
    snapshot = c.StateSnapshot(week=1, horizon_T=3)
    snapshot.stock[0, 0] = c.StockItem(0, "sink", 0, "chip_le", 0.0, True)
    snapshot.backlog[0, 0] = c.BacklogItem(0, "sink", 0, "chip_le", 0.0, True, 100.0)
    snapshot.arrival_calendar[0, 0, 1] = c.ArrivalEntry(0, 0, 1, confirmed_qty=5.0)
    observation = {
        "demand_forecast.qty": np.asarray([[0.0, 5.0, 5.0]]),
        "demand_forecast.qty.observed": np.ones((1, 3), dtype=int),
    }

    needs = mine_api.needs.NeedsForecaster(config).generate_delivery_needs(observation, snapshot)

    assert [(need.due_week, need.qty) for need in needs] == [(3, 5.0)]
