"""V4: allocate existing delivery needs against shared current-week resources.

Future lane edges/pools are not reserved. Current dispatch uses pre-dispatch
stock; same-week ordinary arrivals, supply and production are unavailable.
"""

import math
from collections import defaultdict

import numpy as np


if __package__:
    from .contracts import AllocationResult, DecisionReason, ResourceUsage, UnmetNeed, need_order_key
    from .delivery import DeliveryEvaluator
    from .network import NetworkTracker, StaticNetwork
    from .queue_forecast import QueueForecaster
else:
    from contracts import AllocationResult, DecisionReason, ResourceUsage, UnmetNeed, need_order_key
    from delivery import DeliveryEvaluator
    from network import NetworkTracker, StaticNetwork
    from queue_forecast import QueueForecaster


class Allocator:
    def __init__(self, config, network=None):
        self.config = config
        self.network = network if network is not None else StaticNetwork(config)
        self.tracker = NetworkTracker(config, self.network)
        self.delivery = DeliveryEvaluator(config, self.network)
        # Reuse the state owner's tested public-instance detour interpretation.
        fleet = QueueForecaster(config)
        self.fleet_terms, self.fleet_caps = fleet.fleet_terms, fleet.fleet_caps
        self.stock_indices = {tuple(pair): i for i, pair in enumerate(config["layout"]["stock_slots"])}
        self.pools = tuple(config["static"]["commodities"]["pool"])
        self.allowed = tuple(config["static"]["edges"]["K"])
        self.last_snapshot = None

    def _weight(self, edge, lane):
        return sum(delta for match, delta in self.fleet_terms.get(edge, ()) if match is None or match == lane)

    def _release_fleet_bound(self, state, observation, snapshot):
        """Upper bound for today's automatic releases, not a future reservation.

        With complete visible cargo, group eligible releases by (edge, pool)
        and cap by edge/throughput. With incomplete cargo metadata, bound each
        chokepoint detour by its full current estimated capacity. This can
        over-reserve fleet; it never fabricates an empty queue during blackout.
        """
        net, fields = self.network, snapshot.fields
        incomplete = any(issue.startswith(("pipeline:", "queue:")) for issue in getattr(state, "issues", ()))
        for key in ("pipeline.qty.observed", "queue_lots.qty.observed"):
            if key in observation and not np.all(observation[key]):
                incomplete = True
        groups = defaultdict(list)
        positions = {node: p for p, node in enumerate(net.chokepoints)}

        def add(edge, commodity, lane, quantity):
            nonlocal incomplete
            if edge is None or lane is None or quantity.value is None:
                incomplete = True
                return
            if commodity not in self.allowed[edge] or fields["graph_now.prohibited"].values[edge, commodity]:
                return
            weight = self._weight(edge, lane)
            if weight:
                groups[edge, self.pools[commodity]].append((quantity.value, weight))

        for lot in state.queues:
            add(lot.next_edge_id, lot.commodity_id, lot.lane_id, lot.quantity)
        for lot in state.pipeline:
            if lot.edge_id is None or lot.edge_arrival_week is None:
                incomplete = True
                continue
            if lot.edge_arrival_week != state.week or net.edge_head[lot.edge_id] not in positions:
                continue
            if lot.lane_status != "known" or lot.lane_id is None:
                incomplete = True
                continue
            path = net.lane_edges[lot.lane_id]
            add(path[path.index(lot.edge_id) + 1], lot.commodity_id, lot.lane_id, lot.quantity)
        if incomplete:
            groups.clear()
            for edge, terms in self.fleet_terms.items():
                if net.edge_tail[edge] not in positions:
                    continue
                # Sum matching terms; a wildcard can coexist with lane terms.
                lanes = {lane for lane, _delta in terms} | {None}
                weight = max(self._weight(edge, lane) for lane in lanes)
                for pool in {self.pools[k] for k in self.allowed[edge]}:
                    groups[edge, pool].append((math.inf, weight))
        bound = {pool: 0.0 for pool in self.fleet_caps}
        for (edge, pool), lots in groups.items():
            position = positions[net.edge_tail[edge]]
            capacity = min(
                float(fields["graph_now.u"].values[edge]),
                float(fields[f"graph_now.kappa.{pool}"].values[position]),
            )
            if fields["graph_now.open"].values[position] == 0:
                capacity = 0.0
            bound[pool] += min(sum(qty for qty, _ in lots), capacity) * max(weight for _, weight in lots)
        return {pool: min(cap, bound[pool]) for pool, cap in self.fleet_caps.items()}

    def allocate(self, state, needs, observation, network=None):
        if network is not None and network is not self.network:
            raise ValueError("allocator must share the configured StaticNetwork")
        if getattr(state, "availability_mode", "pre_dispatch_stock_t_minus_1") != "pre_dispatch_stock_t_minus_1":
            raise ValueError("allocator requires pre-dispatch stock; same-week supply/arrivals are not dispatchable")
        if state.week != int(observation["week"][0]) or state.horizon != self.config["T"]:
            raise ValueError("state week/horizon does not match observation/config")
        snapshot = self.tracker.update(observation)
        net = self.network
        self.last_snapshot = snapshot
        # Unknown/estimated stock is not spent as if physically available.
        stock_start = {
            key: float(qty.value) if qty.source == "observed" and qty.value is not None else 0.0
            for key, qty in state.available_stock.items()
        }
        stocks = dict(stock_start)
        edge_start = snapshot.fields["graph_now.u"].values
        edges = edge_start.copy()
        release_bound = self._release_fleet_bound(state, observation, snapshot)
        fleet_start = {pool: max(0.0, cap - release_bound[pool]) for pool, cap in self.fleet_caps.items()}
        fleet = dict(fleet_start)
        flows = np.zeros(len(net.routes), dtype=np.float64)
        unmet, reasons, cache = [], [], {}
        for need in sorted(needs, key=need_order_key):
            key = need.destination_node, need.commodity_id
            if key not in cache:
                cache[key] = self.delivery.options(snapshot, *key)
            remaining = float(need.quantity)
            blocked = set()
            for option in cache[key]:
                if remaining <= 0:
                    break
                route = net.routes[option.slot_id]
                origin = route.source_node, route.commodity_id
                if option.no_wait_arrival_week > state.horizon:
                    blocked.add("earliest_arrival_beyond_horizon")
                    continue
                if not option.permission_observed:
                    blocked.add("unconfirmed_permission")
                    continue
                weight = self._weight(route.edge_id, route.lane_id)
                qty = min(remaining, stocks.get(origin, 0.0), float(edges[route.edge_id]))
                if weight:
                    qty = min(qty, fleet[route.pool] / weight)
                if qty <= 0:
                    blocked.add("current_resources_exhausted_or_unknown")
                    continue
                flows[route.slot_id] += qty
                stocks[origin] -= qty
                edges[route.edge_id] -= qty
                fleet[route.pool] = max(0.0, fleet[route.pool] - weight * qty)
                remaining = max(0.0, remaining - qty)
                reasons.append(
                    DecisionReason(
                        "allocated_current_resources",
                        f"Assigned {qty:g} {route.unit}; full-route transport estimate "
                        f"{option.transport_cost_per_unit:g} USD/unit; later edges/pools are not reserved.",
                        need.need_id,
                        route.slot_id,
                    )
                )
                if option.uncertain_fields or option.estimated_completion_week is None:
                    reasons.append(
                        DecisionReason(
                            "delivery_estimate_uncertain",
                            "Current capacity/price estimates or missing queue forecast make completion uncertain.",
                            need.need_id,
                            route.slot_id,
                        )
                    )
            if remaining > 0:
                reason = (
                    ",".join(sorted(blocked)) or "current_resources_exhausted_or_unknown"
                    if cache[key]
                    else "no_permitted_delivery_slot"
                )
                unmet.append(UnmetNeed(need.need_id, remaining, reason))
        usage = []
        for key, start in stock_start.items():
            used = start - stocks[key]
            if used > 0:
                unit = net.routes[next(iter(net.slots_from[key]))].unit
                usage.append(ResourceUsage("stock", self.stock_indices[key], unit, used, start, "observed"))
        for edge in sorted({route.edge_id for route in net.routes}):
            used = float(edge_start[edge] - edges[edge])
            if used > 0:
                commodity = self.allowed[edge][0]
                unit = self.config["static"]["units"][net.commodity_names[commodity]]
                source = "observed" if snapshot.fields["graph_now.u"].observed[edge] else "estimated"
                usage.append(ResourceUsage("edge", edge, unit, used, float(edge_start[edge]), source))
        for position, pool in enumerate(("tb", "ct")):
            used = fleet_start[pool] - fleet[pool]
            if used > 0:
                usage.append(
                    ResourceUsage(
                        "fleet_pool", position, f"{pool} native-unit weeks", used, fleet_start[pool], "estimated", pool
                    )
                )
            if release_bound[pool] > 0:
                reasons.append(
                    DecisionReason(
                        "automatic_release_fleet_bound",
                        f"Reserved up to {release_bound[pool]:g} {pool} unit-weeks for today's automatic releases.",
                    )
                )
        return AllocationResult(flows, tuple(unmet), tuple(usage), tuple(reasons))
