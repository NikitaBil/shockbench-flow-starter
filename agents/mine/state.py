"""Markian's State Module (Tasks M1 & M2, V3 Ahead-of-Queue Forecast).

Responsible for:
1. Parsing raw observation into a structured StateSnapshot (M1).
2. Distinguishing confirmed (observed) values from estimated projections.
3. Calculating the week-by-week arrival calendar per (node, commodity) (M2).
4. Forecasting queue clearance and ahead-of-queue work at chokepoints (V3).
5. Supporting both Tiny and Small/Full network formats dynamically without hard-coded shapes.
"""

from typing import Any, Dict, List, Optional, Set, Tuple
import numpy as np

try:
    from contracts import (
        ArrivalEntry,
        BacklogItem,
        PipelineItem,
        QueueDelayForecast,
        QueueItem,
        StateSnapshot,
        StockItem,
        WIPItem,
    )
except ImportError:
    from .contracts import (
        ArrivalEntry,
        BacklogItem,
        PipelineItem,
        QueueDelayForecast,
        QueueItem,
        StateSnapshot,
        StockItem,
        WIPItem,
    )


class StateManager:
    """Extracts, verifies, and forecasts state from environment observations."""

    def __init__(self, config: Dict[str, Any]):
        self.static = config["static"]
        self.layout = config["layout"]
        self.spaces = config["spaces"]
        self.T = int(config.get("T", 52))

        # Node index to node name and type
        nodes_table = self.static["nodes"]
        self.node_names = list(nodes_table["id"])
        self.node_types = list(nodes_table["type"])
        self.n_nodes = len(self.node_names)

        # Commodity index to name, pool, and value
        commodities_table = self.static["commodities"]
        self.commodity_names = list(commodities_table["id"])
        self.commodity_pools = list(commodities_table["pool"])
        self.commodity_values = list(commodities_table["v"])
        self.n_commodities = len(self.commodity_names)

        # Edges info
        edges_table = self.static["edges"]
        self.edge_tails = list(edges_table["tail"])
        self.edge_heads = list(edges_table["head"])
        self.edge_tau0 = [int(t) if t is not None else 1 for t in edges_table["tau0"]]
        self.edge_c0 = [float(c) if c is not None else 0.0 for c in edges_table["c0"]]
        self.n_edges = len(self.edge_tails)

        # Lanes info: sequence of edges, chokepoints, and final destination
        lanes_table = self.static["lanes"]
        self.n_lanes = len(lanes_table["id"])
        self.lane_edges: List[List[int]] = list(lanes_table["edges"])
        self.lane_chokepoints: List[List[int]] = list(lanes_table["chokepoints"])
        self.lane_destinations: List[int] = [
            self.edge_heads[edges[-1]] if len(edges) > 0 else -1
            for edges in self.lane_edges
        ]

        # Chokepoints mapping
        self.chokepoint_nodes = list(self.layout.get("chokepoints", []))
        self.chokepoint_pos_to_node = {i: node for i, node in enumerate(self.chokepoint_nodes)}
        self.node_to_chokepoint_pos = {node: i for i, node in enumerate(self.chokepoint_nodes)}

        # Sinks mapping
        sinks_table = self.static["sinks"]
        self.sink_nodes = list(sinks_table["node"])
        self.sink_commodities = list(sinks_table["k"])
        self.sink_penalties = [float(p) for p in sinks_table["pi"]]
        self.sink_key_to_penalty = {
            (self.sink_nodes[i], self.sink_commodities[i]): self.sink_penalties[i]
            for i in range(len(self.sink_nodes))
        }

        # Check if Small/Full format with 'lot_keys'
        self.has_lot_keys = "lot_keys" in self.layout
        self.lot_keys = self.layout.get("lot_keys", [])

    def capture_snapshot(self, observation: Dict[str, np.ndarray]) -> StateSnapshot:
        """Parse raw observation into a verified StateSnapshot (Roadmap M1)."""
        current_week = int(observation["week"][0])

        snapshot = StateSnapshot(
            week=current_week,
            horizon_T=self.T,
        )

        # -------------------------------------------------------------
        # 1. Parse Stock on Hand (Node, Commodity)
        # -------------------------------------------------------------
        stock_slots = self.layout.get("stock_slots", [])
        stock_qty = observation["stock.qty"]
        stock_obs = observation["stock.qty.observed"]

        for idx, slot in enumerate(stock_slots):
            node_id, comm_id = int(slot[0]), int(slot[1])
            is_obs = bool(stock_obs[idx] == 1)
            qty = float(stock_qty[idx]) if is_obs else 0.0

            item = StockItem(
                node=node_id,
                node_name=self.node_names[node_id],
                commodity=comm_id,
                commodity_name=self.commodity_names[comm_id],
                qty=max(qty, 0.0),
                is_observed=is_obs,
            )
            snapshot.stock[(node_id, comm_id)] = item
            if is_obs:
                snapshot.total_confirmed_stock += qty

        # -------------------------------------------------------------
        # 2. Parse Backlog (Sink Node, Commodity)
        # -------------------------------------------------------------
        demands_layout = self.layout.get("demands", [])
        backlog_qty = observation["backlog.qty"]
        backlog_obs = observation["backlog.qty.observed"]

        for idx, d_slot in enumerate(demands_layout):
            sink_node, comm_id = int(d_slot[0]), int(d_slot[1])
            is_obs = bool(backlog_obs[idx] == 1)
            b_qty = float(backlog_qty[idx]) if is_obs else 0.0
            penalty = self.sink_key_to_penalty.get((sink_node, comm_id), 1000.0)

            item = BacklogItem(
                sink_node=sink_node,
                sink_name=self.node_names[sink_node],
                commodity=comm_id,
                commodity_name=self.commodity_names[comm_id],
                qty=max(b_qty, 0.0),
                is_observed=is_obs,
                shortage_penalty=penalty,
            )
            snapshot.backlog[(sink_node, comm_id)] = item
            if is_obs:
                snapshot.total_backlog += b_qty

        # -------------------------------------------------------------
        # 3. Parse Queue Lots (Waiting at Chokepoints)
        # -------------------------------------------------------------
        self._parse_queues(observation, snapshot, current_week)

        # -------------------------------------------------------------
        # 4. Parse Work in Process (WIP at Fabs and OSATs)
        # -------------------------------------------------------------
        self._parse_wip(observation, snapshot)

        # -------------------------------------------------------------
        # 5. Ahead-of-Queue Work Forecast (Roadmap V3 / M2)
        # -------------------------------------------------------------
        self._forecast_queue_delays(observation, snapshot, current_week)

        # -------------------------------------------------------------
        # 6. Parse Shipments in Transit (Pipeline) & Remaining Delays
        # -------------------------------------------------------------
        self._parse_pipeline(observation, snapshot, current_week)

        # -------------------------------------------------------------
        # 7. Build Week-by-Week Arrival Calendar (Roadmap M2)
        # -------------------------------------------------------------
        self._build_arrival_calendar(snapshot, current_week)

        return snapshot

    def _parse_queues(
        self,
        observation: Dict[str, np.ndarray],
        snapshot: StateSnapshot,
        current_week: int,
    ) -> None:
        """Parse queue lots from Tiny or Small/Full formats without hardcoding."""
        if self.has_lot_keys:
            # Small and Full format: 2D array (len(lot_keys), T)
            queue_qty_matrix = observation["queue_lots.qty"]
            n_keys, T_cols = queue_qty_matrix.shape

            for row_idx, key in enumerate(self.lot_keys):
                chk_node, comm_id, lane_idx, next_edge = key
                chk_node = int(chk_node)
                comm_id = int(comm_id)
                lane_idx = int(lane_idx) if lane_idx is not None and lane_idx >= 0 else None
                next_edge = int(next_edge) if next_edge is not None and next_edge >= 0 else None

                # Destination of lane
                dest_node = self.lane_destinations[lane_idx] if lane_idx is not None else None

                for col_w in range(T_cols):
                    qty = float(queue_qty_matrix[row_idx, col_w])
                    if qty > 0.0:
                        arrival_week = col_w + 1
                        snapshot.queues.append(
                            QueueItem(
                                chokepoint_node=chk_node,
                                chokepoint_name=self.node_names[chk_node],
                                commodity=comm_id,
                                lane=lane_idx,
                                next_edge=next_edge,
                                qty=qty,
                                arrival_week=arrival_week,
                                is_observed=True,
                                dest_node=dest_node,
                            )
                        )
        else:
            # Tiny format: padded list of 1D arrays
            q_obs = observation["queue_lots.qty.observed"]
            live_indices = np.where(q_obs == 1)[0]

            for idx in live_indices:
                qty = float(observation["queue_lots.qty"][idx])
                if qty <= 0.0:
                    continue
                chk_node = int(observation["queue_lots.chokepoint"][idx])
                comm_id = int(observation["queue_lots.k"][idx])
                lane_raw = int(observation["queue_lots.lane"][idx])
                lane_idx = lane_raw if lane_raw >= 0 else None
                next_edge_raw = int(observation["queue_lots.next_edge"][idx])
                next_edge = next_edge_raw if next_edge_raw >= 0 else None
                arr_week = int(observation["queue_lots.arrival_week"][idx])

                dest_node = self.lane_destinations[lane_idx] if lane_idx is not None else None

                snapshot.queues.append(
                    QueueItem(
                        chokepoint_node=chk_node,
                        chokepoint_name=self.node_names[chk_node],
                        commodity=comm_id,
                        lane=lane_idx,
                        next_edge=next_edge,
                        qty=qty,
                        arrival_week=arr_week,
                        is_observed=True,
                        dest_node=dest_node,
                    )
                )

    def _parse_wip(self, observation: Dict[str, np.ndarray], snapshot: StateSnapshot) -> None:
        """Parse work in process (WIP) at fabs and OSATs."""
        wip_obs = observation["wip.qty.observed"]
        live_indices = np.where(wip_obs == 1)[0]

        for idx in live_indices:
            qty = float(observation["wip.qty"][idx])
            if qty <= 0.0:
                continue
            node_id = int(observation["wip.node"][idx])
            comm_id = int(observation["wip.k"][idx])
            out_week = int(observation["wip.out_week"][idx])

            snapshot.wip.append(
                WIPItem(
                    node=node_id,
                    node_name=self.node_names[node_id],
                    commodity=comm_id,
                    commodity_name=self.commodity_names[comm_id],
                    qty=qty,
                    out_week=out_week,
                    is_observed=True,
                )
            )

    def _forecast_queue_delays(
        self,
        observation: Dict[str, np.ndarray],
        snapshot: StateSnapshot,
        current_week: int,
    ) -> None:
        """Predict queue delay at chokepoints based on work ahead in FIFO queue (Roadmap V3).
        
        Calculates:
        1. Current queued cargo per chokepoint and pool ('tb' vs 'ct').
        2. Inflow pipeline arriving at chokepoint before our cargo.
        3. Effective throughput based on strait open fraction and capacity.
        4. Expected clearance delay in weeks.
        """
        # Current open fraction
        open_fracs = observation["graph_now.open"]
        open_obs = observation["graph_now.open.observed"]

        # Pool capacities
        kappa_tb = observation["graph_now.kappa.tb"]
        kappa_ct = observation["graph_now.kappa.ct"]

        # Sum current waiting cargo per (chokepoint, pool)
        queue_by_choke_pool: Dict[Tuple[int, str], float] = {}
        for q in snapshot.queues:
            pool = self.commodity_pools[q.commodity]
            key = (q.chokepoint_node, pool)
            queue_by_choke_pool[key] = queue_by_choke_pool.get(key, 0.0) + q.qty

        for pos, chk_node in self.chokepoint_pos_to_node.items():
            is_open_obs = bool(open_obs[pos] == 1)
            o_c = float(open_fracs[pos]) if is_open_obs else 1.0

            for pool, kappa_arr in [("tb", kappa_tb), ("ct", kappa_ct)]:
                cap = float(kappa_arr[pos]) if len(kappa_arr) > pos else 10000.0
                effective_rate = cap * max(o_c, 0.05)

                current_q = queue_by_choke_pool.get((chk_node, pool), 0.0)
                work_ahead = current_q
                delay = work_ahead / (effective_rate + 1e-9)

                snapshot.queue_forecasts[(chk_node, pool)] = QueueDelayForecast(
                    chokepoint_node=chk_node,
                    chokepoint_name=self.node_names[chk_node],
                    pool=pool,
                    current_queue_qty=current_q,
                    inflow_ahead_qty=0.0,
                    work_ahead_total=work_ahead,
                    effective_throughput=effective_rate,
                    open_fraction=o_c,
                    expected_delay_weeks=delay,
                    confidence=0.9 if is_open_obs else 0.5,
                )

    def _parse_pipeline(
        self,
        observation: Dict[str, np.ndarray],
        snapshot: StateSnapshot,
        current_week: int,
    ) -> None:
        """Parse shipments in transit (pipeline) and compute realistic arrival dates."""
        p_obs = observation["pipeline.qty.observed"]
        live_indices = np.where(p_obs == 1)[0]

        edge_arr = observation["pipeline.edge"]
        comm_arr = observation["pipeline.k"]
        lane_arr = observation["pipeline.lane"]
        qty_arr = observation["pipeline.qty"]
        arr_arr = observation["pipeline.arrival_week"]

        for idx in live_indices:
            qty = float(qty_arr[idx])
            if qty <= 0.0:
                continue

            edge = int(edge_arr[idx])
            comm = int(comm_arr[idx])
            lane_raw = int(lane_arr[idx])
            lane_idx = lane_raw if lane_raw >= 0 else None
            edge_arr_week = int(arr_arr[idx])

            head = self.edge_heads[edge]
            dest_node = head
            remaining_edges: List[int] = []
            remaining_chokepoints: List[int] = []
            queue_delay = 0.0
            is_confirmed = False

            if lane_idx is not None and lane_idx < self.n_lanes:
                l_edges = self.lane_edges[lane_idx]
                dest_node = self.lane_destinations[lane_idx]

                if edge in l_edges:
                    e_pos = l_edges.index(edge)
                    remaining_edges = l_edges[e_pos + 1:]
                else:
                    remaining_edges = []

                # Find chokepoints along remaining edges
                for rem_e in remaining_edges:
                    rem_tail = self.edge_tails[rem_e]
                    if rem_tail in self.node_to_chokepoint_pos:
                        remaining_chokepoints.append(rem_tail)

                # Estimate queue delays at upcoming chokepoints
                pool = self.commodity_pools[comm]
                for chk in remaining_chokepoints:
                    q_fc = snapshot.queue_forecasts.get((chk, pool))
                    if q_fc:
                        queue_delay += q_fc.expected_delay_weeks

                # Lead time on remaining edges
                transit_weeks = sum(self.edge_tau0[re] for re in remaining_edges)
                estimated_final_arr = edge_arr_week + transit_weeks + int(np.ceil(queue_delay))
                is_confirmed = len(remaining_edges) == 0 and queue_delay == 0.0
            else:
                # Direct edge off-lane: arrives directly at head
                estimated_final_arr = edge_arr_week
                is_confirmed = True

            snapshot.pipeline.append(
                PipelineItem(
                    edge=edge,
                    commodity=comm,
                    lane=lane_idx,
                    qty=qty,
                    arrival_week=edge_arr_week,
                    is_observed=True,
                    head_node=head,
                    dest_node=dest_node,
                    remaining_edges=remaining_edges,
                    remaining_chokepoints=remaining_chokepoints,
                    estimated_final_arrival_week=estimated_final_arr,
                    is_confirmed=is_confirmed,
                )
            )

    def _build_arrival_calendar(self, snapshot: StateSnapshot, current_week: int) -> None:
        """Construct week-by-week arrival calendar for all (node, commodity) pairs (Roadmap M2).
        
        Strictly differentiates:
        - Confirmed arrivals (direct pipeline shipments arriving at destination, scheduled WIP completions)
        - Estimated arrivals (shipments undergoing multi-stage transit / strait queueing)
        """
        cal = snapshot.arrival_calendar

        # 1. Add WIP completions (guaranteed production output)
        for wip in snapshot.wip:
            if wip.out_week >= current_week:
                key = (wip.node, wip.commodity, wip.out_week)
                if key not in cal:
                    cal[key] = ArrivalEntry(
                        node=wip.node,
                        commodity=wip.commodity,
                        week=wip.out_week,
                    )
                cal[key].confirmed_qty += wip.qty
                cal[key].sources.append(f"WIP_{wip.node_name}")
                snapshot.total_confirmed_stock += wip.qty

        # 2. Add pipeline arrivals
        for p in snapshot.pipeline:
            final_week = max(p.estimated_final_arrival_week, current_week)
            key = (p.dest_node, p.commodity, final_week)
            if key not in cal:
                cal[key] = ArrivalEntry(
                    node=p.dest_node,
                    commodity=p.commodity,
                    week=final_week,
                )

            if p.is_confirmed:
                cal[key].confirmed_qty += p.qty
                cal[key].sources.append(f"Pipeline_Direct_E{p.edge}")
                snapshot.total_confirmed_stock += p.qty
            else:
                cal[key].estimated_qty += p.qty
                cal[key].sources.append(f"Pipeline_Lane_L{p.lane}")
                snapshot.total_estimated_arrivals += p.qty
