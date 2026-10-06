"""Real state/needs modules and conditional queue forecasts on all wire layouts."""

import importlib
import sys
from collections import defaultdict
from dataclasses import replace
from time import process_time
from types import SimpleNamespace

import gymnasium as gym
import numpy as np
import pytest
from shockbench_flow_gym import agent_config_from_reset

from sbf_starter import env_id
from sbf_starter.agents import load
from tests.conftest import ROOT


@pytest.fixture(scope="module")
def resets():
    cache = {}

    def get(task):
        if task not in cache:
            env = gym.make(env_id(task), entropy=12345)
            try:
                obs, info = env.reset(seed=0, options={"episode": 0})
                cache[task] = agent_config_from_reset(env, obs, info), obs
            finally:
                env.close()
        return cache[task]

    return get


@pytest.fixture
def api(resets, monkeypatch):
    def build(task="small"):
        config, original = resets(task)
        agent = load(ROOT / "agents" / "team_agent")
        monkeypatch.syspath_prepend(str(ROOT / "agents" / "team_agent"))
        for name in ("state", "needs", "queue_forecast", "observations"):
            monkeypatch.delitem(sys.modules, name, raising=False)
        state = importlib.import_module("state")
        needs = importlib.import_module("needs")
        forecast = importlib.import_module("queue_forecast")
        return SimpleNamespace(
            Agent=agent,
            config=config,
            obs={key: arr.copy() for key, arr in original.items()},
            state=state,
            needs=needs,
            forecast=forecast,
            contracts=sys.modules["contracts"],
            integration=sys.modules["integration"],
            network=sys.modules["network"].StaticNetwork(config),
        )

    return build


def empty(api):
    for block in ("pipeline", "queue_lots", "wip"):
        api.obs[f"{block}.qty"][:] = 0
        api.obs[f"{block}.qty.observed"][:] = 0
    for field in ("stock.qty", "backlog.qty", "demand_forecast.qty"):
        api.obs[field][:] = 0
        api.obs[f"{field}.observed"][:] = 1
    return api


def pipeline(api, route, *, qty=10.0, due=1, row=0, lane_seen=True):
    for field, value in (
        ("edge", route.edge_id),
        ("k", route.commodity_id),
        ("lane", route.lane_id or 0),
        ("qty", qty),
        ("arrival_week", due),
    ):
        api.obs[f"pipeline.{field}"][row] = value
        api.obs[f"pipeline.{field}.observed"][row] = 1
    api.obs["pipeline.lane.observed"][row] = int(lane_seen and route.lane_id is not None)


@pytest.mark.parametrize("task", ["tiny", "small", "full"])
def test_real_state_and_needs_fit_decision_pipeline(api, task):
    a = api(task)
    builder = a.state.StateBuilder(a.config)
    planner = a.needs.NeedPlanner(a.config)
    state = builder.build(a.obs, a.network)
    assert set(state.available_stock) == set(map(tuple, a.config["layout"]["stock_slots"]))
    assert set(state.backlog) == set(map(tuple, a.config["layout"]["demands"]))
    assert state.availability_mode == "pre_dispatch_stock_t_minus_1"
    needs = planner.plan(state, a.obs, a.network)
    assert len({need.need_id for need in needs}) == len(needs)
    assert all(need.confidence is None and need.quantity > 0 for need in needs)
    allocator = SimpleNamespace(
        allocate=lambda *args: a.contracts.AllocationResult(np.zeros(a.config["spaces"]["action"]["flows"]["shape"]))
    )
    agent = a.Agent(a.config, pipeline=a.integration.DecisionPipeline(builder, planner, allocator))
    assert agent.act(a.obs)["flows"].shape == tuple(a.config["spaces"]["action"]["flows"]["shape"])
    assert agent.last_allocation is not None


def test_available_stock_does_not_include_supply_arrival_or_wip(api):
    a = empty(api())
    route = next(route for route in a.network.routes if route.lane_id is None)
    pipeline(a, route)
    pair = (route.destination_node, route.commodity_id)
    stock_row = list(map(tuple, a.config["layout"]["stock_slots"])).index(pair)
    a.obs["stock.qty"][stock_row] = 7
    a.obs["graph_now.supply.avail"][:] = 123
    state = a.state.StateBuilder(a.config).build(a.obs, a.network)
    assert state.available_stock[pair] == a.contracts.Quantity(7, "observed")
    assert sum(item.quantity.value for item in state.arrivals if item.destination_node == pair[0]) == 10


def test_hidden_stock_is_unknown_and_input_not_mutated(api):
    a = api()
    a.obs["stock.qty"][0] = 999
    a.obs["stock.qty.observed"][0] = 0
    before = {key: arr.copy() for key, arr in a.obs.items()}
    state = a.state.StateBuilder(a.config).build(a.obs, a.network)
    pair = tuple(a.config["layout"]["stock_slots"][0])
    assert state.available_stock[pair] == a.contracts.Quantity(None, "unknown")
    for key, arr in before.items():
        np.testing.assert_array_equal(a.obs[key], arr)


def test_off_lane_and_hidden_lane_are_not_lane_zero(api):
    a = empty(api())
    direct = next(
        route
        for route in a.network.routes
        if route.lane_id is None and all(route.edge_id not in path for path in a.network.lane_edges)
    )
    pipeline(a, direct, lane_seen=False)
    lane = next(route for route in a.network.routes if route.lane_id is not None)
    pipeline(a, lane, row=1, lane_seen=False)
    state = a.state.StateBuilder(a.config).build(a.obs, a.network)
    assert state.pipeline[0].lane_status == "off_lane"
    assert state.pipeline[0].destination_node == direct.destination_node
    assert state.pipeline[1].lane_status == "unknown"
    assert state.pipeline[1].destination_node is None
    assert len(state.arrivals) == 1


def test_intermediate_arrival_has_unknown_final_date_without_v3(api):
    a = empty(api())
    route = next(route for route in a.network.routes if route.lane_id is not None)
    pipeline(a, route)
    state = a.state.StateBuilder(a.config).build(a.obs, a.network)
    assert state.pipeline[0].edge_arrival_week == 1
    assert state.arrivals[0].destination_node == route.destination_node
    assert state.arrivals[0].arrival_week is None
    progress = a.network.transit_progress(route.edge_id, route.lane_id)
    assert state.pipeline[0].destination_node == progress.destination_node
    assert state.pipeline[0].remaining_edges == progress.remaining_edges
    assert state.pipeline[0].remaining_route_weeks == progress.remaining_nominal_transit_weeks


def test_dense_queue_honors_live_mask_and_calendar_is_idempotent(api):
    a = empty(api())
    a.obs["week"][:] = 2
    a.obs["queue_lots.qty"][0, 0] = 11
    builder = a.state.StateBuilder(a.config)
    assert not builder.build(a.obs, a.network).queues
    a.obs["queue_lots.qty.observed"][0, 0] = 1
    state = builder.build(a.obs, a.network)
    assert len(state.queues) == 1 and len(state.arrivals) == 1
    assert state.arrivals[0].arrival_week is None
    assert state.arrival_calendar == state.arrival_calendar


def test_wip_gross_observed_output_estimated(api):
    a = api()
    state = a.state.StateBuilder(a.config).build(a.obs, a.network)
    assert state.wip
    assert all(item.gross_quantity.source == "observed" for item in state.wip)
    assert all(item.quantity.source == "estimated" for item in state.arrivals if item.source_kind == "wip")


@pytest.mark.parametrize("first_demand", [0.0, 10.0])
def test_each_arrival_used_once_including_zero_demand_week(api, first_demand):
    a = empty(api())
    state = a.state.StateBuilder(a.config).build(a.obs, a.network)
    node, commodity = a.config["layout"]["demands"][0]
    arrival = a.contracts.ExpectedArrival(
        "a", "p", "pipeline", node, commodity, a.contracts.Quantity(10, "observed"), 1, "observed"
    )
    state = replace(state, arrivals=(arrival,))
    a.obs["demand_forecast.qty"][0, :3] = [first_demand, 10, 10]
    planner = a.needs.NeedPlanner(a.config, production_horizon=1, safety_stock=False)
    needs = [
        need
        for need in planner.plan(state, a.obs, a.network)
        if need.destination_node == node and need.commodity_id == commodity
    ]
    expected = [(3, 10)] if first_demand == 0 else [(2, 10), (3, 10)]
    assert [(need.due_week, need.quantity) for need in needs] == expected


def test_unknown_or_estimated_arrivals_do_not_cover_by_default(api):
    a = empty(api())
    state = a.state.StateBuilder(a.config).build(a.obs, a.network)
    node, commodity = a.config["layout"]["demands"][0]
    a.obs["demand_forecast.qty"][0, 0] = 10
    estimated = a.contracts.ExpectedArrival(
        "a", "p", "pipeline", node, commodity, a.contracts.Quantity(10, "estimated"), 1, "estimated"
    )
    unknown = replace(estimated, arrival_id="b", source_id="q", arrival_week=None, source="unknown")
    state = replace(state, arrivals=(estimated, unknown))
    planner = a.needs.NeedPlanner(a.config, production_horizon=1, safety_stock=False)
    assert any(need.quantity == 10 and need.destination_node == node for need in planner.plan(state, a.obs, a.network))
    planner = a.needs.NeedPlanner(a.config, production_horizon=1, safety_stock=False, include_estimated_arrivals=True)
    assert not any(need.destination_node == node for need in planner.plan(state, a.obs, a.network))


def test_backlog_and_current_demand_share_coverage_and_keep_due_week(api):
    a = empty(api())
    pair = tuple(a.config["layout"]["demands"][0])
    row = list(map(tuple, a.config["layout"]["stock_slots"])).index(pair)
    a.obs["stock.qty"][row] = 5
    a.obs["backlog.qty"][0] = 10
    a.obs["demand_forecast.qty"][0, 0] = 10
    planner = a.needs.NeedPlanner(a.config, production_horizon=1, safety_stock=False)
    builder = a.state.StateBuilder(a.config)
    needs = [
        need
        for need in planner.plan(builder.build(a.obs, a.network), a.obs, a.network)
        if (need.destination_node, need.commodity_id) == pair
    ]
    assert [(need.reason, need.quantity) for need in needs] == [("backlog", 5), ("current_demand", 10)]
    a.obs["week"][:] = 2
    needs = planner.plan(builder.build(a.obs, a.network), a.obs, a.network)
    assert next(need for need in needs if need.reason == "backlog").due_week == 1


def test_duplicate_arrival_rejected(api):
    a = empty(api())
    state = a.state.StateBuilder(a.config).build(a.obs, a.network)
    node, commodity = a.config["layout"]["demands"][0]
    arrival = a.contracts.ExpectedArrival(
        "a", "p", "pipeline", node, commodity, a.contracts.Quantity(10, "observed"), 1, "observed"
    )
    with pytest.raises(ValueError, match="duplicate arrival_id"):
        a.needs.NeedPlanner(a.config).plan(replace(state, arrivals=(arrival, arrival)), a.obs, a.network)


def queue_scenario(a, *, inbound_week=3, old_qty=15, incoming_qty=5):
    route = next(route for route in a.network.routes if route.lane_id is not None and len(route.chokepoints) == 1)
    path = route.edges
    into = next(edge for edge in path if a.network.edge_head[edge] in a.network.chokepoints)
    out = path[path.index(into) + 1]
    choke = a.network.edge_head[into]
    old = a.contracts.QueueLot(
        "old", choke, route.commodity_id, route.lane_id, "known", out, 0, a.contracts.Quantity(old_qty, "observed")
    )
    incoming = a.contracts.PipelineLot(
        "incoming",
        into,
        route.commodity_id,
        route.lane_id,
        "known",
        a.contracts.Quantity(incoming_qty, "observed"),
        inbound_week,
        route.destination_node,
    )
    state = a.state.StateBuilder(a.config).build(a.obs, a.network)
    state = replace(state, pipeline=(incoming,), queues=(old,))
    a.obs["graph_now.u"][:] = 1000
    a.obs["graph_now.u.observed"][:] = 1
    a.obs["graph_now.prohibited"][:] = 0
    a.obs["graph_now.prohibited.observed"][:] = 1
    a.obs["graph_now.open"][:] = 1
    a.obs["graph_now.open.observed"][:] = 1
    for pool in ("tb", "ct"):
        a.obs[f"graph_now.kappa.{pool}"][:] = 10
        a.obs[f"graph_now.kappa.{pool}.observed"][:] = 1
    return state, route, out, choke


def test_v3_projects_work_ahead_at_arrival_not_current_queue(api):
    a = empty(api("tiny"))
    state, route, out, choke = queue_scenario(a)
    forecast = a.forecast.QueueForecaster(a.config).forecast(state, a.obs, a.network)
    visit = next(visit for visit in forecast.visits if visit.source_id == "incoming")
    assert visit.arrival_week == visit.evaluated_week == 3
    assert visit.work_ahead.value == 0  # Today's 15 cleared in weeks 1 and 2.
    assert visit.first_release_week == visit.completion_release_week == 3
    assert forecast.completion_weeks["incoming"] == 3 + a.network.edge_transit_weeks[out]
    assert all(arrival.source == "estimated" for arrival in forecast.arrivals)


def test_v3_same_cohort_is_pro_rata_not_arbitrary_fifo(api):
    a = empty(api("tiny"))
    state, route, out, choke = queue_scenario(a, inbound_week=1, old_qty=0, incoming_qty=10)
    rival = replace(state.pipeline[0], lot_id="rival", quantity=a.contracts.Quantity(10, "observed"))
    state = replace(state, queues=(), pipeline=state.pipeline + (rival,))
    forecast = a.forecast.QueueForecaster(a.config).forecast(state, a.obs, a.network)
    totals = defaultdict(float)
    for arrival in forecast.arrivals:
        if arrival.arrival_week is not None:
            totals[arrival.source_id, arrival.arrival_week] += arrival.quantity.value
    due = 1 + a.network.edge_transit_weeks[out]
    assert totals["incoming", due] == totals["rival", due] == 5
    assert next(visit for visit in forecast.visits if visit.source_id == "incoming").same_cohort_competition.value == 10


@pytest.mark.parametrize("case", ["closed", "unknown_rate", "unknown_ban", "prohibited", "zero_edge"])
def test_v3_does_not_fabricate_completion(api, case):
    a = empty(api("tiny"))
    state, route, out, choke = queue_scenario(a)
    position = a.network.chokepoints.index(choke)
    if case == "closed":
        a.obs["graph_now.open"][position] = 0
    elif case == "unknown_rate":
        a.obs[f"graph_now.kappa.{route.pool}.observed"][position] = 0
    elif case == "unknown_ban":
        a.obs["graph_now.prohibited.observed"][out, route.commodity_id] = 0
    elif case == "prohibited":
        a.obs["graph_now.prohibited"][out, route.commodity_id] = 1
    else:
        a.obs["graph_now.u"][out] = 0
    forecast = a.forecast.QueueForecaster(a.config).forecast(state, a.obs, a.network)
    assert forecast.completion_weeks["incoming"] is None
    assert not any(arrival.arrival_week is not None for arrival in forecast.arrivals)


def test_v3_kappa_is_not_multiplied_by_open_twice(api):
    a = empty(api("tiny"))
    state, route, out, choke = queue_scenario(a, inbound_week=1, old_qty=0, incoming_qty=10)
    state = replace(state, queues=())
    a.obs["graph_now.open"][:] = 0.5
    forecast = a.forecast.QueueForecaster(a.config).forecast(state, a.obs, a.network)
    assert forecast.completion_weeks["incoming"] == 1 + a.network.edge_transit_weeks[out]


def test_v3_partial_delivery_preserves_total_and_unknown_residual(api):
    a = empty(api("tiny"))
    state, route, out, choke = queue_scenario(a, inbound_week=1, old_qty=0, incoming_qty=30)
    state = replace(state, queues=())
    end = 1 + a.network.edge_transit_weeks[out]
    forecast = a.forecast.QueueForecaster(a.config, max_weeks=end).forecast(state, a.obs, a.network)
    pieces = [arrival for arrival in forecast.arrivals if arrival.source_id == "incoming"]
    assert sum(arrival.quantity.value for arrival in pieces) == pytest.approx(30)
    assert sum(arrival.quantity.value for arrival in pieces if arrival.arrival_week is not None) == 10
    assert forecast.completion_weeks["incoming"] is None


def test_state_v3_replaces_unknown_arrivals_without_double_count(api):
    a = empty(api("tiny"))
    route = next(route for route in a.network.routes if route.lane_id is not None)
    pipeline(a, route)
    builder = a.state.StateBuilder(a.config, queue_forecaster=a.forecast.QueueForecaster(a.config))
    state = builder.build(a.obs, a.network)
    assert sum(arrival.quantity.value for arrival in state.arrivals) == pytest.approx(10)
    assert state.queue_forecast is not None


@pytest.mark.parametrize("field", ["stock.qty", "pipeline.qty"])
def test_invalid_observed_quantities_rejected(api, field):
    a = empty(api())
    a.obs[field][0] = np.nan
    a.obs[f"{field}.observed"][0] = 1
    with pytest.raises(ValueError, match="invalid observed"):
        a.state.StateBuilder(a.config).build(a.obs, a.network)


@pytest.mark.parametrize("task", ["tiny", "small", "full"])
def test_full_episode_analytics_and_v3(api, task, record_property):
    a = api(task)
    env = gym.make(env_id(task), entropy=12345)
    try:
        obs, info = env.reset(seed=0, options={"episode": 0})
        config = agent_config_from_reset(env, obs, info)
        builder = a.state.StateBuilder(config, queue_forecaster=a.forecast.QueueForecaster(config))
        planner = a.needs.NeedPlanner(config)
        allocator = SimpleNamespace(
            allocate=lambda *args: a.contracts.AllocationResult(np.zeros(config["spaces"]["action"]["flows"]["shape"]))
        )
        pipeline = a.integration.DecisionPipeline(builder, planner, allocator)
        agent = a.Agent(config)
        weeks, max_cpu = 0, 0.0
        while True:
            start = process_time()
            result = pipeline.run(obs, agent.network, config)
            agent.validator.validate({"flows": result.flows}, obs)
            max_cpu = max(max_cpu, process_time() - start)
            # Follow the existing policy to exercise changing real pipeline/queues.
            obs, _, terminated, truncated, _ = env.step(agent.act(obs))
            weeks += 1
            if terminated or truncated:
                break
        assert weeks == config["T"]
        record_property(f"{task}_analytics_max_local_cpu_s", max_cpu)
    finally:
        env.close()


@pytest.mark.parametrize("edge_cap", [5, 1000])
def test_v3_matches_public_simulator_under_its_stated_scenario(api, edge_cap):
    from shockbench_flow.dynamics.sim import initial_state, step
    from shockbench_flow.dynamics.state import Lot, Shipment
    from shockbench_flow.instance.io import load_instance
    from shockbench_flow.marks import event_free_marks

    a = empty(api("tiny"))
    state, route, out, choke = queue_scenario(a, old_qty=20)
    inst = load_instance(a.config["static"]["instance"])
    marks = event_free_marks(inst)
    marks = replace(marks, u=np.full_like(marks.u, edge_cap), kappa=np.full_like(marks.kappa, 10))
    a.obs["graph_now.u"][:] = edge_cap
    sim = initial_state(inst)
    sim.stock[:] = 0
    sim.fab_wip = {}
    sim.osat_wip = {}
    sim.lots = [Lot(0, choke, route.commodity_id, 20, route.lane_id, out, 0, route.edge_id, 0)]
    cargo = state.pipeline[0]
    sim.pipeline = [
        Shipment(cargo.edge_id, cargo.commodity_id, cargo.lane_id, cargo.quantity.value, 0, cargo.edge_arrival_week, 1)
    ]
    sim.next_lot_id = 2
    forecast = a.forecast.QueueForecaster(a.config).forecast(state, a.obs, a.network)
    forecast_releases = defaultdict(float)
    for arrival in forecast.arrivals:
        if arrival.arrival_week is not None:
            forecast_releases[arrival.arrival_week - a.network.edge_transit_weeks[out]] += arrival.quantity.value
    for week in range(1, 8):
        record = step(inst, marks, sim, {})
        actual = record.x.get((out, route.commodity_id, route.lane_id), 0.0)
        assert actual == pytest.approx(forecast_releases[week])


@pytest.mark.parametrize("task", ["tiny", "small", "full"])
def test_v3_fleet_terms_match_public_schema(api, task):
    from shockbench_flow.instance.io import load_instance

    a = api(task)
    inst = load_instance(a.config["static"]["instance"])
    actual_terms = defaultdict(list)
    for edge, lane, delta in inst.dup_items:
        actual_terms[edge].append((lane, delta))
    assert a.forecast.QueueForecaster(a.config).fleet_terms == dict(actual_terms)


def test_v3_tandem_queues_advance_without_duplicating_cargo(api):
    a = empty(api())
    route = next(route for route in a.network.routes if len(route.chokepoints) >= 2)
    pipeline(a, route, qty=10)
    a.obs["graph_now.u"][:] = 1000
    a.obs["graph_now.u.observed"][:] = 1
    a.obs["graph_now.prohibited"][:] = 0
    a.obs["graph_now.prohibited.observed"][:] = 1
    a.obs["graph_now.open"][:] = 1
    a.obs["graph_now.open.observed"][:] = 1
    for pool in ("tb", "ct"):
        a.obs[f"graph_now.kappa.{pool}"][:] = 1000
        a.obs[f"graph_now.kappa.{pool}.observed"][:] = 1
    state = a.state.StateBuilder(a.config, queue_forecaster=a.forecast.QueueForecaster(a.config)).build(
        a.obs, a.network
    )
    assert len(state.queue_forecast.visits) == len(route.chokepoints)
    assert sum(arrival.quantity.value for arrival in state.arrivals) == pytest.approx(10)
    source = state.pipeline[0].lot_id
    expected = 1 + sum(a.network.edge_transit_weeks[edge] for edge in route.edges[1:])
    assert state.queue_forecast.completion_weeks[source] == expected


def test_retrospective_backtest_uses_latest_prediction_and_aggregate_actuals(api):
    a = empty(api("tiny"))
    builder = a.state.StateBuilder(a.config)
    state = builder.build(a.obs, a.network)
    node, commodity = tuple(a.config["layout"]["stock_slots"][0])

    def forecast(qty):
        arrival = a.contracts.ExpectedArrival(
            f"p{qty}", "grouped", "queue", node, commodity, a.contracts.Quantity(qty, "estimated"), 3, "estimated"
        )
        return a.forecast.QueueForecast((arrival,), (), {})

    actual = a.contracts.ExpectedArrival(
        "observed", "wire-row", "pipeline", node, commodity, a.contracts.Quantity(7, "observed"), 3, "observed"
    )
    observed_state = replace(state, week=3, arrivals=(actual,))
    rows = a.forecast.retrospective_backtest(((1, forecast(5)), (2, forecast(9))), (observed_state,))
    assert rows == (
        {
            "destination_node": node,
            "commodity_id": commodity,
            "arrival_week": 3,
            "predicted_quantity": 9,
            "actual_quantity": 7,
            "absolute_error": 2,
            "cargo_identity": "aggregate; grouped lots have no stable IDs",
        },
    )


def test_unknown_demand_does_not_erase_arrivals_before_known_demand(api):
    a = empty(api())
    state = a.state.StateBuilder(a.config).build(a.obs, a.network)
    node, commodity = a.config["layout"]["demands"][0]
    arrival = a.contracts.ExpectedArrival(
        "a", "p", "pipeline", node, commodity, a.contracts.Quantity(10, "observed"), 1, "observed"
    )
    a.obs["demand_forecast.qty"][0, 0] = 999
    a.obs["demand_forecast.qty.observed"][0, 0] = 0
    a.obs["demand_forecast.qty"][0, 1] = 20
    planner = a.needs.NeedPlanner(a.config, production_horizon=1, safety_stock=False)
    needs = [
        need
        for need in planner.plan(replace(state, arrivals=(arrival,)), a.obs, a.network)
        if need.destination_node == node
    ]
    assert [(need.due_week, need.quantity) for need in needs] == [(2, 10)]
    assert planner.last_issues and all(need.confidence is None for need in needs)


def test_osat_requests_are_bounded_by_downstream_demand(api):
    a = empty(api("full"))
    a.obs["graph_now.osat.thr_eff"][:] = 100
    a.obs["graph_now.osat.thr_eff.observed"][:] = 1
    state = a.state.StateBuilder(a.config).build(a.obs, a.network)
    planner = a.needs.NeedPlanner(a.config, production_horizon=1, safety_stock=False)
    needs = planner.plan(state, a.obs, a.network)
    for node in a.config["layout"]["osats"]:
        production = [need for need in needs if need.destination_node == node and need.reason == "production"]
        assert sum(need.quantity for need in production) == 0
    # One published sink forecast activates only its compatible package BOM.
    demand_row = 0
    a.obs["demand_forecast.qty"][demand_row, 0] = 23
    needs = planner.plan(state, a.obs, a.network)
    osat_nodes = set(a.config["layout"]["osats"])
    assert (
        sum(need.quantity for need in needs if need.reason == "production" and need.destination_node in osat_nodes)
        <= 23
    )
    exported = planner.export_examples(needs)
    assert all(row["priority_rank"] == i for i, row in enumerate(exported, 1))
    assert all("assumptions" in row and row["confidence"] is None for row in exported)


def test_existing_osat_output_and_fab_output_reduce_production_targets(api):
    a = empty(api("full"))
    demand_pair = tuple(a.config["layout"]["demands"][0])
    package = a.config["static"]["commodities"]["id"][demand_pair[1]]
    osat_nodes = a.config["layout"]["osats"]
    matching = [
        node
        for node in osat_nodes
        if package in a.config["static"]["instance"]["nodes"][node].get("osat", {}).get("packages", {}).values()
    ]
    if not matching:
        pytest.skip("fixture demand commodity has no OSAT conversion")
    a.obs["graph_now.osat.thr_eff"][:] = 100
    a.obs["graph_now.osat.thr_eff.observed"][:] = 1
    a.obs["graph_now.fab.cap_eff"][:] = 10000
    a.obs["graph_now.fab.cap_eff.observed"][:] = 1
    a.obs["demand_forecast.qty"][0, 0] = 23
    slots = list(map(tuple, a.config["layout"]["stock_slots"]))
    for node in matching:
        row = slots.index((node, demand_pair[1]))
        a.obs["stock.qty"][row] = 4
    for node in a.config["layout"]["fabs"]:
        profile = a.config["static"]["instance"]["nodes"][node].get("fab", {})
        if profile.get("product"):
            pair = node, a.config["static"]["commodities"]["id"].index(profile["product"])
            if pair in slots:
                a.obs["stock.qty"][slots.index(pair)] = 10000
    state = a.state.StateBuilder(a.config).build(a.obs, a.network)
    planner = a.needs.NeedPlanner(a.config, production_horizon=1, safety_stock=False)
    needs = planner.plan(state, a.obs, a.network)
    osat_needs = [need for need in needs if need.reason == "production" and need.destination_node in matching]
    assert sum(need.quantity for need in osat_needs) <= max(0, 23 - 4 * len(matching))
    fab_nodes = set(a.config["layout"]["fabs"])
    assert not any(need.reason == "production" and need.destination_node in fab_nodes for need in needs)


def test_fab_nominal_bom_ignores_scrap_window_and_lead_time(api):
    a = empty(api("full"))
    pair = tuple(a.config["layout"]["demands"][0])
    package = a.config["static"]["commodities"]["id"][pair[1]]
    nodes = a.config["layout"]["osats"]
    matching = [
        node for node in nodes
        if package in a.config["static"]["instance"]["nodes"][node].get("osat", {}).get("packages", {}).values()
    ]
    if not matching:
        pytest.skip("fixture demand commodity has no OSAT conversion")
    a.obs["graph_now.osat.thr_eff"][:] = 100
    a.obs["graph_now.osat.thr_eff.observed"][:] = 1
    a.obs["graph_now.fab.cap_eff"][:] = 10000
    a.obs["graph_now.fab.cap_eff.observed"][:] = 1
    a.obs["demand_forecast.qty"][0, 0] = 23
    state = a.state.StateBuilder(a.config).build(a.obs, a.network)
    planner = a.needs.NeedPlanner(a.config, production_horizon=1, safety_stock=False)

    def fab_inputs():
        return {
            (need.destination_node, need.commodity_id): need.quantity
            for need in planner.plan(state, a.obs, a.network)
            if need.reason == "production" and need.destination_node in a.config["layout"]["fabs"]
        }

    original = fab_inputs()
    assert original and sum(original.values()) == pytest.approx(23)
    profiles = [a.config["static"]["instance"]["nodes"][node]["fab"] for node in a.config["layout"]["fabs"]]
    old_values = [(profile.get("w_scr", 0), profile.get("tau", 1)) for profile in profiles]
    try:
        for profile in profiles:
            profile["w_scr"], profile["tau"] = 99_999, 1
        assert fab_inputs() == original
    finally:
        for profile, (w_scr, tau) in zip(profiles, old_values, strict=True):
            profile["w_scr"], profile["tau"] = w_scr, tau


def test_unknown_stock_does_not_create_a_zero_based_replenishment(api):
    a = empty(api())
    pair = tuple(a.config["layout"]["demands"][0])
    stock_row = list(map(tuple, a.config["layout"]["stock_slots"])).index(pair)
    a.obs["stock.qty.observed"][stock_row] = 0
    a.obs["demand_forecast.qty"][0, 0] = 10
    builder = a.state.StateBuilder(a.config)
    planner = a.needs.NeedPlanner(a.config, safety_stock=False)
    assert not any(
        (need.destination_node, need.commodity_id) == pair
        for need in planner.plan(builder.build(a.obs, a.network), a.obs, a.network)
    )
    assert f"stock:{pair}:unknown_coverage" in planner.last_issues


def test_forecasts_stop_at_episode_horizon(api):
    a = empty(api())
    a.obs["week"][:] = a.config["T"]
    a.obs["demand_forecast.qty"][0, :] = 10
    state = a.state.StateBuilder(a.config).build(a.obs, a.network)
    needs = a.needs.NeedPlanner(a.config).plan(state, a.obs, a.network)
    assert needs and all(need.due_week == a.config["T"] for need in needs)


def test_v3_other_pool_does_not_consume_target_pool_throughput(api):
    a = empty(api("tiny"))
    state, route, out, choke = queue_scenario(a, inbound_week=1, old_qty=0, incoming_qty=10)
    rival_route = next(
        candidate for candidate in a.network.routes if choke in candidate.chokepoints and candidate.pool != route.pool
    )
    rival_out = next(edge for edge in rival_route.edges if a.network.edge_tail[edge] == choke)
    rival = a.contracts.QueueLot(
        "container",
        choke,
        rival_route.commodity_id,
        rival_route.lane_id,
        "known",
        rival_out,
        0,
        a.contracts.Quantity(1000, "observed"),
    )
    forecast = a.forecast.QueueForecaster(a.config).forecast(replace(state, queues=(rival,)), a.obs, a.network)
    assert forecast.completion_weeks["incoming"] == 1 + a.network.edge_transit_weeks[out]


def test_missing_pipeline_metadata_keeps_v3_eta_unknown(api):
    a = empty(api())
    route = next(route for route in a.network.routes if route.lane_id is not None)
    pipeline(a, route, lane_seen=False)
    state = a.state.StateBuilder(a.config, queue_forecaster=a.forecast.QueueForecaster(a.config)).build(
        a.obs, a.network
    )
    assert state.pipeline[0].destination_node is None
    assert state.issues and not state.arrivals
