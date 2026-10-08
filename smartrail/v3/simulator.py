"""V3.0：仿真器（Simulator）—— 让策略在可控的订单流中自我博弈。

README 第 7 节 V3.0 要求"构建 Simulator，让 AI 在百万级模拟订单中自我博弈"。
本模块提供这个仿真环境，且**不依赖任何第三方库**（PyTorch 只用在策略网络侧），
因此可以单独跑、单独测。

仿真器负责三件事
----------------
1. **生成订单流**：按真实客流分布（单人 / 伴侣 / 带娃家庭 / 多代家庭 / 团体 /
   特殊群体）采样订单，可配置到达强度与高峰曲线；
2. **维护列车状态**：占座、余票率、静音车厢占用、无障碍专区占用、信用分账本；
3. **执行策略并记账**：每个订单交给策略做决策，记录亲和度、Tier 0 违规、
   候补人数、静音车厢误用、时延等指标，最后汇总成可比对的报告。

两种策略接口
------------
* :class:`RulePolicy` —— 复用 V1/V2 引擎的规则策略（作为基线）；
* :class:`NeuralPolicyAdapter` —— V3.0 学习型策略（见 :mod:`.policy`），
  只依赖"给候选座位打分"这一件事。

反馈闭环
--------
仿真器支持把乘务员投诉回灌到信用账本（``complaint_rate``），于是"动态信用体系"
本身也进入了自我博弈：策略若把活泼儿童塞进静音车厢，后续这些乘客的信用分下降、
静音车厢权限被屏蔽，长期回报会变差 —— 这正是 V3.0 希望学到的权衡。
"""

from __future__ import annotations

import random
import statistics
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Protocol, Sequence

from ..carriage import crh_16_car_formation
from ..clustering import BookingState
from ..config import DEFAULT_CONFIG, EngineConfig
from ..credit import CreditLedger
from ..engine import SeatEngine
from ..models import (
    BondType,
    DeclaredBehavior,
    Order,
    Passenger,
    RelationType,
    Seat,
    Solution,
    SupportNeed,
    TicketType,
)
from ..router import AllocationMode, RoutingSignals

# ---------------------------------------------------------------------------
# 订单流生成
# ---------------------------------------------------------------------------


@dataclass
class TrafficProfile:
    """客流画像（权重之和不必为 1，内部会归一化后采样）。"""

    solo: float = 0.22
    couple: float = 0.26
    family_with_child: float = 0.30
    multi_gen: float = 0.06
    group: float = 0.08
    wheelchair: float = 0.03
    blind: float = 0.02
    pregnant: float = 0.02
    elderly_care: float = 0.01

    def weights(self) -> list[tuple[str, float]]:
        return [
            ("solo", self.solo),
            ("couple", self.couple),
            ("family_with_child", self.family_with_child),
            ("multi_gen", self.multi_gen),
            ("group", self.group),
            ("wheelchair", self.wheelchair),
            ("blind", self.blind),
            ("pregnant", self.pregnant),
            ("elderly_care", self.elderly_care),
        ]


class OrderStream:
    """可复现的随机订单流（给定 seed 必然产出同一序列）。"""

    def __init__(self, seed: int = 7, profile: TrafficProfile | None = None) -> None:
        self.rng = random.Random(seed)
        self.profile = profile or TrafficProfile()
        self.counter = 0

    def next_order(self) -> Order:
        self.counter += 1
        kinds, weights = zip(*self.profile.weights())
        kind = self.rng.choices(kinds, weights=weights, k=1)[0]
        index = self.counter
        builder = getattr(self, f"_build_{kind}")
        return builder(index)

    # -- 各类订单 ---------------------------------------------------------
    def _build_solo(self, index: int) -> Order:
        age = self.rng.randint(18, 68)
        return Order(
            order_id=f"SIM{index:05d}-SOLO",
            passengers=(
                Passenger(
                    f"S{index}",
                    ticket_type=TicketType.ADULT,
                    age=age,
                    preference_aisle=self.rng.random() < 0.4,
                    preference_window=self.rng.random() < 0.25,
                ),
            ),
            relation=RelationType.SOLO,
            bonds={},
        )

    def _build_couple(self, index: int) -> Order:
        a = Passenger(f"C{index}a", ticket_type=TicketType.ADULT, age=self.rng.randint(22, 70))
        b = Passenger(f"C{index}b", ticket_type=TicketType.ADULT, age=self.rng.randint(22, 70))
        return Order(
            order_id=f"SIM{index:05d}-CPL",
            passengers=(a, b),
            relation=RelationType.COUPLE,
            bonds={frozenset((a.passenger_id, b.passenger_id)): BondType.STRONG},
        )

    def _build_family_with_child(self, index: int) -> Order:
        child_age = self.rng.choice([1, 2, 4, 6, 9, 12])
        behavior = self.rng.choice(list(DeclaredBehavior))
        a = Passenger(f"F{index}a", ticket_type=TicketType.ADULT, age=self.rng.randint(26, 45), is_caregiver=True)
        b = Passenger(f"F{index}b", ticket_type=TicketType.ADULT, age=self.rng.randint(26, 45))
        child = Passenger(
            f"F{index}c",
            ticket_type=TicketType.CHILD,
            age=child_age,
            declared_behavior=behavior,
            needs_caregiver=True,
            support_needs=frozenset(
                {SupportNeed.INFANT} if child_age <= 3 else {SupportNeed.TODDLER}
            ),
        )
        return Order(
            order_id=f"SIM{index:05d}-FAM",
            passengers=(a, b, child),
            relation=RelationType.NUCLEAR_FAMILY,
            bonds={
                frozenset((a.passenger_id, child.passenger_id)): BondType.MANDATORY,
                frozenset((a.passenger_id, b.passenger_id)): BondType.STRONG,
                frozenset((b.passenger_id, child.passenger_id)): BondType.STRONG,
            },
        )

    def _build_multi_gen(self, index: int) -> Order:
        g1 = Passenger(f"M{index}g1", ticket_type=TicketType.ADULT, age=self.rng.randint(68, 85), support_needs=frozenset({SupportNeed.ELDERLY}))
        g2 = Passenger(f"M{index}g2", ticket_type=TicketType.ADULT, age=self.rng.randint(66, 82), support_needs=frozenset({SupportNeed.ELDERLY}))
        a1 = Passenger(f"M{index}a1", ticket_type=TicketType.ADULT, age=self.rng.randint(28, 50), is_caregiver=True)
        a2 = Passenger(f"M{index}a2", ticket_type=TicketType.ADULT, age=self.rng.randint(28, 50))
        baby = Passenger(
            f"M{index}b",
            ticket_type=TicketType.CHILD,
            age=self.rng.choice([1, 3]),
            needs_caregiver=True,
            support_needs=frozenset({SupportNeed.INFANT}),
        )
        bonds = {
            frozenset((a1.passenger_id, baby.passenger_id)): BondType.MANDATORY,
            frozenset((a2.passenger_id, baby.passenger_id)): BondType.MANDATORY,
            frozenset((a1.passenger_id, a2.passenger_id)): BondType.STRONG,
            frozenset((g1.passenger_id, g2.passenger_id)): BondType.STRONG,
            frozenset((a1.passenger_id, g1.passenger_id)): BondType.STRONG,
        }
        return Order(
            order_id=f"SIM{index:05d}-MULTI",
            passengers=(g1, g2, a1, a2, baby),
            relation=RelationType.MULTI_GEN_FAMILY,
            bonds=bonds,
        )

    def _build_group(self, index: int) -> Order:
        size = self.rng.randint(3, 10)
        people = tuple(
            Passenger(f"G{index}-{i}", ticket_type=TicketType.ADULT, age=self.rng.randint(18, 70))
            for i in range(size)
        )
        bonds = {
            frozenset((people[i].passenger_id, people[i + 1].passenger_id)): BondType.STRONG
            for i in range(size - 1)
        }
        return Order(
            order_id=f"SIM{index:05d}-GRP",
            passengers=people,
            relation=RelationType.GROUP,
            bonds=bonds,
        )

    def _build_wheelchair(self, index: int) -> Order:
        w = Passenger(
            f"W{index}",
            ticket_type=TicketType.ADULT,
            age=self.rng.randint(20, 75),
            support_needs=frozenset({SupportNeed.WHEELCHAIR}),
        )
        if self.rng.random() < 0.5:
            return Order(order_id=f"SIM{index:05d}-WHL", passengers=(w,), relation=RelationType.SOLO, bonds={})
        helper = Passenger(f"W{index}h", ticket_type=TicketType.ADULT, age=self.rng.randint(25, 70), is_caregiver=True)
        return Order(
            order_id=f"SIM{index:05d}-WHL",
            passengers=(w, helper),
            relation=RelationType.CARE,
            bonds={frozenset((w.passenger_id, helper.passenger_id)): BondType.STRONG},
        )

    def _build_blind(self, index: int) -> Order:
        return Order(
            order_id=f"SIM{index:05d}-BLD",
            passengers=(
                Passenger(
                    f"B{index}",
                    ticket_type=TicketType.ADULT,
                    age=self.rng.randint(25, 70),
                    support_needs=frozenset({SupportNeed.INDEPENDENT_BLIND}),
                    preference_aisle=True,
                ),
            ),
            relation=RelationType.SOLO,
            bonds={},
        )

    def _build_pregnant(self, index: int) -> Order:
        p = Passenger(
            f"P{index}",
            ticket_type=TicketType.ADULT,
            age=self.rng.randint(22, 40),
            support_needs=frozenset({SupportNeed.PREGNANT_LATE}),
            preference_aisle=True,
            needs_caregiver=True,
        )
        h = Passenger(f"P{index}h", ticket_type=TicketType.ADULT, age=self.rng.randint(24, 45), is_caregiver=True)
        return Order(
            order_id=f"SIM{index:05d}-PRG",
            passengers=(p, h),
            relation=RelationType.CARE,
            bonds={frozenset((p.passenger_id, h.passenger_id)): BondType.MANDATORY},
        )

    def _build_elderly_care(self, index: int) -> Order:
        senior = Passenger(
            f"E{index}",
            ticket_type=TicketType.ADULT,
            age=self.rng.randint(70, 88),
            support_needs=frozenset({SupportNeed.ELDERLY}),
        )
        helper = Passenger(f"E{index}h", ticket_type=TicketType.ADULT, age=self.rng.randint(30, 60), is_caregiver=True)
        return Order(
            order_id=f"SIM{index:05d}-ELD",
            passengers=(senior, helper),
            relation=RelationType.CARE,
            bonds={frozenset((senior.passenger_id, helper.passenger_id)): BondType.STRONG},
        )


# ---------------------------------------------------------------------------
# 策略接口
# ---------------------------------------------------------------------------


@dataclass
class DecisionContext:
    """交给策略的最小决策上下文（避免策略直接依赖引擎内部结构）。"""

    order: Order
    state: BookingState
    config: EngineConfig
    credit: CreditLedger
    step: int


class Policy(Protocol):
    """策略协议：给定上下文，返回一个分配方案。"""

    name: str

    def decide(self, context: DecisionContext) -> Solution: ...


class RulePolicy:
    """规则策略：直接调用 V1（启发式）或 V2（CP-SAT）后端，作为学习型策略的基线。"""

    def __init__(self, backend: str = "v1_heuristic", mode: str | None = None) -> None:
        self.backend = backend
        self.mode = mode
        self.name = f"{backend}" if not mode else f"{backend}:{mode}"

    def decide(self, context: DecisionContext) -> Solution:
        from ..router import DecisionRouter
        from ..v2 import solve_with

        mode = self.mode or DecisionRouter().route(
            RoutingSignals(availability_ratio=context.state.availability_ratio)
        )[0].value
        return solve_with(
            self.backend,
            context.order,
            context.state,
            context.config,
            mode=mode,
        )


# ---------------------------------------------------------------------------
# 仿真器
# ---------------------------------------------------------------------------


@dataclass
class SimMetrics:
    """一次仿真的汇总指标（可直接进报告与前端图表）。"""

    steps: int = 0
    passengers: int = 0
    seated: int = 0
    waitlisted: int = 0
    total_affinity: float = 0.0
    tier0_violations: int = 0
    tier1_violations: int = 0
    isolated_care: int = 0
    quiet_misuse: int = 0
    mode_counts: dict[str, int] = field(default_factory=dict)
    latencies_ms: list[float] = field(default_factory=list)
    affinity_curve: list[float] = field(default_factory=list)
    complaints: int = 0

    @property
    def seat_rate(self) -> float:
        return self.seated / self.passengers if self.passengers else 1.0

    def summary(self) -> dict[str, Any]:
        values = sorted(self.latencies_ms)

        def pct(p: float) -> float:
            if not values:
                return 0.0
            return values[min(len(values) - 1, int(len(values) * p))]

        return {
            "steps": self.steps,
            "passengers": self.passengers,
            "seated": self.seated,
            "waitlisted": self.waitlisted,
            "seat_rate": round(self.seat_rate, 4),
            "total_affinity": round(self.total_affinity, 1),
            "affinity_per_passenger": round(
                self.total_affinity / self.seated, 3
            )
            if self.seated
            else 0.0,
            "tier0_violations": self.tier0_violations,
            "tier1_violations": self.tier1_violations,
            "isolated_care": self.isolated_care,
            "quiet_misuse": self.quiet_misuse,
            "complaints": self.complaints,
            "mode_counts": dict(self.mode_counts),
            "latency_ms": {
                "p50": round(statistics.median(values), 3) if values else 0.0,
                "p95": round(pct(0.95), 3),
                "p99": round(pct(0.99), 3),
                "max": round(max(values), 3) if values else 0.0,
            },
        }


class Simulator:
    """列车售座仿真器：按订单流推进，逐单调用策略并记账。"""

    def __init__(
        self,
        policy: Policy,
        seed: int = 7,
        config: EngineConfig | None = None,
        profile: TrafficProfile | None = None,
        fill: float = 0.0,
        complaint_rate: float = 0.0,
        train_code: str = "G1234",
    ) -> None:
        self.config = config or DEFAULT_CONFIG
        self.formation = crh_16_car_formation(
            train_code=train_code,
            aisle_weight=self.config.aisle_crossing_weight,
            near_door_rows=self.config.near_door_rows,
            near_toilet_rows=self.config.near_toilet_rows,
        )
        self.state = BookingState(formation=self.formation)
        self.credit = CreditLedger()
        self.policy = policy
        self.stream = OrderStream(seed=seed, profile=profile)
        self.metrics = SimMetrics()
        self.rng = random.Random(seed ^ 0x5EED)
        self.complaint_rate = complaint_rate
        self.engine = SeatEngine(
            formation=self.formation, config=self.config, credit_ledger=self.credit
        )
        if fill > 0:
            self.prefill(fill)

    # -- 环境操作 ---------------------------------------------------------
    def prefill(self, ratio: float) -> None:
        """预先占座，模拟"列车已经有其他旅客"。"""
        seats = [seat.seat_id for seat in self.formation.seats]
        self.rng.shuffle(seats)
        self.state.mark_occupied(seats[: int(len(seats) * ratio)])

    @property
    def availability(self) -> float:
        return self.state.availability_ratio

    def step(self, mode: AllocationMode | None = None) -> Solution:
        """推进一个订单。"""
        order = self.stream.next_order()
        prepared = self.engine.prepare_order(order)
        context = DecisionContext(
            order=prepared,
            state=self.state,
            config=self.config,
            credit=self.credit,
            step=self.metrics.steps,
        )
        solution = self.policy.decide(context)
        self._record(prepared, solution)
        seat_ids = [a.seat_id for a in solution.assignments.values()]
        if seat_ids:
            self.state.occupy(seat_ids, prepared.order_id)
        _ = mode
        return solution

    def run(self, steps: int = 200, stop_below_availability: float = 0.0) -> SimMetrics:
        for _ in range(steps):
            self.step()
            if self.availability <= stop_below_availability:
                break
        return self.metrics

    # -- 记账 -------------------------------------------------------------
    def _record(self, order: Order, solution: Solution) -> None:
        m = self.metrics
        m.steps += 1
        m.passengers += len(order.passengers)
        m.seated += len(solution.assignments)
        m.waitlisted += len(solution.waitlisted)
        m.total_affinity += solution.total_affinity
        m.affinity_curve.append(m.total_affinity)
        m.latencies_ms.append(solution.elapsed_ms)
        m.tier0_violations += sum(1 for v in solution.violations if v.tier == 0)
        m.tier1_violations += sum(1 for v in solution.violations if v.tier == 1)
        m.isolated_care += sum(
            1 for v in solution.violations if v.code == "T1_ISOLATED_CARE_MEMBER"
        )
        m.mode_counts[solution.mode or "auto"] = m.mode_counts.get(solution.mode or "auto", 0) + 1

        # 静音车厢误用 + 乘务员投诉回灌（动态信用体系的闭环）
        for assignment in solution.assignments.values():
            passenger = next(
                (p for p in order.passengers if p.passenger_id == assignment.passenger_id), None
            )
            if passenger is None or not assignment.quiet_carriage:
                continue
            if passenger.quiet_repulsion >= 0.5:
                m.quiet_misuse += 1
                if self.rng.random() < self.complaint_rate:
                    self.credit.report_complaint(
                        passenger.passenger_id, "仿真：静音车厢被投诉"
                    )
                    m.complaints += 1


# ---------------------------------------------------------------------------
# 便捷入口
# ---------------------------------------------------------------------------


def run_policy(
    policy: Policy,
    steps: int = 200,
    seed: int = 7,
    fill: float = 0.0,
    profile: TrafficProfile | None = None,
    config: EngineConfig | None = None,
    complaint_rate: float = 0.0,
) -> dict[str, Any]:
    """跑一次仿真并返回可直接序列化的汇总（供 CLI / API / 验收页使用）。"""
    simulator = Simulator(
        policy,
        seed=seed,
        config=config,
        profile=profile,
        fill=fill,
        complaint_rate=complaint_rate,
    )
    simulator.run(steps=steps)
    summary = simulator.metrics.summary()
    summary["policy"] = policy.name
    summary["affinity_curve"] = [
        round(v, 1) for v in _downsample(simulator.metrics.affinity_curve, 60)
    ]
    return summary


def _downsample(values: Sequence[float], target: int) -> list[float]:
    if len(values) <= target:
        return list(values)
    step = len(values) / target
    return [values[min(len(values) - 1, int(i * step))] for i in range(target)]


def compare_policies(
    policies: Iterable[Policy],
    steps: int = 120,
    seed: int = 7,
    fill: float = 0.0,
    complaint_rate: float = 0.0,
) -> list[dict[str, Any]]:
    """并排跑多个策略（同一订单流），返回可比对的汇总列表。"""
    return [
        run_policy(policy, steps=steps, seed=seed, fill=fill, complaint_rate=complaint_rate)
        for policy in policies
    ]


__all__ = [
    "DecisionContext",
    "OrderStream",
    "Policy",
    "RulePolicy",
    "SimMetrics",
    "Simulator",
    "TrafficProfile",
    "compare_policies",
    "run_policy",
]
