"""SmartRail-Seat-Engine —— 智能铁路座位协同分配引擎（V1.0）。

    >>> from smartrail import SeatEngine, Order, Passenger
    >>> engine = SeatEngine()
    >>> result = engine.book(Order(order_id="O1", passengers=(Passenger("P1"),)))
    >>> result.mode.value in {"free", "smart", "degraded"}
    True
"""

from __future__ import annotations

from .carriage import CarriageSpec, build_formation, crh_16_car_formation, mini_formation
from .clustering import BookingState, SeatBlock, blocks_of, cluster_blocks
from .config import DEFAULT_CONFIG, EngineConfig
from .credit import BLOCK_THRESHOLD, CreditLedger, QuietnessRecord
from .engine import BookResult, SeatEngine
from .free_seat import is_quiet_carriage_visible, validate_selection
from .gov_api import (
    GovernmentApiClient,
    InMemorySupportProvider,
    SupportDataProvider,
    SupportProfile,
    demo_provider,
)
from .models import (
    Assignment,
    BondType,
    Carriage,
    DataSource,
    DeclaredBehavior,
    Order,
    Passenger,
    PassengerUnit,
    RelationType,
    Seat,
    SeatFeature,
    Solution,
    SupportNeed,
    TicketType,
    TrainFormation,
    Violation,
)
from .router import (
    DEFAULT_ROUTER,
    AllocationMode,
    DecisionRouter,
    RoutingSignals,
    Thresholds,
)
from .scoring import Scorer, split_units
from .solver import assign_exact, assign_greedy, build_context, solve

__version__ = "1.0.0"

__all__ = [
    "__version__",
    # 引擎
    "SeatEngine",
    "BookResult",
    "solve",
    "Scorer",
    "split_units",
    "build_context",
    "assign_exact",
    "assign_greedy",
    # 配置与路由
    "EngineConfig",
    "DEFAULT_CONFIG",
    "AllocationMode",
    "DecisionRouter",
    "DEFAULT_ROUTER",
    "RoutingSignals",
    "Thresholds",
    # 数据模型
    "Passenger",
    "PassengerUnit",
    "Order",
    "Seat",
    "SeatFeature",
    "Carriage",
    "TrainFormation",
    "SupportNeed",
    "TicketType",
    "RelationType",
    "BondType",
    "DeclaredBehavior",
    "DataSource",
    "Assignment",
    "Solution",
    "Violation",
    # 座位与聚类
    "SeatBlock",
    "cluster_blocks",
    "blocks_of",
    "BookingState",
    "CarriageSpec",
    "build_formation",
    "crh_16_car_formation",
    "mini_formation",
    # 信用与政务
    "CreditLedger",
    "QuietnessRecord",
    "BLOCK_THRESHOLD",
    "GovernmentApiClient",
    "InMemorySupportProvider",
    "SupportDataProvider",
    "SupportProfile",
    "demo_provider",
    # 自由选座
    "validate_selection",
    "is_quiet_carriage_visible",
]
