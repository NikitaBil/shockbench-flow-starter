"""Markian's Demand & Needs Module (Tasks M3 & M4, M6).

Generates structured DeliveryNeed orders:
- need_id: unique identifier
- destination_node: receiving node
- commodity_id: required commodity
- qty: quantity needed
- due_week: target delivery week
- priority: 1 (backlog), 2 (market/grid), 3 (fab production), 4 (safety stock)
- reason: 'backlog', 'market_demand', 'production', 'safety_stock'
- shortage_cost_est: penalty cost per unit in USD
- confidence: forecast certainty

Strictly isolates backlog, current consumption, production requirements,
and safety stocks without masking urgent deficits.
"""

from typing import Any, Dict, List, Optional, Tuple
import numpy as np

try:
    from contracts import DeliveryNeed, StateSnapshot
except ImportError:
    from .contracts import DeliveryNeed, StateSnapshot


class NeedsForecaster:
    """Computes supply chain requirements and outputs prioritized DeliveryNeed objects."""

    def __init__(self, config: Dict[str, Any]):
        self.static = config["static"]
        self.layout = config["layout"]
        self.T = int(config.get("T", 52))

        # Node metadata
        nodes = self.static["nodes"]
        self.node_names = list(nodes["id"])
        self.node_types = list(nodes["type"])
        self.node_id_to_idx = {name: i for i, name in enumerate(self.node_names)}

        # Commodity metadata
        commodities = self.static["commodities"]
        self.commodity_names = list(commodities["id"])
        self.commodity_values = list(commodities["v"])
        self.comm_id_to_idx = {name: i for i, name in enumerate(self.commodity_names)}

        # Sink metadata (market demand)
        sinks = self.static["sinks"]
        self.sink_nodes = list(sinks["node"])
        self.sink_commodities = list(sinks["k"])
        self.sink_penalties = [float(p) for p in sinks["pi"]]
        self.sink_key_to_penalty = {
            (self.sink_nodes[i], self.sink_commodities[i]): self.sink_penalties[i]
            for i in range(len(self.sink_nodes))
        }

        # Fab & Grid metadata from static instance
        self.fabs_info: List[Dict[str, Any]] = []
        self.grids_info: List[Dict[str, Any]] = []
        self.osats_info: List[Dict[str, Any]] = []

        instance_nodes = self.static.get("instance", {}).get("nodes", [])
        for n_dict in instance_nodes:
            n_type = n_dict.get("type")
            n_name = n_dict.get("id")
            n_idx = self.node_id_to_idx.get(n_name, -1)
            if n_type == "fab":
                fab_data = n_dict.get("fab", {})
                self.fabs_info.append({
                    "node": n_idx,
                    "name": n_name,
                    "input_k": self.comm_id_to_idx.get(fab_data.get("input", "wafer"), 0),
                    "product_k": self.comm_id_to_idx.get(fab_data.get("product", "chip_le_raw"), 0),
                    "cap0": float(fab_data.get("cap0", 1000.0)),
                    "grid": fab_data.get("grid"),
                })
            elif n_type == "grid":
                grid_data = n_dict.get("grid", {})
                fuel_shares = grid_data.get("shares", {})
                primary_fuel = grid_data.get("rationed", "lng")
                self.grids_info.append({
                    "node": n_idx,
                    "name": n_name,
                    "base_load": float(grid_data.get("base_load", 1000.0)),
                    "fuel_k": self.comm_id_to_idx.get(primary_fuel, 0),
                    "fuel_share": float(fuel_shares.get(primary_fuel, 0.4)),
                    "voll": float(grid_data.get("voll", 1000000.0)),
                })
            elif n_type == "osat":
                osat_data = n_dict.get("osat", {})
                self.osats_info.append({
                    "node": n_idx,
                    "name": n_name,
                    "thr": float(osat_data.get("thr", 10000.0)),
                })

    def generate_delivery_needs(
        self,
        observation: Dict[str, np.ndarray],
        snapshot: StateSnapshot,
        global_risk: float = 0.0,
    ) -> List[DeliveryNeed]:
        """Generate full list of DeliveryNeed orders (Roadmap M4)."""
        current_week = snapshot.week
        needs: List[DeliveryNeed] = []

        # -------------------------------------------------------------
        # Category 1: Backlog Needs (Priority 1 - Immediate / Critical)
        # -------------------------------------------------------------
        for (sink_node, comm_id), b_item in snapshot.backlog.items():
            if b_item.qty > 1e-4:
                needs.append(
                    DeliveryNeed(
                        need_id=f"backlog_{sink_node}_{comm_id}_w{current_week}",
                        destination_node=sink_node,
                        destination_name=b_item.sink_name,
                        commodity_id=comm_id,
                        commodity_name=b_item.commodity_name,
                        qty=b_item.qty,
                        due_week=current_week,
                        priority=1,
                        reason="backlog",
                        shortage_cost_est=b_item.shortage_penalty,
                        confidence=1.0,
                        created_week=current_week,
                    )
                )

        # -------------------------------------------------------------
        # Category 2: Market Demand Needs (Priority 2 - 8-week horizon)
        # -------------------------------------------------------------
        forecast_qty = observation["demand_forecast.qty"]
        forecast_obs = observation["demand_forecast.qty.observed"]
        demands_layout = self.layout.get("demands", [])

        for d_idx, d_slot in enumerate(demands_layout):
            sink_node, comm_id = int(d_slot[0]), int(d_slot[1])
            penalty = self.sink_key_to_penalty.get((sink_node, comm_id), 5000.0)

            # Cumulative stock projection at this sink
            curr_stock_item = snapshot.stock.get((sink_node, comm_id))
            running_stock = curr_stock_item.qty if curr_stock_item else 0.0

            n_horizons = forecast_qty.shape[1] if forecast_qty.ndim == 2 else len(forecast_qty)
            for h in range(min(8, n_horizons)):
                due_week = current_week + h
                if due_week > self.T:
                    break  # Roadmap M6: Do not plan beyond episode horizon T

                is_obs = bool(forecast_obs[d_idx, h] == 1) if forecast_qty.ndim == 2 else bool(forecast_obs[h] == 1)
                d_val = float(forecast_qty[d_idx, h]) if forecast_qty.ndim == 2 else float(forecast_qty[h])
                if not is_obs or d_val <= 0.0:
                    continue

                # Add confirmed arrivals due up to this week
                for arr_w in range(current_week, due_week + 1):
                    cal_entry = snapshot.arrival_calendar.get((sink_node, comm_id, arr_w))
                    if cal_entry:
                        running_stock += cal_entry.confirmed_qty + cal_entry.estimated_qty

                # Net need after subtracting consumption
                if running_stock < d_val:
                    deficit = d_val - max(running_stock, 0.0)
                    running_stock = 0.0
                    confidence = max(0.4, 1.0 - 0.06 * h)

                    needs.append(
                        DeliveryNeed(
                            need_id=f"mkt_{sink_node}_{comm_id}_w{due_week}",
                            destination_node=sink_node,
                            destination_name=self.node_names[sink_node],
                            commodity_id=comm_id,
                            commodity_name=self.commodity_names[comm_id],
                            qty=deficit,
                            due_week=due_week,
                            priority=2,
                            reason="market_demand",
                            shortage_cost_est=penalty,
                            confidence=confidence,
                            created_week=current_week,
                        )
                    )
                else:
                    running_stock -= d_val

        # -------------------------------------------------------------
        # Category 3: Power Grid Fuel Requirements (Priority 2 - High VOLL)
        # -------------------------------------------------------------
        for g in self.grids_info:
            grid_node = g["node"]
            fuel_k = g["fuel_k"]
            voll = g["voll"]

            # Fuel demand based on base load and fuel share
            fuel_need_weekly = g["base_load"] * g["fuel_share"]
            if fuel_need_weekly <= 0.0:
                continue

            curr_fuel = snapshot.stock.get((grid_node, fuel_k))
            fuel_on_hand = curr_fuel.qty if curr_fuel else 0.0

            # Incoming fuel in next 3 weeks
            incoming_fuel = sum(
                snapshot.arrival_calendar.get((grid_node, fuel_k, w), None).confirmed_qty
                + snapshot.arrival_calendar.get((grid_node, fuel_k, w), None).estimated_qty
                for w in range(current_week, min(current_week + 4, self.T + 1))
                if snapshot.arrival_calendar.get((grid_node, fuel_k, w)) is not None
            )

            total_fuel_visible = fuel_on_hand + incoming_fuel
            target_fuel_cover = fuel_need_weekly * (3.0 + 2.0 * global_risk)

            if total_fuel_visible < target_fuel_cover:
                fuel_shortage = target_fuel_cover - total_fuel_visible
                needs.append(
                    DeliveryNeed(
                        need_id=f"grid_{grid_node}_{fuel_k}_w{current_week + 1}",
                        destination_node=grid_node,
                        destination_name=g["name"],
                        commodity_id=fuel_k,
                        commodity_name=self.commodity_names[fuel_k],
                        qty=fuel_shortage,
                        due_week=min(current_week + 2, self.T),
                        priority=2,
                        reason="production",
                        shortage_cost_est=voll,
                        confidence=0.95,
                        created_week=current_week,
                    )
                )

        # -------------------------------------------------------------
        # Category 4: Fab Wafer Input Requirements (Priority 3 - Production)
        # -------------------------------------------------------------
        for fab in self.fabs_info:
            fab_node = fab["node"]
            wafer_k = fab["input_k"]
            cap_eff = fab["cap0"]

            # Effective wafer requirement
            curr_wafers = snapshot.stock.get((fab_node, wafer_k))
            wafers_on_hand = curr_wafers.qty if curr_wafers else 0.0

            incoming_wafers = sum(
                snapshot.arrival_calendar.get((fab_node, wafer_k, w), None).confirmed_qty
                + snapshot.arrival_calendar.get((fab_node, wafer_k, w), None).estimated_qty
                for w in range(current_week, min(current_week + 4, self.T + 1))
                if snapshot.arrival_calendar.get((fab_node, wafer_k, w)) is not None
            )

            target_wafer_cover = cap_eff * (2.0 + 1.5 * global_risk)
            if wafers_on_hand + incoming_wafers < target_wafer_cover:
                wafer_deficit = target_wafer_cover - (wafers_on_hand + incoming_wafers)
                # Shortage cost: upstream value of wafer + downstream chip delay
                downstream_loss_est = 25000.0

                needs.append(
                    DeliveryNeed(
                        need_id=f"fab_{fab_node}_{wafer_k}_w{current_week + 2}",
                        destination_node=fab_node,
                        destination_name=fab["name"],
                        commodity_id=wafer_k,
                        commodity_name=self.commodity_names[wafer_k],
                        qty=wafer_deficit,
                        due_week=min(current_week + 2, self.T),
                        priority=3,
                        reason="production",
                        shortage_cost_est=downstream_loss_est,
                        confidence=0.90,
                        created_week=current_week,
                    )
                )

        # -------------------------------------------------------------
        # Category 5: Risk-Aware Safety Stock Buffer (Priority 4 - Safety)
        # -------------------------------------------------------------
        # Roadmap M6: Safety stock depends on risk, lead time, and remaining T
        if global_risk > 0.25 and current_week < self.T - 4:
            for (sink_node, comm_id), b_item in snapshot.backlog.items():
                curr_stock_item = snapshot.stock.get((sink_node, comm_id))
                stock_avail = curr_stock_item.qty if curr_stock_item else 0.0

                # Target buffer: 1-2 weeks of average demand
                weekly_rate = 500.0  # conservative baseline
                target_safety = weekly_rate * (1.0 + 2.0 * global_risk)

                if stock_avail < target_safety:
                    buffer_need = target_safety - stock_avail
                    needs.append(
                        DeliveryNeed(
                            need_id=f"safety_{sink_node}_{comm_id}_w{current_week + 3}",
                            destination_node=sink_node,
                            destination_name=b_item.sink_name,
                            commodity_id=comm_id,
                            commodity_name=b_item.commodity_name,
                            qty=buffer_need,
                            due_week=min(current_week + 3, self.T),
                            priority=4,
                            reason="safety_stock",
                            shortage_cost_est=b_item.shortage_penalty * 0.5,
                            confidence=0.75,
                            created_week=current_week,
                        )
                    )

        # Sort needs by priority (1 is most urgent) and then due_week
        needs.sort(key=lambda n: (n.priority, n.due_week))
        return needs
