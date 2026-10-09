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
    "fulfilled": "全部出票",
    "confirmed": "已出票（用户已确认例外）",
    "action_required": "已出票，需现场处理",
    "partial": "部分出票，其余候补",
    "impossible": "无座可发（全车已满）",
}

# 无法满足的原因码
FEASIBILITY_CODES: dict[str, str] = {
    "OK": "可满足",
    "NO_SEATS": "全车余票不足（唯一会候补的情形）",
    "NO_WHEELCHAIR_SLOT": "无障碍专区不足，已出票并转站车协助",
    "WHEELCHAIR_OVER_QUOTA": "单张订单轮椅旅客偏多，已出票（超出者坐普通座位）",
    "HARD_BOND_INFEASIBLE": "难以全部同车厢，已出票并生成现场调剂提示",
}


@dataclass
class OrderFeasibility:
    """一张订单的可行性结论。

    **默认全部可出票**。只有"全车真的一个空座都没有"才会 ``blocking``；
    其余情况一律是 ``advisory``（可出票，但有需要告知/确认的事），
    由 :attr:`question` 交给用户确认。
    """

    order_id: str
    code: str = "OK"
    feasible: bool = True
    severity: str = "info"
    """``info``（无特殊情况）/ ``advisory``（可出票，但需提示或确认）/
    ``blocking``（确实无座可发，只能候补）。"""
    message: str = ""
    details: list[str] = field(default_factory=list)
    question: str = ""
    """需要**询问用户**的问题（有值时前端应弹出确认，而不是直接拒票）。"""
    wheelchair_count: int = 0
    zone_available: int = 0
    passengers: int = 0
    same_carriage_required: bool = False

    @property
    def needs_confirmation(self) -> bool:
        return bool(self.question)

    def to_dict(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id,
            "code": self.code,
            "feasible": self.feasible,
            "severity": self.severity,
            "message": self.message,
            "details": list(self.details),
            "question": self.question,
            "needs_confirmation": self.needs_confirmation,
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
    """判断一张订单**会遇到什么情况**，以及是否需要向用户确认。

    出票优先是本引擎的第一原则（见 :mod:`smartrail.config` 的"出票策略"段）：

    * **只有"全车无空座"才 ``blocking``**（这时的候补是事实，不是策略）；
    * 特殊席位（无障碍专区、同车厢相邻）不够时**照常出票**，
      转为 ``advisory`` + 一条可执行提示；
    * 需要用户拍板的情形给出 :attr:`OrderFeasibility.question`，
      由调用方询问后再出票 —— 而不是替用户拒绝。
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

    per_carriage: dict[int, int] = {}
    for seat in free:
        per_carriage[seat.carriage] = per_carriage.get(seat.carriage, 0) + 1
    best_carriage = max(per_carriage.values(), default=0)
    result.details.append(f"当前最空的单个车厢余票 {best_carriage} 个")
    result.details.append(f"无障碍专区余票 {len(zone_free)} 个")

    # ---- 唯一真正"拒票"的情形：全车确实没有空座 ----
    if not free:
        result.code = "NO_SEATS"
        result.feasible = False
        result.severity = "blocking"
        result.message = "全车已无余票，本单只能候补。"
        return result
    if len(free) < len(order.passengers):
        result.code = "NO_SEATS"
        result.feasible = False
        result.severity = "blocking"
        result.message = (
            f"全车余票 {len(free)} 个，少于本单 {len(order.passengers)} 人 —— "
            f"只能部分出票，其余候补。"
        )
        return result

    # ---- 以下全部"可出票"，只是需要提示或询问 ----

    # 1) 单张订单轮椅人数超过专区配额：照常出票，超出部分坐普通座位
    limit = int(getattr(config, "max_wheelchairs_per_order", 2))
    if wheelchair_count > limit:
        result.code = "WHEELCHAIR_OVER_QUOTA"
        result.severity = "advisory"
        result.message = (
            f"本单有 {wheelchair_count} 位轮椅旅客，超过单张订单建议上限（{limit} 位）："
            f"**可以出票**，但无障碍专区座位可能不够，超出者将安排普通座位并转站车协助。"
        )
        result.question = (
            f"本单 {wheelchair_count} 位轮椅旅客，无障碍专区只有 10 座"
            f"（单张订单建议不超过 {limit} 位）。是否仍然出票？"
            f"（超出者坐普通座位，列车员协助上下车）"
        )
        result.details.append(
            "若希望每位轮椅旅客都有专区座位，可拆成多张订单分别下单。"
        )
        # 不 return：继续检查专区余量，把更具体的情况也带上

    # 2) 专区余票不足：照常出票 + 站车协助（这是运营例外，不是拒票理由）
    if wheelchair_count and len(zone_free) < wheelchair_count:
        result.code = "NO_WHEELCHAIR_SLOT"
        result.severity = "advisory"
        short = wheelchair_count - len(zone_free)
        result.message = (
            f"本单有 {wheelchair_count} 位轮椅旅客，无障碍专区仅剩 {len(zone_free)} 个空位："
            f"**仍然出票**，其中 {short} 位将安排普通座位，并生成站车协助提示。"
        )
        if not result.question:
            result.question = (
                f"无障碍专区只剩 {len(zone_free)} 个空位，本单有 {wheelchair_count} 位"
                f"轮椅旅客。是否接受『照常出票 + 站车协助』？"
            )
        result.details.append(
            "专区的空位会优先留给轮椅旅客；不足的部分发给普通座位并通知列车员协助。"
        )
        return result

    # 3) 要求同车厢但最空的车厢也装不下：软化为"尽量同车厢"，照常出票
    if same_carriage and best_carriage < len(order.passengers):
        result.code = "HARD_BOND_INFEASIBLE"
        result.severity = "advisory"
        reason = (
            f"且含 {wheelchair_count} 位轮椅旅客" if wheelchair_count else ""
        )
        result.message = (
            f"本单 {len(order.passengers)} 人{reason}、要求同车厢，"
            f"但当前最空的单个车厢只剩 {best_carriage} 个空位：**仍然出票**，"
            f"系统会尽量把人放在同一车厢，并在无法做到时生成现场调剂提示。"
        )
        if not result.question:
            result.question = (
                f"本单 {len(order.passengers)} 人要求同车厢，但最空的车厢只剩 "
                f"{best_carriage} 个空位。是否接受『先出票、上车后找列车员调剂』？"
            )
        result.details.append("也可拆成多张订单，或把绑定强度改为『软绑定』。")
        return result

    if result.severity == "info":
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

    # 分档原则：**先把"票发出去了没有"说清楚**，再说例外。
    # 早期把"专区不足"一律算作"无法满足"，与"出票优先"原则直接冲突 ——
    # 明明还有 800 多个空座，却告诉用户"无法满足"。
    has_confirmation = bool(feasibility and feasibility.needs_confirmation)
    if seated == 0:
        level = "impossible"
    elif waitlisted or hard_violations:
        level = "partial"
    elif any(notice["level"] == "action" for notice in notices):
        level = "action_required"
    elif has_confirmation:
        level = "confirmed"
    else:
        level = "fulfilled"

    reasons: list[str] = []
    # 顺序有讲究：**可行性说明排在最前**，因为它才是可操作的那条。
    # 早期把"N 位旅客未出票：编号列表"排在前面，用户看到一串内部 ID
    # 却不知道该怎么办 —— 实测验收时被指出"提示没用"。
    if feasibility is not None and feasibility.message:
        reasons.append(feasibility.message)
    if waitlisted:
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
        "question": feasibility.question if feasibility else "",
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
