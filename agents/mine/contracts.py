"""Contracts and data transfer structures for the agent architecture.

Definitions:
- StateSnapshot
- DeliveryNeed
- RiskState
- QueueDelayForecast
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple


@dataclass
class StockItem:
    """Stock on hand at a specific node for a specific commodity."""
    node: int
    node_name: str
    commodity: int
    commodity_name: str
    qty: float
    is_observed: bool
    holding_cost: float = 0.0
    storage_capacity: float = float("inf")


@dataclass
class BacklogItem:
    """Unserved demand carried at a backlog sink."""
    sink_node: int
    sink_name: str
    commodity: int
    commodity_name: str
    qty: float
    is_observed: bool
    shortage_penalty: float = 0.0


@dataclass
class PipelineItem:
    """Shipment in transit along an edge/lane."""
    edge: int
    commodity: int
    lane: Optional[int]
    qty: float
    arrival_week: int
    is_observed: bool
    head_node: int
    dest_node: int
    remaining_edges: List[int] = field(default_factory=list)
    remaining_chokepoints: List[int] = field(default_factory=list)
    estimated_final_arrival_week: int = 0
    is_confirmed: bool = False


@dataclass
class QueueItem:
    """Cargo currently waiting in queue at a chokepoint strait."""
    chokepoint_node: int
    chokepoint_name: str
    commodity: int
    lane: Optional[int]
    next_edge: Optional[int]
    qty: float
    arrival_week: int
    is_observed: bool
    dest_node: Optional[int] = None
    estimated_departure_week: int = 0


@dataclass
class WIPItem:
    """Work in progress at a fab or OSAT."""
    node: int
    node_name: str
    commodity: int
    commodity_name: str
    qty: float
    out_week: int
    is_observed: bool


@dataclass
class ArrivalEntry:
    """Arrival calendar entry per (node, commodity, arrival_week)."""
    node: int
    commodity: int
    week: int
    confirmed_qty: float = 0.0
    estimated_qty: float = 0.0
    sources: List[str] = field(default_factory=list)


@dataclass
class QueueDelayForecast:
    """Forecast of work ahead in queue at a chokepoint upon arrival."""
    chokepoint_node: int
    chokepoint_name: str
    pool: str
    current_queue_qty: float
    inflow_ahead_qty: float
    work_ahead_total: float
    effective_throughput: float
    open_fraction: float
    expected_delay_weeks: float
    confidence: float


@dataclass
class StateSnapshot:
    """Consolidated state snapshot produced each week."""
    week: int
    horizon_T: int
    stock: Dict[Tuple[int, int], StockItem] = field(default_factory=dict)
    backlog: Dict[Tuple[int, int], BacklogItem] = field(default_factory=dict)
    pipeline: List[PipelineItem] = field(default_factory=list)
    queues: List[QueueItem] = field(default_factory=list)
    wip: List[WIPItem] = field(default_factory=list)
    arrival_calendar: Dict[Tuple[int, int, int], ArrivalEntry] = field(default_factory=dict)
    queue_forecasts: Dict[Tuple[int, str], QueueDelayForecast] = field(default_factory=dict)
    
    total_confirmed_stock: float = 0.0
    total_estimated_arrivals: float = 0.0
    total_backlog: float = 0.0
    data_quality_flags: Dict[str, bool] = field(default_factory=dict)


@dataclass
class DeliveryNeed:
    """Delivery request separating backlog, market demand, production inputs, and safety buffer."""
    need_id: str
    destination_node: int
    destination_name: str
    commodity_id: int
    commodity_name: str
    qty: float
    due_week: int
    priority: int
    reason: str
    shortage_cost_est: float
    confidence: float
    created_week: int


@dataclass
class RiskState:
    """Risk signals and computed threat levels."""
    week: int
    chokepoint_risk: Dict[int, float] = field(default_factory=dict)
    edge_risk: Dict[int, float] = field(default_factory=dict)
    region_risk: Dict[int, float] = field(default_factory=dict)
    global_risk: float = 0.0
    active_threat_ids: Set[int] = field(default_factory=set)
    pending_sanctions: List[Dict[str, Any]] = field(default_factory=list)
    closure_forecasts: Dict[int, Optional[int]] = field(default_factory=dict)
