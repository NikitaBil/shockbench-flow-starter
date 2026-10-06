"""Send the maximum, less into a strait that is partly closed: queued cargo pays holding costs and arrives late.

A sea lane through a strait ships its capacity times the strait's observed open fraction; other routes ship at
capacity. A ``params.json`` beside this file replaces ``PARAMS`` (examples/06_policy_search.py writes one). A starting
point, not a tuned policy: it ignores warnings, announcements and pending sanctions.
"""

import json
from pathlib import Path

import numpy as np


HERE = Path(__file__).resolve().parent
PARAMS = {
    "fraction": 1.0,  # share of each slot's capacity: one number, or a list with one per slot
    "closure_power": 1.0,  # a lane through a strait ships (its open fraction) ** closure_power; 0 ignores closures
}
if (HERE / "params.json").is_file():
    PARAMS |= json.loads((HERE / "params.json").read_text())


class Agent:
    def __init__(self, config=None):
        static, layout = config["static"], config["layout"]
        u0 = static["edges"]["u0"]  # each edge's nominal capacity per week
        slots = static["action_slots"]
        self.cap = np.array([u0[e] for e in slots["edge"]], dtype=float) * np.asarray(PARAMS["fraction"], dtype=float)
        self.power = float(PARAMS["closure_power"])
        position = {node: i for i, node in enumerate(layout["chokepoints"])}  # strait -> its index in graph_now.open
        lane_chokepoints = static["lanes"]["chokepoints"]
        # per slot, the indices of the straits its lane passes ([] off any lane)
        self.through = [
            [position[c] for c in lane_chokepoints[lane]] if lane is not None else [] for lane in slots["lane"]
        ]

    def act(self, observation):
        flows = self.cap * observation["action_mask"]
        open_now = observation["graph_now.open"]  # 1 open .. 0 closed
        seen = observation["graph_now.open.observed"] == 1
        for s, chokepoints in enumerate(self.through):
            for c in chokepoints:
                if seen[c]:
                    flows[s] *= max(float(open_now[c]), 0.0) ** self.power
        return {"flows": flows}
