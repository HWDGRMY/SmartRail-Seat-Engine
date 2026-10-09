"""分层多目标代价函数（Tier 0-5）。

设计取舍
--------
代价被拆成三块，以便在毫秒级求解器里做**增量打分**与**可采纳下界**：

1. ``individual_cost(passenger, seat)``：只与座位本身有关（设施匹配、静音奖励、
   静音信用屏蔽）。
2. ``pair_cost(passenger_a, seat_a, passenger_b, seat_b)``：只与一对乘客及其
   座位关系有关（绑定、跨车厢、过道分隔、同排奖励）。
3. 总代价 = Σ 单项 + Σ 关系项，因此可以安全地做分支限界。

惩罚为负、奖励为正；求解目标是**全局最低分**。
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
from typing import Iterable, Sequence

from .config import EngineConfig
from .models import (
    BondType,
    DeclaredBehavior,
    Order,
    Passenger,
    PassengerUnit,
    Seat,
    SeatFeature,
    SupportNeed,
    Violation,
)

# 代价条目代码（供可解释性与测试断言使用）
# 说明：ISOLATED_CARE 实际挂在 Tier 1（安全底线），代码名沿用 T3_ 前缀会误导，
# 因此这里以"实际生效的 Tier"为准命名。
BOND_SEPARATED = "T0_BOND_SEPARATED"
WHEELCHAIR_NO_ZONE = "T0_WHEELCHAIR_NO_ACCESSIBLE"
WHEELCHAIR_ZONE_SOLD_OUT = "T5_WHEELCHAIR_ZONE_SOLD_OUT"
"""专区售罄后的轮椅兜底（服务条目，非安全违规）。

与 :data:`WHEELCHAIR_NO_ZONE` 的区别：后者是"专区有空位却没给"的求解器失误；
前者是"专区确实已售罄，按出票优先原则发放普通座位 + 站车协助"的运营例外。
两者分开统计，Tier 0 才能真实反映系统可避免的事故数。
"""
PREGNANT_ALONE = "T0_PREGNANT_NO_COMPANION"
VULNERABLE_CROSS_CAR = "T1_VULNERABLE_CROSS_CARRIAGE"
CARE_UNIT_SPLIT = "T1_CARE_UNIT_SPLIT"
QUIET_CONFLICT = "T1_CONFLICT_ISOLATION"
CARE_BOND_SAME_CAR = "T1_CARE_BOND_SAME_CARRIAGE"
ISOLATED_CARE = "T1_ISOLATED_CARE_MEMBER"
ADULT_CROSS_CAR = "T2_ADULT_CROSS_CARRIAGE"
QUIET_FAMILY = "T3_QUIET_FAMILY_REPULSION"
QUIET_CREDIT_BLOCKED = "T3_QUIET_CREDIT_BLOCKED"
ACCESSIBLE_MISUSE = "T3_ACCESSIBLE_MISUSE"
QUIET_GROUP_OVERUSE = "T3_QUIET_GROUP_OVERUSE"
AISLE_SEPARATED = "T4_AISLE_SEPARATED"
NOT_TOGETHER = "T4_NOT_SAME_ROW"
QUIET_SOLO_ADULT = "T5_QUIET_SOLO_ADULT"
FACILITY_MATCH = "T5_FACILITY_MATCH"
ACCESSIBLE_MATCH = "T5_ACCESSIBLE_MATCH"
COMPANION_TOGETHER = "T5_COMPANION_TOGETHER"
CHILD_ADJACENT = "T5_CHILD_ADJACENT"

# 自由选座校验专用：非法输入必须变成可解释的拦截理由，而不是异常
UNKNOWN_SEAT = "V_UNKNOWN_SEAT"
UNKNOWN_PASSENGER = "V_UNKNOWN_PASSENGER"
SEAT_TAKEN = "V_SEAT_TAKEN"

# 需照护者的"安全底线"判定：只要被绑定人在同一排紧邻，即视为未被孤立。
CARE_ADJACENT_TOLERANCE = 1

# 与静音车厢存在"硬冲突"的群体：他们的出行方式本身就会持续产生噪音/需要协助，
# 进入静音车厢属于 Tier 1 冲突隔离（而非可协商的偏好冲突）。
_QUIET_CONFLICT_NEEDS = frozenset(
    {
        SupportNeed.WHEELCHAIR,
        SupportNeed.INDEPENDENT_BLIND,
        SupportNeed.INTELLECTUAL_DISABILITY,
    }
)


def is_care_dependent(passenger: Passenger, order: Order) -> bool:
    """该乘客是否必须时刻有绑定人在旁（儿童 / 婴儿 / 智力障碍 / 孕晚期）。"""
    if passenger.is_child or passenger.needs_caregiver:
        return True
    if passenger.support_needs & {SupportNeed.INTELLECTUAL_DISABILITY, SupportNeed.PREGNANT_LATE}:
        return True
    return bool(order.caregivers_of(passenger.passenger_id))


def support_people_of(passenger: Passenger, order: Order) -> frozenset[str]:
    """在旁支持人集合。

    不仅包括 MANDATORY 绑定的照护人，也包括本单中任何被标记为照护者的同行人
    （例如另一位家长）。判据因此变成"**是否真的孤身一人**"，
    而不是"是否紧贴那一位指定家长"——这才是"绝不孤立儿童"的工程含义。
    """
    people = set(order.caregivers_of(passenger.passenger_id))
    people |= {pid for pid in order.caregivers() if pid != passenger.passenger_id}
    return frozenset(people)


@dataclass(frozen=True)
class CostTerm:
    """代价函数的一个可加项。"""

    code: str
    tier: int
    value: float
    passengers: tuple[str, ...] = ()
    detail: str = ""

    def to_violation(self) -> Violation:
        return Violation(
            code=self.code,
            tier=self.tier,
            affinity=self.value,
            passengers=self.passengers,
            detail=self.detail,
        )


def accessible_zone_has_free_seat(
    all_seats: Sequence[Seat] | None, occupied: Iterable[str]
) -> bool:
    """无障碍专区是否还有**可用**座位。

    这个判据决定"轮椅旅客拿到普通座位"该被记成什么：

    * 专区**还有空位**却没给他 → 求解器失误，必须记 Tier 0 违规；
    * 专区**已全部售出** → 运营例外：出票 + 站车协助提示，
      **不计 Tier 0 违规**（那不是安全违规，而是运力事实）。

    早期实现不区分这两种情况，导致"专区售罄后按出票优先原则兜底"也被记成
    Tier 0 违规（代价 100000 分），让安全指标失去意义。Tier 0 应当只统计
    "系统本可以避免、却没有避免"的事故。
    """
    if not all_seats:
        return True  # 拿不到编组信息时按"还有空位"处理，宁可严格
    occupied_set = set(occupied)
    return any(
        seat.in_accessible_zone() and seat.seat_id not in occupied_set for seat in all_seats
    )


class Scorer:
    """代价评估器：无状态（除配置外），可安全复用与并发调用。"""

    def __init__(self, config: EngineConfig, accessible_available: bool = True) -> None:
        self.cfg = config
        self.accessible_available = accessible_available
        """无障碍专区是否还有可用座位（决定轮椅无专区的记法，见上方说明）。"""

    # ------------------------------------------------------------------
    # 单项代价：乘客 × 座位
    # ------------------------------------------------------------------
    def individual_cost(self, p: Passenger, seat: Seat) -> tuple[float, list[CostTerm]]:
        cfg = self.cfg
        cost = 0.0
        terms: list[CostTerm] = []

        def add(code: str, tier: int, value: float, detail: str = "") -> None:
            nonlocal cost
            if value == 0.0:
                return
            cost += value
            terms.append(CostTerm(code, tier, value, (p.passenger_id,), detail))

        # ---- Tier 0：硬约束设施 ----
        if p.is_mobility_impaired and not seat.in_accessible_zone():
            if self.accessible_available:
                # 专区还有空位却没给他：求解器失误，记为 Tier 0
                add(
                    WHEELCHAIR_NO_ZONE,
                    0,
                    cfg.t0_wheelchair_no_accessible,
                    f"座位 {seat.seat_id} 非无障碍专区（专区仍有空位）",
                )
            else:
                # 专区已售罄：这是运营例外（出票优先 + 站车协助），
                # 记一条**低层级**的服务条目，保证可解释性但不污染 Tier 0 指标。
                add(
                    WHEELCHAIR_ZONE_SOLD_OUT,
                    5,
                    cfg.b5_accessible_match,
                    f"座位 {seat.seat_id} 非无障碍专区（专区已售罄，已出票并转站车协助）",
                )

        # ---- Tier 3：高权重排斥 ----
        if seat.is_quiet_carriage:
            if p.quiet_carriage_blocked:
                add(
                    QUIET_CREDIT_BLOCKED,
                    3,
                    cfg.t3_quiet_blocked_credit,
                    f"静音信用分 {p.quietness_score:.0f} < 60，已屏蔽静音车厢",
                )
            if p.quiet_repulsion > 0:
                # 带娃/特殊群体进入静音车厢：这是本系统的招牌约束（README Tier 3）。
                # 该惩罚按**座位**计（与个体项口径一致），因此单元越好地避开静音车厢，
                # 总分越优；多人同时进入会被线性累加，方向正确。
                add(
                    QUIET_FAMILY,
                    3,
                    cfg.t3_quiet_family_base * p.quiet_repulsion,
                    f"静音车厢排斥权重 {p.quiet_repulsion:.2f}"
                    f"（{'婴儿' if p.is_infant else '儿童/特殊群体'}）",
                )
        if seat.in_accessible_zone() and not p.is_mobility_impaired:
            add(ACCESSIBLE_MISUSE, 3, cfg.t3_accessible_misuse, "无障碍专区被无需求旅客占用")

        # ---- Tier 5：设施精准匹配 ----
        reward = 0.0
        # 无障碍专区给予轮椅乘客的专属奖励：只有真正需要用的人才有资格拿，
        # 否则"挤进无障碍专区"会变成所有乘客的通用加分项。
        if seat.in_accessible_zone() and p.is_mobility_impaired:
            reward += cfg.b5_accessible_match
        if p.preference_aisle and seat.is_aisle:
            reward += cfg.b5_facility_match
        if p.preference_window and SeatFeature.WINDOW in seat.features:
            reward += cfg.b5_facility_match
        # 用户主动勾选静音车厢 —— 与下面的"合规性奖励"是两件事。
        # 若只写合规奖励不写这一条，界面上的勾选框就成了装饰：
        # 勾与不勾的下座完全一样（这是被实测抓出来的缺陷）。
        if (
            p.preference_quiet
            and seat.is_quiet_carriage
            and not p.quiet_carriage_blocked
        ):
            reward += cfg.t5_quiet_by_request
        if SupportNeed.INDEPENDENT_BLIND in p.support_needs:
            if seat.is_aisle:
                reward += cfg.b5_facility_match
            if SeatFeature.NEAR_DOOR in seat.features:
                reward += cfg.b5_facility_match
        if SupportNeed.PREGNANT_LATE in p.support_needs:
            if SeatFeature.NEAR_TOILET in seat.features:
                reward += cfg.b5_facility_match
            if seat.is_aisle:
                reward += cfg.b5_facility_match
        if p.age >= 70 and SeatFeature.NEAR_DOOR in seat.features:
            reward += cfg.b5_facility_match * 0.5
        if SupportNeed.ELDERLY in p.support_needs and seat.is_aisle:
            reward += cfg.b5_facility_match * 0.5
        if (
            seat.is_quiet_carriage
            and not p.is_child
            and not p.support_needs
            and not p.quiet_carriage_blocked
            and p.declared_behavior is not DeclaredBehavior.LIVELY
        ):
            reward += cfg.t5_quiet_solo_adult
        if reward > 0:
            # 地板：奖励低于阈值时不再发放，避免"多个小奖励叠加"把奖励碎片化
            # （碎片化奖励会让求解器为了凑分而牺牲真实的偏好匹配）。
            if reward < cfg.min_floor_reward:
                return cost, terms
            # 硬钳制：奖励总额不得超过 max_reward_per_passenger，
            # 确保"约束优先于偏好"永远成立（见 EngineConfig 注释）。
            reward = min(reward, cfg.max_reward_per_passenger)
            add(FACILITY_MATCH if reward < cfg.t5_quiet_solo_adult else QUIET_SOLO_ADULT,
                5, reward, f"设施匹配奖励 @ {seat.seat_id}")
        return cost, terms

    # ------------------------------------------------------------------
    # 关系代价：乘客对 × 座位对
    # ------------------------------------------------------------------
    def pair_cost(
        self,
        a: Passenger,
        seat_a: Seat,
        b: Passenger,
        seat_b: Seat,
        bond: BondType,
    ) -> tuple[float, list[CostTerm]]:
        cfg = self.cfg
        terms: list[CostTerm] = []
        cross_carriage = seat_a.carriage != seat_b.carriage
        distance = seat_a.manhattan_to(seat_b)

        # ---- Tier 0 + Tier 1：绑定 ----
        if bond is BondType.MANDATORY:
            if cross_carriage:
                # README Tier 0 的原文是"完全分离"：跨车厢即为灾难级。
                terms.append(
                    CostTerm(
                        BOND_SEPARATED, 0, cfg.t0_separated_care_bond,
                        (a.passenger_id, b.passenger_id),
                        f"{a.passenger_id} 在 {seat_a.seat_id}，{b.passenger_id} 在 {seat_b.seat_id}，跨车厢完全分离",
                    )
                )
            elif distance == 1:
                terms.append(
                    CostTerm(COMPANION_TOGETHER, 5, cfg.b5_companion_together,
                             (a.passenger_id, b.passenger_id), "照护绑定紧邻满足")
                )
            else:
                terms.append(
                    CostTerm(
                        CARE_BOND_SAME_CAR, 1, cfg.t1_care_bond_same_carriage,
                        (a.passenger_id, b.passenger_id),
                        f"照护绑定同车厢但未紧邻（相隔 {distance} 步）",
                    )
                )
        elif bond is BondType.STRONG:
            if cross_carriage:
                code = CARE_UNIT_SPLIT if a.is_vulnerable or b.is_vulnerable else ADULT_CROSS_CAR
                tier = 1 if code == CARE_UNIT_SPLIT else 2
                value = cfg.t1_care_unit_split_carriage if tier == 1 else cfg.t2_adult_cross_carriage
                terms.append(
                    CostTerm(code, tier, value, (a.passenger_id, b.passenger_id),
                             f"强绑定同行人被拆到 {seat_a.carriage} 车与 {seat_b.carriage} 车")
                )
            elif distance > 1:
                # 无法紧邻时给**就近梯度**，让求解器有动力把人往一起凑。
                # 没有梯度的话，"隔 1 排"与"隔 10 排"同样扣 −30 分，
                # 实测会把一家人分散到 3 排（01车01A / 01车02A / 01车03A）。
                row_gap = abs(seat_a.row - seat_b.row)
                if cfg.prefer_adjacent_for_weak_bonds and row_gap == 1:
                    terms.append(
                        CostTerm(COMPANION_TOGETHER, 5, cfg.b5_same_carriage_adjacent_row,
                                 (a.passenger_id, b.passenger_id),
                                 "同行人在相邻排（可现场调剂到一起）")
                    )
                elif cfg.prefer_adjacent_for_weak_bonds and row_gap == 0:
                    terms.append(
                        CostTerm(COMPANION_TOGETHER, 5, cfg.b5_near_row_together,
                                 (a.passenger_id, b.passenger_id), "同行人同排（中间隔座）")
                    )
                else:
                    terms.append(
                        CostTerm(NOT_TOGETHER, 4, cfg.t4_not_together, (a.passenger_id, b.passenger_id),
                                 "同车厢但未相邻")
                    )
            else:
                terms.append(
                    CostTerm(COMPANION_TOGETHER, 5, cfg.b5_companion_together,
                             (a.passenger_id, b.passenger_id), "同行人相邻")
                )
        else:  # SOFT
            if cross_carriage:
                vulnerable = a.is_vulnerable or b.is_vulnerable
                terms.append(
                    CostTerm(
                        VULNERABLE_CROSS_CAR if vulnerable else ADULT_CROSS_CAR,
                        1 if vulnerable else 2,
                        cfg.t1_vulnerable_cross_carriage if vulnerable else cfg.t2_adult_cross_carriage,
                        (a.passenger_id, b.passenger_id),
                        f"同订单人员被拆分到 {seat_a.carriage} 车与 {seat_b.carriage} 车",
                    )
                )
            elif self._aisle_separated(seat_a, seat_b):
                terms.append(
                    CostTerm(AISLE_SEPARATED, 4, cfg.t4_aisle_separated,
                             (a.passenger_id, b.passenger_id), "被过道分隔")
                )
            elif seat_a.row == seat_b.row and abs(seat_a.col_index - seat_b.col_index) == 1:
                terms.append(
                    CostTerm(COMPANION_TOGETHER, 5, cfg.b5_companion_together * 0.5,
                             (a.passenger_id, b.passenger_id), "同排紧邻")
                )

        # ---- Tier 1：特殊群体被分配至静音车厢（冲突隔离）----
        # 只对"硬冲突"群体生效：轮椅、独立视障、智力障碍。带娃家庭进入静音车厢
        # 属于 Tier 3 的高权重排斥（-2500 量级），若也按 Tier 1（-10000）计，
        # 会一举压垮其他所有比较、让代价函数失去分辨力。
        for p, seat in ((a, seat_a), (b, seat_b)):
            if seat.is_quiet_carriage and p.support_needs & _QUIET_CONFLICT_NEEDS:
                terms.append(
                    CostTerm(QUIET_CONFLICT, 1, cfg.t1_conflict_isolation, (p.passenger_id,),
                             f"{p.passenger_id} 属冲突群体却被分配至静音车厢")
                )
                break
        return sum(t.value for t in terms), terms

    @staticmethod
    def _aisle_separated(seat_a: Seat, seat_b: Seat) -> bool:
        if seat_a.carriage != seat_b.carriage or seat_a.row != seat_b.row:
            return False
        low, high = sorted((seat_a.col_index, seat_b.col_index))
        return (high - low) == 2 and not seat_a.is_aisle and not seat_b.is_aisle

    # ------------------------------------------------------------------
    # 可解释性：把 Violation 渲染成中文说明
    # ------------------------------------------------------------------
    @staticmethod
    def explain(violations: list[Violation]) -> str:
        """生成客服可直接使用的中文解释（README 6.2）。"""
        if not violations:
            return "全部软硬约束均已满足，未产生任何代价条目。"
        # Tier 数字越小越严重；同 Tier 内惩罚（负）排在奖励（正）之前
        ordered = sorted(violations, key=lambda v: (v.tier, v.affinity))
        total_cost = sum(v.penalty for v in ordered if not v.is_reward)
        rewards = sum(v.penalty for v in ordered if v.is_reward)
        worst = ordered[0]
        parts = [
            f"全局代价 {total_cost:.0f} 分（奖励抵扣 {rewards:.0f} 分），共 {len(ordered)} 条条目；"
            f"最严重为 Tier {worst.tier}（{worst.code}，{worst.penalty:.0f} 分）：{worst.detail}。"
        ]
        for v in ordered[1:5]:
            tag = "奖励" if v.is_reward else "惩罚"
            parts.append(f"Tier {v.tier} {v.code}（{tag} {v.penalty:.0f} 分）：{v.detail}")
        return " ".join(parts)


def split_units(order: Order, config: EngineConfig | None = None) -> tuple[PassengerUnit, ...]:
    """把订单拆解为"硬核单元"与"软性单元"（README 4.2 步骤 2）。

    * 含需照护乘客（儿童/婴儿/智力障碍/孕晚期）的连通分量 -> ``MANDATORY``
    * 尺寸 > 1 的其余连通分量                              -> ``STRONG``
    * 落单乘客                                            -> ``SOFT``
    """
    if order.units:
        return order.units
    ids = [p.passenger_id for p in order.passengers]
    parent: dict[str, str] = {pid: pid for pid in ids}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: str, y: str) -> None:
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[ry] = rx

    for a, b in combinations(ids, 2):
        bond = order.bond_of(a, b)
        if bond in (BondType.MANDATORY, BondType.STRONG):
            union(a, b)

    groups: dict[str, list[Passenger]] = {}
    for p in order.passengers:
        groups.setdefault(find(p.passenger_id), []).append(p)

    units: list[PassengerUnit] = []
    for index, (_, members) in enumerate(sorted(groups.items())):
        members.sort(key=lambda p: (not p.is_caregiver, p.passenger_id))
        has_hard = any(
            order.bond_of(a.passenger_id, b.passenger_id) is BondType.MANDATORY
            for a, b in combinations(members, 2)
        )
        if has_hard:
            bond = BondType.MANDATORY
        elif len(members) > 1:
            bond = BondType.STRONG
        else:
            bond = BondType.SOFT
        units.append(
            PassengerUnit(
                unit_id=f"{order.order_id}#U{index}",
                passengers=tuple(members),
                bond=bond,
                relation=order.relation,
            )
        )
    return tuple(units)
