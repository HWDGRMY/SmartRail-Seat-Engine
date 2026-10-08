"""订单可行性分析：**在求解之前**判断一张订单能不能被满足，并给出原因。

为什么需要它（来自验收反馈）
----------------------------
之前只报"候补 N 人"，用户看不出**为什么**。而这个引擎里有几类硬约束会让
一张订单**结构性地**无法满足，跟车厢空不空没关系：

* 含轮椅旅客的订单必须有无障碍专区座位，专区只有 10 座，售罄就真的没辙；
* 含婴儿/幼童的订单若被声明为硬绑定，整单必须同车厢，车厢余量不足就会失败；
* 同一张订单里塞进两位轮椅旅客 + 一堆家属时，专区的可用座位数可能不够。

因此本模块做三件事：

1. **求解前**给出"这张单能不能满足"的判定与原因（:func:`analyse_order`）；
2. **求解后**把实际结果与判定对照，生成面向用户的提示（:func:`outcome_summary`）；
3. 把结果归类成明确的四档（:data:`OUTCOME_LEVELS`），而不是笼统的"成功/失败"。

判定原则：**宁可提前提示、不可事后惊讶**。分析器只做结构性判断
（座位数量、设施匹配、硬绑定可满足性），不预测具体落座方案。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from .models import BondType, Order, Seat, Solution, SupportNeed

# 结果分档：让"满足 / 部分满足 / 需现场处理 / 无法满足"一眼可分
OUTCOME_LEVELS: dict[str, str] = {
    "fulfilled": "完全满足",
    "partial": "部分满足",
    "action_required": "已出票，需现场处理",
    "impossible": "无法满足",
}

# 无法满足的原因码
FEASIBILITY_CODES: dict[str, str] = {
    "OK": "可满足",
    "ZONE_TOO_SMALL": "无障碍专区座位数少于本单轮椅旅客人数",
    "NO_WHEELCHAIR_SLOT": "当前无障碍专区余票不足，本单轮椅旅客无法安排",
    "TOO_MANY_WHEELCHAIRS": "单张订单的轮椅旅客人数超过上限，应拆分为多张订单",
    "CARRIAGE_CAPACITY": "找不到能容纳整单人数的车厢",
    "NO_SEATS": "全车余票不足",
    "HARD_BOND_INFEASIBLE": "硬绑定要求同车厢，但当前余票分布无法同时容纳",
    "MIXED_CLASS": "订单内坐席等级不一致，无法安排在同一车厢",
}


@dataclass
class OrderFeasibility:
    """一张订单的可行性结论。"""

    order_id: str
    code: str = "OK"
    feasible: bool = True
    severity: str = "info"
    """``info`` / ``warning`` / ``blocking``。"""
    message: str = ""
    details: list[str] = field(default_factory=list)
    wheelchair_count: int = 0
    zone_available: int = 0
    passengers: int = 0
    same_carriage_required: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id,
            "code": self.code,
            "feasible": self.feasible,
            "severity": self.severity,
            "message": self.message,
            "details": list(self.details),
            "wheelchair_count": self.wheelchair_count,
            "zone_available": self.zone_available,
            "passengers": self.passengers,
            "same_carriage_required": self.same_carriage_required,
        }


def _requires_same_carriage(order: Order) -> bool:
    """订单里是否存在"必须同车厢"的绑定。"""
    for index, a in enumerate(order.passengers):
        for b in order.passengers[index + 1 :]:
            if order.bond_of(a.passenger_id, b.passenger_id) is BondType.MANDATORY:
                return True
    return False


def analyse_order(
    order: Order,
    all_seats: Sequence[Seat],
    occupied: Iterable[str] = (),
    config: Any | None = None,
) -> OrderFeasibility:
    """判断一张订单**在结构性意义上**能否被满足。

    只看硬约束，不预测具体方案：
    轮椅旅客需要无障碍专区座位；硬绑定需要同车厢；人数不能超过车厢容量；
    单张订单的轮椅旅客人数不能超过专区配额。
    """
    if config is None:
        from .config import DEFAULT_CONFIG as config  # noqa: PLC0415

    occupied_set = set(occupied)
    free = [seat for seat in all_seats if seat.seat_id not in occupied_set]
    zone_free = [seat for seat in free if seat.in_accessible_zone()]
    wheelchair_count = sum(
        1 for passenger in order.passengers if passenger.is_mobility_impaired
    )
    same_carriage = _requires_same_carriage(order)
    result = OrderFeasibility(
        order_id=order.order_id,
        passengers=len(order.passengers),
        wheelchair_count=wheelchair_count,
        zone_available=len(zone_free),
        same_carriage_required=same_carriage,
    )

    limit = int(getattr(config, "max_wheelchairs_per_order", 2))
    if wheelchair_count > limit:
        result.code = "TOO_MANY_WHEELCHAIRS"
        result.feasible = False
        result.severity = "blocking"
        result.message = (
            f"本单有 {wheelchair_count} 位轮椅旅客，超过单张订单上限（{limit} 位）"
            f" —— 请拆成多张订单（每位轮椅旅客各自与其陪同人一起下单）。"
        )
        result.details.append(
            "无障碍专区只有 10 座，单张订单最多占用一半；多位轮椅旅客分别下单"
            "既能保证每位都有专区座位，也避免把专区一次吃光。"
        )
        return result

    if not free:
        result.code = "NO_SEATS"
        result.feasible = False
        result.severity = "blocking"
        result.message = "全车已无余票，本单无法出票。"
        return result

    if wheelchair_count and len(zone_free) < wheelchair_count:
        result.code = "NO_WHEELCHAIR_SLOT"
        result.feasible = False
        result.severity = "blocking"
        result.message = (
            f"本单有 {wheelchair_count} 位轮椅旅客，但无障碍专区仅剩 "
            f"{len(zone_free)} 个空位 —— 无法满足，请改签其他车次或减少轮椅旅客人数。"
        )
        result.details.append(
            "无障碍专区为轮椅旅客的硬性要求（Tier 0），不可用普通座位替代。"
        )
        return result

    # 「同车厢能装下吗」——这一条**必须有**。
    # 早期只检查了"专区座位够不够"，于是"4 位轮椅 + 4 位家属"这种单被判成
    # "可满足"（专区剩 7 个 >= 4 个轮椅），实际求解时却因为**一节车厢装不下
    # 整单 8 人**而全部候补。判成"可满足"再失败，比直接说"不能满足"更糟：
    # 用户拿到的是一串内部编号，而不是可操作的原因。
    per_carriage: dict[int, int] = {}
    for seat in free:
        per_carriage[seat.carriage] = per_carriage.get(seat.carriage, 0) + 1
    best_carriage = max(per_carriage.values(), default=0)
    result.details.append(
        f"当前最空的单个车厢余票 {best_carriage} 个"
        + ("（本单要求同车厢）" if same_carriage else "")
    )

    if len(free) < len(order.passengers):
        result.code = "NO_SEATS"
        result.feasible = False
        result.severity = "blocking"
        result.message = (
            f"全车余票 {len(free)} 个，少于本单 {len(order.passengers)} 人 —— 无法全部出票。"
        )
        return result

    if same_carriage and best_carriage < len(order.passengers):
        result.code = "HARD_BOND_INFEASIBLE"
        result.feasible = False
        result.severity = "warning"
        # 区分两种情形，给出的建议完全不同
        if wheelchair_count:
            result.message = (
                f"本单 {len(order.passengers)} 人且含 {wheelchair_count} 位轮椅旅客，"
                f"要求同车厢；无障碍专区剩 {len(zone_free)} 个空位、"
                f"最空的单个车厢余票 {best_carriage} 个，装不下整单 —— "
                f"建议拆成多张订单（轮椅旅客与家属分开下单）或改签其他车次。"
            )
        else:
            result.message = (
                f"本单 {len(order.passengers)} 人要求同车厢，"
                f"但当前最空的单个车厢只剩 {best_carriage} 个空位 —— "
                f"建议拆成多张订单，或把绑定强度改为『软绑定』。"
            )
        return result

    result.message = "可满足"
    return result


def outcome_summary(
    order: Order,
    solution: Solution,
    feasibility: OrderFeasibility | None = None,
) -> dict[str, Any]:
    """把求解结果归类成四档，并汇总面向用户的提示。"""
    requested = len(order.passengers)
    seated = len(solution.assignments)
    waitlisted = list(solution.waitlisted)
    notices = [notice.to_dict() for notice in solution.notices]
    hard_violations = len(solution.hard_violations)

    if feasibility is not None and not feasibility.feasible and seated == 0:
        level = "impossible"
    elif seated == 0:
        level = "impossible"
    elif waitlisted or hard_violations:
        level = "partial"
    elif any(notice["level"] == "action" for notice in notices):
        level = "action_required"
    else:
        level = "fulfilled"

    reasons: list[str] = []
    # 顺序有讲究：**可行性原因排在最前**，因为它才是可操作的那条。
    # 早期把"N 位旅客未出票：编号列表"排在前面，用户看到一串内部 ID
    # 却不知道该怎么办 —— 实测验收时被指出"提示没用"。
    if feasibility is not None and not feasibility.feasible:
        reasons.append(feasibility.message)
    if waitlisted and not (feasibility is not None and not feasibility.feasible):
        reasons.append(f"{len(waitlisted)} 位旅客未出票：{'、'.join(waitlisted)}")
    for notice in notices:
        if notice["level"] in ("action", "attention"):
            reasons.append(notice["message"])
    if not reasons and level == "fulfilled":
        reasons.append("全部出票且相邻需求满足，无需现场处理。")

    return {
        "order_id": order.order_id,
        "level": level,
        "level_label": OUTCOME_LEVELS[level],
        "requested": requested,
        "seated": seated,
        "waitlisted": waitlisted,
        "tier0": hard_violations,
        "reasons": reasons,
        "notices": notices,
        "feasibility": feasibility.to_dict() if feasibility else None,
    }


def feasibility_catalog() -> list[dict[str, str]]:
    """原因码清单（供前端做图例/文档）。"""
    return [{"code": code, "label": label} for code, label in FEASIBILITY_CODES.items()]


# 让静态检查知道 SupportNeed 被有意导入（用于未来的按需判据扩展）
_ = SupportNeed

__all__ = [
    "FEASIBILITY_CODES",
    "OUTCOME_LEVELS",
    "OrderFeasibility",
    "analyse_order",
    "feasibility_catalog",
    "outcome_summary",
]
