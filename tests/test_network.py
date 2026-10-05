"""V1 regressions: complete lanes, shared resources and intermediate arrivals."""

import copy

import numpy as np
import pytest

from agents.team_agent.network import NetworkTracker, StaticNetwork


def make_config():
    return {
        "spaces": {"action": {"flows": {"shape": [5]}}},
        "layout": {"chokepoints": [2, 1]},
        "static": {
            "nodes": {"id": ["source", "first", "second", "terminal_a", "terminal_b", "grid", "fab"]},
            "commodities": {"id": ["fuel", "wafer"], "pool": ["tb", "ct"]},
            "units": {"fuel": "GWh fuel", "wafer": "wafer-eq"},
            "edges": {
                "id": [
                    "entry",
                    "middle",
                    "exit_a",
                    "exit_b",
                    "terminal_grid",
                    "coupling",
                    "air",
                    "ct_entry",
                    "ct_middle",
                    "ct_exit",
                ],
                "tail": [0, 1, 2, 2, 3, 5, 0, 0, 1, 2],
                "head": [1, 2, 3, 4, 5, 6, 4, 1, 2, 3],
                "K": [[0], [0], [0], [0], [0], [], [0], [1], [1], [1]],
                "tau0": [1, 2, 1, 4, 0, 0, 1, 1, 1, 1],
                "c0": [1.0, 2.0, 3.0, 4.0, 0.25, 0.0, 12.0, 1.0, 1.0, 1.0],
                "u0": [10.0, 8.0, 6.0, 5.0, 7.0, None, 9.0, 2.0, 2.0, 2.0],
            },
            "lanes": {
                "id": ["to_a", "to_b", "containers"],
                "edges": [[0, 1, 2], [0, 1, 3], [7, 8, 9]],
                "chokepoints": [[1, 2], [1, 2], [1, 2]],
            },
            "action_slots": {"edge": [0, 0, 7, 4, 6], "k": [0, 0, 1, 0, 0], "lane": [0, 1, 2, None, None]},
        },
    }


@pytest.fixture
def config():
    return make_config()


def test_shared_entry_does_not_collapse_lanes_or_change_indices(config):
    before = copy.deepcopy(config)
    network = StaticNetwork(config)
    a, b, _, direct, _ = network.routes
    assert (a.source_node, a.destination_node, a.edges) == (0, 3, (0, 1, 2))
    assert (b.destination_node, b.edges) == (4, (0, 1, 3))
    assert a.chokepoints == (1, 2)
    assert a.chokepoint_positions == (1, 0)  # Observation order differs from node indices.
    assert a.nominal_transit_weeks == 4
    assert a.nominal_freight_per_unit == 6.0
    assert (direct.source_node, direct.destination_node, direct.nominal_transit_weeks) == (3, 5, 0)
    assert network.slots_to[4, 0] == (1, 4)
    assert network.slots_from[0, 0] == (0, 1, 4)
    assert network.out_edges[5] == (5,)  # Coupling stays in topology, never an action route.
    assert network.edges_to_slots[5] == ()
    assert config == before


def test_shared_chokepoint_risk_is_distinct_from_throughput_pool(config):
    network = StaticNetwork(config)
    same_pool = network.shared_resources(0, 1)
    assert same_pool.edges == (0, 1)
    assert same_pool.chokepoints == (1, 2)
    assert same_pool.chokepoint_pools == ((1, "tb"), (2, "tb"))
    different_pool = network.shared_resources(0, 2)
    assert different_pool.edges == ()
    assert different_pool.chokepoints == (1, 2)
    assert different_pool.chokepoint_pools == ()
    assert network.chokepoints_to_slots[1] == (0, 1, 2)
    assert network.chokepoint_pools_to_slots[1, "tb"] == (0, 1)
    assert network.chokepoint_pools_to_slots[1, "ct"] == (2,)


def test_pipeline_arrival_at_chokepoint_is_not_final_delivery(config):
    network = StaticNetwork(config)
    first = network.transit_progress(0, 0)
    assert first.arrival_node == 1
    assert first.destination_node == 3
    assert not first.reaches_destination
    assert first.remaining_edges == (1, 2)
    assert first.remaining_chokepoints == (1, 2)
    assert first.remaining_nominal_transit_weeks == 3
    last = network.transit_progress(2, 0)
    assert last.reaches_destination
    assert last.destination_node == 3  # Terminal, not the grid reached by another slot.
    assert last.remaining_edges == ()
    direct = network.transit_progress(4, None)
    assert direct.reaches_destination and direct.destination_node == 5
    with pytest.raises(ValueError, match="known lane"):
        network.transit_progress(0, None)
    with pytest.raises(ValueError, match="not on its lane"):
        network.transit_progress(3, 0)
    with pytest.raises(ValueError, match="outside"):
        network.transit_progress(-1, 0)  # Padded rows must not silently select the last edge.


def test_discontinuous_lane_is_rejected(config):
    config["static"]["lanes"]["edges"][0] = [0, 2]
    with pytest.raises(ValueError, match="continuous directed path"):
        StaticNetwork(config)


@pytest.mark.parametrize("task", ["tiny", "small", "full"])
def test_all_routes_against_public_instance_and_pipeline(task):
    import gymnasium as gym
    from shockbench_flow.instance.io import load_instance
    from shockbench_flow_gym import agent_config_from_reset

    from sbf_starter import env_id

    env = gym.make(env_id(task), entropy=12345)
    try:
        obs, info = env.reset(seed=0, options={"episode": 0})
        config = agent_config_from_reset(env, obs, info)
        instance = load_instance(config["static"]["instance"])
        network = StaticNetwork(config)
        snapshot = NetworkTracker(config, network).update(obs)
        assert len(snapshot.routes) == len(network.routes)
        for key, field in snapshot.fields.items():
            np.testing.assert_array_equal(field.values[field.observed], obs[key][field.observed])
        assert len(network.routes) == env.action_space["flows"].shape[0]
        for route in network.routes:
            path = (route.edge_id,) if route.lane_id is None else instance.lanes[route.lane_id].edges
            assert route.edges == path
            assert route.source_node == instance.edges[path[0]].tail
            assert route.destination_node == instance.edges[path[-1]].head
            assert all(route.commodity_id in instance.edges[e].K for e in path)
            if route.lane_id is not None:
                assert route.chokepoints == instance.lanes[route.lane_id].chokepoints
            for e in path:
                assert route.slot_id in network.edges_to_slots[e]

        # Validate intermediate arrivals against real reset observations too.
        seen = 0
        for row, visible in enumerate(obs["pipeline.edge.observed"]):
            if not visible:
                continue
            edge = int(obs["pipeline.edge"][row])
            lane = int(obs["pipeline.lane"][row]) if obs["pipeline.lane.observed"][row] else None
            progress = network.transit_progress(edge, lane)
            assert progress.arrival_node == instance.edges[edge].head
            if progress.arrival_node in instance.chokepoints:
                assert not progress.reaches_destination
            seen += 1
        assert seen > 0
    finally:
        env.close()
