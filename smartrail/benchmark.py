"""仿真基准：随机订单流下的时延分布与质量指标（README 6.1）。

用法：
    python -m smartrail.benchmark --orders 400 --seed 7
    python -m smartrail.benchmark --orders 200 --fill 0.85      # 高负载（运力紧张）
    python -m smartrail.benchmark --orders 200 --json out.json  # 机器可读输出

指标
----
* P50 / P95 / P99 / max 决策时延（毫秒）
* 模式分布（free / smart / degraded）
* Tier 0 违规次数（必须为 0 —— 这是本项目的底线指标）
* 是否触发"儿童与照护人跨车厢分离"的反例计数
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import time
from dataclasses import dataclass, field
from typing import Any

from . import SeatEngine, crh_16_car_formation
from .config import EngineConfig
from .credit import CreditLedger
from .fixtures import (
    blind_order,
    couple_order,
    elderly,
    family_with_child_order,
    multi_gen_order,
    pregnant_order,
    solo_order,
    wheelchair_order,
)
from .models import BondType, DeclaredBehavior, Order, Passenger, RelationType, TicketType
from .router import DecisionRouter, RoutingSignals


@dataclass
class BenchStats:
    orders: int = 0
    seated: int = 0
    waitlisted: int = 0
    modes: dict[str, int] = field(default_factory=dict)
    tier0_violations: int = 0
    bond_separations: int = 0
    latencies_ms: list[float] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        values = sorted(self.latencies_ms)
        def pct(p: float) -> float:
            if not values:
                return 0.0
            return values[min(len(values) - 1, int(len(values) * p))]

        return {
            "orders": self.orders,
            "seated_passengers": self.seated,
            "waitlisted_passengers": self.waitlisted,
            "modes": dict(self.modes),
            "tier0_violations": self.tier0_violations,
            "tier0_bond_separations": self.bond_separations,
            "latency_ms": {
                "p50": round(statistics.median(values), 3) if values else 0.0,
                "p95": round(pct(0.95), 3),
                "p99": round(pct(0.99), 3),
                "max": round(max(values), 3) if values else 0.0,
                "mean": round(statistics.fmean(values), 3) if values else 0.0,
            },
        }


def random_order(rng: random.Random, index: int) -> Order:
    """按真实客流分布生成一个随机订单（家庭占多数，穿插各类特殊群体）。"""
    roll = rng.random()
    if roll < 0.30:
        return family_with_child_order(
            order_id=f"B{index:04d}-FAM",
            child_age=rng.choice([1, 3, 5, 8, 11]),
            behavior=rng.choice(list(DeclaredBehavior)),
        )
    if roll < 0.36:
        return multi_gen_order(order_id=f"B{index:04d}-MULTI")
    if roll < 0.44:
        return couple_order(order_id=f"B{index:04d}-CPL", a_id=f"A{index}a", b_id=f"A{index}b")
    if roll < 0.52:
        return wheelchair_order(order_id=f"B{index:04d}-WHL")
    if roll < 0.58:
        return blind_order(order_id=f"B{index:04d}-BLD")
    if roll < 0.64:
        return pregnant_order(order_id=f"B{index:04d}-PRG")
    if roll < 0.74:
        size = rng.randint(3, 8)
        people = tuple(
            Passenger(f"T{index}-{i}", ticket_type=TicketType.ADULT, age=rng.randint(19, 68))
            for i in range(size)
        )
        bonds = {
            frozenset((people[i].passenger_id, people[i + 1].passenger_id)): BondType.STRONG
            for i in range(size - 1)
        }
        return Order(order_id=f"B{index:04d}-GRP", passengers=people, relation=RelationType.GROUP, bonds=bonds)
    if roll < 0.86:
        # 老人 + 照护人
        senior = elderly(f"E{index}", age=rng.randint(70, 88))
        helper = Passenger(f"H{index}", ticket_type=TicketType.ADULT, age=rng.randint(30, 55), is_caregiver=True)
        return Order(
            order_id=f"B{index:04d}-ELD",
            passengers=(senior, helper),
            relation=RelationType.CARE,
            bonds={frozenset((senior.passenger_id, helper.passenger_id)): BondType.STRONG},
        )
    return solo_order(f"S{index}", age=rng.randint(18, 65), aisle=rng.random() < 0.4)


def run_benchmark(
    orders: int = 300,
    seed: int = 7,
    fill: float = 0.0,
    router: DecisionRouter | None = None,
) -> BenchStats:
    """跑一轮随机订单流。``fill`` 为初始占座比例（模拟运力紧张）。"""
    rng = random.Random(seed)
    engine = SeatEngine(
        formation=crh_16_car_formation(),
        credit_ledger=CreditLedger(),
        thresholds=None,
    )
    if router is not None:
        engine.router = router
    if fill > 0:
        seats = [s.seat_id for s in engine.formation.seats]
        rng.shuffle(seats)
        engine.state.mark_occupied(seats[: int(len(seats) * fill)])

    stats = BenchStats()
    for index in range(orders):
        order = random_order(rng, index)
        signals = RoutingSignals(
            availability_ratio=engine.state.availability_ratio,
            concurrency=rng.choice([0, 50, 400, 1500]),
        )
        result = engine.book(order, signals=signals)
        solution = result.solution
        stats.orders += 1
        stats.seated += len(solution.assignments)
        stats.waitlisted += len(solution.waitlisted)
        stats.modes[result.mode.value] = stats.modes.get(result.mode.value, 0) + 1
        stats.tier0_violations += sum(1 for v in solution.violations if v.tier == 0)
        stats.bond_separations += sum(1 for v in solution.violations if v.code == "T0_BOND_SEPARATED")
        stats.latencies_ms.append(solution.elapsed_ms)
        if engine.state.availability_ratio < 0.02:
            break
    return stats


def main() -> None:  # pragma: no cover - 手工运行入口
    parser = argparse.ArgumentParser(description="SmartRail-Seat-Engine 仿真基准")
    parser.add_argument("--orders", type=int, default=300)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--fill", type=float, default=0.0, help="初始占座比例 0..1")
    parser.add_argument("--json", default=None, help="把结果写入 JSON 文件")
    args = parser.parse_args()

    started = time.perf_counter()
    stats = run_benchmark(orders=args.orders, seed=args.seed, fill=args.fill)
    elapsed = time.perf_counter() - started
    summary = stats.summary()
    summary["wall_clock_s"] = round(elapsed, 3)
    summary["config"] = EngineConfig().to_dict()

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    latency = summary["latency_ms"]
    print("-" * 64)
    print(f"决策时延：P50={latency['p50']}ms  P95={latency['p95']}ms  P99={latency['p99']}ms")
    print(f"Tier 0 违规：{summary['tier0_violations']}（含跨车厢照护分离 {summary['tier0_bond_separations']}）")
    print(f"模式分布：{summary['modes']}")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(summary, handle, ensure_ascii=False, indent=2)
        print(f"已写入 {args.json}")


if __name__ == "__main__":  # pragma: no cover
    main()
