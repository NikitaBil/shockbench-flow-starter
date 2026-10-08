"""Route preference modifiers preserve masks, units and baseline defaults."""

import importlib
import sys
from types import SimpleNamespace

import numpy as np
import pytest

from tests.conftest import ROOT


@pytest.fixture
def policy_type(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "agents/team_agent"))
    monkeypatch.delitem(sys.modules, "dispatch", raising=False)
    return importlib.import_module("dispatch").RoutePreferences


def case():
    routes = tuple(
        SimpleNamespace(slot_id=i, edge_id=i, source_node=0, destination_node=1 if i < 2 else 2,
                        commodity_id=0, edges=(i,), nominal_freight_per_unit=c)
        for i, c in enumerate((1.0, 10.0, 50.0))
    )
    config = {"static": {"edges": {"id": ["sea.a", "air.b", "sea.c"]}}}
    network = SimpleNamespace(routes=routes, edge_transit_weeks=(2, 0, 1), commodity_names=("chip",))
    obs = {
        "graph_now.tau": np.asarray([2, 0, 1]), "graph_now.tau.observed": np.ones(3),
        "graph_now.c": np.asarray([1.0, 10.0, 50.0]), "graph_now.c.observed": np.ones(3),
    }
    return config, network, obs


def test_default_is_exact_baseline_identity(policy_type):
    config, network, obs = case()
    flows = np.asarray([100.0, 100.0, 100.0])
    assert policy_type(config, network).apply(flows, obs) is flows


def test_price_preference_does_not_mix_destinations_or_amplify(policy_type):
    config, network, obs = case()
    flows = np.asarray([100.0, 100.0, 100.0])
    out = policy_type(config, network, cost_bias=2.0).apply(flows, obs)
    assert out[0] == 100 and 0 < out[1] < out[0] and out[2] == 100
    np.testing.assert_array_equal(flows, [100, 100, 100])
    assert np.all(out <= flows)


def test_commodity_transit_preference_overrides_only_its_own_time_weight(policy_type):
    config, network, obs = case()
    flows = np.full(3, 100.)
    out = policy_type(config, network, commodity_transit_bias={"chip": 0.5}).apply(flows, obs)
    np.testing.assert_allclose(out, [100 * np.exp(-1), 100, 100])
    disabled = policy_type(config, network, transit_bias=0.5, commodity_transit_bias={"chip": 0})
    np.testing.assert_array_equal(disabled.apply(flows, obs), flows)


@pytest.mark.parametrize("mapping", [[], {"chip": True}, {"chip": -1}, {"chip": float("nan")}, {"other": 1}])
def test_invalid_commodity_transit_preference_is_rejected(policy_type, mapping):
    config, network, _ = case()
    with pytest.raises(ValueError, match="commodity_transit_bias"):
        policy_type(config, network, commodity_transit_bias=mapping)


def test_warning_signal_uses_config_mapping_and_only_modifies_competing_routes(policy_type):
    config, network, obs = case()
    config["layout"] = {"warning_units": [["chokepoint", 11], ["region", 5], ["chokepoint", 10]]}
    for route, chokes in zip(network.routes, [(10,), (11,), ()], strict=True):
        route.chokepoints = chokes
    obs["warning.score"] = np.asarray([0., 2., 2.])
    obs["warning.score.observed"] = np.ones(3)
    flows = np.full(3, 100.)
    policy = policy_type(config, network, warning_bias=1)
    np.testing.assert_allclose(policy.apply(flows, obs), [100 * np.exp(-2), 100, 100])
    obs["warning.score.observed"][2] = 0
    obs["warning.score"][2] = np.nan
    np.testing.assert_array_equal(policy.apply(flows, obs), flows)


def test_warning_memory_is_causal_idempotent_and_ignores_hidden_padding(policy_type):
    config, network, obs = case()
    config["layout"] = {"warning_units": [["chokepoint", 11], ["chokepoint", 10]]}
    for route, chokes in zip(network.routes, [(10,), (11,), ()], strict=True):
        route.chokepoints = chokes
    obs.update({"week": np.asarray([1]), "warning.score": np.asarray([0., 2.]),
                "warning.score.observed": np.ones(2)})
    policy = policy_type(config, network, warning_bias=1, warning_memory=0.75)
    flows = np.full(3, 100.)
    np.testing.assert_allclose(policy.apply(flows, obs), [100 * np.exp(-2), 100, 100])
    obs["week"][:] = 2
    obs["warning.score"][1] = 0
    expected = [100 * np.exp(-1.5), 100, 100]
    np.testing.assert_allclose(policy.apply(flows, obs), expected)
    np.testing.assert_allclose(policy.apply(flows, obs), expected)
    obs["week"][:] = 4
    obs["warning.score.observed"][1] = 0
    obs["warning.score"][1] = np.nan
    np.testing.assert_array_equal(policy.apply(flows, obs), flows)
    obs["warning.score.observed"][1] = 1
    obs["warning.score"][1] = 0
    np.testing.assert_allclose(policy.apply(flows, obs), [100 * np.exp(-1.5 * 0.75 ** 2), 100, 100])
    obs["week"][:] = 1
    np.testing.assert_array_equal(policy.apply(flows, obs), flows)


@pytest.mark.parametrize("memory", [True, -0.1, 1, float("nan"), "0.5"])
def test_invalid_warning_memory_is_rejected(policy_type, memory):
    config, network, _obs = case()
    with pytest.raises(ValueError, match="warning_memory"):
        policy_type(config, network, warning_memory=memory)


def test_hidden_prices_use_nominal_proxy_not_padding(policy_type):
    config, network, obs = case()
    obs["graph_now.c.observed"][:] = 0
    prefs = policy_type(config, network, cost_bias=2.0)
    flows = np.ones(3)
    expected = prefs.apply(flows, obs)
    obs["graph_now.c"][:] = [np.nan, 1e100, -10]
    np.testing.assert_array_equal(expected, prefs.apply(flows, obs))


def test_forbidden_or_closed_zero_slot_is_never_reactivated(policy_type):
    config, network, obs = case()
    flows = np.asarray([0.0, 100.0, 0.0])
    np.testing.assert_array_equal(flows, policy_type(config, network, cost_bias=8.0).apply(flows, obs))


def test_transit_preference_is_not_an_on_time_claim(policy_type):
    config, network, obs = case()
    flows = np.ones(3)
    out = policy_type(config, network, transit_bias=1.0).apply(flows, obs)
    assert out[0] < out[1] == 1
    assert out[2] == 1


def test_commodity_override_can_keep_fast_option_without_touching_other_destinations(policy_type):
    config, network, obs = case()
    flows = np.ones(3)
    out = policy_type(config, network, cost_bias=4, commodity_cost_bias={"chip": 0}).apply(flows, obs)
    np.testing.assert_array_equal(out, flows)


def test_absent_zero_preference_is_portable_between_network_sizes(policy_type):
    config, network, obs = case()
    flows = np.ones(3)
    out = policy_type(config, network, commodity_cost_bias={"not_in_this_network": 0}).apply(flows, obs)
    np.testing.assert_array_equal(out, flows)


def pending_case():
    config, network, obs = case()
    network.routes[0].edges = (0, 2)
    obs["week"] = np.asarray([1])
    for key, value in (("edge", 2), ("k", 0), ("effective_week", 3)):
        obs["pending_prohibitions." + key] = np.asarray([value, np.nan])
        obs["pending_prohibitions." + key + ".observed"] = np.asarray([1, 0])
    return config, network, obs


def test_pending_ban_on_later_edge_favors_other_route_without_amplifying(policy_type):
    config, network, obs = pending_case()
    out = policy_type(config, network, pending_bias=1).apply(np.ones(3), obs)
    np.testing.assert_allclose(out, [np.exp(-1), 1, 1])


def test_future_ban_on_current_dispatch_edge_does_not_cancel_departure(policy_type):
    config, network, obs = pending_case()
    obs["pending_prohibitions.edge"][0] = 0
    np.testing.assert_array_equal(policy_type(config, network, pending_bias=4).apply(np.ones(3), obs), np.ones(3))


@pytest.mark.parametrize("hidden", ["edge", "k", "effective_week"])
def test_incomplete_pending_message_does_not_use_hidden_numeric_padding(policy_type, hidden):
    config, network, obs = pending_case()
    obs["pending_prohibitions." + hidden + ".observed"][:] = 0
    obs["pending_prohibitions." + hidden][:] = np.nan
    np.testing.assert_array_equal(policy_type(config, network, pending_bias=4).apply(np.ones(3), obs), np.ones(3))


def test_queue_route_preference_uses_visible_pool_load_without_claiming_eta(policy_type):
    config, network, obs = case()
    config.update(T=26, layout={"chokepoints": [3], "lot_keys": [(3, 0, None, 0)]})
    config["static"]["commodities"] = {"pool": ["tb"]}
    for route in network.routes:
        route.pool, route.chokepoints = "tb", (3,) if route.slot_id == 0 else ()
    obs.update({"week": np.asarray([1]), "queue_lots.qty": np.asarray([[100., np.nan]]),
                "queue_lots.qty.observed": np.asarray([[1, 0]]),
                "pipeline.qty.observed": np.zeros(1), "graph_now.kappa.tb": np.asarray([10.]),
                "graph_now.kappa.tb.observed": np.ones(1)})
    policy = policy_type(config, network, queue_bias=0.1)
    result = policy.apply(np.ones(3), obs)
    assert result[0] == pytest.approx(np.exp(-1))
    np.testing.assert_array_equal(result[1:], [1, 1])
    obs["queue_lots.qty.observed"][:] = 0
    obs["queue_lots.qty"][:] = np.nan
    np.testing.assert_array_equal(policy.apply(np.ones(3), obs), np.ones(3))


@pytest.mark.parametrize("settings", [{"not_a_good": 1}, {"chip": -1}, {"chip": float("nan")}, []])
def test_invalid_commodity_weights_rejected(policy_type, settings):
    config, network, _ = case()
    with pytest.raises(ValueError, match="commodity_cost_bias"):
        policy_type(config, network, commodity_cost_bias=settings)


@pytest.mark.parametrize("name", ["transit_bias", "cost_bias", "air_fraction"])
@pytest.mark.parametrize("value", [True, -1, float("nan"), float("inf"), "1"])
def test_invalid_settings_are_rejected(policy_type, name, value):
    config, network, _obs = case()
    with pytest.raises(ValueError, match=name):
        policy_type(config, network, **{name: value})
