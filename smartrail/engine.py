"""引擎编排层：把决策路由、代价函数求解器、余票状态与降级策略串起来。

这是 FastAPI 服务与仿真器共用的门面（Facade）。对外只有三个动作：
``book`` / ``release`` / ``snapshot``。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field, replace
from typing import Any

from .carriage import crh_16_car_formation
from .clustering import BookingState
from .config import DEFAULT_CONFIG, EngineConfig
from .credit import CreditLedger
from .gov_api import GovernmentApiClient, SupportDataProvider
from .models import Assignment, Order, Passenger, Seat, Solution, TrainFormation
from .notices import assess_adjacency, build_notices
from .router import (
    DEFAULT_ROUTER,
    AllocationMode,
    DecisionRouter,
    RoutingSignals,
    Thresholds,
)
from .scoring import Scorer, accessible_zone_has_free_seat
from .solver import build_context, solve


@dataclass
class BookResult:
    """一次购票决策的完整输出。"""

    order_id: str
    mode: AllocationMode
    routing_reason: str
    solution: Solution
    crew_warnings: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        payload = self.solution.to_dict()
        payload.update(
            {
                "order_id": self.order_id,
                "mode": self.mode.value,
                "routing_reason": self.routing_reason,
                "crew_warnings": self.crew_warnings,
                "explanation": Scorer.explain(self.solution.violations),
            }
        )
        return payload


class SeatEngine:
    """智能铁路座位协同分配引擎。"""

    def __init__(
        self,
        formation: TrainFormation | None = None,
        config: EngineConfig | None = None,
        router: DecisionRouter | None = None,
        credit_ledger: CreditLedger | None = None,
        support_provider: SupportDataProvider | None = None,
        thresholds: Thresholds | None = None,
    ) -> None:
        self.config = config or DEFAULT_CONFIG
        self.formation = formation or crh_16_car_formation(
            aisle_weight=self.config.aisle_crossing_weight,
            near_door_rows=self.config.near_door_rows,
            near_toilet_rows=self.config.near_toilet_rows,
        )
        self.router = router or (DecisionRouter(thresholds) if thresholds else DEFAULT_ROUTER)
        self.state = BookingState(formation=self.formation)
        self.credit = credit_ledger or CreditLedger()
        self.gov = GovernmentApiClient(support_provider) if support_provider else None
        self._lock = threading.RLock()
        self.latency_samples: list[float] = []

    # ------------------------------------------------------------------
    # 对外动作
    # ------------------------------------------------------------------
    def prepare_order(self, order: Order) -> Order:
        """把政务资格与信用分注入乘客图谱（公开接口，模式一/校验器也需要）。"""
        return self._prepare_order(order)

    def validate_free_seats(
        self, order: Order, chosen: dict[str, str]
    ) -> Solution:
        """模式一：校验用户自选座位（隐性拦截）。"""
        from .free_seat import validate_free_selection

        with self._lock:
            prepared = self._prepare_order(order)
            solution = validate_free_selection(prepared, self.state, self.config, chosen)
            seat_ids = [a.seat_id for a in solution.assignments.values()]
            if seat_ids:
                self.state.occupy(seat_ids, order.order_id)
            return solution

    def book(
        self,
        order: Order,
        signals: RoutingSignals | None = None,
        mode: AllocationMode | str | None = None,
        time_budget_ms: float | None = None,
    ) -> BookResult:
        """处理一次购票请求。"""
        with self._lock:
            order = self._prepare_order(order)
            signals = signals or self._signals(order)
            if mode is None:
                resolved, reason = self.router.route(signals)
            else:
                resolved = AllocationMode(mode)
                reason = f"调用方显式指定模式：{resolved.value}。"
            # 无障碍专区是否还有可用座位，必须在**本单发牌之前**采样：
            # 它决定"轮椅拿到普通座位"被记成求解器失误（Tier 0）还是售罄兜底（服务例外）。
            accessible_available = accessible_zone_has_free_seat(
                self.formation.seats, self.state.occupied
            )
            solution = solve(
                order,
                self.state,
                self.config,
                mode=resolved.value,
                time_budget_ms=time_budget_ms,
                accessible_available=accessible_available,
            )
            solution.mode = resolved.value
            # 补齐"面向旅客/乘务员"的待办提示与相邻结论。
            # 放在引擎层而不是各求解器内部，是为了保证 V1/V2/V3 **用同一份实现**
            # 生成提示 —— 否则三个版本的措辞与判据会各自漂移。
            self._attach_notices(order, solution)
            self.latency_samples.append(solution.elapsed_ms)
            seat_ids = [a.seat_id for a in solution.assignments.values()]
            self.state.occupy(seat_ids, order.order_id)
            result = BookResult(
                order_id=order.order_id,
                mode=resolved,
                routing_reason=reason,
                solution=solution,
                crew_warnings=self._crew_warnings(order, solution),
            )
            return result

    def release(self, order_id: str) -> int:
        """释放某订单占用的座位（退票 / 超时未支付）。"""
        with self._lock:
            seat_ids = [sid for sid, oid in self.state.holds.items() if oid == order_id]
            self.state.release(seat_ids)
            return len(seat_ids)

    def snapshot(self) -> dict[str, Any]:
        """余票与编组快照（供可视化座位图使用）。"""
        with self._lock:
            occupied = self.state.occupied
            seats = [
                {
                    "seat_id": s.seat_id,
                    "carriage": s.carriage,
                    "row": s.row,
                    "col": s.col,
                    "class_code": s.class_code,
                    "quiet": s.is_quiet_carriage,
                    "accessible": s.in_accessible_zone(),
                    "aisle": s.is_aisle,
                    "near_door": "near_door" in {f.value for f in s.features},
                    "near_toilet": "near_toilet" in {f.value for f in s.features},
                    "occupied": s.seat_id in occupied,
                }
                for s in self.formation.seats
            ]
            return {
                "train_code": self.formation.train_code,
                "carriages": [
                    {
                        "number": c.number,
                        "class_code": c.class_code,
                        "columns": list(c.columns),
                        "rows": c.rows,
                        "quiet": c.is_quiet_carriage,
                        "accessible": c.has_accessible_zone,
                        "toilet": c.has_toilet,
                    }
                    for c in self.formation.carriages
                ],
                "seats": seats,
                "availability_ratio": round(self.state.availability_ratio, 4),
                "occupied_count": len(occupied),
                "total_seats": len(self.formation.seats),
                "latency": self.latency_stats(),
            }

    def latency_stats(self) -> dict[str, float]:
        samples = sorted(self.latency_samples)
        if not samples:
            return {"count": 0, "p50": 0.0, "p99": 0.0, "max": 0.0}
        return {
            "count": float(len(samples)),
            "p50": round(samples[len(samples) // 2], 3),
            "p99": round(samples[min(len(samples) - 1, int(len(samples) * 0.99))], 3),
            "max": round(samples[-1], 3),
        }

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    def _signals(self, order: Order) -> RoutingSignals:
        ratio = self.state.availability_ratio
        concurrency = 0
        p99 = 0.0
        stats = self.latency_stats()
        p99 = stats["p99"]
        return RoutingSignals(availability_ratio=ratio, concurrency=concurrency, p99_latency_ms=p99)

    def _prepare_order(self, order: Order) -> Order:
        """合并政务数据与信用分，保证引擎看到的是完整乘客图谱。

        两条容易踩空的规则
        ------------------
        1. **信用分不能被"空账本"抬高**。订单里申报的 ``quietness_score``
           （例如 45，表示该旅客已被投诉屏蔽）不能被信用账本的默认 100 覆盖 ——
           早期实现直接 ``quietness_score=ledger.score(pid)``，于是走 HTTP/引擎
           入口的订单**信用约束被静默丢弃**，实测"低信用旅客照样坐进静音车厢"。
           现在取两者中较低的：账本里的处罚有效，申报的处罚同样有效。
        2. **``default_bond`` 必须透传**。它承载"同订单默认同座"这条策略；
           早期重建 Order 时漏了它，策略在引擎层被抹掉，同一订单的人又变回散客。
        """
        passengers: list[Passenger] = []
        for passenger in order.passengers:
            needs = passenger.support_needs
            if self.gov is not None:
                profile = self.gov.lookup(passenger.passenger_id)
                if profile is not None:
                    needs = needs | profile.support_needs
            ledger_score = self.credit.score(passenger.passenger_id)
            score = min(float(passenger.quietness_score), float(ledger_score))
            passengers.append(
                replace(passenger, support_needs=needs, quietness_score=score)
            )
        return Order(
            order_id=order.order_id,
            passengers=tuple(passengers),
            relation=order.relation,
            units=order.units,
            bonds=order.bonds,
            default_bond=order.default_bond,
        )

    def _attach_notices(self, order: Order, solution: Solution) -> None:
        """为一个已完成的解补齐相邻结论与待办提示（幂等）。

        注意这里传入 ``all_seats`` 与 ``occupied_seats``：只有掌握了"整节车厢
        还有哪些空位"，才能把提示区分为"车内可现场调剂"或"已无相邻空位，
        请找列车员"。缺这两个信息时提示仍在，但措辞会保守一些。
        """
        # 关键：把**本单自己刚发的座位**从"已占"里排除，否则会把自家座位当成
        # 已被别人占用，从而误判"车厢里没有相邻空位"。
        own = {a.seat_id for a in solution.assignments.values()}
        ctx = build_context(
            order,
            self.config,
            all_seats=self.formation.seats,
            occupied_seats=set(self.state.occupied) - own,
        )
        placed = {
            pid: self.formation.seat(assignment.seat_id)
            for pid, assignment in solution.assignments.items()
        }
        adjacency = assess_adjacency(placed, ctx, self.config)
        solution.adjacency = adjacency
        solution.notices = build_notices(
            placed, list(solution.waitlisted), ctx, self.config, adjacency
        )
        if adjacency.needs_crew:
            solution.notes.append(
                "相邻就座未完全满足，但**已按出票优先原则全部出票**："
                + "；".join(adjacency.messages)
            )
        for notice in solution.notices:
            if notice.kind == "accessible_zone_full":
                solution.notes.append(
                    f"轮椅乘客 {notice.passenger_id} 无可用无障碍座位，"
                    "已按**出票优先原则出票**普通座位并生成站车协助提示"
                    "（如需改为保守策略，设 waitlist_wheelchair_without_accessible=True）。"
                )
                break

    def _crew_warnings(self, order: Order, solution: Solution) -> list[dict[str, Any]]:
        """闭环反馈：给乘务员终端的重点关注名单（活泼型儿童 / 冲突隔离）。"""
        seat_index = self.formation.by_id()
        warnings: list[dict[str, Any]] = []
        for pid, assignment in solution.assignments.items():
            passenger = next((p for p in order.passengers if p.passenger_id == pid), None)
            if passenger is None:
                continue
            seat = seat_index.get(assignment.seat_id)
            if seat is None:
                continue
            reasons: list[str] = []
            if passenger.is_child and passenger.declared_behavior.value == "lively":
                reasons.append("活泼型儿童：建议关注并现场调剂")
            if seat.is_quiet_carriage and passenger.quiet_repulsion >= 0.5:
                reasons.append("特殊群体进入静音车厢：降级放行，需现场沟通")
            if passenger.is_mobility_impaired:
                reasons.append("轮椅乘客：确认无障碍踏板与专区衔接")
            if "independent_blind" in {n.value for n in passenger.support_needs}:
                reasons.append("独立视障旅客：静默触发站车引导服务")
            if reasons:
                warnings.append(
                    {
                        "passenger_id": pid,
                        "seat_id": assignment.seat_id,
                        "carriage": assignment.carriage,
                        "reasons": reasons,
                    }
                )
        return warnings

    def crew_warnings(self, order: Order, solution: Solution) -> list[dict[str, Any]]:
        """公开的闭环反馈接口（供 API 层复用）。"""
        return self._crew_warnings(order, solution)


__all__ = ["SeatEngine", "BookResult", "Passenger", "Order", "Seat", "Assignment"]
