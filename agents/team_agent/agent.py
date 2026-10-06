"""Team integration: a working heuristic plus an explicit teammate pipeline.

Static route information is built once per episode. Weekly closures and the
action mask still follow the original heuristic until real modules are wired.
"""

import json
from pathlib import Path

import numpy as np
from integration import ActionValidator, build_pipeline
from network import StaticNetwork


HERE = Path(__file__).resolve().parent
PARAMS = {
    "fraction": 1.0,  # share of each slot's capacity: one number, or a list with one per slot
    "closure_power": 1.0,  # a lane through a strait ships (its open fraction) ** closure_power; 0 ignores closures
}
if (HERE / "params.json").is_file():
    PARAMS |= json.loads((HERE / "params.json").read_text())


class Agent:
    def __init__(self, config=None, *, pipeline=None):
        self.config = config
        self.network = StaticNetwork(config)
        self.validator = ActionValidator(config)
        self.pipeline = pipeline if pipeline is not None else build_pipeline(config, self.network)
        self.last_allocation = None
        self.cap = np.array(
            [self.network.edge_capacity[route.edge_id] for route in self.network.routes], dtype=float
        ) * np.asarray(PARAMS["fraction"], dtype=float)
        self.power = float(PARAMS["closure_power"])
        self.through = tuple(route.chokepoint_positions for route in self.network.routes)

    def act(self, observation):
        self.last_allocation = None
        if self.pipeline is not None:
            allocation = self.pipeline.run(observation, self.network, self.config)
            action = {"flows": allocation.flows}
            for name in ("override_qty", "release_mode"):
                value = getattr(allocation, name)
                if value is not None:
                    action[name] = value
            action = self.validator.validate(action, observation)
            self.last_allocation = allocation
            return action
        return self.validator.validate(self._heuristic_action(observation), observation)

    def _heuristic_action(self, observation):
        flows = self.cap * observation["action_mask"]
        open_now = observation["graph_now.open"]  # 1 open .. 0 closed
        seen = observation["graph_now.open.observed"] == 1
        for s, chokepoints in enumerate(self.through):
            for c in chokepoints:
                if seen[c]:
                    flows[s] *= max(float(open_now[c]), 0.0) ** self.power
        return {"flows": flows}
