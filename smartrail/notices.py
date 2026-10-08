"""相邻就座评估与"待办提示"生成（三个版本共用）。

设计口径：**出票优先于座位理想度**
--------------------------------
真实运营里，"没有相邻座位"绝不能成为拒票的理由 —— 拒票直接损失客票收入，
而"一家人暂时没挨着"是可以由列车员现场调剂的（高铁上换座是常态）。

因此本模块把"相邻"从**硬性发牌条件**降级为**服务质量指标**：

* 求解器尽力满足相邻（代价函数给强绑定加成就近梯度）；
* 满足不了时**照常出票**，并生成结构化提示，明确"谁需要被怎么处理"；
* 只有"确实一个空座都没有"才是候补的正当理由。

三类结论
--------
``satisfied``     所有强绑定同行人都相邻；
``compromised``   有人未相邻、但同车厢内仍有可调剂空间；
``impossible``    车厢内已无相邻空位（通常发生在隔位占座后），
                  此时仍出票，并提示旅客上车后找列车员。

与 :class:`~smartrail.models.Violation` 的分工
--------------------------------------------
``Violation`` 说明"代价函数扣了多少分"（给算法与审计看）；
:class:`~smartrail.models.Notice` 说明"谁需要被谁怎么处理"（给站车服务看）。
两者由同一份座位分配推导，因此永远一致。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Iterable

from .models import AdjacencyStatus, BondType, Notice, Seat, SupportNeed

if TYPE_CHECKING:  # pragma: no cover
    from .config import EngineConfig
    from .solver import OrderContext

ADJACENT_THRESHOLD = 1
"""相邻判定：曼哈顿距离 ≤ 1（同排紧邻，或过道两侧正对）。

注意：同排但中间隔一个座位（距离 = 2）**不算相邻** —— 这是"可现场调剂"的
候选，而不是"已经坐在一起"。
"""


def is_adjacent(seat_a: Seat, seat_b: Seat) -> bool:
    return (
        seat_a.carriage == seat_b.carriage
        and seat_a.manhattan_to(seat_b) <= ADJACENT_THRESHOLD
    )


def has_nearby_slot(
    seat: Seat, seats: Iterable[Seat], occupied: set[str], row_window: int
) -> bool:
    """``seat`` 附近（同排或前后 ``row_window`` 排）是否还有空位可调剂。

    用于把结论从"只能上车再说"细化为"车内其实还有空位，可以让乘务员安排"。
    """
    for other in seats:
        if other.seat_id in occupied or other.seat_id == seat.seat_id:
            continue
        if other.carriage != seat.carriage:
            continue
        if abs(other.row - seat.row) <= row_window:
            return True
    return False


def adjacent_free_pair_in_carriage(
    seats: Iterable[Seat], occupied: set[str], carriage: int
) -> bool:
    """**指定车厢内**是否还存在相邻的两个空位。

    为什么要限定车厢：一辆 16 节编组的列车几乎总能在别处找到相邻空位，但对
    "想坐在一起的一家人"毫无帮助 —— 他们不会为此换到 12 号车厢去。早期版本
    只看全车，导致系统给出"车内仍有空位，可现场调剂"的**落空承诺**。
    """
    free = [
        seat
        for seat in seats
        if seat.seat_id not in occupied and seat.carriage == carriage
    ]
    for index, seat_a in enumerate(free):
        for seat_b in free[index + 1 :]:
            if seat_a.manhattan_to(seat_b) <= ADJACENT_THRESHOLD:
                return True
    return False


def adjacent_free_pair_exists(seats: Iterable[Seat], occupied: set[str]) -> bool:
    """全车范围内是否还存在相邻的两个空位（用于粗粒度的"还有空位吗"判断）。"""
    by_carriage: dict[int, list[Seat]] = {}
    for seat in seats:
        if seat.seat_id in occupied:
            continue
        by_carriage.setdefault(seat.carriage, []).append(seat)
    for group in by_carriage.values():
        for index, seat_a in enumerate(group):
            for seat_b in group[index + 1 :]:
                if seat_a.manhattan_to(seat_b) <= ADJACENT_THRESHOLD:
                    return True
    return False


def assess_adjacency(
    placed: dict[str, Seat],
    ctx: "OrderContext",
    config: "EngineConfig",
) -> AdjacencyStatus:
    """评估订单的相邻就座情况（只针对 MANDATORY / STRONG 绑定）。

    三档结论：

    * ``satisfied``：所有绑定同行人都相邻；
    * ``compromised``：有人未相邻，但**车内还有相邻空位**可现场调剂；
    * ``impossible``：车内已无相邻空位 —— 只能靠与其他旅客协商换座，
      提示相应升级为"请上车后找列车员解决"。
    """
    status = AdjacencyStatus()
    for a, b in _bound_pairs(ctx):
        seat_a, seat_b = placed.get(a), placed.get(b)
        if seat_a is None or seat_b is None:
            continue  # 有人候补，由候补相关提示负责
        if seat_a.carriage != seat_b.carriage:
            continue  # 跨车厢是 Tier 0/1 事故，不是"相邻"问题，交由 Violation 报告
        if is_adjacent(seat_a, seat_b):
            continue
        status.affected.extend([a, b])
        status.messages.append(
            f"{a}({seat_a.seat_id}) 与 {b}({seat_b.seat_id}) 未相邻"
            f"（相隔 {seat_a.manhattan_to(seat_b)} 步）"
        )
    if not status.affected:
        status.level = "satisfied"
        return status

    status.affected = sorted(set(status.affected))
    all_seats = tuple(getattr(ctx, "all_seats", ()) or ())
    occupied = {seat.seat_id for seat in placed.values()} | set(
        getattr(ctx, "occupied_seats", ()) or ()
    )
    if not all_seats:
        # 拿不到编组信息时保守地报 compromised（提示措辞仍会引导找列车员）
        status.level = "compromised"
        return status
    # 只看**受影响的这几个人所在的**车厢：别处有相邻空位对他们没有意义
    carriages = {
        seat.carriage
        for pid in status.affected
        if (seat := placed.get(pid)) is not None
    }
    reunion_possible = any(
        adjacent_free_pair_in_carriage(all_seats, occupied, carriage)
        for carriage in carriages
    )
    status.level = "compromised" if reunion_possible else "impossible"
    return status


def _bound_pairs(ctx: "OrderContext") -> list[tuple[str, str]]:
    """订单内所有 MANDATORY / STRONG 绑定（去重，顺序稳定）。"""
    order = ctx.order
    ids = list(ctx.passengers)
    pairs: list[tuple[str, str]] = []
    for index, a in enumerate(ids):
        for b in ids[index + 1 :]:
            if order.bond_of(a, b) in (BondType.MANDATORY, BondType.STRONG):
                pairs.append((a, b))
    return pairs


def build_notices(
    placed: dict[str, Seat],
    waitlisted: list[str],
    ctx: "OrderContext",
    config: "EngineConfig",
    adjacency: AdjacencyStatus | None = None,
) -> list[Notice]:
    """生成面向旅客 / 乘务员的待办提示。

    覆盖四类"已出票但需要现场处理"的情形，以及候补这一种真正的未出票情形。
    """
    if adjacency is None:
        adjacency = assess_adjacency(placed, ctx, config)
    notices: list[Notice] = []

    # 1) 轮椅旅客：没有无障碍座位，但已出票（普通座位）
    for pid, seat in placed.items():
        passenger = ctx.passengers[pid]
        if passenger.is_mobility_impaired and not seat.in_accessible_zone():
            notices.append(
                Notice(
                    passenger_id=pid,
                    kind="accessible_zone_full",
                    level="action",
                    seat_id=seat.seat_id,
                    carriage=seat.carriage,
                    message=(
                        f"无障碍专区已满，已为 {pid} 出票普通座位 {seat.seat_id}。"
                        "请站车协助（无障碍踏板 / 就近调剂到专区）。"
                    ),
                )
            )

    # 2) 需照护者与支持人未相邻（已出票）
    for pid, seat in placed.items():
        passenger = ctx.passengers[pid]
        if not (passenger.needs_caregiver or passenger.is_child):
            continue
        helpers = [h for h in ctx.helpers.get(pid, ()) if h in placed]
        if not helpers:
            continue
        if any(is_adjacent(seat, placed[h]) for h in helpers):
            continue
        nearest = min(placed[h].manhattan_to(seat) for h in helpers)
        # "能否现场调剂"直接复用订单级结论：它已经回答了"车内还有没有相邻空位"。
        # 早期这里另算了一套"附近有空位"的判据，两者标准不一致，会给出
        # "建议调剂"却其实无处可调的落空承诺。
        reunion = adjacency.level == "compromised"
        notices.append(
            Notice(
                passenger_id=pid,
                kind="caregiver_split",
                level="action" if passenger.is_child else "attention",
                seat_id=seat.seat_id,
                carriage=seat.carriage,
                companions=sorted(helpers),
                message=(
                    f"{pid} 与照护人 {', '.join(sorted(helpers))} 未相邻"
                    f"（相隔 {nearest} 步），已出票。"
                    + (
                        "车内仍有可调剂座位，建议上车后找列车员安排坐到一起。"
                        if reunion
                        else "请上车后找列车员协调。"
                    )
                ),
            )
        )

    # 3) 孕晚期旅客未与同行人相邻
    for pid, seat in placed.items():
        passenger = ctx.passengers[pid]
        if SupportNeed.PREGNANT_LATE not in passenger.support_needs:
            continue
        companions = [c for c in ctx.helpers.get(pid, ()) if c in placed]
        if not companions or any(is_adjacent(seat, placed[c]) for c in companions):
            continue
        notices.append(
            Notice(
                passenger_id=pid,
                kind="pregnant_no_companion",
                level="attention",
                seat_id=seat.seat_id,
                carriage=seat.carriage,
                companions=sorted(companions),
                message=(
                    f"{pid} 未与同行人 {', '.join(sorted(companions))} 相邻，已出票。"
                    "建议上车后找列车员就近调剂。"
                ),
            )
        )

    # 4) 订单级：同行人被分散
    #    只有当**没有**更具体的逐人提示时才生成，避免同一件事被说两遍
    #    （例如孕妇场景同时产出 pregnant_no_companion 与 scattered，读起来像两条问题）。
    specific = {n.passenger_id for n in notices}
    if adjacency.level in {"compromised", "impossible"} and not specific:
        notices.append(
            Notice(
                passenger_id="__order__",
                kind="scattered",
                level="attention" if adjacency.level == "compromised" else "action",
                companions=list(adjacency.affected),
                message=(
                    "同行人未全部相邻，已全部出票："
                    + "；".join(adjacency.messages)
                    + "。"
                    + (
                        "车内仍有空位，可现场调剂。"
                        if adjacency.level == "compromised"
                        else "本车厢已无相邻空位，请上车后找列车员解决。"
                    )
                ),
            )
        )

    # 5) 真正未出票：候补（只有在"无座可发"或显式配置下才会出现）
    for pid in waitlisted:
        passenger = ctx.passengers.get(pid)
        if passenger is None:
            continue
        notices.append(
            Notice(
                passenger_id=pid,
                kind="no_seat_available",
                level="action",
                message=(
                    f"{pid} 未出票（候补）：当前车次已无可用座位。"
                    + (
                        "如需乘车请联系站车安排无座或改签。"
                        if not passenger.is_mobility_impaired
                        else "轮椅旅客请由站车协助安排后续车次或无障碍车厢。"
                    )
                ),
            )
        )
    return notices


__all__ = [
    "ADJACENT_THRESHOLD",
    "adjacent_free_pair_exists",
    "adjacent_free_pair_in_carriage",
    "assess_adjacency",
    "build_notices",
    "has_nearby_slot",
    "is_adjacent",
]
