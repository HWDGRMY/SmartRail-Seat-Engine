"""并发订单模拟：把多张订单"同时"压给引擎，并给出逐单与整体结果。

与仿真器的区别
--------------
:mod:`smartrail.v3.simulator` 是**长时间订单流**（几百到几千单，用来看时延分位与
降级比例）；本模块是**短时间并发峰值**（十几到几十单一次性涌入），用来回答验收时
最关心的问题：

1. 峰值下每个订单**是否都出票**（出票优先原则在压力下是否还成立）；
2. **重点旅客**是否被正确安置（轮椅坐无障碍、儿童挨着家长、孕妇有同伴）；
3. 引擎会不会因为"想把人凑一起"而在拥挤时反过来**拒票**。

设计上刻意不做任何"排队"或"限流"：订单按提交顺序依次求解，先到先得 ——
这正是真实售票的语义。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from ..config import EngineConfig
from ..credit import CreditLedger
from ..engine import SeatEngine
from ..models import Order, Solution, SupportNeed
from .presets import PRESETS, PRESET_BY_KEY, PresetType, build_order, coverage_report

MAX_ORDERS = 60
"""单次并发模拟的订单上限：再高会让页面等待过久，且车厢早就卖空了。"""


@dataclass
class OrderOutcome:
    """一张订单的处理结果。"""

    order_id: str
    preset_key: str
    preset_label: str
    requested: int
    seated: int
    waitlisted: list[str]
    seats: dict[str, str]
    carriages: list[int]
    adjacency_level: str
    total_cost: float
    tier0: int
    elapsed_ms: float
    notices: list[dict[str, Any]] = field(default_factory=list)
    unmet_needs: list[str] = field(default_factory=list)
    satisfied_needs: list[str] = field(default_factory=list)
    same_carriage: bool = True
    """同订单的人是否都落在同一车厢（"同订单默认同座"的检验指标）。"""
    passengers: list[dict[str, Any]] = field(default_factory=list)
    """本单每位乘客的类型标签与支持需求（供前端展示"这张单里都是谁"）。"""

    def to_dict(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id,
            "preset": self.preset_key,
            "preset_label": self.preset_label,
            "requested": self.requested,
            "seated": self.seated,
            "waitlisted": list(self.waitlisted),
            "seats": dict(self.seats),
            "carriages": list(self.carriages),
            "adjacency_level": self.adjacency_level,
            "total_cost": round(self.total_cost, 1),
            "tier0": self.tier0,
            "elapsed_ms": round(self.elapsed_ms, 3),
            "notices": list(self.notices),
            "unmet_needs": list(self.unmet_needs),
            "satisfied_needs": list(self.satisfied_needs),
            "same_carriage": self.same_carriage,
            "passengers": list(self.passengers),
        }


def _assess_needs(
    order: Order,
    solution: Solution,
    seat_lookup,
    accessible_zone_free_before: bool,
) -> tuple[list[str], list[str]]:
    """逐位检查"重点需求"是否被满足，返回 (已满足, 未满足) 的中文描述。

    判定口径与线上一致：
    * 轮椅 -> 必须坐在无障碍专区；但**若下单时专区已售罄**，则属于运营例外
      （出票优先 + 站车协助），记在 ``satisfied`` 里说明，不计为未满足；
    * 静音信用分过低 -> 不得被排进静音车厢；
    * 未出票的重点旅客单独列出。

    ``accessible_zone_free_before`` 是**本单开始前**专区是否还有空位，
    必须由调用方采样后传入 —— 用事后状态判断会把"本来就没位置"误判成"没给安排"。
    """
    satisfied: list[str] = []
    unmet: list[str] = []
    assignments = solution.assignments
    for passenger in order.passengers:
        pid = passenger.passenger_id
        assignment = assignments.get(pid)
        if assignment is None:
            if passenger.support_needs or passenger.needs_caregiver:
                unmet.append(f"{pid}（{passenger.name}）未出票")
            continue
        seat = seat_lookup.get(assignment.seat_id)
        if seat is None:
            continue
        if passenger.is_mobility_impaired:
            if seat.in_accessible_zone():
                satisfied.append(f"{pid} 轮椅坐无障碍专区（{seat.seat_id}）")
            elif accessible_zone_free_before:
                unmet.append(f"{pid} 轮椅未坐无障碍专区（{seat.seat_id}）")
            else:
                satisfied.append(
                    f"{pid} 专区售罄，已出票普通座位并转站车协助（{seat.seat_id}）"
                )
        if passenger.quiet_carriage_blocked:
            if seat.is_quiet_carriage:
                unmet.append(f"{pid} 低信用旅客被排进静音车厢")
            else:
                satisfied.append(f"{pid} 低信用旅客成功避开静音车厢")
    return satisfied, unmet


def _same_carriage(seated_seats: dict[str, str], seat_lookup) -> tuple[bool, list[int]]:
    carriages = sorted({seat_lookup[sid].carriage for sid in seated_seats.values() if sid in seat_lookup})
    return len(carriages) <= 1, carriages


def run_concurrent(
    preset_keys: Sequence[str] | None = None,
    repeats: int = 1,
    fill: float = 0.0,
    seed: int = 7,
    solver: str = "v1_heuristic",
    formation: str = "crh16",
    config: EngineConfig | None = None,
    archetype_counts: dict[str, int] | None = None,
    group_friends: bool = False,
    auto_caregiver: bool = True,
) -> dict[str, Any]:
    """把多张订单并发压给引擎，返回逐单结果 + 整体统计 + 人群覆盖报告。

    两种下单方式（**推荐第一种**）：

    * ``archetype_counts={"adult": 5, "wheelchair": 2, "infant": 1}`` ——
      **模块化**：按"人"自由组合，系统自动把有照护关系的人组织成订单
      （轮椅旅客会自动配一位陪护，婴儿会配看护人），零散旅客各自成单
      （零散旅客默认不拼陌生人，可用 ``group_friends=True`` 显式拼团）；
    * ``preset_keys=[...]`` —— 旧的"固定套餐"方式，保留兼容。

    ``archetype_counts`` 优先于 ``preset_keys``。
    """
    config = config or EngineConfig()

    if archetype_counts:
        from .archetypes import build_orders_from_archetypes

        built = build_orders_from_archetypes(
            archetype_counts,
            group_friends=group_friends,
            auto_caregiver=auto_caregiver,
        )
        plan_orders: list[tuple[Order, str, str]] = [
            (item.order, item.origin, ",".join(item.archetype_keys)) for item in built
        ]
        plan_orders = plan_orders[:MAX_ORDERS]
    else:
        keys = list(preset_keys) if preset_keys else [preset.key for preset in PRESETS]
        unknown = [key for key in keys if key not in PRESET_BY_KEY]
        if unknown:
            raise KeyError(f"未知人群类型：{', '.join(unknown)}")
        pairs: list[tuple[str, int]] = []
        for repeat in range(max(1, repeats)):
            for key in keys:
                pairs.append((key, repeat))
        plan_orders = []
        for index, (key, repeat) in enumerate(pairs[:MAX_ORDERS]):
            preset = PRESET_BY_KEY[key]
            plan_orders.append(
                (
                    build_order(preset, f"SIM-{key}-{repeat}-{index}", index=index),
                    preset.key,
                    preset.label,
                )
            )

    ledger = CreditLedger()
    from ..api import service

    engine: SeatEngine = service.reset_engine(
        service.create_engine(formation, ledger), formation=formation, fill=fill, seed=seed
    )
    seat_lookup = engine.formation.by_id()

    orders: list[Order] = []
    outcomes: list[OrderOutcome] = []
    started = time.perf_counter()

    for index, (order, origin, label) in enumerate(plan_orders):
        orders.append(order)
        # 必须在**下单前**采样专区余量：事后判断会把"本来就没位置"误判成"没给安排"
        from ..scoring import accessible_zone_has_free_seat

        zone_free_before = accessible_zone_has_free_seat(
            engine.formation.seats, engine.state.occupied
        )
        result = engine.book(order, mode="smart")
        solution: Solution = result.solution

        seats = {a.passenger_id: a.seat_id for pid, a in solution.assignments.items()}
        same, carriages = _same_carriage(seats, seat_lookup)
        satisfied, unmet = _assess_needs(order, solution, seat_lookup, zone_free_before)

        # 补一条"同订单是否同车厢"的判定（这是"同订单默认同座"的直接检验）
        if len(order.passengers) > 1 and not same:
            unmet.append(
                f"同订单 {len(order.passengers)} 人被分到 {len(carriages)} 节车厢"
            )

        outcomes.append(
            OrderOutcome(
                order_id=order.order_id,
                preset_key=origin,
                preset_label=label,
                requested=len(order.passengers),
                seated=len(seats),
                waitlisted=list(solution.waitlisted),
                seats=seats,
                carriages=carriages,
                adjacency_level=solution.adjacency.level,
                total_cost=solution.total_cost,
                tier0=len(solution.hard_violations),
                elapsed_ms=solution.elapsed_ms,
                notices=[notice.to_dict() for notice in solution.notices],
                unmet_needs=unmet,
                satisfied_needs=satisfied,
                same_carriage=same,
                passengers=[
                    {
                        "passenger_id": p.passenger_id,
                        "label": p.name,
                        "support_needs": sorted(need.value for need in p.support_needs),
                        "needs_caregiver": bool(p.needs_caregiver or p.is_child),
                    }
                    for p in order.passengers
                ],
            )
        )

    wall_ms = (time.perf_counter() - started) * 1000.0
    latencies = sorted(outcome.elapsed_ms for outcome in outcomes)

    def percentile(values: list[float], ratio: float) -> float:
        if not values:
            return 0.0
        index = min(len(values) - 1, int(round(ratio * (len(values) - 1))))
        return values[index]

    total_requested = sum(outcome.requested for outcome in outcomes)
    total_seated = sum(outcome.seated for outcome in outcomes)
    coverage = coverage_report(orders)
    snapshot = engine.snapshot()

    # 每张订单一个颜色索引，供座位图区分不同订单
    payloads = []
    for index, outcome in enumerate(outcomes):
        item = outcome.to_dict()
        item["color_index"] = index % 12
        payloads.append(item)

    return {
        "solver": solver,
        "fill": fill,
        "seed": seed,
        "formation": formation,
        "orders": payloads,
        "summary": {
            "orders": len(outcomes),
            "requested_passengers": total_requested,
            "seated_passengers": total_seated,
            "waitlisted_passengers": total_requested - total_seated,
            "seat_rate": round(total_seated / max(1, total_requested), 4),
            "tier0_violations": sum(outcome.tier0 for outcome in outcomes),
            "orders_fully_seated": sum(
                1 for outcome in outcomes if outcome.seated == outcome.requested
            ),
            "orders_same_carriage": sum(
                1
                for outcome in outcomes
                # 只统计"多乘客订单"里同车厢的：单人订单天然"同车厢"，
                # 算进来会出现 18/15 这种自相矛盾的比值（真实踩过）。
                if outcome.requested > 1 and outcome.same_carriage
            ),
            "multi_passenger_orders": sum(
                1 for outcome in outcomes if outcome.requested > 1
            ),
            "orders_with_notices": sum(1 for outcome in outcomes if outcome.notices),
            "unmet_need_count": sum(len(outcome.unmet_needs) for outcome in outcomes),
            "wall_ms": round(wall_ms, 2),
            "latency_ms": {
                "p50": round(percentile(latencies, 0.5), 3),
                "p95": round(percentile(latencies, 0.95), 3),
                "max": round(latencies[-1] if latencies else 0.0, 3),
            },
        },
        "coverage": coverage,
        "train": {
            "train_code": snapshot["train_code"],
            "total_seats": snapshot["total_seats"],
            "occupied_count": snapshot["occupied_count"],
            "availability_ratio": snapshot["availability_ratio"],
            "carriages": snapshot["carriages"],
            "seats": snapshot["seats"],
        },
    }


def preset_catalog() -> list[dict[str, Any]]:
    """人群类型清单（供前端渲染"添加乘客"面板）。

    同时返回两套：

    * ``archetypes`` —— **模块化的人**（成人 / 孕妇 / 盲人 / 婴儿……），推荐使用；
    * ``presets`` —— 旧的固定套餐，保留兼容。
    """
    from .archetypes import archetype_catalog

    return archetype_catalog()


__all__ = [
    "MAX_ORDERS",
    "OrderOutcome",
    "preset_catalog",
    "run_concurrent",
]
