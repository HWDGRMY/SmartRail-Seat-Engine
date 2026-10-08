"""命令行演示与自检：一键跑通全部预置场景，打印分配结果与数学解释。

用法：
    python -m smartrail.demo                      # 全部场景，smart 模式
    python -m smartrail.demo --mode degraded      # 降级模式对比
    python -m smartrail.demo --scenario family_with_child --explain
"""

from __future__ import annotations

import argparse
from typing import Any

from . import SeatEngine, crh_16_car_formation
from .config import DEFAULT_CONFIG
from .fixtures import SCENARIOS


def run(
    scenario: str,
    mode: str | None = None,
    explain: bool = False,
    quiet_fill: float = 0.0,
) -> dict[str, Any]:
    """运行单个场景。``quiet_fill`` 用于预先占座，模拟运力紧张。"""
    engine = SeatEngine(formation=crh_16_car_formation())
    if quiet_fill > 0:
        target = int(len(engine.formation.seats) * quiet_fill)
        seats = [s.seat_id for s in engine.formation.seats if not s.in_accessible_zone()][:target]
        engine.state.mark_occupied(seats)
    order = SCENARIOS[scenario]()
    result = engine.book(order, mode=mode)
    payload = result.to_dict()
    if explain:
        payload["explanation"] = result.to_dict()["explanation"]
    return payload


def _summary(name: str, mode: str, payload: dict[str, Any]) -> str:
    seats: dict[int, list[str]] = {}
    for assignment in payload["assignments"]:
        seats.setdefault(assignment["carriage"], []).append(
            f"{assignment['passenger_id']}@{assignment['row']:02d}{assignment['col']}"
        )
    layout = " ".join(f"{car:02d}车[{' '.join(sorted(v))}]" for car, v in sorted(seats.items()))
    wait = f" 候补={payload['waitlisted']}" if payload["waitlisted"] else ""
    return (
        f"{name:<18} {mode:<9} 代价={payload['total_cost']:>9.1f} "
        f"求解器={payload['solver']:<18} {layout}{wait}"
    )


def main() -> None:  # pragma: no cover - 手工运行入口
    parser = argparse.ArgumentParser(description="SmartRail-Seat-Engine 场景演示")
    parser.add_argument("--scenario", default="all", help="场景名，或 all")
    parser.add_argument("--mode", default=None, help="free | smart | degraded（默认自动路由）")
    parser.add_argument("--explain", action="store_true", help="打印数学解释")
    parser.add_argument("--quiet-fill", type=float, default=0.0, help="预先占座比例，模拟运力紧张")
    parser.add_argument(
        "--compare-modes",
        action="store_true",
        help="对每个场景同时跑 smart 与 degraded 做对比",
    )
    args = parser.parse_args()

    names = sorted(SCENARIOS) if args.scenario == "all" else [args.scenario]
    print(f"编组：{crh_16_car_formation().train_code}，共 {len(crh_16_car_formation().seats)} 个座位")
    print(f"代价权重：Tier0={DEFAULT_CONFIG.t0_separated_care_bond:.0f} "
          f"Tier1={DEFAULT_CONFIG.t1_vulnerable_cross_carriage:.0f} "
          f"Tier2={DEFAULT_CONFIG.t2_adult_cross_carriage:.0f}")
    print("-" * 132)
    modes = ["smart", "degraded"] if args.compare_modes else [args.mode]
    for name in names:
        for mode in modes:
            payload = run(name, mode=mode, quiet_fill=args.quiet_fill)
            print(_summary(name, payload["mode"], payload))
            if args.explain:
                print("   解释：" + payload["explanation"])
    print("-" * 132)
    print("说明：代价为负 = 该方案带奖励（约束满足良好）；代价为正 = 存在惩罚项，")
    print("      数值越大越严重（Tier 0 = 100000 分，Tier 1 = 10000 分）。")


if __name__ == "__main__":  # pragma: no cover
    main()
