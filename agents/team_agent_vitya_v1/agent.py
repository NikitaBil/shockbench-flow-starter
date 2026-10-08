"""Team integration: a working heuristic plus an explicit teammate pipeline.

Static route information is built once per episode. Weekly closures and the
action mask still follow the original heuristic until real modules are wired.
"""

import json
from pathlib import Path

import numpy as np
from closed_route_dispatch import ClosedRouteDispatch
from dispatch import RoutePreferences
from dispatch_priority import DispatchPriority
from fuel_batch import FuelBatch
from fuel_lookahead import FuelLookahead
from fuel_mpc import FuelMPC
from fuel_release import FuelRelease
from integration import ActionValidator, build_pipeline
from network import StaticNetwork
from production_horizon import ProductionHorizon
from production_residual import ProductionResidual
from rebalance import StockRebalancer
from sales_dispatch import SalesDispatch


HERE = Path(__file__).resolve().parent
PARAMS = {
    "fraction": 1.0,  # share of each slot's capacity: one number, or a list with one per slot
    "closure_power": 1.0,  # a lane through a strait ships (its open fraction) ** closure_power; 0 ignores closures
}
if (HERE / "params.json").is_file():
    PARAMS |= json.loads((HERE / "params.json").read_text())


class Agent:
    def __init__(self, config=None, *, pipeline=None):
        self.config = config
        self.network = StaticNetwork(config)
        self.validator = ActionValidator(config)
        enabled = PARAMS.get("allocation_enabled", False)
        if not isinstance(enabled, bool):
            raise ValueError("allocation_enabled must be a boolean")
        queue_eta = PARAMS.get("queue_eta_enabled", False)
        if not isinstance(queue_eta, bool):
            raise ValueError("queue_eta_enabled must be a boolean")
        queue_forecast = PARAMS.get("queue_forecast_enabled", False)
        if not isinstance(queue_forecast, bool):
            raise ValueError("queue_forecast_enabled must be a boolean")
        self.pipeline = (
            pipeline
            if pipeline is not None
            else build_pipeline(
                config,
                self.network,
                enabled=enabled,
                queue_eta_enabled=queue_eta,
                queue_forecast_enabled=queue_forecast,
                planner_options=PARAMS.get("planner_options"),
            )
        )
        self.last_allocation = None
        self.cap = np.array(
            [self.network.edge_capacity[route.edge_id] for route in self.network.routes], dtype=float
        ) * np.asarray(PARAMS["fraction"], dtype=float)
        self.power = float(PARAMS["closure_power"])
        closure_powers = PARAMS.get("closure_powers", {})
        if not isinstance(closure_powers, dict):
            raise ValueError("closure_powers must be a mapping")
        for name, value in closure_powers.items():
            if (name not in self.network.commodity_names or isinstance(value, bool)
                    or not isinstance(value, (int, float)) or not np.isfinite(value) or value < 0):
                raise ValueError("closure_powers contains an invalid commodity or weight")
        self.slot_powers = tuple(closure_powers.get(self.network.commodity_names[route.commodity_id], self.power)
                                 for route in self.network.routes)
        self.through = tuple(route.chokepoint_positions for route in self.network.routes)
        self.closed_route_dispatch = ClosedRouteDispatch(
            self.network, floor=PARAMS.get("closed_route_floor", 0.0),
            minimum_lead=PARAMS.get("closed_route_min_lead", 1))
        self.route_preferences = RoutePreferences(
            config, self.network,
            transit_bias=PARAMS.get("transit_bias", 0.0),
            cost_bias=PARAMS.get("cost_bias", 0.0),
            air_fraction=PARAMS.get("air_fraction", 1.0),
            commodity_cost_bias=PARAMS.get("commodity_cost_bias"),
            commodity_transit_bias=PARAMS.get("commodity_transit_bias"),
            queue_bias=PARAMS.get("route_queue_bias", 0.0),
            pending_bias=PARAMS.get("route_pending_bias", 0.0),
            warning_bias=PARAMS.get("route_warning_bias", 0.0),
            warning_memory=PARAMS.get("route_warning_memory", 0.0),
        )
        self.dispatch_priority = DispatchPriority(
            config, self.network,
            skip_zero_demand=PARAMS.get("skip_zero_demand", False),
            product_priority_power=PARAMS.get("product_priority_power", 0.0),
            sink_demand_power=PARAMS.get("sink_demand_power", 0.0),
            fuel_region_weights=PARAMS.get("fuel_region_weights"),
        )
        self.production_horizon = ProductionHorizon(
            config, self.network, enabled=PARAMS.get("production_horizon_enabled", False))
        self.stock_rebalancer = StockRebalancer(
            config, self.network,
            fuel_power=PARAMS.get("balance_fuel_power", 0.0),
            sink_power=PARAMS.get("balance_sink_power", 0.0),
            cover_floor=PARAMS.get("balance_cover_floor", 1.0),
            stock_first=PARAMS.get("stock_first", False),
            fill_spare=PARAMS.get("fill_spare", False),
            pipeline_horizon=PARAMS.get("balance_pipeline_horizon", 0),
            rate_power=PARAMS.get("balance_rate_power", 0.0),
            production_power=PARAMS.get("balance_production_power", 0.0),
            fuel_industry_bonus=PARAMS.get("fuel_industry_bonus", 0.0),
            priority_first=PARAMS.get("priority_first", False),
            production_value_power=PARAMS.get("production_value_power", 0.0),
            production_energy_power=PARAMS.get("production_energy_power", 0.0),
            production_output_power=PARAMS.get("production_output_power", 0.0),
            production_wip_horizon=PARAMS.get("production_wip_horizon", 0),
            fuel_mark_rate_floor=PARAMS.get("fuel_mark_rate_floor", 0.0),
            fuel_margin_bonus=PARAMS.get("fuel_margin_bonus", 0.0),
        )
        self.fuel_release = FuelRelease(
            config, self.network,
            power=PARAMS.get("fuel_release_power", 0.0),
            transit_bias=PARAMS.get("fuel_release_transit_bias", 0.0),
            pipeline_horizon=PARAMS.get("balance_pipeline_horizon", 0),
            rate_power=PARAMS.get("fuel_release_rate_power", 0.0),
            cover_floor=PARAMS.get("balance_cover_floor", 1.0),
            only_blocked=PARAMS.get("fuel_release_only_blocked", False),
            preserve_routes=PARAMS.get("fuel_release_preserve_routes", False),
            resource_safe=PARAMS.get("fuel_release_resource_safe", False),
            queue_first_fleet=PARAMS.get("queue_first_fleet", False),
            industry_bonus=PARAMS.get("fuel_industry_bonus", 0.0),
            receiver_cover=PARAMS.get("release_receiver_cover", 0.0),
            origin_bonus=PARAMS.get("fuel_release_origin_bonus", 1.25),
            origin_urgency=PARAMS.get("fuel_release_origin_urgency", 0.0),
            fuel_mark_rate_floor=PARAMS.get("fuel_mark_rate_floor", 0.0),
            fuel_margin_bonus=PARAMS.get("fuel_margin_bonus", 0.0),
        )
        self.sales_dispatch = SalesDispatch(
            config, self.network, cover=PARAMS.get("sales_cover", 0.0),
            discount=PARAMS.get("sales_discount", 0.05),
        )
        self.upstream_dispatch = SalesDispatch(
            config, self.network, stage="fab", cover=PARAMS.get("upstream_cover", 0.0),
            discount=PARAMS.get("upstream_discount", 0.05),
        )
        self.packaging_dispatch = SalesDispatch(
            config, self.network, stage="raw", cover=PARAMS.get("raw_cover", 0.0),
            discount=PARAMS.get("raw_discount", 0.05),
        )
        self.fuel_dispatch = SalesDispatch(
            config, self.network, stage="fuel", cover=PARAMS.get("fuel_cover", 0.0),
            discount=PARAMS.get("fuel_discount", 0.05),
            retain_dispatch=PARAMS.get("fuel_retain_dispatch", False),
        )
        self.fuel_mpc = FuelMPC(
            config, self.network, horizon=PARAMS.get("fuel_mpc_horizon", 0),
            industry_bonus=PARAMS.get("fuel_mpc_industry_bonus", 0.0),
        )
        self.fuel_batch = FuelBatch(config, self.network, lng=PARAMS.get("batch_lng", 0.0),
                                    crude=PARAMS.get("batch_crude", 0.0),
                                    complete_pulse=PARAMS.get("batch_complete_pulse", False),
                                    power_margin_only=PARAMS.get("batch_power_margin_only", False),
                                    end_aware=PARAMS.get("batch_end_aware", False),
                                    sync_crude=PARAMS.get("batch_sync_crude", False),
                                    sync_margin_only=PARAMS.get("batch_sync_margin_only", False),
                                    load_aware=PARAMS.get("batch_load_aware", False),
                                    end_buffer=PARAMS.get("batch_end_buffer", 0))
        self.fuel_lookahead = FuelLookahead(
            config, self.network, self.fuel_batch, horizon=PARAMS.get("fuel_lookahead_horizon", 0),
            industry_weight=PARAMS.get("fuel_lookahead_industry_weight", 1.0),
            stock_value=PARAMS.get("fuel_lookahead_stock_value", 0.25),
            preserve_rationed=PARAMS.get("fuel_lookahead_preserve_rationed", False),
        )
        self.production_residual = ProductionResidual(
            config, self.network, enabled=PARAMS.get("production_residual_enabled", False))

    def act(self, observation):
        self.last_allocation = None
        if self.pipeline is not None:
            allocation = self.pipeline.run(observation, self.network, self.config)
            action = {"flows": allocation.flows}
            for name in ("override_qty", "release_mode"):
                value = getattr(allocation, name)
                if value is not None:
                    action[name] = value
            action = self.validator.validate(action, observation)
            self.last_allocation = allocation
            return action
        return self.validator.validate(self._heuristic_action(observation), observation)

    def _heuristic_action(self, observation):
        flows = self.cap * observation["action_mask"]
        open_now = observation["graph_now.open"]  # 1 open .. 0 closed
        seen = observation["graph_now.open.observed"] == 1
        staged = self.closed_route_dispatch.adjustments(observation)
        for s, chokepoints in enumerate(self.through):
            for c in chokepoints:
                if seen[c]:
                    flows[s] *= max(staged.get((s, c), float(open_now[c])), 0.0) ** self.slot_powers[s]
        flows = self.dispatch_priority.apply(flows, observation)
        flows = self.production_horizon.apply(flows, observation)
        optimized = (self.sales_dispatch.enabled or self.upstream_dispatch.enabled
                     or self.packaging_dispatch.enabled or self.fuel_dispatch.enabled)
        requests = flows.copy() if optimized else flows
        flows = self.route_preferences.apply(flows, observation)
        preferred = flows.copy() if self.production_residual.enabled else flows
        flows = self.stock_rebalancer.apply(flows, observation)
        flows = self.sales_dispatch.apply(flows, requests, observation)
        flows = self.upstream_dispatch.apply(flows, requests, observation)
        flows = self.packaging_dispatch.apply(flows, requests, observation)
        flows = self.fuel_dispatch.apply(flows, requests, observation)
        flows = self.fuel_mpc.apply(flows, observation)
        flows = self.production_residual.apply(flows, requests, observation, preferred=preferred)
        batched = self.fuel_batch.apply(flows, observation)
        action = {"flows": self.fuel_lookahead.apply(batched, flows, observation)}
        return self.fuel_release.apply(action, observation)
