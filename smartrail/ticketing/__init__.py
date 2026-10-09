"""12306 风格购票流程的服务层：乘车人档案、车次余票、下单、开发者台账。

与 :mod:`smartrail.api.service` 的分工
--------------------------------------
``service`` 是"求解器怎么算"的通用层；
本模块是"**界面怎么走**"的流程层，严格对齐 12306 的真实交互：

* :mod:`smartrail.passenger_store` —— 乘车人档案（各类人群预制各一位）
* :mod:`smartrail.booking`        —— 车次余票、选座偏好、"余票不足才自动分票"
* :mod:`smartrail.devstore`       —— 开发者模式的余票调整与全局座位视图

三者都是**零依赖纯逻辑**，因此可以脱离 HTTP 单独测试。
"""

from __future__ import annotations

from .booking import (
    SeatPreference,
    TrainAvailability,
    available_trains,
    book_ticket_order,
    build_order_from_profiles,
    seat_row_options,
)
from .devstore import DevStore, get_dev_store, reset_dev_store
from .passenger_store import (
    PassengerProfile,
    PassengerStore,
    build_profile_order,
    get_passenger_store,
    passenger_type_catalog,
    reset_passenger_store,
)

__all__ = [
    "DevStore",
    "PassengerProfile",
    "PassengerStore",
    "SeatPreference",
    "TrainAvailability",
    "available_trains",
    "book_ticket_order",
    "build_order_from_profiles",
    "build_profile_order",
    "get_dev_store",
    "get_passenger_store",
    "passenger_type_catalog",
    "reset_dev_store",
    "reset_passenger_store",
    "seat_row_options",
]
