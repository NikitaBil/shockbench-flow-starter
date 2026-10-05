"""V2: sanctions, closures, throughput and blackout estimates stay distinct."""

import copy

import numpy as np
import pytest

from agents.team_agent.network import DataSource, NetworkTracker
from tests.test_network import make_config


@pytest.fixture
def current_config():
    cfg = make_config()
    cfg["T"] = 12
    cfg["static"]["commodities"]["v"] = [100.0, 200.0]
    cfg["static"]["instance"] = {
        "prohibitions_at_reset": [],
        "nodes": [
            {
                "id": name,
                "chokepoint": {
                    "k_c": 1.0,
                    "mu": {"tb": 10.0, "ct": 4.0},
                    "war_risk_cost": {"fuel": [0.0, 7.0, 11.0], "wafer": [0.0, 3.0, 5.0]},
                },
            }
            for name in cfg["static"]["nodes"]["id"]
        ],
    }
    return cfg


def observation(cfg, week=1):
    tracker = NetworkTracker(cfg)
    obs = {"week": np.array([week])}
    for key, nominal in tracker.nominal.items():
        obs[key] = np.nan_to_num(nominal.copy())
        obs[key + ".observed"] = np.ones((1,) if key == "action_mask" else nominal.shape, dtype=np.int8)
    obs["graph_now.u.observed"][5] = 0  # Grid coupling has no cargo capacity.
    return obs


def hide_external(obs):
    for key in list(obs):
        if key.endswith(".observed"):
            obs[key][:] = 0
        elif key != "week":
            obs[key][:] = 1 if key == "action_mask" else 0


def test_partial_openness_is_not_applied_twice(current_config):
    obs = observation(current_config)
    obs["graph_now.open"][0] = 0.5
    obs["graph_now.kappa.tb"][0] = 5.0
    state = NetworkTracker(current_config).update(obs)
    assert state.routes[0].snapshot_throughput == 5.0  # Not 5 * 0.5.
    assert state.routes[0].sanction_allowed
    assert state.routes[0].closed_chokepoints == ()


def test_closed_route_can_be_legally_allowed_and_entry_can_still_work(current_config):
    obs = observation(current_config)
    obs["graph_now.open"][0] = 0
    obs["graph_now.kappa.tb"][0] = 0
    state = NetworkTracker(current_config).update(obs)
    assert state.routes[0].sanction_allowed
    assert state.routes[0].entry_capacity == 10
    assert state.routes[0].snapshot_throughput == 0
    assert state.routes[0].closed_chokepoints == (2,)
    assert state.routes[0].zero_capacity_edges == ()


def test_downstream_sanction_blocks_lane_not_its_alternative(current_config):
    obs = observation(current_config)
    obs["graph_now.prohibited"][2, 0] = 1
    obs["action_mask"][0] = 0
    state = NetworkTracker(current_config).update(obs)
    assert not state.routes[0].sanction_allowed and state.routes[0].permission_observed
    assert state.routes[1].sanction_allowed
    assert state.routes[0].entry_capacity > 0


def test_blackout_uses_bounded_history_and_never_confirms_hidden_zero(current_config):
    tracker = NetworkTracker(current_config, max_history_age=1)
    first = observation(current_config)
    first["graph_now.c"][0] = 9
    first["action_mask"][0] = 0
    first["graph_now.prohibited"][2, 0] = 1
    tracker.update(first)
    hidden = observation(current_config, week=2)
    hide_external(hidden)
    state = tracker.update(hidden)
    assert state.fields["graph_now.c"].values[0] == 9
    assert state.fields["graph_now.c"].source[0] == DataSource.HISTORY
    assert state.fields["graph_now.c"].age_weeks[0] == 1
    assert not state.routes[0].sanction_allowed
    assert not state.routes[0].permission_observed
    assert state.fields["graph_now.open"].values[0] == 1  # Hidden zero is not closure.
    hidden["week"][0] = 3
    expired = tracker.update(hidden)
    assert expired.fields["graph_now.c"].values[0] == 1
    assert expired.fields["graph_now.c"].source[0] == DataSource.NOMINAL
    assert expired.routes[0].sanction_allowed and not expired.routes[0].permission_observed


def test_visible_mask_overrides_stale_prohibition_and_snapshots_are_independent(current_config):
    tracker = NetworkTracker(current_config)
    first = observation(current_config)
    first["graph_now.prohibited"][2, 0] = 1
    first["action_mask"][0] = 0
    old = tracker.update(first)
    new = observation(current_config, week=2)
    new["graph_now.prohibited.observed"][:] = 0
    before = copy.deepcopy(new)
    state = tracker.update(new)
    assert state.routes[0].sanction_allowed and state.routes[0].permission_observed
    assert not old.routes[0].sanction_allowed
    for key in new:
        np.testing.assert_array_equal(new[key], before[key])
    with pytest.raises(ValueError):
        state.fields["graph_now.c"].values[0] = 999


def test_hidden_kappa_is_derived_from_openness_with_quality_label(current_config):
    obs = observation(current_config)
    obs["graph_now.open"][0] = 0.5
    obs["graph_now.kappa.tb.observed"][0] = 0
    state = NetworkTracker(current_config).update(obs)
    field = state.fields["graph_now.kappa.tb"]
    assert field.values[0] == 5 and not field.observed[0]
    assert field.source[0] == DataSource.DERIVED


def test_invalid_observed_values_do_not_poison_memory(current_config):
    tracker = NetworkTracker(current_config)
    obs = observation(current_config)
    obs["graph_now.c"][0] = np.nan
    with pytest.raises(ValueError, match="finite"):
        tracker.update(obs)
    state = tracker.update(observation(current_config))
    assert state.week == 1
