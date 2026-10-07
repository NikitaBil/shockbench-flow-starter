"""Bounded candidate FIFO forecasts using the state owner's existing engine.

Proposals are hypothetical pipeline, never current stock or future reservations.
Each allocator call owns its cache and deterministic forecast budget.
"""

from dataclasses import replace
from types import SimpleNamespace

import numpy as np


if __package__:
    from .contracts import PipelineLot, Quantity
    from .observations import ObservationReader
else:
    from contracts import PipelineLot, Quantity
    from observations import ObservationReader


class CandidateETA:
    MAX_FORECASTS = 16
    MAX_WEEKS = 24

    def __init__(
        self, forecaster, state, observation, network, snapshot, *, announced_guard=False, closure_schedule=None
    ):
        self.forecaster = forecaster
        self.state, self.observation = state, observation
        self.network, self.snapshot = network, snapshot
        self.proposals, self.completions = [], {}
        self.accepted_slots, self.uncertified_slots = set(), set()
        self.cache = {}
        self.calls = 0
        self.rejections = set()
        self.inputs_complete = (
            not any(issue.startswith(("pipeline:", "queue:")) for issue in state.issues)
            and bool(np.all(ObservationReader(observation).field("stock.qty")[1]))
            and all(
                lot.edge_id is not None
                and lot.edge_arrival_week is not None
                and lot.lane_status != "unknown"
                and lot.quantity.value is not None
                for lot in state.pipeline
            )
            and all(
                lot.lane_id is not None and lot.next_edge_id is not None and lot.quantity.value is not None
                for lot in state.queues
            )
        )
        self.pending = self._pending() if announced_guard else {}
        self.closure_schedule = closure_schedule
        # Topology is static; future transit persists at observed current tau.
        self.forecast_network = SimpleNamespace(
            edge_transit_weeks=tuple(int(tau) for tau in snapshot.fields["graph_now.tau"].values),
            lane_edges=network.lane_edges,
            chokepoints=network.chokepoints,
            edge_head=network.edge_head,
            edge_tail=network.edge_tail,
            transit_progress=network.transit_progress,
        )

    def _pending(self):
        # This is a noisy publication feed, not privileged future graph data.
        # Padding or a masked member never establishes a prohibition.
        fields = tuple(f"pending_prohibitions.{name}" for name in ("edge", "k", "effective_week"))
        if any(name not in self.observation for name in fields):
            return {}
        reader = ObservationReader(self.observation)
        columns = [reader.field(name) for name in fields]
        if any(values.ndim != 1 or values.shape != columns[0][0].shape for values, _ in columns):
            raise ValueError("pending_prohibitions: inconsistent column shapes")
        visible = np.logical_and.reduce([seen for _, seen in columns])
        pending = {}
        for row in np.flatnonzero(visible):
            edge = reader.integer(fields[0], row, high=len(self.network.edge_names) - 1)
            commodity = reader.integer(fields[1], row, high=len(self.network.commodity_names) - 1)
            effective = reader.integer(fields[2], row, low=1)
            if self.state.week < effective <= self.state.horizon:
                key = edge, commodity
                pending[key] = min(effective, pending.get(key, effective))
        return pending

    def _earliest_conflict(self, route):
        entry = self.state.week
        transit = self.snapshot.fields["graph_now.tau"]
        for edge in route.edges:
            if entry >= self.pending.get((edge, route.commodity_id), float("inf")):
                return True
            if not transit.observed[edge]:
                return False  # An estimated duration cannot prove the conflict.
            entry += int(transit.values[edge])
        return False

    def _forecast_conflict(self, forecast, proposal):
        sources = {
            cargo.lot_id: (cargo.lane_id, cargo.commodity_id)
            for cargo in (*self.state.pipeline, *self.state.queues, *self.proposals, proposal)
        }
        for visit in forecast.visits:
            lane, commodity = sources[visit.source_id]
            path = self.network.lane_edges[lane]
            edge = next(edge for edge in path if self.network.edge_tail[edge] == visit.chokepoint_node)
            effective = self.pending.get((edge, commodity))
            if effective is not None and (
                (visit.first_release_week is not None and visit.first_release_week >= effective)
                or (visit.completion_release_week is not None and visit.completion_release_week >= effective)
                or (
                    visit.first_release_week is not None
                    and visit.completion_release_week is None
                    and effective <= min(self.state.horizon, self.state.week + self.MAX_WEEKS - 1)
                )
            ):
                # Checking all competing cargo matters: its invalid releases
                # can change the proposed cargo's FIFO share as well.
                return True
        return False

    def _proposal(self, slot, quantity):
        route = self.network.routes[slot]
        return PipelineLot(
            f"allocation:{len(self.proposals)}:{slot}",
            route.edge_id,
            route.commodity_id,
            route.lane_id,
            "known",
            Quantity(quantity, "estimated"),
            self.state.week + self.forecast_network.edge_transit_weeks[route.edge_id],
            route.destination_node,
            remaining_edges=route.edges[1:],
            remaining_route_weeks=sum(self.network.edge_transit_weeks[edge] for edge in route.edges[1:]),
        )

    def evaluate(self, option, quantity):
        route = self.network.routes[option.slot_id]
        if not route.chokepoints:
            return option
        key = option.slot_id, quantity
        if key in self.cache:
            return self.cache[key]
        reason, eta, holding = None, None, 0.0
        if self.pending and self._earliest_conflict(route):
            reason = "queue_eta_announced_prohibition_conflict"
        elif self.calls >= self.MAX_FORECASTS:
            reason = "queue_eta_forecast_budget_exhausted"
        else:
            # Every future leg in the joint forecast needs known transit.
            lanes = {route.lane_id}
            lanes.update(lot.lane_id for lot in (*self.state.pipeline, *self.state.queues, *self.proposals))
            edges = sorted({edge for lane in lanes if lane is not None for edge in self.network.lane_edges[lane]})
            if not np.all(self.snapshot.fields["graph_now.tau"].observed[edges]):
                reason = "queue_eta_transit_unknown"
            else:
                proposal = self._proposal(option.slot_id, quantity)
                self.calls += 1
                schedule = {"throughput_schedule": self.closure_schedule} if self.closure_schedule is not None else {}
                forecast = self.forecaster.forecast(
                    self.state,
                    self.observation,
                    self.forecast_network,
                    proposed_pipeline=(*self.proposals, proposal),
                    **schedule,
                )
                eta = forecast.completion_weeks.get(proposal.lot_id)
                if forecast.issues:
                    reason = "queue_eta_inputs_unknown"
                    eta = None
                elif self.pending and self._forecast_conflict(forecast, proposal):
                    reason = "queue_eta_announced_prohibition_conflict"
                    eta = None
                elif eta is None:
                    reason = "queue_eta_completion_unresolved"
                elif any(
                    forecast.completion_weeks.get(source) is None or forecast.completion_weeks[source] > previous
                    for source, previous in self.completions.items()
                ):
                    # A lower-priority addition must not silently invalidate
                    # an earlier selected shipment's reported completion.
                    reason = "queue_eta_delays_selected_shipment"
                    eta = None
                if eta is not None and self.closure_schedule is not None and self.closure_schedule.affected(route):
                    # Upper bound: the whole proposed quantity pays until each
                    # cohort's last release. Partial releases actually pay less.
                    for visit in forecast.visits:
                        if visit.source_id == proposal.lot_id and visit.completion_release_week is not None:
                            position = self.closure_schedule.positions[visit.chokepoint_node]
                            risk = int(self.snapshot.fields["graph_now.war_risk"].values[position])
                            attrs = self.forecaster.config["static"]["instance"]["nodes"][visit.chokepoint_node][
                                "chokepoint"
                            ]
                            costs = attrs.get("queue_holding", {}).get(
                                self.network.commodity_names[route.commodity_id], (0, 0, 0)
                            )
                            holding += float(costs[risk]) * max(0, visit.completion_release_week - visit.arrival_week)
        if reason:
            self.rejections.add(reason)
        flags = tuple(flag for flag in option.delay_flags if flag != "queue_work_unknown")
        delay = None if eta is None else max(0, eta - option.no_wait_arrival_week)
        reopening = (
            ("announced_reopening_forecast",)
            if eta is not None and self.closure_schedule is not None and self.closure_schedule.ends
            else ()
        )
        result = replace(
            option,
            quantity=quantity,
            transport_cost=option.transport_cost_per_unit * quantity,
            estimated_completion_week=eta,
            queue_delay_weeks=delay,
            beyond_horizon=eta is not None and eta > self.state.horizon,
            delay_flags=flags + ((reason,) if reason else ("conditional_fifo_forecast",)) + reopening,
            queue_holding_cost_per_unit=holding,
        )
        self.cache[key] = result
        return result

    def accept(self, option, quantity, completion):
        if self.network.routes[option.slot_id].chokepoints:
            proposal = self._proposal(option.slot_id, quantity)
            self.proposals.append(proposal)
            self.accepted_slots.add(option.slot_id)
            if completion is None:
                # The joint plan now contains unforecast cargo. Withdraw the
                # earlier joint completion claims rather than treating them
                # as future queue reservations. A later evaluated candidate
                # still includes every proposal, including these unknowns.
                self.uncertified_slots.update(self.accepted_slots)
                self.completions.clear()
            else:
                self.completions[proposal.lot_id] = completion
            # All later forecasts must include this newly selected cargo.
            self.cache.clear()
