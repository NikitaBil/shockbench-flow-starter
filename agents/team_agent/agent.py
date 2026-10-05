"""Integrated Agent based on the ShockBench-Flow Team Roadmap and Agent3 architecture.

Executes the agreed team cycle (Roadmap Section 3.5):
  Observation
  → StateSnapshot (Markian M1, M2: stock, backlog, pipeline, WIP, arrival calendar)
  → RiskState (Markian M5: warnings, messages de-dup, pending sanctions, closures)
  → DeliveryNeed (Markian M3, M4: prioritized delivery needs, shortage cost, confidence)
  → QueueDelayForecast (Markian / V3: ahead-of-queue work & transit delays)
  → Route Scoring & Flow Allocation (Vitya / Nikita: capacity, mask, diversification)
  → Action

Only imports Python standard library and numpy (fully server-compliant).
Dynamically reads all dimensions from config (supports Tiny, Small, and Full).
"""

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

try:
    from contracts import DeliveryNeed, QueueDelayForecast, RiskState, StateSnapshot
    from needs import NeedsForecaster
    from risk import RiskAnalyzer
    from state import StateManager
except ImportError:
    from .contracts import DeliveryNeed, QueueDelayForecast, RiskState, StateSnapshot
    from .needs import NeedsForecaster
    from .risk import RiskAnalyzer
    from .state import StateManager


class Agent:
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.static = config["static"]
        self.layout = config["layout"]
        self.spaces = config["spaces"]
        self.T = int(config.get("T", 52))

        self.rng = np.random.default_rng(config.get("policy_seed", 0))

        # Initialize Markian's state, needs, and risk modules
        self.state_mgr = StateManager(config)
        self.needs_forecaster = NeedsForecaster(config)
        self.risk_analyzer = RiskAnalyzer(config)

        # Action space shapes
        self.n_slots = self.spaces["action"]["flows"]["shape"][0]

        # Action slots metadata
        slots = self.static["action_slots"]
        self.slot_edges = np.array(slots["edge"], dtype=int)
        self.slot_comms = np.array(slots["k"], dtype=int)
        self.slot_lanes = [
            int(lane) if lane is not None and lane >= 0 else None
            for lane in slots["lane"]
        ]

        # Edge nominal properties
        u0 = self.static["edges"]["u0"]
        c0 = self.static["edges"]["c0"]
        tau0 = self.static["edges"]["tau0"]
        self.capacity = np.array(
            [u0[e] if u0[e] is not None else 0.0 for e in self.slot_edges],
            dtype=float,
        )
        self.slot_c0 = np.array(
            [c0[e] if c0[e] is not None else 0.0 for e in self.slot_edges],
            dtype=float,
        )
        self.slot_tau0 = np.array(
            [tau0[e] if tau0[e] is not None else 1.0 for e in self.slot_edges],
            dtype=float,
        )

        # Chokepoints mapping per slot
        pos_by_chk = {node: i for i, node in enumerate(self.layout.get("chokepoints", []))}
        lane_chokes = self.static["lanes"]["chokepoints"]
        self.slot_chokepoint_nodes: List[List[int]] = [
            list(lane_chokes[lane]) if lane is not None else []
            for lane in self.slot_lanes
        ]
        self.slot_chokepoint_indices: List[List[int]] = [
            [pos_by_chk[c] for c in chks if c in pos_by_chk]
            for chks in self.slot_chokepoint_nodes
        ]

        # Destination node for each slot
        edges_head = self.static["edges"]["head"]
        lanes_edges = self.static["lanes"]["edges"]
        self.slot_destinations: List[int] = []
        for s in range(self.n_slots):
            lane_idx = self.slot_lanes[s]
            if lane_idx is not None and len(lanes_edges[lane_idx]) > 0:
                final_edge = lanes_edges[lane_idx][-1]
                self.slot_destinations.append(edges_head[final_edge])
            else:
                self.slot_destinations.append(edges_head[self.slot_edges[s]])

        # Alt route flags
        edges_alt = self.static["edges"]["alt_of"]
        self.slot_is_alt = np.array(
            [edges_alt[self.slot_edges[s]] is not None for s in range(self.n_slots)],
            dtype=bool,
        )

        # Commodity pool & values
        self.commodity_v = np.array(self.static["commodities"]["v"], dtype=float)
        self.commodity_pools = list(self.static["commodities"]["pool"])

    def act(self, observation: Dict[str, np.ndarray]) -> Dict[str, np.ndarray]:
        """Weekly decision loop executing the full StateSnapshot and DeliveryNeed cycle."""
        current_week = int(observation["week"][0])

        # -------------------------------------------------------------
        # Step 1: Markian M1 & M2 — Capture StateSnapshot & Arrival Calendar
        # -------------------------------------------------------------
        snapshot: StateSnapshot = self.state_mgr.capture_snapshot(observation)

        # -------------------------------------------------------------
        # Step 2: Markian M5 — Multi-Source Risk State
        # -------------------------------------------------------------
        risk_state: RiskState = self.risk_analyzer.analyze_risks(observation)
        global_risk = risk_state.global_risk

        # -------------------------------------------------------------
        # Step 3: Markian M3, M4 & M6 — Forecast Needs & DeliveryNeed
        # -------------------------------------------------------------
        delivery_needs: List[DeliveryNeed] = self.needs_forecaster.generate_delivery_needs(
            observation, snapshot, global_risk
        )

        # -------------------------------------------------------------
        # Step 4: Route Scoring with Ahead-of-Queue Delays (Roadmap V3)
        # -------------------------------------------------------------
        route_scores = self._score_routes(observation, snapshot, risk_state)

        # -------------------------------------------------------------
        # Step 5: Allocate Flows to satisfy DeliveryNeed orders
        # -------------------------------------------------------------
        flows = self._allocate_flows(
            observation, snapshot, delivery_needs, route_scores, risk_state
        )

        # Final verification: enforce non-negative, finite, and action_mask
        mask = observation["action_mask"].astype(float)
        flows = np.nan_to_num(flows, nan=0.0, posinf=0.0, neginf=0.0)
        flows = np.clip(flows, 0.0, None) * mask

        return {"flows": flows}

    def _score_routes(
        self,
        observation: Dict[str, np.ndarray],
        snapshot: StateSnapshot,
        risk_state: RiskState,
    ) -> np.ndarray:
        """Evaluate route attractiveness combining cost, lead time, queue delay, and risk."""
        scores = np.zeros(self.n_slots, dtype=float)

        edge_c = observation["graph_now.c"]
        edge_c_obs = observation["graph_now.c.observed"]
        edge_tau = observation["graph_now.tau"]
        edge_tau_obs = observation["graph_now.tau.observed"]
        open_frac = observation["graph_now.open"]
        open_obs = observation["graph_now.open.observed"]
        tariff = observation["graph_now.tariff"]
        tariff_obs = observation["graph_now.tariff.observed"]

        for s in range(self.n_slots):
            e = self.slot_edges[s]
            k = self.slot_comms[s]
            pool = self.commodity_pools[k]

            # 1. Base freight cost
            cost = float(edge_c[e]) if edge_c_obs[e] == 1 else float(self.slot_c0[s])
            scores[s] += cost

            # 2. Tariff penalty
            if tariff_obs[e, k] == 1:
                t_rate = float(tariff[e, k])
                scores[s] += t_rate * float(self.commodity_v[k]) * 0.5

            # 3. Nominal edge lead time
            tau = float(edge_tau[e]) if edge_tau_obs[e] == 1 else float(self.slot_tau0[s])
            scores[s] += tau * 0.5

            # 4. Ahead-of-Queue delay forecast (Roadmap V3 / M2)
            for chk_node in self.slot_chokepoint_nodes[s]:
                q_forecast = snapshot.queue_forecasts.get((chk_node, pool))
                if q_forecast and q_forecast.expected_delay_weeks > 0.0:
                    # Delay penalty: delayed cargo incurs holding cost and late delivery
                    scores[s] += q_forecast.expected_delay_weeks * 10.0

            # 5. Strait closure & risk penalties
            for c_pos in self.slot_chokepoint_indices[s]:
                if open_obs[c_pos] == 1:
                    closure = 1.0 - float(open_frac[c_pos])
                    scores[s] += closure * 15.0

            # 6. Specific chokepoint risk from early warnings
            for chk_node in self.slot_chokepoint_nodes[s]:
                chk_r = risk_state.chokepoint_risk.get(chk_node, 0.0)
                if chk_r > 0.2:
                    scores[s] += chk_r * 5.0

            # 7. Edge sanction risk
            e_r = risk_state.edge_risk.get(e, 0.0)
            if e_r > 0.2:
                scores[s] += e_r * 8.0

        return scores

    def _allocate_flows(
        self,
        observation: Dict[str, np.ndarray],
        snapshot: StateSnapshot,
        delivery_needs: List[DeliveryNeed],
        route_scores: np.ndarray,
        risk_state: RiskState,
    ) -> np.ndarray:
        """Allocate dispatch quantities respecting capacities, delivery needs, and risks."""
        mask = observation["action_mask"].astype(float)
        flows = self.capacity.copy() * mask

        open_frac = observation["graph_now.open"]
        open_obs = observation["graph_now.open.observed"]

        # Phase 1: Throttle flows through closed straits to prevent costly holding in queue
        for s in range(self.n_slots):
            for c_pos in self.slot_chokepoint_indices[s]:
                if open_obs[c_pos] == 1:
                    openness = max(float(open_frac[c_pos]), 0.0)
                    flows[s] *= (openness ** 1.2)

        # Phase 2: Prioritized fulfillment for urgent delivery needs (Priority 1 backlog & Priority 2 markets)
        urgent_destinations: Dict[Tuple[int, int], float] = {}
        for nd in delivery_needs:
            if nd.priority <= 2:
                key = (nd.destination_node, nd.commodity_id)
                urgent_destinations[key] = urgent_destinations.get(key, 0.0) + nd.qty

        # Ensure slots serving urgent destinations are maintained at full capacity
        for s in range(self.n_slots):
            dest = self.slot_destinations[s]
            comm = self.slot_comms[s]
            if (dest, comm) in urgent_destinations and mask[s] > 0:
                # Do not throttle this slot if route is open
                flows[s] = max(flows[s], self.capacity[s] * 0.95 * mask[s])

        # Phase 3: Risk-dependent diversification when risk is elevated
        global_risk = risk_state.global_risk
        if global_risk > 0.35 and self.n_slots > 1:
            commodity_slots: Dict[int, List[int]] = {}
            for s in range(self.n_slots):
                if mask[s] > 0:
                    commodity_slots.setdefault(self.slot_comms[s], []).append(s)

            for comm, s_list in commodity_slots.items():
                if len(s_list) <= 1:
                    continue
                scores_k = [route_scores[s] for s in s_list]
                max_sc = max(scores_k) + 1e-9
                inv_scores = np.array([max_sc - sc + 1.0 for sc in scores_k])

                temp = 0.5 if global_risk < 0.5 else 1.5
                exp_w = np.exp(inv_scores / (temp * max_sc + 1e-9))
                weights = exp_w / (np.sum(exp_w) + 1e-9)

                total_pool = sum(flows[s] for s in s_list)
                for idx, s in enumerate(s_list):
                    flows[s] = total_pool * float(weights[idx])

        # Phase 4: Pending sanctions — preemptively taper off
        for s in range(self.n_slots):
            e = self.slot_edges[s]
            e_risk = risk_state.edge_risk.get(e, 0.0)
            if e_risk > 0.6:
                flows[s] *= 0.15
            elif e_risk > 0.3:
                flows[s] *= 0.50

        # Phase 5: Capacity clipping
        edge_u = observation["graph_now.u"]
        edge_u_obs = observation["graph_now.u.observed"]
        for s in range(self.n_slots):
            e = self.slot_edges[s]
            if edge_u_obs[e] == 1:
                cap = float(edge_u[e])
                if cap >= 0:
                    flows[s] = min(flows[s], cap)

        return flows
