"""Conditional pool throughput after observed active-closure end dates.

Unknown future events remain unknown. No future capacity is reserved.
"""

import numpy as np


if __package__:
    from .observations import ObservationReader
else:
    from observations import ObservationReader


class ClosureSchedule:
    def __init__(self, observation, network, snapshot):
        self.snapshot = snapshot
        self.positions = {node: row for row, node in enumerate(network.chokepoints)}
        self.ends, unknown = {}, set()
        names = ("closure_end.chokepoint", "closure_end.end_week")
        if any(name not in observation for name in names):
            return
        reader = ObservationReader(observation)
        nodes, node_seen = reader.field(names[0])
        dates, date_seen = reader.field(names[1])
        if nodes.ndim != 1 or nodes.shape != dates.shape:
            raise ValueError("closure_end: inconsistent column shapes")
        if np.any(date_seen & ~node_seen):
            return  # A live event with hidden node may overlap any known node.
        for row in np.flatnonzero(node_seen):
            node = reader.integer(names[0], row, high=len(network.node_names) - 1)
            if node not in self.positions:
                raise ValueError("closure_end: node is not a chokepoint")
            end = reader.integer(names[1], row, low=1)
            if end is None or end <= snapshot.week:
                unknown.add(node)
            else:
                self.ends[node] = max(end, self.ends.get(node, end))
        for node in tuple(self.ends):
            position = self.positions[node]
            opened = snapshot.fields["graph_now.open"]
            if node in unknown or not opened.observed[position] or opened.values[position] >= 1:
                del self.ends[node]

    def permits_wait(self, route, status):
        if not status.closed_chokepoints or status.zero_capacity_edges:
            return False
        field = self.snapshot.fields[f"graph_now.kappa.{route.pool}"]
        return all(
            node in self.ends and field.observed[self.positions[node]] and field.nominal[self.positions[node]] > 0
            for node in status.closed_chokepoints
        )

    def affected(self, route):
        return any(node in self.ends for node in route.chokepoints)

    def earliest_arrival(self, route, network):
        """Conditional transit/closure lower bound, never an on-time ETA."""
        transit = self.snapshot.fields["graph_now.tau"]
        week = self.snapshot.week
        for edge in route.edges:
            node = network.edge_tail[edge]
            if node in self.ends and self.snapshot.fields["graph_now.open"].values[self.positions[node]] == 0:
                week = max(week, self.ends[node])
            if not transit.observed[edge]:
                return None
            week += int(transit.values[edge])
        return week

    def __call__(self, week, current):
        throughput = dict(current)
        for node, pool in throughput:
            position = self.positions[node]
            field = self.snapshot.fields[f"graph_now.kappa.{pool}"]
            if node in self.ends and week >= self.ends[node] and field.observed[position]:
                throughput[node, pool] = float(field.nominal[position])
        return throughput
