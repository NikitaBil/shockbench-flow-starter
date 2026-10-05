"""V1: static routes and shared resources, using the config's integer indices.

Build StaticNetwork once in Agent(config). This module describes existing
action slots; it does not choose flows, forecast needs or read weekly signals.
"""

import math
from collections import defaultdict
from dataclasses import dataclass
from operator import index
from types import MappingProxyType


@dataclass(frozen=True, slots=True)
class SlotRoute:
    slot_id: int
    commodity_id: int
    source_node: int
    destination_node: int
    edge_id: int
    lane_id: int | None
    edges: tuple[int, ...]
    nodes: tuple[int, ...]
    chokepoints: tuple[int, ...]
    chokepoint_positions: tuple[int, ...]
    pool: str
    unit: str
    nominal_transit_weeks: int
    nominal_freight_per_unit: float


@dataclass(frozen=True, slots=True)
class SharedResources:
    edges: tuple[int, ...]
    chokepoints: tuple[int, ...]
    chokepoint_pools: tuple[tuple[int, str], ...]


@dataclass(frozen=True, slots=True)
class TransitProgress:
    """Where a shipment arrives on its current edge, and what remains afterwards.

    The remaining nominal transit excludes the current edge and queue waiting.
    This describes a route, not a forecast of actual arrival at destination.
    """

    edge_id: int
    lane_id: int | None
    arrival_node: int
    destination_node: int
    remaining_edges: tuple[int, ...]
    remaining_chokepoints: tuple[int, ...]
    remaining_nominal_transit_weeks: int

    @property
    def reaches_destination(self):
        return not self.remaining_edges and self.arrival_node == self.destination_node


def _checked_index(value, size, label):
    if isinstance(value, bool):
        raise ValueError(f"{label} must be an integer index")
    try:
        value = index(value)
    except TypeError as exc:
        raise ValueError(f"{label} must be an integer index") from exc
    if not 0 <= value < size:
        raise ValueError(f"{label} index {value} outside [0, {size})")
    return value


def _freeze_groups(groups):
    return MappingProxyType({key: tuple(values) for key, values in groups.items()})


class StaticNetwork:
    """Topology cached from static tables, without Small/Full slot constants.

    edges_to_slots shares edge capacity across commodities. Chokepoint pools
    share throughput only within tb/ct; a shared physical chokepoint can still
    expose two different pools to the same disruption.
    """

    def __init__(self, config):
        static = config["static"]
        edges, lanes, goods, slots = (static["edges"], static["lanes"], static["commodities"], static["action_slots"])
        self.node_names = tuple(static["nodes"]["id"])
        self.edge_names = tuple(edges["id"])
        self.commodity_names = tuple(goods["id"])
        self.lane_names = tuple(lanes["id"])
        self.edge_tail = tuple(edges["tail"])
        self.edge_head = tuple(edges["head"])
        self.edge_capacity = tuple(edges["u0"])
        self.edge_transit_weeks = tuple(edges["tau0"])
        self.chokepoints = tuple(config["layout"]["chokepoints"])
        positions = {node: p for p, node in enumerate(self.chokepoints)}
        out_edges = [[] for _ in self.node_names]
        in_edges = [[] for _ in self.node_names]
        for e, (tail, head) in enumerate(zip(self.edge_tail, self.edge_head, strict=True)):
            _checked_index(tail, len(self.node_names), "edge tail")
            _checked_index(head, len(self.node_names), "edge head")
            out_edges[tail].append(e)
            in_edges[head].append(e)
        self.out_edges = tuple(map(tuple, out_edges))
        self.in_edges = tuple(map(tuple, in_edges))
        self.lane_edges = tuple(tuple(path) for path in lanes["edges"])
        self.lane_chokepoints = tuple(tuple(path) for path in lanes["chokepoints"])
        for li, path in enumerate(self.lane_edges):
            nodes = self._path_nodes(path)
            if tuple(node for node in nodes if node in positions) != self.lane_chokepoints[li]:
                raise ValueError(f"lane {li}: chokepoints do not match ordered edge path")

        routes = []
        edge_users = [[] for _ in self.edge_names]
        choke_users, pool_users = defaultdict(list), defaultdict(list)
        from_slots, to_slots = defaultdict(list), defaultdict(list)
        for s, (e, k, li) in enumerate(zip(slots["edge"], slots["k"], slots["lane"], strict=True)):
            e = _checked_index(e, len(self.edge_names), "slot edge")
            k = _checked_index(k, len(self.commodity_names), "slot commodity")
            if li is None:
                path, chokes = (e,), ()
                if self.edge_tail[e] in positions or self.edge_head[e] in positions:
                    raise ValueError(f"slot {s}: a chokepoint route requires a lane")
            else:
                li = _checked_index(li, len(self.lane_names), "slot lane")
                path, chokes = self.lane_edges[li], self.lane_chokepoints[li]
                if e != path[0]:
                    raise ValueError(f"slot {s}: edge must be the first edge of its lane")
            if any(k not in edges["K"][pe] for pe in path):
                raise ValueError(f"slot {s}: commodity is not allowed on every route edge")
            nodes = self._path_nodes(path)
            route = SlotRoute(
                slot_id=s,
                commodity_id=k,
                source_node=nodes[0],
                destination_node=nodes[-1],
                edge_id=e,
                lane_id=li,
                edges=path,
                nodes=nodes,
                chokepoints=chokes,
                chokepoint_positions=tuple(positions[c] for c in chokes),
                pool=goods["pool"][k],
                unit=static["units"][self.commodity_names[k]],
                nominal_transit_weeks=sum(self.edge_transit_weeks[pe] for pe in path),
                nominal_freight_per_unit=math.fsum(edges["c0"][pe] for pe in path),
            )
            routes.append(route)
            from_slots[route.source_node, k].append(s)
            to_slots[route.destination_node, k].append(s)
            for pe in path:
                edge_users[pe].append(s)
            for c in chokes:
                choke_users[c].append(s)
                pool_users[c, route.pool].append(s)
        shape = tuple(config["spaces"]["action"]["flows"]["shape"])
        if shape != (len(routes),):
            raise ValueError("action slots do not match flows shape")
        self.routes = tuple(routes)
        self.edges_to_slots = tuple(map(tuple, edge_users))
        self.chokepoints_to_slots = _freeze_groups(choke_users)
        self.chokepoint_pools_to_slots = _freeze_groups(pool_users)
        self.slots_from = _freeze_groups(from_slots)
        self.slots_to = _freeze_groups(to_slots)

    def _path_nodes(self, path):
        if not path or len(set(path)) != len(path):
            raise ValueError("route must contain a nonempty path without repeated edges")
        for e in path:
            _checked_index(e, len(self.edge_names), "route edge")
        if any(self.edge_head[a] != self.edge_tail[b] for a, b in zip(path, path[1:])):
            raise ValueError("route edges must form a continuous directed path")
        return (self.edge_tail[path[0]],) + tuple(self.edge_head[e] for e in path)

    def shared_resources(self, first_slot, second_slot):
        a = self.routes[_checked_index(first_slot, len(self.routes), "slot")]
        b = self.routes[_checked_index(second_slot, len(self.routes), "slot")]
        edges = tuple(e for e in a.edges if e in b.edges)
        chokes = tuple(c for c in a.chokepoints if c in b.chokepoints)
        pools = tuple((c, a.pool) for c in chokes) if a.pool == b.pool else ()
        return SharedResources(edges, chokes, pools)

    def transit_progress(self, edge_id, lane_id):
        """Resolve a known pipeline edge/lane, never an observed-mask padding row.

        Pass None only for a shipment confirmed off a lane. Hidden lane data
        must be treated as unknown by the caller, not inferred to be None.
        """
        e = _checked_index(edge_id, len(self.edge_names), "pipeline edge")
        if lane_id is None:
            if self.edge_tail[e] in self.chokepoints or self.edge_head[e] in self.chokepoints:
                raise ValueError("a shipment at a chokepoint needs a known lane")
            remaining, chokes, destination = (), (), self.edge_head[e]
        else:
            lane_id = _checked_index(lane_id, len(self.lane_names), "pipeline lane")
            path = self.lane_edges[lane_id]
            if e not in path:
                raise ValueError("pipeline edge is not on its lane")
            remaining = path[path.index(e) + 1 :]
            destination = self.edge_head[path[-1]]
            remaining_nodes = (self.edge_head[e],) + tuple(self.edge_head[pe] for pe in remaining)
            chokes = tuple(node for node in remaining_nodes if node in self.chokepoints)
        return TransitProgress(
            e,
            lane_id,
            self.edge_head[e],
            destination,
            remaining,
            chokes,
            sum(self.edge_transit_weeks[pe] for pe in remaining),
        )
