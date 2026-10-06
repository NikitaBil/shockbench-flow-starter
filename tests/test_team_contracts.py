"""Integration boundary tests with test doubles, not teammate implementations."""

import sys
from types import SimpleNamespace

import gymnasium as gym
import numpy as np
import pytest
from shockbench_flow_gym import agent_config_from_reset

from sbf_starter import env_id
from sbf_starter.agents import load
from tests.conftest import ROOT


@pytest.fixture(scope="module")
def small_reset():
    env = gym.make(env_id("small"), entropy=12345)
    try:
        observation, info = env.reset(seed=0, options={"episode": 0})
        yield agent_config_from_reset(env, observation, info), observation
    finally:
        env.close()


@pytest.fixture
def api(small_reset):
    agent_class = load(ROOT / "agents" / "team_agent")
    config, observation = small_reset
    return SimpleNamespace(
        Agent=agent_class,
        c=sys.modules["contracts"],
        i=sys.modules["integration"],
        config=config,
        obs={key: value.copy() for key, value in observation.items()},
    )


def snapshot(api):
    return SimpleNamespace(
        week=int(api.obs["week"][0]),
        horizon=api.config["T"],
        available_stock={tuple(key): api.c.Quantity(None, "unknown") for key in api.config["layout"]["stock_slots"]},
        backlog={tuple(key): api.c.Quantity(None, "unknown") for key in api.config["layout"]["demands"]},
        pipeline=(),
        queues=(),
        arrivals=(),
    )


def need(api, need_id="n1", **changes):
    node, commodity = api.config["layout"]["demands"][0]
    fields = dict(
        need_id=need_id,
        destination_node=node,
        commodity_id=commodity,
        quantity=5.0,
        due_week=1,
        priority=1.0,
        reason="backlog",
        shortage_cost_per_unit_usd=None,
        confidence=None,
    )
    fields.update(changes)
    return SimpleNamespace(**fields)


def pipeline(api, state=None, needs=None, result=None, events=None):
    state = snapshot(api) if state is None else state
    needs = (need(api),) if needs is None else needs
    if result is None:
        result = api.c.AllocationResult(np.zeros(api.config["spaces"]["action"]["flows"]["shape"]))
    events = [] if events is None else events

    def build(observation, network):
        events.append(("state", observation, network))
        return state

    def plan(received_state, observation, network):
        assert received_state is state
        events.append(("needs", observation, network))
        return needs

    def allocate(received_state, received_needs, observation, network):
        assert received_state is state
        events.append(("allocation", observation, network, received_needs))
        return result

    return api.i.DecisionPipeline(
        SimpleNamespace(build=build), SimpleNamespace(plan=plan), SimpleNamespace(allocate=allocate)
    )


def test_order_inputs_diagnostics_and_action_isolation(api):
    events = []
    needs = (need(api, "low", priority=0), need(api, "b", priority=2), need(api, "a", priority=2))
    node, commodity = api.config["layout"]["stock_slots"][0]
    unit = api.config["static"]["units"][api.config["static"]["commodities"]["id"][commodity]]
    qty = np.zeros(api.config["spaces"]["action"]["flows"]["shape"])
    result = api.c.AllocationResult(
        qty,
        unmet_needs=(api.c.UnmetNeed("low", 5.0, "no_stock"),),
        resource_usage=(api.c.ResourceUsage("stock", 0, unit, 0, None, "unknown"),),
        reasons=(api.c.DecisionReason("no_stock", "Stock is hidden", "low", 0),),
    )
    agent = api.Agent(api.config, pipeline=pipeline(api, needs=needs, result=result, events=events))
    action = agent.act(api.obs)
    assert [entry[0] for entry in events] == ["state", "needs", "allocation"]
    assert all(entry[1] is api.obs and entry[2] is agent.network for entry in events)
    assert [item.need_id for item in events[-1][3]] == ["a", "b", "low"]
    assert agent.last_allocation is result
    assert action.keys() == {"flows"}
    assert action["flows"].dtype == np.float64
    assert not np.shares_memory(action["flows"], qty)
    assert node >= 0


def test_due_week_tie_break_and_overdue_not_shifted(api):
    api.obs["week"][:] = 3
    events = []
    needs = (need(api, "later", due_week=3), need(api, "overdue", due_week=1))
    agent = api.Agent(api.config, pipeline=pipeline(api, needs=needs, events=events))
    agent.act(api.obs)
    ordered = events[-1][3]
    assert [(item.need_id, item.due_week) for item in ordered] == [("overdue", 1), ("later", 3)]


@pytest.mark.parametrize(
    "changes,match",
    [
        ({"destination_node": -1}, "destination_node"),
        ({"commodity_id": True}, "commodity_id"),
        ({"quantity": -1}, "quantity"),
        ({"quantity": "5"}, "quantity"),
        ({"priority": float("nan")}, "priority"),
        ({"due_week": 0}, "due_week"),
        ({"due_week": 1.5}, "due_week"),
        ({"confidence": 1.1}, "confidence"),
        ({"shortage_cost_per_unit_usd": -1}, "shortage_cost"),
        ({"reason": ""}, "reason"),
    ],
)
def test_invalid_need_rejected_before_allocation(api, changes, match):
    events = []
    agent = api.Agent(api.config, pipeline=pipeline(api, needs=(need(api, **changes),), events=events))
    with pytest.raises(ValueError, match=match):
        agent.act(api.obs)
    assert [row[0] for row in events] == ["state", "needs"]
    assert agent.last_allocation is None


def test_duplicate_needs_rejected(api):
    agent = api.Agent(api.config, pipeline=pipeline(api, needs=(need(api), need(api))))
    with pytest.raises(ValueError, match="duplicate need_id"):
        agent.act(api.obs)


@pytest.mark.parametrize("quantity", [(0, "unknown"), (None, "observed"), (-1, "estimated")])
def test_unknown_and_estimated_stock_not_conflated(api, quantity):
    state = snapshot(api)
    key = next(iter(state.available_stock))
    state.available_stock[key] = api.c.Quantity(*quantity)
    agent = api.Agent(api.config, pipeline=pipeline(api, state=state))
    with pytest.raises(ValueError):
        agent.act(api.obs)


def test_missing_stock_pair_does_not_mean_zero(api):
    state = snapshot(api)
    state.available_stock.pop(next(iter(state.available_stock)))
    agent = api.Agent(api.config, pipeline=pipeline(api, state=state))
    with pytest.raises(ValueError, match="include every layout pair"):
        agent.act(api.obs)


def test_stale_state_rejected(api):
    state = snapshot(api)
    api.obs["week"][:] = 2
    agent = api.Agent(api.config, pipeline=pipeline(api, state=state))
    with pytest.raises(ValueError, match="week/horizon"):
        agent.act(api.obs)


@pytest.mark.parametrize("kind", ["shape", "negative", "nan", "bool", "complex", "forbidden"])
def test_final_gate_rejects_bad_flows_without_silent_clipping(api, kind):
    count = api.config["spaces"]["action"]["flows"]["shape"][0]
    flows = np.zeros(count)
    if kind == "shape":
        flows = flows.reshape(1, count)
    elif kind == "negative":
        flows[0] = -1
    elif kind == "nan":
        flows[0] = np.nan
    elif kind == "bool":
        flows = flows.astype(bool)
    elif kind == "complex":
        flows = flows.astype(complex)
    else:
        api.obs["action_mask"][0] = 0
        api.obs["action_mask.observed"][:] = 1
        flows[0] = 1
    agent = api.Agent(api.config, pipeline=pipeline(api, result=api.c.AllocationResult(flows)))
    with pytest.raises(ValueError):
        agent.act(api.obs)
    assert agent.last_allocation is None


def test_blackout_mask_not_treated_as_confirmed_prohibition(api):
    api.obs["action_mask"][:] = 0
    api.obs["action_mask.observed"][:] = 0
    flows = np.ones(api.config["spaces"]["action"]["flows"]["shape"])
    agent = api.Agent(api.config, pipeline=pipeline(api, result=api.c.AllocationResult(flows)))
    np.testing.assert_array_equal(agent.act(api.obs)["flows"], flows)


def test_optional_release_arrays_and_modes(api):
    specs = api.config["spaces"]["action"]
    result = api.c.AllocationResult(
        np.zeros(specs["flows"]["shape"]),
        override_qty=np.zeros(specs["override_qty"]["shape"]),
        release_mode=np.full(specs["release_mode"]["shape"], api.config["release_modes"]["hold"], dtype=np.int64),
    )
    agent = api.Agent(api.config, pipeline=pipeline(api, result=result))
    action = agent.act(api.obs)
    assert action.keys() == {"flows", "override_qty", "release_mode"}
    assert action["release_mode"].dtype == np.int64
    for values in (result.release_mode.astype(float), np.full(result.release_mode.shape, 99)):
        with pytest.raises(ValueError, match="release_mode"):
            agent.validator.validate({"flows": result.flows, "release_mode": values}, api.obs)


@pytest.mark.parametrize("invalid", ["unknown_need", "too_much_unmet", "over_limit", "duplicate_resource", "bad_unit"])
def test_invalid_allocation_diagnostics_rejected(api, invalid):
    fields = {}
    if invalid == "unknown_need":
        fields["unmet_needs"] = (api.c.UnmetNeed("other", 1, "no_stock"),)
    elif invalid == "too_much_unmet":
        fields["unmet_needs"] = (api.c.UnmetNeed("n1", 6, "no_stock"),)
    else:
        commodity = api.config["layout"]["stock_slots"][0][1]
        unit = api.config["static"]["units"][api.config["static"]["commodities"]["id"][commodity]]
        usage = api.c.ResourceUsage(
            "stock", 0, "USD" if invalid == "bad_unit" else unit, 2, 1 if invalid == "over_limit" else 3, "observed"
        )
        fields["resource_usage"] = (usage, usage) if invalid == "duplicate_resource" else (usage,)
    result = api.c.AllocationResult(np.zeros(api.config["spaces"]["action"]["flows"]["shape"]), **fields)
    agent = api.Agent(api.config, pipeline=pipeline(api, result=result))
    with pytest.raises(ValueError):
        agent.act(api.obs)


def test_component_exception_is_not_hidden_as_zero_action(api):
    def broken(*args):
        raise RuntimeError("planner bug")

    modules = pipeline(api)
    modules.need_planner = SimpleNamespace(plan=broken)
    agent = api.Agent(api.config, pipeline=modules)
    with pytest.raises(RuntimeError, match="planner bug"):
        agent.act(api.obs)


@pytest.mark.parametrize("task", ["tiny", "small", "full"])
def test_connected_pipeline_uses_real_config_shapes(task):
    env = gym.make(env_id(task), entropy=12345)
    try:
        observation, info = env.reset(seed=0, options={"episode": 0})
        agent_class = load(ROOT / "agents" / "team_agent")
        api = SimpleNamespace(
            Agent=agent_class,
            c=sys.modules["contracts"],
            i=sys.modules["integration"],
            config=agent_config_from_reset(env, observation, info),
            obs=observation,
        )
        agent = api.Agent(api.config, pipeline=pipeline(api, needs=()))
        action = agent.act(observation)
        assert action["flows"].shape == env.action_space["flows"].shape
        env.step(action)
    finally:
        env.close()
