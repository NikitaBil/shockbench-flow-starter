"""Allocator V1: deadline-aware greedy using the unchanged team contracts.

Future lane edges/pools are not reserved. Current dispatch uses pre-dispatch
stock; same-week ordinary arrivals, supply and production are unavailable.
"""

import math
from collections import defaultdict
from dataclasses import replace

import numpy as np


if __package__:
    from .contracts import AllocationResult, DecisionReason, ResourceUsage, UnmetNeed, need_order_key
    from .delivery import DeliveryEvaluator
    from .delivery_closure import ClosureSchedule
    from .delivery_eta import CandidateETA
    from .network import NetworkTracker, StaticNetwork
    from .queue_forecast import QueueForecaster
else:
    from contracts import AllocationResult, DecisionReason, ResourceUsage, UnmetNeed, need_order_key
    from delivery import DeliveryEvaluator
    from delivery_closure import ClosureSchedule
    from delivery_eta import CandidateETA
    from network import NetworkTracker, StaticNetwork
    from queue_forecast import QueueForecaster


class Allocator:
    def __init__(
        self,
        config,
        network=None,
        *,
        queue_eta_enabled=False,
        announced_eta_guard_enabled=False,
        closure_wait_enabled=False,
    ):
        if not isinstance(queue_eta_enabled, bool):
            raise ValueError("queue_eta_enabled must be a boolean")
        if not isinstance(announced_eta_guard_enabled, bool):
            raise ValueError("announced_eta_guard_enabled must be a boolean")
        if announced_eta_guard_enabled and not queue_eta_enabled:
            raise ValueError("announced_eta_guard_enabled requires queue_eta_enabled")
        self.announced_eta_guard_enabled = announced_eta_guard_enabled
        if not isinstance(closure_wait_enabled, bool):
            raise ValueError("closure_wait_enabled must be a boolean")
        if closure_wait_enabled and not queue_eta_enabled:
            raise ValueError("closure_wait_enabled requires queue_eta_enabled")
        self.closure_wait_enabled = closure_wait_enabled
        self.config = config
        self.network = network if network is not None else StaticNetwork(config)
        self.tracker = NetworkTracker(config, self.network)
        self.delivery = DeliveryEvaluator(config, self.network)
        # Reuse the state owner's tested public-instance detour interpretation.
        fleet = QueueForecaster(config)
        self.fleet_terms, self.fleet_caps = fleet.fleet_terms, fleet.fleet_caps
        self.queue_forecaster = QueueForecaster(config, max_weeks=CandidateETA.MAX_WEEKS) if queue_eta_enabled else None
        self.stock_indices = {tuple(pair): i for i, pair in enumerate(config["layout"]["stock_slots"])}
        self.pools = tuple(config["static"]["commodities"]["pool"])
        self.allowed = tuple(config["static"]["edges"]["K"])
        self.last_snapshot = None
        self._snapshot_input = None
        # Local diagnostic hook; absent during normal Agent execution. Does
        # not alter the team handoff or keep an unbounded runtime trace.
        self.trace_callback = None

    def _trace(self, need, rank, week, slot, stage, reason, **details):
        if self.trace_callback is not None:
            self.trace_callback(
                dict(
                    week=week,
                    need_id=need.need_id,
                    need_rank=rank,
                    priority=need.priority,
                    due_week=need.due_week,
                    destination_node=need.destination_node,
                    commodity_id=need.commodity_id,
                    slot_id=slot,
                    stage=stage,
                    reason=reason,
                    **details,
                )
            )

    def _snapshot(self, observation):
        week = int(observation["week"][0])
        keys = ("week",) + tuple(name for field in self.tracker.nominal for name in (field, field + ".observed"))
        if self.last_snapshot is not None and week == self.last_snapshot.week:
            if any(not np.array_equal(observation[key], self._snapshot_input[key], equal_nan=True) for key in keys):
                raise ValueError("network observations changed within a previously evaluated week")
            return self.last_snapshot
        snapshot = self.tracker.update(observation)
        self._snapshot_input = {key: np.array(observation[key], copy=True) for key in keys}
        self.last_snapshot = snapshot
        return snapshot

    @staticmethod
    def _candidate_key(option, need, quantity):
        # ETA is for the assigned quantity, not a nominal lower bound. The V3
        # unit probe's batch correction is conservative for current throughput.
        eta = option.estimated_completion_week
        if eta is not None and option.quantity != quantity:
            eta += max(0, math.ceil(quantity / option.snapshot_throughput) - 1)
        late = None if eta is None else max(0, eta - need.due_week)
        cost = option.transport_cost_per_unit + option.queue_holding_cost_per_unit
        penalty = need.shortage_cost_per_unit_usd
        if eta is None:
            lower_bound = option.no_wait_arrival_week
            if option.snapshot_throughput > 0:
                lower_bound += max(0, math.ceil(quantity / option.snapshot_throughput) - 1)
            # Unknown remains a separate tier, never "on time". Its lower
            # bound orders last-resort dispatches only, without inventing ETA.
            economic_bound = (
                cost + max(0, lower_bound - need.due_week) * penalty if penalty is not None else lower_bound
            )
            return (
                2,
                economic_bound,
                lower_bound if penalty is not None else cost,
                -quantity,
                option.slot_id,
            ), None
        if late > 0 and penalty is None:
            # Missing marginal shortage cost is not zero damage. When every
            # feasible route is late, restore service earlier before choosing
            # the cheaper equally fast route. No USD penalty is fabricated.
            return (1, late, cost, -quantity, option.slot_id), eta
        economic = cost + (late * penalty if late is not None and penalty is not None else 0)
        return (
            2 if eta is None else int(late > 0),
            economic,
            eta if eta is not None else math.inf,
            -quantity,
            option.slot_id,
        ), eta

    @staticmethod
    def _forecast_order(option, need):
        nominally_late = option.no_wait_arrival_week > need.due_week
        fastest_first = nominally_late and need.shortage_cost_per_unit_usd is None
        return (
            int(nominally_late),
            option.no_wait_arrival_week if fastest_first else option.transport_cost_per_unit,
            option.transport_cost_per_unit if fastest_first else option.no_wait_arrival_week,
            option.slot_id,
        )

    def _optimistic_key(self, option, need, quantity, snapshot):
        """A ranking bound, never a completion forecast or an empty queue.

        With observed transit, queueing cannot beat no-wait arrival under the
        predictor's persistence model. With hidden transit use this week as
        an optimistic bound. Holding and extra queue delay cannot be negative.
        """
        route = self.network.routes[option.slot_id]
        transit_seen = np.all(snapshot.fields["graph_now.tau"].observed[list(route.edges)])
        earliest = option.no_wait_arrival_week if transit_seen else snapshot.week
        probe = replace(option, quantity=quantity, estimated_completion_week=earliest, queue_holding_cost_per_unit=0)
        return self._candidate_key(probe, need, quantity)[0]

    def _unknown_dispatch_allowed(self, option, quantity, snapshot, predictor):
        """Computation truncation is not a physical dispatch prohibition.

        This fallback never repairs hidden inputs or a known forecast conflict.
        It requires a fully observed, open physical route and room for the
        batch's empty-queue lower bound within the episode. ETA stays None.
        """
        truncation = "queue_eta_forecast_budget_exhausted" in option.delay_flags or (
            "queue_eta_completion_unresolved" in option.delay_flags
            and snapshot.week + predictor.MAX_WEEKS - 1 < snapshot.horizon
        )
        if not truncation or not predictor.inputs_complete:
            return False
        if predictor.rejections & {"queue_eta_inputs_unknown", "queue_eta_transit_unknown"}:
            return False  # A prior forecast exposed missing joint inputs, not merely a CPU cutoff.
        route, fields = self.network.routes[option.slot_id], snapshot.fields
        es, ps = list(route.edges), list(route.chokepoint_positions)
        status = snapshot.routes[route.slot_id]
        if not option.permission_observed or status.closed_chokepoints or status.snapshot_throughput <= 0:
            return False
        if any((edge, route.commodity_id) in predictor.pending for edge in route.edges[1:]):
            return False  # Unknown queue delay cannot certify traversal before an announced ban.
        if not (
            np.all(fields["graph_now.u"].observed[es])
            and np.all(fields["graph_now.tau"].observed[es])
            and np.all(fields["graph_now.open"].observed[ps])
            and np.all(fields[f"graph_now.kappa.{route.pool}"].observed[ps])
        ):
            return False
        batch_lower_bound = option.no_wait_arrival_week + max(0, math.ceil(quantity / status.snapshot_throughput) - 1)
        return batch_lower_bound <= snapshot.horizon

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
        # Cargo masks mark live rows/cohorts; zero masks also mark padding.
        # The parsed state's quality/metadata and own-stock visibility identify
        # incomplete own state, rather than declaring every empty row unknown.
        if any(qty.source != "observed" for qty in state.available_stock.values()):
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
        snapshot = self._snapshot(observation)
        net = self.network
        closure_schedule = ClosureSchedule(observation, net, snapshot) if self.closure_wait_enabled else None
        predictor = (
            CandidateETA(
                self.queue_forecaster,
                state,
                observation,
                net,
                snapshot,
                announced_guard=self.announced_eta_guard_enabled,
                closure_schedule=closure_schedule,
            )
            if self.queue_forecaster is not None
            else None
        )
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
        unmet, reasons, cache, assignments = [], [], {}, []
        for need_rank, need in enumerate(sorted(needs, key=need_order_key), 1):
            key = need.destination_node, need.commodity_id
            if key not in cache:
                cache[key] = self.delivery.options(snapshot, *key)
            if not cache[key]:
                slots = net.slots_to.get(key, ())
                code = "all_delivery_slots_prohibited" if slots else "no_action_slot_to_destination"
                reasons.append(
                    DecisionReason(
                        code,
                        f"Existing delivery slots {tuple(slots)} were excluded by current permission estimates."
                        if slots
                        else "StaticNetwork has no action slot ending at this destination for this commodity; "
                        "check upstream need semantics. No route was invented.",
                        need.need_id,
                    )
                )
                if self.trace_callback is not None:
                    self._trace(
                        need,
                        need_rank,
                        state.week,
                        None,
                        "route_lookup",
                        code,
                        static_slots=list(slots),
                        permission_observed=(
                            all(snapshot.routes[slot].permission_observed for slot in slots) if slots else None
                        ),
                        commodity_destinations=sorted(
                            {r.destination_node for r in net.routes if r.commodity_id == need.commodity_id}
                        ),
                        incoming_commodities=sorted(
                            {r.commodity_id for r in net.routes if r.destination_node == need.destination_node}
                        ),
                        need_reason=need.reason,
                    )
            remaining = float(need.quantity)
            blocked = set()
            options = cache[key]
            if predictor is not None:
                # Forecast scheduling only: routes that can still meet the
                # deadline without queue delay go first. This lower bound is
                # never promoted to a known/on-time completion estimate.
                options = sorted(
                    options,
                    key=lambda option: self._forecast_order(option, need),
                )
            while remaining > 0:
                candidates = []
                for option in options:
                    route = net.routes[option.slot_id]
                    origin = route.source_node, route.commodity_id
                    status = snapshot.routes[route.slot_id]
                    can_wait = closure_schedule is not None and closure_schedule.permits_wait(route, status)
                    if (
                        status.closed_chokepoints or status.zero_capacity_edges or status.snapshot_throughput <= 0
                    ) and not can_wait:
                        blocked.add("currently_blocked_delivery_route")
                        self._trace(
                            need, need_rank, state.week, route.slot_id, "prefilter", "currently_blocked_delivery_route"
                        )
                        continue
                    if not option.permission_observed:
                        blocked.add("unconfirmed_permission")
                        self._trace(need, need_rank, state.week, route.slot_id, "prefilter", "unconfirmed_permission")
                        continue
                    weight = self._weight(route.edge_id, route.lane_id)
                    quantity = min(remaining, stocks.get(origin, 0.0), float(edges[route.edge_id]))
                    if weight:
                        quantity = min(quantity, fleet[route.pool] / weight)
                    if quantity <= 0:
                        blocked.add("current_resources_exhausted_or_unknown")
                        code = (
                            "no_source_stock"
                            if stocks.get(origin, 0.0) <= 0
                            else "entry_capacity_exhausted"
                            if edges[route.edge_id] <= 0
                            else "fleet_budget_exhausted"
                        )
                        self._trace(
                            need,
                            need_rank,
                            state.week,
                            route.slot_id,
                            "prefilter",
                            code,
                            source_stock=stocks.get(origin, 0.0),
                            entry_capacity=float(edges[route.edge_id]),
                            fleet_remaining=fleet[route.pool],
                            fleet_weight=weight,
                            quantity=quantity,
                        )
                        continue
                    if predictor is not None:
                        # Branch-and-bound: once a feasible option is known,
                        # spend no FIFO simulation on an alternative whose
                        # optimistic rank cannot improve it. Quantity and slot
                        # tie-breaks are included; each ledger change rebuilds
                        # the bound. Priority across needs remains unchanged.
                        if candidates and self._optimistic_key(option, need, quantity, snapshot) >= min(
                            candidate[0] for candidate in candidates
                        ):
                            self._trace(
                                need, need_rank, state.week, route.slot_id, "prefilter", "dominated_delivery_candidate"
                            )
                            continue
                        # Even an empty FIFO queue cannot beat observed transit
                        # under the same persistence assumptions as the forecast.
                        # Hidden transit is not a known lower bound.
                        lower_bound = option.no_wait_arrival_week
                        if closure_schedule is not None and closure_schedule.affected(route):
                            scheduled_bound = closure_schedule.earliest_arrival(route, net)
                            if scheduled_bound is not None:
                                lower_bound = max(lower_bound, scheduled_bound)
                        if lower_bound > state.horizon and np.all(
                            snapshot.fields["graph_now.tau"].observed[list(route.edges)]
                        ):
                            blocked.add("estimated_arrival_beyond_horizon")
                            self._trace(
                                need,
                                need_rank,
                                state.week,
                                route.slot_id,
                                "prefilter",
                                "estimated_arrival_beyond_horizon",
                                no_wait_arrival=option.no_wait_arrival_week,
                                conditional_arrival_lower_bound=lower_bound,
                            )
                            continue
                        calls_before = predictor.calls
                        cached_before = (option.slot_id, quantity) in predictor.cache
                        option = predictor.evaluate(option, quantity)
                        self._trace(
                            need,
                            need_rank,
                            state.week,
                            route.slot_id,
                            "eta",
                            next((flag for flag in option.delay_flags if flag.startswith("queue_eta_")), "estimated"),
                            source_stock=stocks.get(origin, 0.0),
                            entry_capacity=float(edges[route.edge_id]),
                            permission_observed=option.permission_observed,
                            fleet_remaining=fleet[route.pool],
                            fleet_weight=weight,
                            quantity=quantity,
                            calls_before=calls_before,
                            calls_after=predictor.calls,
                            cache_hit=cached_before,
                            eta=option.estimated_completion_week,
                            no_wait_arrival=option.no_wait_arrival_week,
                        )
                    rank, eta = self._candidate_key(option, need, quantity)
                    if eta is None:
                        if predictor is None or not self._unknown_dispatch_allowed(
                            option, quantity, snapshot, predictor
                        ):
                            blocked.add("delivery_eta_unknown")
                            blocked.update(flag for flag in option.delay_flags if flag.startswith("queue_eta_"))
                            continue
                    if eta is not None and eta > state.horizon:
                        blocked.add("estimated_arrival_beyond_horizon")
                        continue
                    candidates.append((rank, option, quantity, eta))
                if not candidates:
                    break
                _rank, option, qty, eta = min(candidates, key=lambda candidate: candidate[0])
                route = net.routes[option.slot_id]
                if closure_schedule is not None and any(
                    closure_schedule.affected(net.routes[item[1].slot_id]) for item in candidates
                ):
                    compared = [
                        (
                            item[1].slot_id,
                            item[3],
                            item[1].transport_cost_per_unit + item[1].queue_holding_cost_per_unit,
                        )
                        for item in sorted(candidates, key=lambda candidate: candidate[0])[:8]
                    ]
                    reasons.append(
                        DecisionReason(
                            "closure_route_comparison",
                            f"Chose slot {route.slot_id}, due {need.due_week}; alternatives "
                            f"(slot, conditional ETA, transport + queue bound USD/unit): {compared}. "
                            "Timely first; priced needs use transport + queue bound + shortage penalty; "
                            "unpriced late needs prefer earlier restoration then cost; "
                            "stable slot tie-break; no future reservations.",
                            need.need_id,
                            route.slot_id,
                        )
                    )
                origin = route.source_node, route.commodity_id
                weight = self._weight(route.edge_id, route.lane_id)
                flows[route.slot_id] += qty
                stocks[origin] -= qty
                edges[route.edge_id] -= qty
                fleet[route.pool] = max(0.0, fleet[route.pool] - weight * qty)
                remaining = max(0.0, remaining - qty)
                # Emit assignment traces after the joint plan is complete so
                # an unforecast addition cannot leave a stale on-time label.
                assignments.append((need_rank, need, option, qty, eta))
                if predictor is not None:
                    predictor.accept(option, qty, eta)
                reasons.append(
                    DecisionReason(
                        "allocated_current_resources",
                        f"Assigned {qty:g} {route.unit}; full-route transport estimate "
                        f"{option.transport_cost_per_unit:g} USD/unit; conditional ETA {eta}, "
                        f"due week {need.due_week}; later edges/pools are not reserved.",
                        need.need_id,
                        route.slot_id,
                    )
                )
                reasons.append(
                    DecisionReason(
                        "delivery_eta_unknown"
                        if eta is None
                        else "eta_late"
                        if eta > need.due_week
                        else "eta_on_time_estimate",
                        "Dispatched with unknown completion; this does not establish timely coverage."
                        if eta is None
                        else f"Conditional completion week {eta}; "
                        + (
                            "arrives after this need's deadline."
                            if eta > need.due_week
                            else "not a guaranteed arrival."
                        ),
                        need.need_id,
                        route.slot_id,
                    )
                )
                if eta is None:
                    reasons.append(
                        DecisionReason(
                            "dispatch_without_certified_eta",
                            f"Assigned {qty:g} {route.unit} after bounded forecasting was truncated: "
                            f"{','.join(flag for flag in option.delay_flags if flag.startswith('queue_eta_'))}. "
                            "Stock, permissions and physical route rates are observed; ETA remains unknown; "
                            "no future queue capacity reserved.",
                            need.need_id,
                            route.slot_id,
                        )
                    )
                if option.uncertain_fields:
                    reasons.append(
                        DecisionReason(
                            "delivery_estimate_uncertain",
                            "Current capacity/price estimates or missing queue forecast make completion uncertain.",
                            need.need_id,
                            route.slot_id,
                        )
                    )
                if "conditional_fifo_forecast" in option.delay_flags:
                    reasons.append(
                        DecisionReason(
                            "queue_eta_estimate",
                            f"Joint FIFO estimate for {qty:g} {route.unit}: completion week {eta}; "
                            + (
                                "pool rates resume nominally after observed closure ends; other rates/transit persist; "
                                if "announced_reopening_forecast" in option.delay_flags
                                else "current rates/transit persist, selected cargo is included; "
                            )
                            + "future dispatches/disruptions are unknown; no future resources reserved.",
                            need.need_id,
                            route.slot_id,
                        )
                    )
                if "announced_reopening_forecast" in option.delay_flags and closure_schedule.affected(route):
                    ends = {
                        node: closure_schedule.ends[node] for node in route.chokepoints if node in closure_schedule.ends
                    }
                    reasons.append(
                        DecisionReason(
                            "wait_for_announced_reopening",
                            f"Dispatched now; active closure end weeks {ends}; conditional completion {eta}; "
                            f"queue holding upper bound {option.queue_holding_cost_per_unit:g} USD/unit; "
                            "replanned next week; no future resources reserved.",
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
                self._trace(need, need_rank, state.week, None, "unmet", reason, quantity=remaining)
        uncertified = predictor.uncertified_slots if predictor is not None else set()
        if uncertified:
            replacements = {
                "eta_on_time_estimate": "delivery_eta_unknown",
                "eta_late": "delivery_eta_unknown",
                "queue_eta_estimate": "queue_eta_estimate_superseded",
                "wait_for_announced_reopening": "closure_wait_completion_unknown",
                "allocated_current_resources": "allocated_current_resources",
            }
            reasons = [
                DecisionReason(
                    replacements[reason.code],
                    "Current resources assigned; final joint completion is unknown after additional unforecast "
                    "cargo. Earlier prefix ETA is superseded, not an on-time claim or future reservation.",
                    reason.need_id,
                    reason.slot_id,
                )
                if reason.slot_id in uncertified and reason.code in replacements
                else reason
                for reason in reasons
            ]
        for rank, need, option, quantity, eta in assignments:
            if option.slot_id in uncertified:
                eta = None
            self._trace(
                need,
                rank,
                state.week,
                option.slot_id,
                "assignment",
                "delivery_eta_unknown"
                if eta is None
                else "eta_late"
                if eta > need.due_week
                else "eta_on_time_estimate",
                quantity=quantity,
                eta=eta,
                late_weeks=None if eta is None else max(0, eta - need.due_week),
                overdue_at_dispatch=need.due_week < state.week,
                no_wait_arrival=option.no_wait_arrival_week,
            )
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
        for pool in ("tb", "ct"):
            used = fleet_start[pool] - fleet[pool]
            if used > 0:
                reasons.append(
                    DecisionReason(
                        "fleet_budget_usage",
                        f"Used {used:g} of {fleet_start[pool]:g} residual {pool} native-unit weeks.",
                    )
                )
            if release_bound[pool] > 0:
                reasons.append(
                    DecisionReason(
                        "automatic_release_fleet_bound",
                        f"Reserved up to {release_bound[pool]:g} {pool} unit-weeks for today's automatic releases.",
                    )
                )
        if predictor is not None:
            reasons.append(
                DecisionReason(
                    "queue_eta_forecast_usage",
                    f"Ran {predictor.calls}/{predictor.MAX_FORECASTS} joint forecasts; "
                    f"lookahead at most {predictor.MAX_WEEKS} weeks. "
                    + ("Rejected estimates: " + ",".join(sorted(predictor.rejections)) if predictor.rejections else ""),
                )
            )
        return AllocationResult(flows, tuple(unmet), tuple(usage), tuple(reasons))
