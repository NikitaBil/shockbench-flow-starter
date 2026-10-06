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
    def __init__(
        self,
        config,
        *,
        production_horizon=4,
        include_estimated_arrivals=False,
        safety_stock=True,
        production_enabled=True,
        shortage_cost_model=True,
    ):
        if not isinstance(production_horizon, int) or isinstance(production_horizon, bool) or production_horizon < 1:
            raise ValueError("production_horizon must be a positive integer")
        self.config = config
        self.production_horizon = production_horizon
        self.include_estimated_arrivals = include_estimated_arrivals
        self.safety_stock = safety_stock
        self.production_enabled = bool(production_enabled)
        self.shortage_cost_model = bool(shortage_cost_model)
        static = config["static"]
        self.commodities = {name: i for i, name in enumerate(static["commodities"]["id"])}
        self.nodes = {name: i for i, name in enumerate(static["nodes"]["id"])}
        self.profiles = {self.nodes[node["id"]]: node for node in static["instance"]["nodes"]}
        sinks = static["sinks"]
        self.penalties = {(n, k): float(pi) for n, k, pi in zip(sinks["node"], sinks["k"], sinks["pi"], strict=True)}
        self._backlog_since = {}
        self.last_issues = ()

    def _shortage_cost(self, pair, reason):
        """USD/(native unit * week): sink pi; otherwise propagated lost-value proxy.

        Assumption: indirect inputs inherit downstream value per unit. Grid fuel
        uses node VOLL ($/MWh) times the observed fuel share and commodity-unit
        conversion (GWh -> MWh). This is a transparent marginal proxy, not price.
        """
        if not self.shortage_cost_model:
            return None
        direct = self.penalties.get(pair)
        if direct is not None:
            return direct
        node, commodity = pair
        profile = self.profiles[node]
        if reason == "grid_fuel" and "grid" in profile:
            grid = profile["grid"]
            name = self.config["static"]["commodities"]["id"][commodity]
            share = float(grid.get("shares", {}).get(name, 0.0))
            voll = float(grid.get("voll", 0.0))
            # fuel is GWh in the shipped instances; VOLL is USD/MWh.
            return voll * share * 1000.0
        # Production inputs inherit the best published downstream unit penalty;
        # one native input unit is assumed to protect one output unit (BOM proxy).
        return max(self.penalties.values(), default=0.0) or None

    def _coverage(self, state, pair, cutoff_week):
        stock = state.available_stock.get(pair)
        if stock is None or stock.value is None:
            return None
        coverage = stock.value
        for arrival in state.arrivals:
            if (arrival.destination_node, arrival.commodity_id) != pair:
                continue
            if arrival.quantity.value is None or arrival.arrival_week is None or arrival.arrival_week > cutoff_week:
                continue
            if arrival.source != "observed" or arrival.quantity.source != "observed":
                if not self.include_estimated_arrivals:
                    continue
            coverage += arrival.quantity.value
        return coverage

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
        grid_energy = defaultdict(float)
        # Translate only published downstream package demand into upstream
        # production inputs. No capacity becomes a target by itself.
        if self.production_enabled:
            packages_needed = defaultdict(float)
            for (sink, product), rows in requirements.items():
                final = self.config["static"]["commodities"]["id"][product]
                for due, qty, *_ in rows:
                    if due < state.week + count:
                        packages_needed[final] += qty
            # Finished packages already at any capable OSAT cover demand before
            # assigning capacity. Unknown stock blocks that package's production
            # target rather than being interpreted as zero.
            osat_nodes = tuple(self.config["layout"]["osats"])
            blocked_packages = set()
            cutoff = state.week + count - 1
            for node in osat_nodes:
                for package in self.profiles[node]["osat"]["packages"].values():
                    pair = node, self.commodities[package]
                    coverage = self._coverage(state, pair, cutoff)
                    if coverage is None:
                        blocked_packages.add(package)
                        issues.append(f"production:{node}:{package}:unknown_finished_stock")
                    else:
                        packages_needed[package] = max(0.0, packages_needed[package] - coverage)
            remaining_packages = dict(packages_needed)
            osat_targets = []
            raw_input_needs = defaultdict(float)
            for row, node in enumerate(self.config["layout"]["osats"]):
                cap = reader.number("graph_now.osat.thr_eff", row)
                if cap is None:
                    issues.append(f"osat:{node}:unknown_throughput")
                    continue
                profile = self.profiles[node]["osat"]["packages"]
                capacity_left = cap * count
                for raw, package in sorted(profile.items()):
                    if package in blocked_packages:
                        continue
                    target = min(capacity_left, remaining_packages.get(package, 0.0))
                    if target <= 0:
                        continue
                    capacity_left -= target
                    remaining_packages[package] -= target
                    osat_targets.append((node, raw, target))
                    raw_input_needs[node, raw] += target
                    pair = node, self.commodities[raw]
                    requirements[pair].append(
                        (
                            state.week,
                            target,
                            2.0,
                            "production",
                            self._shortage_cost(pair, "production"),
                            (
                                f"BOM: {raw} input maps to {package}; target from published downstream forecast",
                                "shared OSAT throughput is a ceiling; no equal product split",
                            ),
                        )
                    )
            # Existing raw inputs at the consuming OSATs reduce upstream Fab
            # production. Fab finished raw stock is also a source that can be
            # shipped, so subtract it from the residual input requirement.
            raw_needed = defaultdict(float)
            for (node, raw), target in raw_input_needs.items():
                pair = node, self.commodities[raw]
                coverage = self._coverage(state, pair, cutoff)
                if coverage is None:
                    issues.append(f"production:{node}:{raw}:unknown_input_stock")
                    continue
                raw_needed[raw] += max(0.0, target - coverage)
            for node in self.config["layout"]["fabs"]:
                fab = self.profiles[node]["fab"]
                output = fab["product"]
                pair = node, self.commodities[output]
                coverage = self._coverage(state, pair, cutoff)
                if coverage is None:
                    raw_needed[output] = 0.0
                    issues.append(f"production:{node}:{output}:unknown_fab_output_stock")
                else:
                    raw_needed[output] = max(0.0, raw_needed[output] - coverage)
            remaining_raw = dict(raw_needed)
            for row, node in enumerate(self.config["layout"]["fabs"]):
                profile = self.profiles[node]["fab"]
                output, input_name = profile["product"], profile["input"]
                capacity = reader.number("graph_now.fab.cap_eff", row)
                if capacity is None:
                    issues.append(f"fab:{node}:unknown_capacity")
                    continue
                target = min(remaining_raw.get(output, 0.0), capacity * count)
                if target <= 0:
                    continue
                remaining_raw[output] -= target
                pair = node, self.commodities[input_name]
                inputs = target * (1.0 + float(profile.get("w_scr", 0.0)) / max(float(profile.get("tau", 1)), 1.0))
                energy = float(profile.get("e", 0.0)) * target
                grid_name = profile.get("grid")
                if grid_name:
                    grid_energy[grid_name] += energy
                requirements[pair].append(
                    (
                        state.week,
                        inputs,
                        2.0,
                        "production",
                        self._shortage_cost(pair, "production"),
                        (
                            f"BOM: {input_name} per {output} includes nominal scrap factor w_scr/tau",
                            f"target {target:g} output units is bounded by downstream package forecast "
                            "and observed capacity",
                        ),
                    )
                )
        for row, node in enumerate(self.config["layout"]["grids"]):
            generation = reader.number("graph_now.grid.G_bar", row)
            if generation is None:
                issues.append(f"grid:{node}:unknown_generation")
                continue
            grid_profile = self.profiles[node]["grid"]
            base_load = float(grid_profile.get("base_load", 0.0))
            planned_generation = min(
                generation,
                base_load + grid_energy.get(self.config["static"]["nodes"]["id"][node], 0.0) / count,
            )
            for name, share in grid_profile["shares"].items():
                if name not in self.commodities:
                    continue
                pair = node, self.commodities[name]
                for h in range(count):
                    requirements[pair].append(
                        (
                            state.week + h,
                            float(share) * planned_generation,
                            3.0,
                            "grid_fuel",
                            self._shortage_cost(pair, "grid_fuel"),
                            (
                                "fuel requirement covers static base load plus grounded Fab energy",
                                "generation is capped by observed deliverable G_bar, "
                                "not treated as a production target",
                                "horizon Fab energy is spread evenly across production_horizon weeks",
                            ),
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
        need_id_counts = defaultdict(int)
        for pair, requests in sorted(requirements.items()):
            stock = state.available_stock.get(pair)
            if stock is None:
                raise ValueError(f"need destination {pair} has no stock slot")
            assumptions = (
                () if stock.value is not None else ("hidden stock: zero lower-bound coverage, not observed zero",)
            )
            if stock.value is None:
                issues.append(f"stock:{pair}:unknown_coverage")
                # Do not silently turn unknown stock into a numeric zero and
                # manufacture a replenishment order from missing information.
                continue
            balance = stock.value
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
                    base_id = f"{reason}:{pair[0]}:{pair[1]}:{due}"
                    occurrence = need_id_counts[base_id]
                    need_id_counts[base_id] += 1
                    result.append(
                        DeliveryNeed(
                            base_id if occurrence == 0 else f"{base_id}:{occurrence + 1}",
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

    @staticmethod
    def export_examples(needs):
        """JSON-ready handoff rows; keep rank and assumptions beside quantities."""
        ordered = sorted(needs, key=need_order_key)
        return tuple(
            {
                "priority_rank": rank,
                "need_id": need.need_id,
                "destination_node": need.destination_node,
                "commodity_id": need.commodity_id,
                "quantity": need.quantity,
                "due_week": need.due_week,
                "priority": need.priority,
                "reason": need.reason,
                "quantity_source": need.quantity_source,
                "shortage_cost_per_unit_usd": need.shortage_cost_per_unit_usd,
                "confidence": need.confidence,
                "assumptions": list(need.assumptions),
            }
            for rank, need in enumerate(ordered, 1)
        )
