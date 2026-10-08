"""Recover current entry capacity stranded by pro-rata edge/stock clipping.

Additive, production-only, bounded by existing pre-preference requests. Route
preferences remain soft marginal weights after their baseline dispatch floor.
Receiver cover and no-queue transit are estimates, never guaranteed arrivals.
Fleet rows constrain only today's duplicate-edge usage, not future route edges.
"""

import numpy as np
from queue_forecast import QueueForecaster
from sales_dispatch import SalesDispatch


class ProductionResidual:
    def __init__(self, config, network, *, enabled=False):
        if not isinstance(enabled, bool):
            raise ValueError("production_residual_enabled must be a boolean")
        self.enabled, self.last = enabled, {"status": "disabled"}
        if not enabled:
            return
        self.network = network
        # Reuse the project's recipe, rate, penalty and arrival interpretation.
        self.models = (SalesDispatch(config, network, stage="fab", cover=1),
                       SalesDispatch(config, network, stage="raw", cover=1))
        self.targets = {pair: model for model in self.models for pair in model.targets}
        self.slots = tuple(r.slot_id for r in network.routes
                           if (r.destination_node, r.commodity_id) in self.targets)
        self.stocks = {tuple(pair): i for i, pair in enumerate(config["layout"]["stock_slots"])}
        self.entries = {}
        for r in network.routes:
            self.entries.setdefault(r.edge_id, []).append(r.slot_id)
        fleet = QueueForecaster(config)
        self.fleet_caps = fleet.fleet_caps
        self.weights = np.asarray([sum(delta for lane, delta in fleet.fleet_terms.get(r.edge_id, ())
                                      if lane is None or lane == r.lane_id) for r in network.routes])
        self.output_stocks = {}
        names = {name: i for i, name in enumerate(network.node_names)}
        goods = {name: i for i, name in enumerate(network.commodity_names)}
        for attrs in config["static"]["instance"]["nodes"]:
            node = names[attrs["id"]]
            recipe = ({attrs["fab"]["input"]: attrs["fab"]["product"]} if "fab" in attrs
                      else attrs.get("osat", {}).get("packages", {}))
            for raw, output in recipe.items():
                if output in attrs.get("stock", {}):
                    self.output_stocks[node, goods[raw]] = (
                        (node, goods[output]), float(attrs["stock"][output]["storage"]))

    @staticmethod
    def number(obs, key, index):
        if key not in obs:
            return None
        seen = np.broadcast_to(obs[key + ".observed"], np.asarray(obs[key]).shape)
        if not seen[index]:
            return None
        value = float(obs[key][index])
        return value if np.isfinite(value) and value >= 0 else None

    def stock(self, obs, pair):
        row = self.stocks.get(pair)
        return None if row is None else self.number(obs, "stock.qty", row)

    def apply(self, flows, requests, observation, *, preferred=None):
        self.last = {"status": "disabled" if not self.enabled else "no_visible_residual"}
        if not self.enabled:
            return flows
        preferred = requests if preferred is None else preferred
        network, obs = self.network, observation
        week = int(obs["week"][0])
        candidates, upper, benefit, distances = [], [], [], {}
        for s in self.slots:
            route = network.routes[s]
            pair = route.destination_node, route.commodity_id
            source = route.source_node, route.commodity_id
            stock, target_stock = self.stock(obs, source), self.stock(obs, pair)
            capacity = self.number(obs, "graph_now.u", route.edge_id)
            if (stock is None or target_stock is None or capacity is None or capacity == 0
                    or not obs["action_mask"][s]):
                continue
            spare_stock = max(0., stock - float(flows[list(network.slots_from[source])].sum()))
            spare_edge = max(0., capacity - float(flows[self.entries[route.edge_id]].sum()))
            bound = min(max(0., requests[s] - flows[s]), spare_stock, spare_edge)
            # A deliberate zero remains zero; weak preferences can be relaxed
            # only within the original closure/horizon-filtered request.
            if preferred[s] <= 0:
                continue
            if bound <= 1e-9:
                continue
            # A visible ban/zero is real; hidden padding is never a constraint.
            if any(self.number(obs, "graph_now.prohibited", (e, route.commodity_id)) == 1
                   or self.number(obs, "graph_now.u", e) == 0 for e in route.edges):
                continue
            output = self.output_stocks.get(pair)
            if output is not None:
                output_stock = self.stock(obs, output[0])
                if output_stock is None or output_stock >= output[1]:
                    continue
            model = self.targets[pair]
            rate_key, row, fraction = model.target_rates[pair]
            observed_rate = self.number(obs, rate_key, row)
            rate = observed_rate * fraction if observed_rate is not None else model.nominal[pair]
            if rate <= 0:
                continue
            distance = sum(self.number(obs, "graph_now.tau", e)
                           if self.number(obs, "graph_now.tau", e) is not None
                           else network.edge_transit_weeks[e] for e in route.edges)
            observed_costs = [self.number(obs, "graph_now.c", e) for e in route.edges]
            freight = (sum(observed_costs) if all(c is not None for c in observed_costs)
                       else route.nominal_freight_per_unit)
            tariff = sum(self.number(obs, "graph_now.tariff", (e, route.commodity_id)) or 0.
                         for e in route.edges)
            value = (model.penalties[pair] * np.exp(-.05 * (distance + model.production_times[pair]))
                     - freight - tariff * model.goods_value[route.commodity_id])
            if value <= 0:
                continue
            candidates.append(s)
            upper.append(bound)
            benefit.append(value * min(1., preferred[s] / requests[s]))
            distances[s] = distance
        if not candidates:
            return flows
        rows, limits = [], []

        def bound(ids, cap, weights=None):
            row = np.zeros(len(candidates))
            ids = set(ids)
            for i, s in enumerate(candidates):
                if s in ids:
                    row[i] = 1. if weights is None else weights[s]
            if row.any():
                rows.append(row)
                limits.append(max(0., float(cap)))

        for pair, ids in network.slots_from.items():
            stock = self.stock(obs, pair)
            if stock is not None:
                bound(ids, stock - float(flows[list(ids)].sum()))
        for edge, ids in self.entries.items():
            capacity = self.number(obs, "graph_now.u", edge)
            if capacity is not None:
                bound(ids, capacity - float(flows[ids].sum()))
        # Baseline has no fleet coordination with releases. Additions must not
        # consume fleet at all: leave that currently nonbinding joint pool alone.
        # Alternate entry edges whose duplicate weight is positive get no refill.
        upper = np.asarray(upper)
        upper[self.weights[candidates] > 0] = 0.
        arrivals = {id(model): model._arrivals(obs) for model in self.models}
        for pair in sorted({(network.routes[s].destination_node, network.routes[s].commodity_id)
                            for s in candidates}):
            model = self.targets[pair]
            rate_key, row, fraction = model.target_rates[pair]
            current = self.number(obs, rate_key, row)
            rate = current * fraction if current is not None else model.nominal[pair]
            ids = list(network.slots_to[pair])
            for delay in sorted({distances[s] for s in candidates if s in ids}):
                active = [s for s in candidates if s in ids and distances[s] <= delay]
                incoming = sum(q for due, q in arrivals[id(model)].get(pair, ()) if due <= week + delay + 1)
                # Conservative credit of all today's baseline requests to this
                # receiver avoids adding twice across sources. This is a cover
                # estimate; queued requests are NOT labelled timely deliveries.
                cap = rate * (delay + 1) - self.stock(obs, pair) - incoming - float(flows[ids].sum())
                bound(active, cap)
        if not upper.any():
            return flows
        from scipy.optimize import linprog

        values = np.asarray(benefit)
        solved = linprog(-values / values.max(), A_ub=np.asarray(rows), b_ub=limits,
                         bounds=np.column_stack((np.zeros(len(upper)), upper)), method="highs",
                         options={"time_limit": .05, "maxiter": 200})
        if not solved.success:
            self.last = {"status": "solver_control_fallback", "solver_status": int(solved.status)}
            return flows
        extra = np.minimum(upper, np.maximum(0., solved.x))
        # Roundoff cannot double-spend the residual budget. Scaling the additive
        # solution down preserves all nonnegative inequalities.
        for row, cap in zip(rows, limits):
            used = float(row @ extra)
            if used > cap and used > 0:
                extra *= max(0., cap / used) * (1 - 1e-12)
        out = flows.copy()
        out[candidates] += extra
        self.last = {"status": "recovered", "slots": tuple(candidates),
                     "extra": extra.copy(), "eta_status": "conditional_estimate"}
        return out
