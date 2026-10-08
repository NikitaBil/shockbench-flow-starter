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
        fuel_replenishment_enabled=False,
    ):
        if not isinstance(production_horizon, int) or isinstance(production_horizon, bool) or production_horizon < 1:
            raise ValueError("production_horizon must be a positive integer")
        for name, value in (("include_estimated_arrivals", include_estimated_arrivals),
                            ("safety_stock", safety_stock), ("production_enabled", production_enabled)):
            if not isinstance(value, bool):
                raise ValueError(f"{name} must be a boolean")
        self.config = config
        self.production_horizon = production_horizon
        self.include_estimated_arrivals = include_estimated_arrivals
        self.safety_stock = safety_stock
        self.safety_buffer_policy = safety_buffer_policy or SafetyBufferPolicy()
        self.production_enabled = bool(production_enabled)
        if not isinstance(shortage_cost_model, bool):
            raise ValueError("shortage_cost_model must be a boolean")
        self.shortage_cost_model = bool(shortage_cost_model)
        if not isinstance(fuel_replenishment_enabled, bool):
            raise ValueError("fuel_replenishment_enabled must be a boolean")
        self.fuel_replenishment_enabled = fuel_replenishment_enabled
        static = config["static"]
        self.commodities = {name: i for i, name in enumerate(static["commodities"]["id"])}
        self.nodes = {name: i for i, name in enumerate(static["nodes"]["id"])}
        self.profiles = {self.nodes[node["id"]]: node for node in static["instance"]["nodes"]}
        sinks = static["sinks"]
        self.penalties = {(n, k): float(pi) for n, k, pi in zip(sinks["node"], sinks["k"], sinks["pi"], strict=True)}
        self._backlog_since = {}
        self.last_issues = ()
        self.last_trace = ()

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
                    raise ValueError(
                        f"VOLL unit mismatch in OSAT BOM {raw}->{package}: "
                        f"{units.get(raw)!r} != {units.get(package)!r}"
                    )
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
        Missing energy observations disable the estimate. A unit mismatch is
        a configuration error and raises ValueError so an enabled VOLL model
        cannot silently return a dimensionally invalid cost.
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
            unit = self.config["static"].get("units", {}).get(name)
            if unit not in {"GWh", "GWh fuel"}:
                raise ValueError(f"VOLL unit mismatch for grid fuel {name}: expected GWh, got {unit!r}")
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
                if package is None:
                    return None
                if units.get(input_name) != units.get(package):
                    raise ValueError(
                        f"VOLL unit mismatch in OSAT BOM {input_name}->{package}: "
                        f"{units.get(input_name)!r} != {units.get(package)!r}"
                    )
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
                    raise ValueError(
                        f"VOLL unit mismatch in Fab BOM {input_name}->{fab.get('product')}: "
                        f"{input_unit!r} != {output_unit!r}"
                    )
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

    def _supply_calendar(self, state, pair, issues):
        """Mutable dated supply, shared across every consumer of this stock."""
        stock = state.available_stock.get(pair)
        if stock is None or stock.value is None:
            issues.append(f"production:{pair}:unknown_time_phased_stock")
            return None
        return sorted(
            [[state.week, stock.value]]
            + [
                [arrival.arrival_week, arrival.quantity.value]
                for arrival in state.arrivals
                if (arrival.destination_node, arrival.commodity_id) == pair
                and arrival.arrival_week is not None
                and arrival.quantity.value is not None
                and (
                    self.include_estimated_arrivals
                    or (arrival.source == "observed" and arrival.quantity.source == "observed")
                )
            ]
        )

    @staticmethod
    def _consume_supply(calendar, deadline, quantity):
        remaining = quantity
        for lot in calendar:
            if lot[0] > deadline:
                break
            covered = min(lot[1], remaining)
            lot[1] -= covered
            remaining -= covered
            if remaining <= 0:
                break
        return remaining

    def _net_events(self, state, pair, events, issues):
        """Net dated requirements against stock and eligible arrivals once."""
        calendar = self._supply_calendar(state, pair, issues)
        if calendar is None:
            return None
        remaining = []
        for due, quantity in sorted(events):
            missing = self._consume_supply(calendar, due, quantity)
            if missing > 0:
                remaining.append((due, missing))
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
        fuel_count = count
        if self.fuel_replenishment_enabled and network is not None:
            source_leads, transfer_leads = [], []
            node_types = self.config["static"]["nodes"]["type"]
            for route in network.routes:
                commodity_name = self.config["static"]["commodities"]["id"][route.commodity_id]
                if commodity_name not in {"lng", "crude"}:
                    continue
                edge_times = [reader.number("graph_now.tau", edge) for edge in route.edges]
                if all(value is not None and value == int(value) for value in edge_times):
                    transit = sum(int(value) for value in edge_times)
                    if (
                        "terminal" in self.profiles[route.destination_node]
                        and node_types[route.source_node] == "source"
                    ):
                        source_leads.append(transit)
                    if (
                        "terminal" in self.profiles[route.source_node]
                        and node_types[route.destination_node] == "grid"
                    ):
                        transfer_leads.append(transit)
            if source_leads:
                fuel_count = min(
                    state.horizon - state.week + 1,
                    max(count, max(source_leads) + max(transfer_leads, default=0) + 1),
                )
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

            fab_rows = {node: row for row, node in enumerate(self.config["layout"]["fabs"])}
            for output, demand_events in raw_to_ship.items():
                output_id = self.commodities[output]
                fab_nodes = [
                    fab_node
                    for fab_node in self.config["layout"]["fabs"]
                    if self.profiles[fab_node]["fab"]["product"] == output
                ]
                calendars, capacities, options_by_osat = {}, {}, {}
                residual_events = []
                # Existing output is netted first, using one ledger per Fab.
                # Stock at an unrelated/unreachable Fab cannot cover this OSAT.
                for osat_node, due, quantity in sorted(demand_events, key=lambda event: (event[1], event[0])):
                    if osat_node not in options_by_osat:
                        possible = []
                        for fab_node in fab_nodes:
                            route_eta = self._minimum_route_transit_weeks(
                                network, reader, fab_node, osat_node, output_id
                            )
                            if route_eta is not None:
                                possible.append((fab_node, route_eta))
                        options_by_osat[osat_node] = sorted(possible, key=lambda option: (option[1], option[0]))
                    options = options_by_osat[osat_node]
                    if not options:
                        issues.append(f"fab:{output}:unknown_route_eta_to:{osat_node}")
                        continue
                    remaining = quantity
                    for fab_node, route_eta in options:
                        if fab_node not in calendars:
                            calendars[fab_node] = self._supply_calendar(state, (fab_node, output_id), issues)
                        if calendars[fab_node] is not None:
                            ship_date = max(state.week, due - route_eta)
                            remaining = self._consume_supply(calendars[fab_node], ship_date, remaining)
                        if remaining <= 0:
                            break
                    if remaining > 0:
                        residual_events.append((osat_node, due, remaining))

                # Each residual can use every reachable Fab's remaining
                # capacity, with a source-specific transit and production date.
                for osat_node, due, quantity in residual_events:
                    remaining = quantity
                    for fab_node, route_eta in options_by_osat[osat_node]:
                        if calendars[fab_node] is None:
                            continue
                        fab = self.profiles[fab_node]["fab"]
                        if fab_node not in capacities:
                            capacities[fab_node] = reader.number("graph_now.fab.cap_eff", fab_rows[fab_node])
                            if capacities[fab_node] is None:
                                issues.append(f"fab:{fab_node}:unknown_capacity")
                        capacity = capacities[fab_node]
                        if capacity is None:
                            continue
                        tau = int(fab.get("tau", 0))
                        ship_date = max(state.week, due - route_eta)
                        production_week = max(state.week, ship_date - tau)
                        capacity_left = max(0.0, capacity - fab_weekly_capacity[fab_node, production_week])
                        target = min(capacity_left, remaining)
                        if target <= 0:
                            continue
                        fab_weekly_capacity[fab_node, production_week] += target
                        remaining -= target
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
                        if remaining <= 0:
                            break
                    if remaining > 0:
                        issues.append(f"fab:{output}:unplanned_input:{osat_node}:{due}:{remaining:g}")
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
                for h in range(fuel_count):
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
        trace = []
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
            current_remaining = stock.value
            wip_credited = 0.0
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
                current_credit = min(current_remaining, covered)
                current_remaining -= current_credit
                arrival_credit = covered - current_credit
                eligible_wip = sum(
                    arrival.quantity.value for arrival in state.arrivals
                    if (arrival.destination_node, arrival.commodity_id) == pair
                    and arrival.source_kind == "wip" and arrival.arrival_week is not None
                    and arrival.arrival_week <= coverage_week and arrival.quantity.value is not None
                    and (self.include_estimated_arrivals or
                         (arrival.source == "observed" and arrival.quantity.source == "observed"))
                )
                wip_credit = min(arrival_credit, max(0.0, eligible_wip - wip_credited))
                wip_credited += wip_credit
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
                    trace.append({
                        "week": state.week, "node": pair[0], "commodity": pair[1],
                        "raw_projected_requirement": qty, "current_inventory_credited": current_credit,
                        "future_arrivals_credited": arrival_credit, "wip_credited": wip_credit,
                        "safety_stock_requirement": 0.0, "final_uncovered_quantity": missing,
                        "required_receipt_week": due, "minimum_route_lead_time_bound": None,
                        "latest_feasible_dispatch_week": None, "emitted_quantity": missing,
                        "due_week": due, "priority": priority, "shortage_value": cost,
                        "reason": reason, "need_id": result[-1].need_id,
                    })
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
                    trace.append({
                        "week": state.week, "node": pair[0], "commodity": pair[1],
                        "raw_projected_requirement": target, "current_inventory_credited": balance,
                        "future_arrivals_credited": 0.0, "wip_credited": 0.0,
                        "safety_stock_requirement": target, "final_uncovered_quantity": missing,
                        "required_receipt_week": due, "minimum_route_lead_time_bound": None,
                        "latest_feasible_dispatch_week": None, "emitted_quantity": missing,
                        "due_week": due, "priority": 1.0, "shortage_value": None,
                        "reason": "safety_stock", "need_id": result[-1].need_id,
                    })
        # A grid-to-terminal need is a distinct upstream stage. It is created
        # only for an uncovered projected grid-fuel need, then netted against
        # terminal stock and dated arrivals once. Coupling slots with no
        # terminal source route (e.g. direct nuclear-fuel grids) are excluded.
        if self.fuel_replenishment_enabled and network is not None:
            fuel_events = defaultdict(list)
            for need in result:
                if need.reason != "grid_fuel":
                    continue
                terminal_options = {}
                for slot in network.slots_to.get((need.destination_node, need.commodity_id), ()):
                    route = network.routes[slot]
                    if "terminal" not in self.profiles[route.source_node]:
                        continue
                    if not network.slots_from.get((route.source_node, need.commodity_id)):
                        continue
                    edge_times = [reader.number("graph_now.tau", edge) for edge in route.edges]
                    transfer = (
                        sum(int(value) for value in edge_times)
                        if all(value is not None and value == int(value) for value in edge_times)
                        else float("inf")
                    )
                    terminal_options[route.source_node] = min(
                        transfer, terminal_options.get(route.source_node, float("inf"))
                    )
                if terminal_options:
                    terminal = min(terminal_options, key=lambda node: (terminal_options[node], node))
                    transfer = terminal_options[terminal]
                    terminal_due = need.due_week - int(transfer) if transfer != float("inf") else need.due_week
                    fuel_events[terminal, need.commodity_id].append(
                        (terminal_due, need.quantity, need, need.due_week)
                    )
            for pair, events in sorted(fuel_events.items()):
                stock = state.available_stock.get(pair)
                if stock is None or stock.value is None:
                    issues.append(f"production:{pair}:unknown_time_phased_stock")
                    continue
                calendar = [[state.week, stock.value, "current"]]
                calendar.extend(
                    [arrival.arrival_week, arrival.quantity.value,
                     "wip" if arrival.source_kind == "wip" else "future"]
                    for arrival in state.arrivals
                    if (arrival.destination_node, arrival.commodity_id) == pair
                    and arrival.arrival_week is not None and arrival.quantity.value is not None
                    and (self.include_estimated_arrivals or
                         (arrival.source == "observed" and arrival.quantity.source == "observed"))
                )
                calendar.sort(key=lambda lot: lot[0])
                coverage_by_need = {}
                for due, qty, grid_need, _grid_due in sorted(events, key=lambda item: (item[0], item[1])):
                    left = qty
                    current_credit = future_credit = wip_credit = 0.0
                    for lot in calendar:
                        if lot[0] > due:
                            break
                        used = min(lot[1], left)
                        lot[1] -= used
                        left -= used
                        if lot[2] == "current":
                            current_credit += used
                        elif lot[2] == "wip":
                            wip_credit += used
                        else:
                            future_credit += used
                        if left <= 0:
                            break
                    coverage_by_need[grid_need.need_id] = (left, current_credit, future_credit, wip_credit)
                fuel_need_counts = defaultdict(int)
                for due, qty, grid_need, _grid_due in events:
                    missing, current_credit, future_credit, wip_credit = coverage_by_need[grid_need.need_id]
                    if missing <= 0:
                        continue
                    slots = network.slots_from.get(pair, ())
                    lead_times = [
                        sum(reader.number("graph_now.tau", edge) for edge in network.routes[s].edges)
                        for s in slots
                        if all(
                            reader.number("graph_now.tau", edge) is not None
                            and reader.number("graph_now.tau", edge) == int(reader.number("graph_now.tau", edge))
                            for edge in network.routes[s].edges
                        )
                    ]
                    lead = min(lead_times) if lead_times else None
                    base_id = f"fuel_replenishment:{pair[0]}:{pair[1]}:{due}"
                    occurrence = fuel_need_counts[base_id]
                    fuel_need_counts[base_id] += 1
                    result.append(DeliveryNeed(
                        base_id if occurrence == 0 else f"{base_id}:{occurrence + 1}", *pair, missing, due,
                        grid_need.priority, "fuel_replenishment",
                        grid_need.shortage_cost_per_unit_usd,
                        assumptions=(f"upstream coverage for {grid_need.need_id}",
                                     "terminal stock and dated arrivals netted once",
                                     "deadline is terminal receipt week; grid coupling is a separate action"),
                    ))
                    trace.append({
                        "week": state.week, "node": pair[0], "commodity": pair[1],
                        "raw_projected_requirement": qty, "current_inventory_credited": current_credit,
                        "future_arrivals_credited": future_credit, "wip_credited": wip_credit,
                        "safety_stock_requirement": 0.0, "final_uncovered_quantity": missing,
                        "required_receipt_week": due, "minimum_route_lead_time_bound": lead,
                        "latest_feasible_dispatch_week": None if lead is None else due - int(lead),
                        "emitted_quantity": missing, "due_week": due,
                        "priority": grid_need.priority,
                        "shortage_value": grid_need.shortage_cost_per_unit_usd,
                        "reason": "fuel_replenishment", "need_id": result[-1].need_id,
                        "upstream_of": grid_need.need_id,
                    })
        if network is not None:
            by_id = {need.need_id: need for need in result}
            for item in trace:
                need = by_id[item["need_id"]]
                candidates = []
                for slot in network.slots_to.get((need.destination_node, need.commodity_id), ()):
                    route = network.routes[slot]
                    times = [reader.number("graph_now.tau", edge) for edge in route.edges]
                    if all(value is not None and value == int(value) for value in times):
                        candidates.append(sum(int(value) for value in times))
                if candidates:
                    item["minimum_route_lead_time_bound"] = min(candidates)
                    item["latest_feasible_dispatch_week"] = need.due_week - min(candidates)
                    if item["latest_feasible_dispatch_week"] < state.week:
                        issues.append(f"need:{need.need_id}:impossible_deadline_at_creation")
        self.last_trace = tuple(trace)
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
