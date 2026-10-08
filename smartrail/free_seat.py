"""模式一（平峰期）：自由选座 + 隐性拦截。

把选座权交还用户，但系统必须做**隐性拦截**：
1. 儿童（或任何需照护乘客）不得与照护人分离 —— 与分配引擎同源的 Tier 0 判定；
2. 静音信用分 < 60 的乘客不得选择静音车厢（前端不展示该车厢）；
3. 活泼型 / 婴儿默认不推荐静音车厢（软拦截：仅告警，不阻断）；
4. 轮椅乘客只能选择无障碍专区的座位（硬拦截）。
"""

from __future__ import annotations

from itertools import combinations
from typing import Sequence

from .clustering import BookingState
from .config import EngineConfig
from .models import BondType, Order, Passenger, Solution, Violation, seat_key
from .scoring import (
    ACCESSIBLE_MISUSE,
    BOND_SEPARATED,
    QUIET_CREDIT_BLOCKED,
    QUIET_FAMILY,
    QUIET_SOLO_ADULT,
    SEAT_TAKEN,
    UNKNOWN_PASSENGER,
    UNKNOWN_SEAT,
    WHEELCHAIR_NO_ZONE,
    Scorer,
)


def is_quiet_carriage_visible(passenger: Passenger, config: EngineConfig) -> bool:
    """静音车厢是否对该乘客可见（前端展示层拦截）。"""
    if passenger.quiet_carriage_blocked:
        return False
    return passenger.quiet_repulsion < 1.0  # 婴儿（0-3 岁）默认隐藏静音车厢


def blocked_reason(passenger: Passenger) -> str:
    """静态拦截 HTML/文案（前端可直接展示）。"""
    if passenger.quiet_carriage_blocked:
        return (
            f"静音信用分 {passenger.quietness_score:.0f} 分（低于 60），"
            "已按规则屏蔽静音车厢选座权限。"
        )
    if passenger.is_infant:
        return "携带婴儿（0-3 岁）出行，系统默认隐藏静音车厢。"
    return ""


def validate_selection(
    order: Order,
    state: BookingState,
    chosen: dict[str, str],
    config: EngineConfig,
) -> list[Violation]:
    """校验用户自选座位，返回**阻断性**违规清单（空表示通过）。

    任何非法输入（不存在的乘客 / 不存在的座位号 / 已被占用的座位）都必须变成
    **可解释的拦截理由**，而不是抛异常把 500 抛给前端——这既是体验问题，
    也是安全性问题（异常信息可能泄露内部结构）。
    """
    scorer = Scorer(config)
    seat_index = state.formation.by_id()
    occupied = state.occupied
    blocked: list[Violation] = []
    known: dict[str, str] = {}  # 乘客 ID -> 已验证存在的座位号

    for pid, seat_id in chosen.items():
        passenger = _passenger_or_none(order, pid)
        if passenger is None:
            blocked.append(
                Violation(UNKNOWN_PASSENGER, 0, config.t0_separated_care_bond, (pid,),
                          f"乘客 {pid} 不属于本订单")
            )
            continue
        seat = seat_index.get(seat_id)
        if seat is None:
            blocked.append(
                Violation(UNKNOWN_SEAT, 0, config.t0_wheelchair_no_accessible, (pid,),
                          f"座位号 {seat_id} 在本编组中不存在")
            )
            continue
        if seat_id in occupied:
            blocked.append(
                Violation(SEAT_TAKEN, 0, config.t2_adult_cross_carriage, (pid,),
                          f"座位 {seat_id} 已被占用，请另选")
            )
            continue
        known[pid] = seat_id
        if passenger.is_mobility_impaired and not seat.in_accessible_zone():
            blocked.append(
                Violation(WHEELCHAIR_NO_ZONE, 0, config.t0_wheelchair_no_accessible, (pid,),
                          f"轮椅乘客 {pid} 必须选择无障碍专区座位（{seat_id} 不是）")
            )
        if seat.is_quiet_carriage and passenger.quiet_carriage_blocked:
            blocked.append(
                Violation(QUIET_CREDIT_BLOCKED, 3, config.t3_quiet_blocked_credit, (pid,),
                          f"静音信用分 {passenger.quietness_score:.0f} < 60，禁止选择静音车厢")
            )

    # 照护绑定不得跨车厢（与 Tier 0 同源判定）
    for a_id, b_id in combinations(sorted(known), 2):
        bond = order.bond_of(a_id, b_id)
        if bond is not BondType.MANDATORY:
            continue
        pa, pb = _passenger(order, a_id), _passenger(order, b_id)
        seat_a, seat_b = seat_index[known[a_id]], seat_index[known[b_id]]
        _value, terms = scorer.pair_cost(pa, seat_a, pb, seat_b, bond)
        blocked.extend(t.to_violation() for t in terms if t.tier == 0 and t.value < 0)
    return blocked


def _passenger_or_none(order: Order, pid: str) -> Passenger | None:
    return next((p for p in order.passengers if p.passenger_id == pid), None)


def _passenger(order: Order, pid: str) -> Passenger:
    passenger = _passenger_or_none(order, pid)
    if passenger is None:
        raise KeyError(pid)
    return passenger


def validate_free_selection(
    order: Order,
    state: BookingState,
    config: EngineConfig,
    chosen: dict[str, str] | None = None,
    accessible_available: bool = True,
) -> Solution:
    """模式一入口。

    * 用户已选座（``chosen``）-> 只做隐性拦截校验；
    * 用户未选座           -> 给出"推荐座位"（软拦截：不推荐静音车厢/不拆家庭）。

    ``accessible_available``：无障碍专区在本单开始前是否还有可用座位，
    决定轮椅无专区的记法（求解器失误 vs 售罄兜底）。见 :func:`smartrail.scoring.accessible_zone_has_free_seat`。
    """
    import time

    start = time.perf_counter()
    scorer = Scorer(config, accessible_available=accessible_available)
    seat_index = state.formation.by_id()
    if chosen:
        blocked = validate_selection(order, state, chosen, config)
        if blocked:
            return Solution(
                assignments={},
                total_affinity=-sum(v.penalty for v in blocked),
                violations=blocked,
                solver="free-seat-validator",
                elapsed_ms=(time.perf_counter() - start) * 1000.0,
                notes=["自由选座校验未通过，请重新选择（详见 violations）。"],
                mode="free",
                breakdown=_breakdown(blocked),
            )
        from .solver import build_context, evaluate_placement
        from .models import Assignment

        placed = {pid: seat_index[sid] for pid, sid in chosen.items()}
        ctx = build_context(order, config)
        cost, violations = evaluate_placement(placed, ctx, scorer)
        assignments = {
            pid: Assignment(
                passenger_id=pid,
                seat_id=seat.seat_id,
                carriage=seat.carriage,
                row=seat.row,
                col=seat.col,
                quiet_carriage=seat.is_quiet_carriage,
                reason="用户自主选座，通过隐性拦截校验。",
            )
            for pid, seat in placed.items()
        }
        solution = Solution(
            assignments=dict(sorted(assignments.items(), key=lambda kv: seat_key(kv[1].seat_id))),
            total_affinity=cost,
            violations=violations,
            solver="free-seat-validator",
            elapsed_ms=(time.perf_counter() - start) * 1000.0,
            mode="free",
        )
        solution.breakdown = _breakdown(violations)
        return solution

    # 无自选：给出推荐座位（用贪心在软约束下取局部最优）
    from .solver import assign_greedy, build_context

    ctx = build_context(order, config)
    solution = assign_greedy(ctx, state, config, mode="free", scorer=scorer)
    solution.solver = "free-seat-recommender"
    solution.notes.append("用户未指定座位，返回系统推荐座位（可继续自由调整）。")
    return solution


def _breakdown(violations: Sequence[Violation]) -> dict[str, float]:
    """按"Tier:代码"聚合亲和度（内部口径，负=惩罚、正=奖励）。"""
    out: dict[str, float] = {}
    for v in violations:
        key = f"tier{v.tier}:{v.code}"
        out[key] = out.get(key, 0.0) + v.affinity
    return dict(sorted(out.items(), key=lambda kv: kv[1]))


__all__ = [
    "validate_free_selection",
    "validate_selection",
    "is_quiet_carriage_visible",
    "blocked_reason",
    "ACCESSIBLE_MISUSE",
    "QUIET_FAMILY",
    "QUIET_SOLO_ADULT",
    "BOND_SEPARATED",
]
