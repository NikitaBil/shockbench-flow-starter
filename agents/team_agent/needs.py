"""Sequential stock projection; each existing arrival and backlog is consumed once."""

from collections import defaultdict
from dataclasses import dataclass

from contracts import need_order_key
from observations import ObservationReader


@dataclass(frozen=True, slots=True)
class DeliveryNeed:
    need_id: str
    destination_node: int
    commodity_id: int
    quantity: float
    due_week: int
    priority: float
    reason: str
    shortage_cost_per_unit_usd: float | None
    confidence: float | None = None
    quantity_source: str = "estimated"
    assumptions: tuple[str, ...] = ()


class NeedPlanner:
    def __init__(self, config, *, production_horizon=4, include_estimated_arrivals=False, safety_stock=True):
        if not isinstance(production_horizon, int) or isinstance(production_horizon, bool) or production_horizon < 1:
            raise ValueError("production_horizon must be a positive integer")
        self.config = config
        self.production_horizon = production_horizon
        self.include_estimated_arrivals = include_estimated_arrivals
        self.safety_stock = safety_stock
        static = config["static"]
        self.commodities = {name: i for i, name in enumerate(static["commodities"]["id"])}
        self.nodes = {name: i for i, name in enumerate(static["nodes"]["id"])}
        self.profiles = {self.nodes[node["id"]]: node for node in static["instance"]["nodes"]}
        sinks = static["sinks"]
        self.penalties = {(n, k): float(pi) for n, k, pi in zip(sinks["node"], sinks["k"], sinks["pi"], strict=True)}
        self._backlog_since = {}
        self.last_issues = ()

    def _requirements(self, state, reader, issues):
        requirements = defaultdict(list)
        values, _seen = reader.field("demand_forecast.qty")
        if values.ndim != 2 or values.shape[0] != len(self.config["layout"]["demands"]):
            raise ValueError("demand_forecast.qty: shape does not match layout")
        for row, pair in enumerate(self.config["layout"]["demands"]):
            pair = tuple(pair)
            for h in range(min(values.shape[1], state.horizon - state.week + 1)):
                qty = reader.number("demand_forecast.qty", (row, h))
                if qty is None:
                    issues.append(f"demand:{pair}:{state.week + h}:unknown")
                elif qty > 0:
                    reason = "current_demand" if h == 0 else "forecast_demand"
                    requirements[pair].append(
                        (
                            state.week + h,
                            qty,
                            3.0,
                            reason,
                            self.penalties[pair],
                            ("published demand forecast, not realized demand",),
                        )
                    )
        count = min(self.production_horizon, state.horizon - state.week + 1)
        for row, node in enumerate(self.config["layout"]["fabs"]):
            cap = reader.number("graph_now.fab.cap_eff", row)
            if cap is None:
                issues.append(f"fab:{node}:unknown_capacity")
                continue
            pair = node, self.commodities[self.profiles[node]["fab"]["input"]]
            for h in range(count):
                requirements[pair].append(
                    (
                        state.week + h,
                        cap,
                        2.0,
                        "production",
                        None,
                        ("current effective fab capacity held constant; energy may limit output",),
                    )
                )
        for row, node in enumerate(self.config["layout"]["osats"]):
            cap = reader.number("graph_now.osat.thr_eff", row)
            packages = self.profiles[node]["osat"]["packages"]
            if cap is None:
                issues.append(f"osat:{node}:unknown_throughput")
                continue
            # A shared throughput budget, not the full capacity requested for every product.
            for raw in sorted(packages):
                pair = node, self.commodities[raw]
                for h in range(count):
                    requirements[pair].append(
                        (
                            state.week + h,
                            cap / len(packages),
                            2.0,
                            "production",
                            None,
                            ("current OSAT throughput held constant; equal package mix",),
                        )
                    )
        for row, node in enumerate(self.config["layout"]["grids"]):
            generation = reader.number("graph_now.grid.G_bar", row)
            if generation is None:
                issues.append(f"grid:{node}:unknown_generation")
                continue
            for name, share in self.profiles[node]["grid"]["shares"].items():
                if name not in self.commodities:
                    continue
                pair = node, self.commodities[name]
                for h in range(count):
                    requirements[pair].append(
                        (
                            state.week + h,
                            float(share) * generation,
                            3.0,
                            "grid_fuel",
                            None,
                            ("fuel target for full current deliverable generation; actual burn may be lower",),
                        )
                    )
        return requirements

    def plan(self, state, observation, network):
        del network  # Topology/feasibility belongs to the allocator, not demand projection.
        reader = ObservationReader(observation)
        issues = []
        requirements = self._requirements(state, reader, issues)
        arrivals = defaultdict(float)
        ids = set()
        for arrival in state.arrivals:
            if arrival.arrival_id in ids:
                raise ValueError("duplicate arrival_id would double count an arrival")
            ids.add(arrival.arrival_id)
            if arrival.arrival_week is None or arrival.quantity.value is None:
                continue
            estimated = arrival.source != "observed" or arrival.quantity.source != "observed"
            if estimated and not self.include_estimated_arrivals:
                continue
            if not state.week <= arrival.arrival_week <= state.horizon:
                continue
            key = arrival.destination_node, arrival.commodity_id, arrival.arrival_week
            arrivals[key] += arrival.quantity.value
        for pair, backlog in state.backlog.items():
            if backlog.value is None:
                issues.append(f"backlog:{pair}:unknown")
                continue
            if backlog.value <= 0:
                self._backlog_since.pop(pair, None)
                continue
            due = self._backlog_since.setdefault(pair, state.week)
            requirements[pair].append(
                (
                    due,
                    backlog.value,
                    4.0,
                    "backlog",
                    self.penalties.get(pair),
                    ("due week is earliest observation of continuous backlog, not original order date",),
                )
            )
        result = []
        for pair, requests in sorted(requirements.items()):
            stock = state.available_stock.get(pair)
            if stock is None:
                raise ValueError(f"need destination {pair} has no stock slot")
            balance = stock.value if stock.value is not None else 0.0
            assumptions = (
                () if stock.value is not None else ("hidden stock: zero lower-bound coverage, not observed zero",)
            )
            if stock.value is None:
                issues.append(f"stock:{pair}:unknown_coverage")
            last_week = state.week - 1
            for due, qty, priority, reason, cost, basis in sorted(
                requests, key=lambda item: (item[0], -item[2], item[3])
            ):
                coverage_week = max(state.week, due)
                for week in range(last_week + 1, coverage_week + 1):
                    balance += arrivals.get((*pair, week), 0.0)
                last_week = coverage_week
                covered = min(balance, qty)
                balance -= covered
                missing = qty - covered
                if missing > 0:
                    result.append(
                        DeliveryNeed(
                            f"{reason}:{pair[0]}:{pair[1]}:{due}",
                            *pair,
                            missing,
                            due,
                            priority,
                            reason,
                            cost,
                            assumptions=assumptions + basis,
                        )
                    )
            if self.safety_stock and "grid" in self.profiles[pair[0]]:
                name = self.config["static"]["commodities"]["id"][pair[1]]
                target = float(self.profiles[pair[0]]["grid"].get("ibar", {}).get(name, 0.0))
                due = max(state.week, max(request[0] for request in requests))
                missing = max(0.0, target - balance)
                if missing > 0:
                    result.append(
                        DeliveryNeed(
                            f"safety_stock:{pair[0]}:{pair[1]}:{due}",
                            *pair,
                            missing,
                            due,
                            1.0,
                            "safety_stock",
                            None,
                            assumptions=assumptions + ("static grid ibar reserve target",),
                        )
                    )
        self.last_issues = tuple(issues)
        return tuple(sorted(result, key=need_order_key))
