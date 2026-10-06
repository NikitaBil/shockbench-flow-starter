"""Sequential stock projection; each existing arrival and backlog is consumed once."""

from collections import defaultdict
from dataclasses import dataclass
from math import isfinite

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


def _nominal_fab_inputs(output_quantity: float) -> float:
    """Return nominal raw input for the current one-to-one Fab BOM proxy.

    Fab ``w_scr`` is the scrap observation window in weeks; it is not a
    fractional yield loss. ``tau`` is a duration as well. Neither belongs in
    the nominal material ratio. Any yield loss or reserve needs its own
    explicitly modeled policy parameter.
    """
    return output_quantity


@dataclass(frozen=True, slots=True)
class SafetyBufferPolicy:
    """Optional fractional buffer for each nominal BOM input.

    It is zero by default and separate from nominal BOM quantities. Existing
    grid ``ibar`` reserves remain controlled independently by ``safety_stock``.
    """

    input_buffer_fraction: float = 0.0

    def __post_init__(self):
        fraction = self.input_buffer_fraction
        if (
            isinstance(fraction, bool)
            or not isinstance(fraction, (int, float))
            or not isfinite(fraction)
            or fraction < 0
        ):
            raise ValueError("input_buffer_fraction must be a finite nonnegative number")

    def apply(self, nominal_input: float) -> float:
        """Apply the explicit policy to one nominal input requirement."""
        return nominal_input * (1.0 + self.input_buffer_fraction)


class NeedPlanner:
    def __init__(
        self,
        config,
        *,
        production_horizon=4,
        include_estimated_arrivals=False,
        safety_stock=True,
        safety_buffer_policy: SafetyBufferPolicy | None = None,
        production_enabled=True,
        shortage_cost_model=False,
    ):
        if not isinstance(production_horizon, int) or isinstance(production_horizon, bool) or production_horizon < 1:
            raise ValueError("production_horizon must be a positive integer")
        self.config = config
        self.production_horizon = production_horizon
        self.include_estimated_arrivals = include_estimated_arrivals
        self.safety_stock = safety_stock
        self.safety_buffer_policy = safety_buffer_policy or SafetyBufferPolicy()
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

    def _downstream_penalty(self, output_name):
        """Return max reachable sink penalty through the declared 1:1 BOM.

        Sink ``pi`` has units USD/(sink unit * weekly cost period). The BOM
        currently has no conversion coefficients, so propagation is allowed
        only when adjacent commodity units match exactly.
        """
        units = self.config["static"].get("units", {})
        candidates = []
        for osat_node in self.config["layout"]["osats"]:
            profile = self.profiles[osat_node].get("osat", {}).get("packages", {})
            for raw, package in profile.items():
                if raw != output_name:
                    continue
                if units.get(raw) != units.get(package):
                    continue
                product = self.commodities[package]
                candidates.extend(
                    pi for (sink, commodity), pi in self.penalties.items() if commodity == product
                )
        return max(candidates, default=None)

    def _shortage_cost(self, pair, reason, reader=None):
        """Marginal damage in USD per native input unit per weekly period.

        The optional model propagates published sink ``pi`` through explicit
        1:1 BOM links. For grid fuel, one GWh of unavailable fuel removes at
        most one GWh of segment generation (the simulator's segment stock is
        also GWh); its damage is grid VOLL [USD/GWh] plus the lost Fab output
        value [USD/output/week] times ``R/e`` [output/GWh]. Fuel shares set the
        segment's generation cap and are not a fuel conversion factor. The
        Fab term is a conservative max across Fabs connected to this grid.
        Unknown/mismatched units or energy observations disable the estimate.
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
            if self.config["static"].get("units", {}).get(name) not in {"GWh", "GWh fuel"}:
                return None
            if float(grid.get("shares", {}).get(name, 0.0)) <= 0.0:
                return None
            voll = float(grid.get("voll", 0.0))
            if reader is None:
                return None
            terms = []
            for row, fab_node in enumerate(self.config["layout"]["fabs"]):
                fab = self.profiles[fab_node].get("fab", {})
                if fab.get("grid") != self.config["static"]["nodes"]["id"][node]:
                    continue
                e = float(fab.get("e", 0.0))  # GWh per output unit
                penalty = self._downstream_penalty(fab.get("product"))
                restoration = reader.number("graph_now.fab.R", row)
                if e <= 0 or penalty is None or restoration is None:
                    continue
                terms.append(penalty * restoration / e)
            return voll + max(terms, default=0.0)
        if reason == "production":
            input_name = self.config["static"]["commodities"]["id"][commodity]
            if "osat" in profile:
                package = profile["osat"].get("packages", {}).get(input_name)
                units = self.config["static"].get("units", {})
                if package is None or units.get(input_name) != units.get(package):
                    return None
                package_id = self.commodities[package]
                return max(
                    (pi for (sink, item), pi in self.penalties.items() if item == package_id),
                    default=None,
                )
            for fab_node in self.config["layout"]["fabs"]:
                fab = self.profiles[fab_node].get("fab", {})
                if fab_node != node or fab.get("input") != input_name:
                    continue
                input_unit = self.config["static"].get("units", {}).get(input_name)
                output_unit = self.config["static"].get("units", {}).get(fab.get("product"))
                if input_unit != output_unit:
                    return None
                return self._downstream_penalty(fab.get("product"))
        return None

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

    def _minimum_route_transit_weeks(self, network, reader, source, destination, commodity):
        """Observed no-queue ETA for a specific source/destination commodity route.

        Queue work is modeled by the allocator's route completion estimate. This
        helper only backs production dates out by current observed edge transit;
        it never substitutes static ``tau0`` when a current edge time is hidden.
        """
        if network is None:
            return 0
        options = []
        for slot in network.slots_to.get((destination, commodity), ()):
            route = network.routes[slot]
            if route.source_node != source:
                continue
            transit = []
            for edge in route.edges:
                weeks = reader.number("graph_now.tau", edge)
                if weeks is None or weeks != int(weeks):
                    break
                transit.append(int(weeks))
            else:
                options.append(sum(transit))
        return min(options) if options else None

    def _net_events(self, state, pair, events, issues):
        """Net dated requirements against stock and eligible arrivals once."""
        stock = state.available_stock.get(pair)
        if stock is None or stock.value is None:
            issues.append(f"production:{pair}:unknown_time_phased_stock")
            return None
        balance = stock.value
        arrivals = sorted(
            (
                arrival.arrival_week,
                arrival.quantity.value,
            )
            for arrival in state.arrivals
            if (arrival.destination_node, arrival.commodity_id) == pair
            and arrival.arrival_week is not None
            and arrival.quantity.value is not None
            and (
                self.include_estimated_arrivals
                or (arrival.source == "observed" and arrival.quantity.source == "observed")
            )
        )
        remaining, index = [], 0
        for due, quantity in sorted(events):
            while index < len(arrivals) and arrivals[index][0] <= due:
                balance += arrivals[index][1]
                index += 1
            covered = min(balance, quantity)
            balance -= covered
            if quantity - covered > 0:
                remaining.append((due, quantity - covered))
        return remaining

    def _requirements(self, state, reader, issues, network=None):
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
            package_events = defaultdict(list)
            for (sink, product), rows in requirements.items():
                final = self.config["static"]["commodities"]["id"][product]
                for due, qty, *_ in rows:
                    if due < state.week + count:
                        package_events[final].append((due, qty))

            pending_packages = {name: sorted(events) for name, events in package_events.items()}
            osat_nodes = tuple(self.config["layout"]["osats"])
            for package, events in tuple(pending_packages.items()):
                for node in osat_nodes:
                    if package not in self.profiles[node]["osat"]["packages"].values():
                        continue
                    events = self._net_events(
                        state, (node, self.commodities[package]), events, issues
                    )
                    if events is None:
                        break
                pending_packages[package] = [] if events is None else events
            raw_events = defaultdict(list)
            osat_weekly_capacity = defaultdict(float)
            osat_rows = {node: row for row, node in enumerate(self.config["layout"]["osats"])}
            for node in self.config["layout"]["osats"]:
                row = osat_rows[node]
                cap = reader.number("graph_now.osat.thr_eff", row)
                if cap is None:
                    issues.append(f"osat:{node}:unknown_throughput")
                    continue
                for raw, package in sorted(self.profiles[node]["osat"]["packages"].items()):
                    events = pending_packages.get(package, [])
                    if not events:
                        continue
                    tau = int(self.profiles[node]["osat"].get("tau", 0))
                    residual, targets = [], []
                    for need_date, quantity in events:
                        production_week = max(state.week, need_date - tau)
                        capacity_left = max(0.0, cap - osat_weekly_capacity[node, production_week])
                        target = min(capacity_left, quantity)
                        osat_weekly_capacity[node, production_week] += target
                        if target > 0:
                            targets.append((production_week, target))
                        if quantity > target:
                            residual.append((need_date, quantity - target))
                    pending_packages[package] = residual
                    raw_id = self.commodities[raw]
                    for production_week, target in targets:
                        buffered_input = self.safety_buffer_policy.apply(target)
                        raw_events[node, raw_id].append((production_week, buffered_input))
                        pair = node, raw_id
                        requirements[pair].append(
                            (
                                production_week,
                                buffered_input,
                                2.0,
                                "production",
                                self._shortage_cost(pair, "production"),
                                (
                                    f"BOM: {raw} input maps to {package}; due before OSAT production",
                                    f"OSAT lead time {tau} weeks; production starts by week {production_week}",
                                    "shared OSAT throughput is a per-week ceiling; no equal product split",
                                ),
                            )
                        )

            fab_weekly_capacity = defaultdict(float)
            raw_to_ship = defaultdict(list)
            for (osat_node, raw_id), events in raw_events.items():
                net_raw = self._net_events(state, (osat_node, raw_id), events, issues)
                if net_raw is None:
                    continue
                raw_name = self.config["static"]["commodities"]["id"][raw_id]
                raw_to_ship[raw_name].extend((osat_node, due, qty) for due, qty in net_raw)

            for output, demand_events in raw_to_ship.items():
                demand_by_fab = []
                for osat_node, due, qty in demand_events:
                    possible = []
                    for fab_node in self.config["layout"]["fabs"]:
                        fab = self.profiles[fab_node]["fab"]
                        if fab["product"] != output:
                            continue
                        output_id = self.commodities[output]
                        route_eta = self._minimum_route_transit_weeks(
                            network, reader, fab_node, osat_node, output_id
                        )
                        if route_eta is not None:
                            possible.append((fab_node, route_eta))
                    if not possible:
                        issues.append(f"fab:{output}:unknown_route_eta_to:{osat_node}")
                        continue
                    # Choose the quickest observed route; the allocator then
                    # scores every candidate route against the same receipt deadline.
                    fab_node, route_eta = min(possible, key=lambda item: item[1])
                    demand_by_fab.append(
                        (fab_node, osat_node, max(state.week, due - route_eta), qty, route_eta)
                    )

                fab_nodes = [
                    fab_node
                    for fab_node in self.config["layout"]["fabs"]
                    if self.profiles[fab_node]["fab"]["product"] == output
                ]
                ship_events = [(due, qty) for _fab, _osat, due, qty, _eta in demand_by_fab]
                for fab_node in fab_nodes:
                    ship_events = self._net_events(
                        state, (fab_node, self.commodities[output]), ship_events, issues
                    )
                    if ship_events is None:
                        break
                if ship_events is None:
                    continue

                uncovered_by_date = defaultdict(float)
                for due, qty in ship_events:
                    uncovered_by_date[due] += qty
                net_demand_by_fab = []
                for fab_node, osat_node, due, _qty, route_eta in sorted(
                    demand_by_fab, key=lambda item: (item[2], item[0], item[1])
                ):
                    remaining = min(_qty, uncovered_by_date[due])
                    uncovered_by_date[due] -= remaining
                    if remaining > 0:
                        net_demand_by_fab.append((fab_node, osat_node, due, remaining, route_eta))

                # Retain each event's receiving OSAT so route ETA and production
                # start dates remain attached to the correct downstream demand.
                remaining_events = list(net_demand_by_fab)
                for fab_node in fab_nodes:
                    if not remaining_events:
                        break
                    fab_row = self.config["layout"]["fabs"].index(fab_node)
                    fab = self.profiles[fab_node]["fab"]
                    capacity = reader.number("graph_now.fab.cap_eff", fab_row)
                    if capacity is None:
                        issues.append(f"fab:{fab_node}:unknown_capacity")
                        continue
                    tau = int(fab.get("tau", 0))
                    candidates = [entry for entry in remaining_events if entry[0] == fab_node]
                    next_remaining = [entry for entry in remaining_events if entry[0] != fab_node]
                    for _node, osat_node, ship_date, quantity, route_eta in candidates:
                        production_week = max(state.week, ship_date - tau)
                        capacity_left = max(0.0, capacity - fab_weekly_capacity[fab_node, production_week])
                        target = min(capacity_left, quantity)
                        if target <= 0:
                            next_remaining.append((_node, osat_node, ship_date, quantity, route_eta))
                            continue
                        fab_weekly_capacity[fab_node, production_week] += target
                        if quantity > target:
                            next_remaining.append((_node, osat_node, ship_date, quantity - target, route_eta))
                        input_name = fab["input"]
                        pair = fab_node, self.commodities[input_name]
                        nominal_inputs = _nominal_fab_inputs(target)
                        inputs = self.safety_buffer_policy.apply(nominal_inputs)
                        requirements[pair].append(
                            (
                                production_week,
                                inputs,
                                2.0,
                                "production",
                                self._shortage_cost(pair, "production"),
                                (
                                    f"BOM: {input_name} per {output} uses the nominal one-to-one input ratio",
                                    f"Fab lead time {tau} weeks and observed route ETA {route_eta} "
                                    f"weeks; production starts by week {production_week}",
                                ),
                            )
                        )
                        energy = float(fab.get("e", 0.0)) * target
                        grid_name = fab.get("grid")
                        if grid_name:
                            grid_energy[grid_name, production_week] += energy
                    remaining_events = next_remaining
        for row, node in enumerate(self.config["layout"]["grids"]):
            generation = reader.number("graph_now.grid.G_bar", row)
            if generation is None:
                issues.append(f"grid:{node}:unknown_generation")
                continue
            grid_profile = self.profiles[node]["grid"]
            base_load = float(grid_profile.get("base_load", 0.0))
            for name, share in grid_profile["shares"].items():
                if name not in self.commodities:
                    continue
                pair = node, self.commodities[name]
                for h in range(count):
                    due_week = state.week + h
                    planned_generation = min(
                        generation,
                        base_load
                        + grid_energy.get((self.config["static"]["nodes"]["id"][node], due_week), 0.0),
                    )
                    requirements[pair].append(
                        (
                            due_week,
                            float(share) * planned_generation,
                            3.0,
                            "grid_fuel",
                            self._shortage_cost(pair, "grid_fuel", reader),
                            (
                                "fuel requirement covers static base load plus grounded Fab energy",
                                "generation is capped by observed deliverable G_bar, "
                                "not treated as a production target",
                                "Fab energy is assigned to its scheduled production week",
                            ),
                        )
                    )
        return requirements

    def plan(self, state, observation, network):
        reader = ObservationReader(observation)
        issues = []
        requirements = self._requirements(state, reader, issues, network)
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
