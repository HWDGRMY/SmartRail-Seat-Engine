"""分配求解器：余票分块 + 图匹配 + 分支限界（V1.0 启发式引擎）。

三种能力
--------
* :func:`assign_greedy`  —— 模式三（降级）：内存级贪心发牌，**但保留 Tier 0
  绑定硬过滤器**。这是本项目对"降级不降底线"的工程承诺：无论算力多紧张，
  "儿童与照护人不分离"这条底线永远由过滤器（而非代价）保证。
* :func:`assign_exact`   —— 模式二：以贪心解为初始上界做分支限界，在时间/节点
  预算内求全局最优；预算耗尽则返回当前最优（保证可用的启发式解）。
* :func:`solve`          —— 统一入口，按模式分派并输出可解释性报告。

性能设计（对应 README 6.1 的 5-10ms 目标）
------------------------------------------
打分被拆成"预计算查找表 + 零分配内层循环"：

1. ``indiv[乘客][座位]`` 单项亲和度矩阵只算一次（O(N·S)）；
2. 成对项按 (乘客对, 座位对) 惰性缓存，命中即 O(1)；
3. 候选池按"单车厢 + 个体分预筛"控制规模，避免 1156 座全枚举。

代价口径：内部统一使用 **affinity（越大越优：惩罚为负、奖励为正）**；
对外报告时由 :class:`smartrail.models.Solution` 翻转为"代价分"。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from itertools import combinations, permutations
from typing import Collection, Iterable, Sequence

_DEBUG_SOLVER = bool(os.environ.get("SMARTRAIL_SOLVER_DEBUG"))
"""打开后打印每个单元的候选池与退化路径（排查"绕过硬约束"类问题用）。"""

ZONE_QUOTA_RATIO = 0.5
"""单个订单最多占用无障碍专区的比例。

专区只有 10 座时，若允许一单吃光，后面到达的轮椅旅客就完全无座可安排。
按一半配额，专区至少能服务两个批次，也更贴近"专区要留给随时可能出现的
重点旅客"这一现实运营原则。
"""

_MIXED_SEARCH_CAP = 400
"""构造"轮椅进专区 + 同行人同车厢"组合时的枚举上限。

这个上限是**必须的**，不是优化：`combinations(zone, k) × combinations(others, n)`
是笛卡尔积，实测"4 位轮椅 + 4 位家属"时组合数接近 20 万，会让候选池构造
无限期卡住（HTTP 请求直接超时）。上限只影响"挑哪个组合"，不影响可行性 ——
拿不到最优组合时外层还有代价评估与兜底路径。
"""

from .clustering import BookingState
from .config import EngineConfig
from .fastscore import FastScorer
from .models import (
    Assignment,
    BondType,
    Order,
    Passenger,
    PassengerUnit,
    Seat,
    Solution,
    SupportNeed,
    Violation,
    seat_key,
)
from .scoring import (
    CARE_ADJACENT_TOLERANCE,
    CHILD_ADJACENT,
    ISOLATED_CARE,
    PREGNANT_ALONE,
    QUIET_GROUP_OVERUSE,
    CostTerm,
    Scorer,
    accessible_zone_has_free_seat,
    is_care_dependent,
    is_wheelchair_seat,
    split_units,
    support_people_of,
    wheelchair_bay_slot_ids,
)


@dataclass
class OrderContext:
    """一次求解所需的全部上下文。

    ``care_dependent`` / ``helpers`` 在构造时算好：这两个判据在热路径与
    可解释性路径里都会被反复查询，每次现算都要走 :meth:`Order.caregivers_of`
    的字典扫描（实测占家庭场景约 8% 时间）。
    """

    order: Order
    units: tuple[PassengerUnit, ...]
    passengers: dict[str, Passenger]
    unit_of: dict[str, PassengerUnit]
    cross_bonds: dict[frozenset[str], BondType]
    care_dependent: dict[str, bool] = field(default_factory=dict)
    helpers: dict[str, frozenset[str]] = field(default_factory=dict)
    all_seats: tuple[Seat, ...] = ()
    """本编组的**全部**座位（含已占），供"是否还有相邻空位可调剂"判断使用。

    为什么需要全部座位而不是仅空位：要判断"车厢里还有没有相邻的两个空位"，
    必须能把空位与已占位一起比较；只给空位列表无法回答这个问题。
    """
    occupied_seats: frozenset[str] = frozenset()
    """本单开始前**已被占用**的座位（不含本单自己发的牌）。

    与 ``all_seats`` 配合才能算出"当前还剩哪些相邻空位"，从而把提示区分为
    "车内还有相邻空位，可现场调剂"与"本车厢已无相邻空位，请找列车员"。
    """


def build_context(
    order: Order,
    config: EngineConfig | None = None,
    all_seats: Sequence[Seat] | None = None,
    occupied_seats: Iterable[str] | None = None,
) -> OrderContext:
    """构造求解上下文。

    ``all_seats`` / ``occupied_seats`` 用于判断"车厢里是否还剩相邻的两个空位"，
    从而把提示区分为"车内可现场调剂"或"已无相邻空位，请找列车员"。
    不传时该判断退化为保守措辞，不影响出票行为。
    """
    units = split_units(order, config)
    passengers = {p.passenger_id: p for p in order.passengers}
    unit_of: dict[str, PassengerUnit] = {}
    for unit in units:
        for p in unit.passengers:
            unit_of[p.passenger_id] = unit
    cross_bonds: dict[frozenset[str], BondType] = {}
    for a, b in combinations(order.passengers, 2):
        if unit_of[a.passenger_id] is not unit_of[b.passenger_id]:
            cross_bonds[frozenset((a.passenger_id, b.passenger_id))] = order.bond_of(
                a.passenger_id, b.passenger_id
            )
    return OrderContext(
        order=order,
        units=units,
        passengers=passengers,
        unit_of=unit_of,
        cross_bonds=cross_bonds,
        care_dependent={pid: is_care_dependent(p, order) for pid, p in passengers.items()},
        helpers={pid: support_people_of(p, order) for pid, p in passengers.items()},
        all_seats=tuple(all_seats) if all_seats is not None else (),
        occupied_seats=frozenset(occupied_seats or ()),
    )


# ---------------------------------------------------------------------------
# 查找表与候选池
# ---------------------------------------------------------------------------


class SolverTables:
    """预计算查找表 + 与代价口径一致的增量打分（零对象分配）。"""

    def __init__(self, ctx: OrderContext, state: BookingState, config: EngineConfig) -> None:
        self.ctx = ctx
        self.cfg = config
        seats = list(state.available_seats)
        # **席别硬约束**：旅客不能坐到自己没买的席别上。
        # 在候选池这一层过滤（而不是罚分），因为罚分只影响"选哪个"，
        # 候选池才决定"能不能选"。
        wanted_class = getattr(ctx.order, "class_code", "") or ""
        if wanted_class:
            seats = [seat for seat in seats if seat.class_code == wanted_class]
        self.seats: list[Seat] = seats
        self.wanted_class: str = wanted_class
        #: 轮椅停放位的账目座位槽（全列 4 个）。空集合表示"该编组没有停放位信息"，
        #: 此时轮椅判定退回旧的"无障碍专区"口径（见 is_wheelchair_seat）。
        self.bay_slot_ids: frozenset[str] = wheelchair_bay_slot_ids(
            getattr(state, "formation", None)
        )
        self.slot = {s.seat_id: i for i, s in enumerate(self.seats)}
        self.by_carriage: dict[int, list[int]] = {}
        for i, seat in enumerate(self.seats):
            self.by_carriage.setdefault(seat.carriage, []).append(i)
        self.by_block: dict[tuple[int, int], list[int]] = {}
        for i, seat in enumerate(self.seats):
            self.by_block.setdefault((seat.carriage, seat.row), []).append(i)
        for key, group in self.by_block.items():
            self.by_block[key] = sorted(group, key=lambda i: self.seats[i].col_index)
        self.fs = FastScorer(
            ctx.order, ctx.passengers, self.seats, config, ctx.unit_of,
            bay_slot_ids=self.bay_slot_ids,
        )
        # 复用上下文里算好的判据，避免同一份数据在两张表里各存一份
        self.care_dependent = ctx.care_dependent
        self.helpers = ctx.helpers
        self._unit_cache: dict[tuple[object, ...], list[tuple[tuple[int, ...], float]]] = {}
        self._dist: dict[tuple[int, int], int] = {}
        # 座位表指纹：候选池是按"构造时的可用座位"算出来的，因此**必须**参与缓存键。
        # 真实事故：缓存键原先只有 unit.ids，于是"座位更多时算出的候选"会被
        # "座位更少时的求解"复用，导致设施硬约束（轮椅无障碍、低信用避静音）
        # 被整体绕过 —— 表现为"低信用旅客照样坐进静音车厢"，而候选池检查却显示
        # 过滤正常（因为检查用的是一个新实例）。
        self._seat_fingerprint = (
            len(self.seats),
            self.seats[0].seat_id if self.seats else "",
            self.seats[-1].seat_id if self.seats else "",
        )

    # -- 单元内关系（含安全底线，口径与权威 Scorer 一致） -----------------
    def unit_bonus(self, pairs: Sequence[tuple[Passenger, int]]) -> float:
        """单元级加成/惩罚：静音车厢团体占用 + 需照护者安全底线。

        刻意**不做缓存**：同一组合会被反复评估，但缓存键需要对每一对做
        O(k·log k) 的哈希计算，比直接算判据更贵（实测是负优化）。
        真正的热点（座位距离）已由 :meth:`_distance` 的矩阵缓存解决。
        """
        cfg = self.cfg
        total = 0.0
        if len(pairs) > 1 and self.seats[pairs[0][1]].is_quiet_carriage:
            total += cfg.t3_quiet_group_extra * (len(pairs) - 1)
        for passenger, seat_slot in pairs:
            if not self.care_dependent.get(passenger.passenger_id, False):
                continue
            helpers = self.helpers.get(passenger.passenger_id, frozenset())
            if not helpers:
                continue
            if any(
                other.passenger_id != passenger.passenger_id
                and other.passenger_id in helpers
                and self._distance(seat_slot, other_slot) <= CARE_ADJACENT_TOLERANCE
                for other, other_slot in pairs
            ):
                total += cfg.b5_child_adjacent
            else:
                total += cfg.t1_isolated_care_member
        return total

    def _distance(self, slot_a: int, slot_b: int) -> int:
        """座位槽之间的曼哈顿距离（按需缓存，避免反复调用 Seat.manhattan_to）。"""
        if slot_a > slot_b:
            slot_a, slot_b = slot_b, slot_a
        key = (slot_a, slot_b)
        value = self._dist.get(key)
        if value is None:
            value = self.seats[slot_a].manhattan_to(self.seats[slot_b])
            self._dist[key] = value
        return value

    def local_affinity(self, pairs: Sequence[tuple[Passenger, int]]) -> float:
        """一个单元内部（不含与已放置乘客的关系）的亲和度。"""
        fs = self.fs
        total = 0.0
        for passenger, seat_slot in pairs:
            total += fs.row(passenger.passenger_id)[seat_slot]
        for (pa, ia), (pb, ib) in combinations(list(pairs), 2):
            total += fs.pair(pa.passenger_id, pb.passenger_id, ia, ib)
        return total + self.unit_bonus(pairs)

    def incremental(
        self,
        pairs: Sequence[tuple[Passenger, int]],
        placed: dict[str, int],
    ) -> float:
        """把单元放到指定座位、并考虑已放置乘客后的**增量**亲和度。"""
        fs = self.fs
        ctx = self.ctx
        total = self.local_affinity(pairs)
        for passenger, seat_slot in pairs:
            unit = ctx.unit_of.get(passenger.passenger_id)
            for other_id, other_slot in placed.items():
                if ctx.unit_of.get(other_id) is unit:
                    continue  # 单元内部已计入
                total += fs.pair(passenger.passenger_id, other_id, seat_slot, other_slot)
        return total

    # -- 候选池 -----------------------------------------------------------
    def candidates(
        self, unit: PassengerUnit, max_per_carriage: int | None = None
    ) -> list[tuple[tuple[int, ...], float]]:
        """生成单元候选（(座位槽, 个体分估计)），按个体分降序、按车厢限额。

        三层退化，保证任何规模的单元都有候选可用：

        1. 排内连续窗口（真正的"连座"）；
        2. 同厢小规模组合（≤4 人，处理"3 人塞 2+1"）；
        3. 兜底：整厢任意组合（≥5 人单元，窗口凑不齐时使用）。
        """
        key = (unit.ids, self._seat_fingerprint)
        cached = self._unit_cache.get(key)
        if cached is not None:
            return cached
        size = unit.size
        fs = self.fs
        passenger_ids = [p.passenger_id for p in unit.passengers]
        # 每节车厢保留的候选数：同一车厢内部候选质量差异很小（设施标签基本相同，
        # 个体分只区分座位类型），取前几个即可。这一步直接决定决策时延。
        if max_per_carriage is None:
            max_per_carriage = {1: 5, 2: 6, 3: 5, 4: 4}.get(size, 4)

        if size == 1:
            pid = passenger_ids[0]
            row = fs.row(pid)
            out = [((slot,), row[slot]) for slot in range(len(self.seats))]
            out.sort(key=lambda item: -item[1])
            # 单人单元只保留个体分最高的若干座位：1156 个座位里真正带奖励的
            # 只有少数（静音/无障碍/过道/近门），其余全为 0 分且彼此等价。
            out = out[:60]
            out = self._merge_required_combos(unit, out)
            out = self._enforce_facility_constraints(unit, out)
            if _DEBUG_SOLVER:
                quiet_before = sum(
                    1 for combo, _v in out if self.seats[combo[0]].is_quiet_carriage
                )
                print(
                    f"      [cand] {pid} 静音屏蔽={self.ctx.passengers[pid].quiet_carriage_blocked} "
                    f"过滤后候选={len(out)} 其中静音={quiet_before} "
                    f"首项={self.seats[out[0][0][0]].seat_id}"
                )
            self._unit_cache[key] = out
            return out
        scored: list[tuple[tuple[int, ...], float]] = []
        # "紧凑"候选：每种 (占用排数, 排号跨度, 车厢) 的代表。
        #
        # 为什么要单独留配额：最终有 global_cap 截断，而截断按**个体分**排序 ——
        # 紧凑组合的个体分往往是 0（普通座位没有奖励），于是它们全部排在
        # 末尾被截掉。实测余票 156 时全列唯一的整排 `10车09B/C/D/F`
        # 就是这样进的候选池又被丢掉的，用户看到一家人被撒在三排上。
        compact: list[tuple[tuple[int, ...], float]] = []
        fallback: list[tuple[tuple[int, ...], float]] = []
        for carriage, slots in self.by_carriage.items():
            combos = self._carriage_combos(unit, carriage)
            if not combos and size >= 5 and len(slots) >= size:
                # **不能对整节车厢做 combinations(slots, size)**：
                # 85 个座位取 8 个是 C(85,8) ≈ 3×10¹⁰ 种，实测直接卡死
                # （HTTP 请求超时那种死法，不是"慢"）。原先写的"凑够 20 个就
                # break"在这里完全无效 —— 因为前 20 个合法组合可能要遍历上亿次。
                #
                # 正确做法：先按乘客个体分排序缩小到"每排最好的几个座位"，
                # 再在小集合上取组合。这样既保留"同车厢任意组合"的表达力，
                # 又把枚举量压到可控范围。
                trimmed = self._trimmed_slots(unit, slots)
                if len(trimmed) >= size:
                    for combo in combinations(trimmed, size):
                        if self._facility_ok(unit, combo):
                            combos.append(combo)
                        if len(combos) >= 20:
                            break
            if not combos:
                continue
            est: list[tuple[float, tuple[int, ...]]] = []
            for combo in combos:
                value = 0.0
                for pid, seat_slot in zip(passenger_ids, combo):
                    value += fs.row(pid)[seat_slot]
                est.append((value, combo))
            # **"坐在一起"必须是排序的一等公民。**
            #
            # 这里的 value 只是"个体分之和" —— 它**完全不反映乘客之间的
            # 距离**。而每个车厢只保留 max_per_carriage（4 人单元仅 4 个）
            # 个候选，于是"分成两排"与"挤在一排"按同一个分数竞争，
            # 前者往往因为能挑到更高个体分的散座而胜出。
            #
            # 实测后果：2 成人 + 2 儿童（一个 mandatory 单元）在余票碎片化时
            # **20 单里 19 单被拆到不同排**，用户直接看出"你把大人小孩分开了"。
            #
            # 修正：先按**占用排数**升序（越少越好）、再按**排号跨度**
            # 升序（跨排时要相邻），最后才按个体分降序。
            # 这样"必须同车厢"的硬约束才有实际可用的候选 —— 约束能不能满足
            # 取决于候选池里有没有对应的组合，而不是取决于罚分多大。
            # 按"先紧凑、再高分"排序：这条顺序同时用于**挑选名额**。
            # 用个体分排序去挑名额是错的 —— 紧凑组合的个体分最低，
            # 排在最后，正好被 max_per_carriage 截掉（实测余票 156 时
            # 全列唯一的整排 10车09B/C/D/F 就是这样被丢掉的）。
            est.sort(key=lambda item: (*_row_key(self.seats, item[1]), -item[0]))
            target = scored if len(slots) >= size else fallback
            # **每节车厢都要给"最紧凑"的组合留位置。**
            #
            # 只取前 max_per_carriage 个（4 人单元仅 4 个）会让池子里
            # "同排"候选被**排在前面的车厢**占满 —— 01 车有 8 个连续空排，
            # 前两个就把名额吃光了，别的车厢的整排一个都进不来。
            # 于是求解器只能在"跨 8 排的散座"里挑，用户看到一家人被撒开。
            #
            # 所以按 (row_key, 车厢) 分别保留，保证每节车厢的各种紧凑度
            # 都有代表。候选池总大小不变（受 global_cap 约束）。
            per_key: dict[tuple[tuple[int, int], int], int] = {}
            reserved: list[tuple[float, tuple[int, ...]]] = []
            filler: list[tuple[float, tuple[int, ...]]] = []
            for value, combo in est:
                key = (_row_key(self.seats, combo),
                       self.seats[combo[0]].carriage)
                if per_key.get(key, 0) < 2:
                    per_key[key] = per_key.get(key, 0) + 1
                    reserved.append((value, combo))
                else:
                    filler.append((value, combo))
            merged = reserved + filler[: max(0, max_per_carriage - len(reserved))]
            target.extend((combo, value) for value, combo in merged)
            if target is scored:
                # 只把**每种紧凑度 × 每节车厢的代表**放进 compact。
                #
                # 不能把整个 reserved（可达 23 个/车厢）塞进去：
                # 每节车厢都塞 23 个的话，compact 里前 24 个全被 01 车占满，
                # 后面的车厢一个都进不了最终池子 —— 实测"全列唯一整排"
                # 10车09B/C/D/F 就是这样被挤掉的。
                com_seen: set[tuple[int, int]] = set()
                for value, combo in est:
                    key = (_row_key(self.seats, combo),
                           self.seats[combo[0]].carriage)
                    if key in com_seen:
                        continue
                    com_seen.add(key)
                    compact.append((combo, value))
        scored.sort(key=lambda item: -item[1])
        # compact 按 **紧凑度优先** 排序，而不是按收集顺序。
        #
        # 收集时是"逐车厢堆叠"的，同一车厢的条目连在一起：实测 compact 里
        # (1,0) 落在索引 0 / 84 / 94，而名额只有 24 个 —— 于是只有第一节
        # 车厢的一整排进得了池子，**全列唯一的整排** `10车09B/C/D/F`
        # （索引 94）永远被挤掉，用户看到一家人被撒在三排上。
        # 按 (占用排数, 排号跨度, -个体分) 排序后，所有车厢的最紧凑组合
        # 都排在前面，名额怎么切都轮得到。
        compact.sort(key=lambda item: (*_row_key(self.seats, item[0]), -item[1]))
        # 全局上限：大单元（≥5 人）每次评估成本高，池子必须更小，
        # 否则 16 节编组 × 每厢多个候选会让单次决策退化到秒级。
        #
        # **配额分配**：先在紧凑候选与高分候选之间五五开，再按分值补齐。
        # 这样"坐在一起"的方案不会被"个体分更高但把人撒开"的方案挤光 ——
        # 约束能不能满足取决于候选池里有没有对应组合，而不是罚分多大。
        global_cap = {1: 60, 2: 72, 3: 60, 4: 48}.get(size, 32)
        base_pool = scored if scored else []
        half = max(1, global_cap // 2)
        chosen: list[tuple[tuple[int, ...], float]] = []
        seen_combos: set[tuple[int, ...]] = set()
        for combo, value in compact[:half]:
            if combo not in seen_combos:
                seen_combos.add(combo)
                chosen.append((combo, value))
        for combo, value in base_pool:
            if len(chosen) >= global_cap:
                break
            if combo not in seen_combos:
                seen_combos.add(combo)
                chosen.append((combo, value))
        # 还有空位就用剩余的紧凑候选补满（大车厢多的场景）
        for combo, value in compact:
            if len(chosen) >= global_cap:
                break
            if combo not in seen_combos:
                seen_combos.add(combo)
                chosen.append((combo, value))
        out = chosen if chosen else fallback[:global_cap]
        out = self._merge_required_combos(unit, out)
        # 再兜一层：任何出口都必须满足设施硬约束。
        # 不同路径（排内窗口 / size>=5 兜底 / 跨厢）各自过滤容易漏，统一在这里收口。
        out = self._enforce_facility_constraints(unit, out)
        if not out:
            # 最后兜底：**按人构造**一个组合。
            # 为什么必须有这一层：`_carriage_combos` 的第一层是"整排连续窗口"，
            # 而二等座一排只有 5 座 —— 8 人单元**永远**窗口不出来，于是候选池为空。
            # 空候选池会一路退化成"整单候补"，即使全车还有 895 个空座（实测）。
            out = self._compose_fallback_combos(unit)
        self._unit_cache[key] = out
        return out

    def _compose_fallback_combos(
        self, unit: PassengerUnit
    ) -> list[tuple[tuple[int, ...], float]]:
        """按**每位乘客的需求**直接拼出同车厢组合（最后兜底）。

        规则：**先给需要用停放位的人留位**（轮椅旅客 -> 停放位槽），
        再用每位乘客个体分最高的座位补齐其余人。

        为什么必须"先给轮椅留位"而不是"轮到谁就按分数选"
        --------------------------------------------------
        早期实现按 ``unit.passengers`` 的**顺序**依次挑选：轮到轮椅旅客时才去
        停放位里挑。真实事故：一张单是「照护人 + 成人 + 轮椅旅客」，
        轮椅排在最后，前两位已经把同车厢的位置挑走 —— 结果**一个含停放位的
        组合都没进候选池**，轮椅旅客被发到普通座位并记 Tier 0 违规。
        而 Tier 0 在本项目里代表"求解器失误"，这个判断是对的：
        停车位当时空着 4 个，系统本可以安排。

        候选池决定"能不能选"，代价只决定"选哪个" —— 这是本项目反复
        吃到的同一个教训，所以留位必须发生在**拼组合的阶段**。
        """
        size = unit.size
        if size == 0 or size > len(self.seats):
            return []
        wheel_slots = [
            i for i, seat in enumerate(self.seats)
            if is_wheelchair_seat(seat, self.bay_slot_ids)
        ]
        out: list[tuple[tuple[int, ...], float]] = []
        for carriage, slots in self.by_carriage.items():
            if len(slots) < size:
                continue
            slot_set = set(slots)
            # 先用本车厢里可用的停放位安置需要停放位的人，剩下的再按分数补齐
            taken: set[int] = set()
            combo: list[int] = []
            for passenger in unit.passengers:
                if passenger.is_mobility_impaired:
                    pool = [s for s in wheel_slots
                            if s in slot_set and s not in taken]
                    if not pool:
                        # 本车厢没有空停放位：退到"出票优先"，发普通座位
                        pool = [s for s in slots if s not in taken]
                else:
                    pool = [s for s in slots if s not in taken]
                if not pool:
                    break
                row = self.fs.row(passenger.passenger_id)
                pick = max(pool, key=lambda slot: row[slot])
                taken.add(pick)
                combo.append(pick)
            if len(combo) != size:
                continue
            value = sum(
                self.fs.row(pid)[slot]
                for pid, slot in zip([p.passenger_id for p in unit.passengers], combo)
            )
            out.append((tuple(combo), value))
        out.sort(key=lambda item: -item[1])
        return out[:8]

    def _merge_required_combos(
        self, unit: PassengerUnit, out: list[tuple[tuple[int, ...], float]]
    ) -> list[tuple[tuple[int, ...], float]]:
        """把"必须有机会被选中"的组合并进候选池（按需）。

        目前只有一种：**含轮椅停放位的组合**。

        为什么必须显式并进来（真实事故）
        --------------------------------
        单元是「照护人 + 成人 + 轮椅旅客」时，轮椅排在最后。候选池按
        "个体分之和"排序取前 N 个，而停放位槽的个体分只比普通座位高 40 分 ——
        前两位乘客的高分座位（静音/过道等）足以把含停放位的组合挤出前 60 名。
        于是 :meth:`_enforce_facility_constraints` 拿到的池子里**一个合法组合都没有**，
        轮椅旅客被发到普通座位并记 Tier 0 违规。

        Tier 0 的判断是对的：当时 4 个停放位全空，系统本可以安排。
        这是本项目第四次吃到同一个教训 —— **候选池决定"能不能选"，
        代价只决定"选哪个"**。凡是"必须满足"的约束，都要在候选池里留位置，
        不能指望它靠分数自己冒头。
        """
        if not any(p.is_mobility_impaired for p in unit.passengers):
            return out
        wheel_slots = frozenset(
            i for i, seat in enumerate(self.seats)
            if is_wheelchair_seat(seat, self.bay_slot_ids)
        )
        if not wheel_slots:
            return out
        if any(set(combo) & wheel_slots for combo, _ in out):
            return out   # 池子里已经有含停放位的组合，不必补充
        extra = [
            (combo, value)
            for combo, value in self._compose_fallback_combos(unit)
            if set(combo) & wheel_slots
        ]
        if not extra:
            return out
        seen = {combo for combo, _ in out}
        merged = list(out) + [item for item in extra if item[0] not in seen]
        merged.sort(key=lambda item: -item[1])
        return merged

    def _enforce_facility_constraints(
        self, unit: PassengerUnit, combos: list[tuple[tuple[int, ...], float]]
    ) -> list[tuple[tuple[int, ...], float]]:
        """把**设施硬约束**做成结构性排除，而不是靠罚分。

        覆盖两条约束：

        1. **轮椅旅客必须坐无障碍专区**；
        2. **静音信用分过低的旅客不得进入静音车厢**（信用体系的执行点）。

        为什么必须结构性排除（真实事故）
        --------------------------------
        ``FastScorer.row``（策略与候选池用的个体分）对轮椅旅客**只有** "+40 无障碍
        匹配"奖励，**没有** Tier 0 的 −100000 惩罚（那个在
        :meth:`Scorer.individual_cost` 里）。于是候选池会把普通座位排在前面，
        轮椅旅客被发到普通车厢。

        本项目反复出现的同一类问题：**罚分只能影响"选哪个"，只有候选池才能
        决定"能不能选"**。

        「轮椅 + 家属」的语义修正（重要）
        --------------------------------
        早期实现要求**全员都进无障碍专区**才成组合，直到专区被占满时，整张
        "轮椅旅客 + 家属"订单被全部拒票 —— 实测在空车、900 多个空座的编组上
        出现 `0/3 人 · 未出票`，与"出票优先"原则直接冲突。

        正确语义是：**只有轮椅本人的座位受专区约束**，家属坐在同一车厢即可
        （照护绑定本就是"同车厢"级要求）。因此这里把"混合组合"作为**始终可选**
        的候选，交给权威评估去挑最优 —— 专区充裕时它自然选"全家都在专区"，
        专区紧张时它退化为"轮椅进专区、家属同车厢"，而不是拒票。
        """
        pool = list(combos)
        if any(p.is_mobility_impaired for p in unit.passengers):
            pool = self._mixed_zone_combos(unit, combos) + pool
        legal = [
            item
            for item in pool
            if self._facility_violations(unit, [item]) == (0, 0)
        ]
        if legal:
            legal.sort(key=lambda item: -item[1])
            return legal

        # 一条合规候选都没有。判据必须按**本单元的需求量**算，而不是
        # "全车专区是否售罄" —— 这两者在多车厢编组下经常不一致：
        # 专区在 1 车还剩几个空位，但本单元有 4 位轮椅旅客，依然坐不下，
        # 此时若按"全车未售罄"处理就会整单候补（实测 85 个空座的车厢上
        # 8 人订单全部候补）。正确判据是：
        #
        #   本单元需要的轮椅座位数 > 当前可用专区分量
        #     -> 按"出票优先"发普通座位 + 生成站车协助提示
        #   否则（专区够，只是人多坐不下）
        #     -> 退回原集，把专区留给别的轮椅旅客
        if self._zone_short_for(unit):
            return pool
        return combos

    def _zone_short_for(self, unit: PassengerUnit) -> bool:
        """本单元需要的轮椅座位数是否超过当前可用的无障碍专区分量。"""
        needed = sum(1 for p in unit.passengers if p.is_mobility_impaired)
        if needed == 0:
            return False
        available = sum(1 for seat in self.seats if seat.in_accessible_zone())
        return available < needed

    def _facility_violations(
        self, unit: PassengerUnit, combos: list[tuple[tuple[int, ...], float]]
    ) -> tuple[int, int]:
        """统计候选集中违反各条设施硬约束的数量（0/0 表示全部合规）。"""
        zone_bad = 0
        quiet_bad = 0
        for item in combos:
            combo = item[0]
            if not isinstance(combo, tuple):
                continue
            for passenger, slot in zip(unit.passengers, combo):
                if not isinstance(slot, int):
                    continue
                seat = self.seats[slot]
                if passenger.is_mobility_impaired and not is_wheelchair_seat(
                    seat, self.bay_slot_ids
                ):
                    zone_bad += 1
                    break
            for passenger, slot in zip(unit.passengers, combo):
                if not isinstance(slot, int):
                    continue
                if self.seats[slot].is_quiet_carriage and passenger.quiet_carriage_blocked:
                    quiet_bad += 1
                    break
        return zone_bad, quiet_bad

    def _mixed_zone_combos(
        self, unit: PassengerUnit, combos: list[tuple[tuple[int, ...], float]]
    ) -> list[tuple[tuple[int, ...], float]]:
        """构造 **轮椅进专区 + 同行人坐同一车厢** 的组合。

        与"全员进专区"的区别：这里只把**轮椅旅客本人**放进无障碍专区，
        其余成员填同一车厢的普通座位。

        专区配额（重要的运营修正）
        --------------------------
        无障碍专区只有 10 座，若允许单个订单把专区吃光，后面到达的轮椅旅客
        就完全无座可安排 —— 实测在**空车**并发时出现过
        `轮椅旅客 + 家属 0/3 人 · 未出票`，而全车还有 862 个空座。

        因此引入**每单专区配额**：单个订单最多占用专区的一半
        （``zone_quota_ratio = 0.5``）。这让专区能服务更多批次的轮椅旅客，
        也更贴近现实（专区座位本就该留给"随时可能出现的"重点旅客）。
        """
        wheelchair_count = sum(1 for p in unit.passengers if p.is_mobility_impaired)
        if wheelchair_count == 0:
            return []
        zone_slots_all = [
            slot for slot, seat in enumerate(self.seats) if seat.in_accessible_zone()
        ]
        if len(zone_slots_all) < wheelchair_count:
            return []
        quota = max(wheelchair_count, int(len(zone_slots_all) * ZONE_QUOTA_RATIO))
        size = unit.size
        out: list[tuple[tuple[int, ...], float]] = []
        for carriage, slots in self.by_carriage.items():
            slot_set = set(slots)
            zone_here = [s for s in zone_slots_all if s in slot_set][:quota]
            if len(zone_here) < wheelchair_count:
                continue
            others = [s for s in slots if s not in set(zone_here)]
            need_others = size - wheelchair_count
            if len(others) < need_others:
                continue
            best_value = -1e18
            best_combo: tuple[int, ...] | None = None
            # **必须限定搜索规模**：``combinations(zone, k) × combinations(others, n)``
            # 是笛卡尔积，实测"4 位轮椅 + 4 位家属"时 zone 有 10 个座位、others 有
            # 数百个，组合数接近 20 万，直接让候选池构造卡死（不是慢，是不返回）。
            # 这里做两件事：① ``others`` 只取必要数量 + 少量余量；
            # ② 枚举到 ``_MIXED_SEARCH_CAP`` 个就停 —— 只要拿到"够好"的组合即可，
            # 精确最优交给外层代价评估去挑。
            others_short = others[: need_others + 2]
            tried = 0
            for zone_pick in combinations(zone_here, wheelchair_count):
                if tried >= _MIXED_SEARCH_CAP:
                    break
                for other_pick in combinations(others_short, need_others):
                    tried += 1
                    if tried > _MIXED_SEARCH_CAP:
                        break
                    combo = tuple(zone_pick) + tuple(other_pick)
                    if len(combo) != size:
                        continue
                    value = sum(
                        self.fs.row(pid)[slot]
                        for pid, slot in zip(
                            [p.passenger_id for p in unit.passengers], combo
                        )
                    )
                    if value > best_value:
                        best_value = value
                        best_combo = combo
            if best_combo is not None:
                out.append((best_combo, best_value))
        out.sort(key=lambda item: -item[1])
        return out[:8]

    def _facility_ok(self, unit: PassengerUnit, combo: tuple[int, ...]) -> bool:
        """该组合是否满足全部**设施硬约束**。

        当前覆盖两条：

        1. **轮椅旅客必须坐无障碍专区**；
        2. **静音信用分低于阈值的旅客不得进入静音车厢**（信用体系的执行点）。

        两者都必须在这里（候选池）排除，而不是靠代价函数罚分：
        罚分决定"选哪个"，候选池决定"能不能选"。历史事故：低信用旅客的
        T3 罚分是 −5000，但静音车厢的独行奖励与其他项叠加后仍可能让它胜出，
        结果"被投诉过的旅客照样坐进静音车厢"，信用体系形同虚设。

        对形状做显式防御：候选池元素历史上出现过 ``(combo, value)`` 与
        ``(value, combo)`` 两种写法，取错会得到
        ``TypeError: list indices must be integers or slices, not float``，
        而报错完全指不出是"元组顺序反了"。这里只接受元组形状，非元组直接放过。
        """
        if not isinstance(combo, tuple):
            return True
        seats = self.seats
        for passenger, slot in zip(unit.passengers, combo):
            if not isinstance(slot, int):
                return True
            seat = seats[slot]
            if passenger.is_mobility_impaired and not is_wheelchair_seat(
                seat, self.bay_slot_ids
            ):
                return False
            if seat.is_quiet_carriage and passenger.quiet_carriage_blocked:
                return False
        return True

    def _trimmed_slots(self, unit: PassengerUnit, slots: list[int]) -> list[int]:
        """把一节车厢的座位缩到"值得参与组合"的候选。

        规则：按**整单成员的个体分之和**给每个座位打分，取最好的
        ``2 × size + 4`` 个。这样大单元（≥5 人）也能构造同车厢任意组合，
        而枚举量被压到 C(20, 8) ≈ 12.6 万的量级以下 —— 且只取前 20 个合法组合。
        """
        size = unit.size
        weights = [0.0] * len(self.seats)
        for passenger in unit.passengers:
            row = self.fs.row(passenger.passenger_id)
            for slot in slots:
                weights[slot] += row[slot]
        ranked = sorted(slots, key=lambda slot: -weights[slot])
        keep = min(len(ranked), 2 * size + 4)
        return ranked[:keep]

    def _carriage_combos(self, unit: PassengerUnit, carriage: int) -> list[tuple[int, ...]]:
        """单车厢内的候选座位组合：连续窗口优先，其次同排/同厢组合。"""
        size = unit.size
        slots = self.by_carriage[carriage]
        if len(slots) < size:
            return []
        out: list[tuple[int, ...]] = []
        rows = sorted({self.seats[i].row for i in slots})
        for row in rows:
            run = self.by_block[(carriage, row)]
            for start in range(0, max(0, len(run) - size + 1)):
                out.append(tuple(run[start : start + size]))
        # 第二层：同厢任意组合（Block 不足时，如 3 人塞 2+1）
        if size <= 4 and len(slots) >= size:
            limit = 40 if size <= 3 else 24
            for combo in combinations(slots, size):
                out.append(combo)
                if len(out) > limit + 40:
                    break
        # 设施硬约束（轮椅 -> 无障碍专区）必须在这里就排除，
        # 否则即使代价函数给了巨额惩罚，贪心/直发路径仍可能选中违规座位。
        #
        # **例外**：无障碍专区一个空位都不剩时，按"出票优先"允许普通座位
        # （由 notices 生成站车协助提示）。这一条不能少 —— 早期实现无条件
        # 排除非专区座位，导致专区占满后到来的轮椅旅客在**还有 899 个空座**的
        # 车厢上被整单候补，与出票优先直接冲突。
        if self._zone_short_for(unit):
            return out
        return [
            combo
            for combo in out
            if all(
                not passenger.is_mobility_impaired
                or self.seats[slot].in_accessible_zone()
                for passenger, slot in zip(unit.passengers, combo)
            )
        ]

    def cross_carriage(self, unit: PassengerUnit, cap: int = 40) -> list[tuple[tuple[int, ...], float]]:
        """跨车厢组合（最后手段）。"""
        size = unit.size
        ranked = sorted(self.by_carriage.items(), key=lambda kv: -len(kv[1]))[:2]
        if len(ranked) < 2:
            return []
        out: list[tuple[tuple[int, ...], float]] = []
        left, right = ranked[0][1][:8], ranked[1][1][:8]
        passenger_ids = [p.passenger_id for p in unit.passengers]
        for take in range(1, size):
            if take > len(left) or (size - take) > len(right):
                continue
            for a in combinations(left, take):
                for b in combinations(right, size - take):
                    combo = a + b
                    value = sum(
                        self.fs.row(pid)[slot]
                        for pid, slot in zip(passenger_ids, combo)
                    )
                    out.append((combo, value))
                    if len(out) >= cap:
                        out.sort(key=lambda item: -item[1])
                        return out
        out.sort(key=lambda item: -item[1])
        return out

    # -- 指派 -------------------------------------------------------------
    def best_pairs(
        self, unit: PassengerUnit, combo: Sequence[int], placed: dict[str, int]
    ) -> tuple[list[tuple[Passenger, int]], float, float] | None:
        """在给定座位上为单元寻找最优乘客指派。

        返回 (指派, 增量亲和度, 个体分估计)。

        * 单元规模 ≤ 4：枚举全部排列（≤ 24 种），保证"家长-孩子-家长"这类
          关键排列一定被找到；
        * 更大单元：贪心指派 + 两两交换局部搜索（O(k²) 次评估），
          避免 k! 的爆炸（6 人 720 种、10 人 360 万种）。

        注意：不能拿"个体分之和"当剪枝依据——单元内成对项与安全底线加成
        （最高 +50/人）完全可能在个体分相同时拉开数百分差距。
        """
        if len(combo) < unit.size:
            return None
        if unit.size <= 4:
            best: tuple[list[tuple[Passenger, int]], float, float] | None = None
            for perm in permutations(unit.passengers):
                pairs = list(zip(perm, combo))
                estimate = sum(
                    self.fs.row(p.passenger_id)[slot] for p, slot in pairs
                )
                value = self.incremental(pairs, placed)
                if best is None or value > best[1]:
                    best = (pairs, value, estimate)
            return best
        return self._heuristic_pairs(unit, combo, placed)
    def _heuristic_pairs(
        self, unit: PassengerUnit, combo: Sequence[int], placed: dict[str, int]
    ) -> tuple[list[tuple[Passenger, int]], float, float]:
        """大单元指派：两阶段贪心 + 两两交换局部搜索。

        阶段一（个体层）：先为每位乘客挑出个体分最高的若干候选位（短名单）；
        阶段二（关系层）：只在短名单上做贪心落座与局部交换。

        这样把 k² 级别的成对计算限制在很小的候选集内——直接对着全部座位做
        贪心会让 6 人以上订单的单次决策退化到秒级。
        """
        fs = self.fs
        order = self.ctx.order
        shortlist_size = 6
        seat_pool = list(combo)
        individual_best: dict[str, list[int]] = {}
        for passenger in unit.passengers:
            pid = passenger.passenger_id
            ranked = sorted(seat_pool, key=lambda slot: -fs.row(pid)[slot])
            individual_best[pid] = ranked[:shortlist_size]

        # 先安置需要照护的人，让支持人能贴着他们坐
        passengers = sorted(
            unit.passengers,
            key=lambda p: (
                0 if order.caregivers_of(p.passenger_id) else 1,
                0 if p.is_caregiver else 1,
                -p.age,
            ),
        )
        pairs: list[tuple[Passenger, int]] = []
        used: set[int] = set()
        for passenger in passengers:
            helpers = self.helpers.get(passenger.passenger_id, frozenset())
            placed_map = {p.passenger_id: slot for p, slot in pairs}
            available = [s for s in individual_best[passenger.passenger_id] if s not in used]
            if not available:  # 短名单被占满时退回全量座位
                available = [s for s in seat_pool if s not in used]

            def score(
                slot: int,
                passenger: Passenger = passenger,
                placed_map: dict[str, int] = placed_map,
                helpers: frozenset[str] = helpers,
            ) -> tuple[float, int]:
                base = fs.row(passenger.passenger_id)[slot]
                for other_id, other_slot in placed_map.items():
                    base += fs.pair(passenger.passenger_id, other_id, slot, other_slot)
                adjacent = any(
                    helper in placed_map
                    and self.seats[slot].manhattan_to(self.seats[placed_map[helper]])
                    <= CARE_ADJACENT_TOLERANCE
                    for helper in helpers
                )
                return (base, 1 if adjacent else 0)

            best_slot = max(available, key=score)
            used.add(best_slot)
            pairs.append((passenger, best_slot))

        # 局部搜索：任意两人交换座位，若改善则接受。
        # 用 **local_affinity**（单元内部）作为接受判据：交换不会改变"与已放置乘客"
        # 的关系（已放置乘客在不同车厢），因此口径一致，但省掉一半成对计算。
        current = self.local_affinity(pairs)
        rounds = 2 if unit.size <= 8 else 1
        for _ in range(rounds):
            improved = False
            for i in range(len(pairs)):
                for j in range(i + 1, len(pairs)):
                    swapped = list(pairs)
                    swapped[i] = (pairs[i][0], pairs[j][1])
                    swapped[j] = (pairs[j][0], pairs[i][1])
                    value = self.local_affinity(swapped)
                    if value > current:
                        pairs, current = swapped, value
                        improved = True
            if not improved:
                break
        estimate = sum(fs.row(p.passenger_id)[slot] for p, slot in pairs)
        return pairs, self.incremental(pairs, placed), estimate


# ---------------------------------------------------------------------------
# 降级：内存级贪心发牌（保留 Tier 0）
# ---------------------------------------------------------------------------


def _priority_units(ctx: OrderContext) -> list[PassengerUnit]:
    return sorted(ctx.units, key=lambda u: (-u.weight, -u.size, u.unit_id))


def mandatory_ok(unit: PassengerUnit, combo: Sequence[int], ctx: OrderContext, seats: list[Seat]) -> bool:
    """Tier 0 硬过滤：硬核单元内部所有 MANDATORY 绑定是否**同车厢**。

    Tier 0 的定义是"完全分离"（跨车厢），因此这里只拦跨车厢；
    "同车厢但不相邻"属于 Tier 1/3，交由代价函数权衡，保证过滤器永远可满足、
    不会把可行解空间清空。
    """
    if unit.bond is not BondType.MANDATORY:
        return True
    for (pa, ia), (pb, ib) in combinations(list(zip(unit.passengers, combo)), 2):
        if ctx.order.bond_of(pa.passenger_id, pb.passenger_id) is not BondType.MANDATORY:
            continue
        if seats[ia].carriage != seats[ib].carriage:
            return False
    return True


def assign_greedy(
    ctx: OrderContext,
    state: BookingState,
    config: EngineConfig,
    mode: str = "greedy",
    scorer: Scorer | None = None,
    tables: SolverTables | None = None,
    accessible_available: bool = True,
) -> Solution:
    """内存级贪心发牌（模式三）。保留 Tier 0 绑定过滤器。"""
    start = time.perf_counter()
    tables = tables or SolverTables(ctx, state, config)
    # 轮椅停放位信息从编组直接取（全列只有 4 个，是稀缺资源）。
    bay_slots = wheelchair_bay_slot_ids(getattr(state, "formation", None))
    bays_total = len(bay_slots)
    scorer = scorer or Scorer(
        config,
        accessible_available=accessible_available,
        wheelchair_bays_free=bays_total,
        bay_slot_ids=bay_slots,
    )
    free = set(range(len(tables.seats)))
    placed: dict[str, int] = {}
    waitlisted: list[str] = []
    notes: list[str] = []
    # 整单共享一个截止时刻，避免"每个单元各自超时一小会儿"累加成几十秒。
    deadline = start + config.greedy_time_budget_ms / 1000.0
    budget_exhausted = False

    for unit in _priority_units(ctx):
        # 停放位余量必须**动态**跟踪：既看下这一单之前已被占用的（state.occupied），
        # 也看本单前面几个单元刚用掉的（placed）。
        #
        # 早期只算 state.occupied，于是同一单里第 2 位轮椅旅客看不到
        # "停放位已被本单第 1 位用掉"，被误记成 **Tier 0 求解器失误** ——
        # 而它实际是"停放位不够"的服务例外，应当记 T5 + 站车协助提示。
        if any(p.is_mobility_impaired for p in unit.passengers):
            need = sum(1 for p in unit.passengers if p.is_mobility_impaired)
            used = set(state.occupied)
            used.update(tables.seats[slot].seat_id for slot in placed.values())
            left = len(bay_slots - used)
            # 本单元放不下 -> 后续轮椅旅客按"停放位已满"记服务例外
            scorer.wheelchair_bays_free = 0 if need > left else left
            if tables._zone_short_for(unit):
                scorer.accessible_available = False
        result = _greedy_place_unit(unit, tables, free, placed, ctx, deadline=deadline)
        if time.perf_counter() > deadline:
            budget_exhausted = True
        if result is None:
            waitlisted.extend(unit.ids)
            notes.append(
                f"单元 {unit.unit_id}（{unit.size} 人）无可行座位，整体进入候补。"
            )
            continue
        for passenger, slot in result:
            placed[passenger.passenger_id] = slot
            free.discard(slot)
        if len({slot for _, slot in result}) != len(result):
            notes.append(f"内部一致性告警：单元 {unit.unit_id} 的座位出现重复，已按代价函数回退。")

    placed_seats = {pid: tables.seats[slot] for pid, slot in placed.items()}
    waitlisted.extend(wheelchair_waitlist(
        placed_seats, ctx, config, notes, tables.bay_slot_ids
    ))
    if budget_exhausted:
        notes.append(
            f"求解时间预算（{config.greedy_time_budget_ms:.0f} ms）已耗尽："
            "本单规模较大，已按当前最好结果出票，未安排的人进入候补。"
        )

    cost, violations = evaluate_placement(
        placed_seats, ctx, scorer or Scorer(config, accessible_available=accessible_available)
    )
    solution = Solution(
        assignments=_to_assignments(placed_seats, ctx, violations),
        waitlisted=sorted(set(waitlisted)),
        total_affinity=cost,
        violations=violations,
        solver="greedy",
        elapsed_ms=(time.perf_counter() - start) * 1000.0,
        mode=mode,
        notes=notes,
    )
    solution.breakdown = _breakdown(violations)
    return solution


def _row_key(seats: Sequence[Seat], combo: Sequence[int]) -> tuple[int, int]:
    """候选的"坐在一起"程度：``(占用排数, 排号跨度)``。

    * **占用排数**越小越好 —— 一排装得下就绝不拆；
    * 装不下时，**排号跨度**越小越好 —— "05车01排 + 05车02排"（相邻）
      明显优于"05车01排 + 05车09排"（隔了八排），
      而成对项只看"人与人是否相邻"，对这两者打分完全一样。

    真实反馈：用户看到 2 成人 + 2 婴儿落到 ``15车7排 / 15车8排 / 15车9排``，
    说"第四个订单不应该拆开的"。核实：余票 156 时**全列没有任何一排还剩 4 个座**
    （83 排只剩 1 个座），跨排是物理约束；但"跨到哪几排"完全可以优化。
    """
    used = sorted({(seats[slot].carriage, seats[slot].row) for slot in combo})
    span = (used[-1][1] - used[0][1]) if len(used) > 1 else 0
    return len(used), span


def _by_row_span(
    tables: "SolverTables",
    options: Sequence[tuple[tuple[int, ...], float]],
) -> list[tuple[tuple[int, ...], float]]:
    """把候选按"占排数升序、排号跨度升序、个体分降序"重排。

    为什么要单独再排一次：``tables.candidates()`` 已按占排数排过一次，
    但为了控制时延，它在**每节车厢内部**做过截断（``max_per_carriage``），
    被截掉的正是"占排少但个体分低"的组合。分支限界只评估前 N 个候选，
    于是"坐在一起"的方案可能整个池子里都不剩几个、甚至一个不剩。

    这里不再截断，只重排 —— 池子本身是有限的（全局上限 32~72），
    排序成本可忽略，换来的是"少分排"方案一定进入评估。
    """
    return sorted(
        options,
        key=lambda item: (*_row_key(tables.seats, item[0]), -item[1]),
    )


def _row_span(seats: Sequence[Seat], combo: Sequence[int]) -> int:
    """一个座位组合占用了几个不同的排。

    "坐在一起"的度量：同排 1、跨两排 2…… 用它给候选排序，
    保证"尽量少分排"的方案先被评估到。
    """
    return _row_key(seats, combo)[0]


def _unit_pair_ceiling(unit: PassengerUnit, ctx: OrderContext, tables: SolverTables) -> float:
    """单元内成对项 + 安全底线加成的**上限**（用于候选剪枝）。

    成对项本身不可能超过 max(同伴相邻奖励, 硬绑定相邻奖励)；安全底线加成
    按每个需照护人 +50 计。这个上限只需"不低"即可，宁大勿小，保证剪枝安全。
    """
    cfg = tables.cfg
    per_pair = max(cfg.b5_companion_together, cfg.b5_child_adjacent)
    ceiling = 0.0
    for pa, pb in combinations(unit.passengers, 2):
        if ctx.order.bond_of(pa.passenger_id, pb.passenger_id) in (BondType.MANDATORY, BondType.STRONG):
            ceiling += max(0.0, per_pair)
    care = sum(1 for p in unit.passengers if tables.care_dependent.get(p.passenger_id, False))
    ceiling += cfg.b5_child_adjacent * care
    return ceiling


def _greedy_place_unit(
    unit: PassengerUnit,
    tables: SolverTables,
    free: set[int],
    placed: dict[str, int],
    ctx: OrderContext,
    deadline: float | None = None,
) -> list[tuple[Passenger, int]] | None:
    """为单个 unit 找最优放置：（同车厢 -> 跨车厢）逐层退化，第一层能成即停止。

    ``deadline`` 是 ``time.perf_counter()`` 口径的截止时刻。**必须有**：
    候选池里的每个组合都要跑一次乘客指派评估，大单元（8 人）的候选可达数十万，
    实测"4 位轮椅 + 4 位家属"一张单会让求解无限期卡死、堵死整条出票链路。
    预算耗尽即返回当前最好结果（部分出票好过不返回）。
    """
    size = unit.size
    if len(free) < size:
        return None
    candidates = tables.candidates(unit)
    fallback: tuple[list[tuple[Passenger, int]], float] | None = None
    timed_out = False

    def scan(options: Iterable[tuple[tuple[int, ...], float]]) -> list[tuple[Passenger, int]] | None:
        nonlocal fallback, timed_out
        best: list[tuple[Passenger, int]] | None = None
        best_value = 0.0
        # 剪枝（有理论依据，不会牺牲正确性方向）：
        # 候选真实亲和度 ≤ 个体分估计 + 单元内成对项上限。若该上界都不如当前最优，
        # 直接跳过昂贵评估。这样既避免"盲目只算前 N 个"导致大团体被拆散，
        # 又能在个体分显著落后的候选上省下大量成对计算。
        pair_ceiling = _unit_pair_ceiling(unit, ctx, tables)
        # 每节车厢的探针上限：候选按车厢分组且已按个体分降序，只取每组前几个。
        # 这既避免"盲目只算全局前 N 个"（会让大团体被拆散），也避免遍历全部候选。
        per_carriage_probe = {1: 8, 2: 8, 3: 8, 4: 6}.get(size, 4)
        probed: dict[int, int] = {}
        checked = 0
        for combo, estimate in options:
            # 时间闸：**每个候选都查一次**。早期写成"每 32 个查一次"，
            # 结果计数器在 `continue` 分支里几乎不增长，闸门形同虚设 ——
            # 实测 8 人订单仍然无限期卡死。`perf_counter()` 的开销远小于一次
            # `best_pairs` 评估，不值得为它省。
            checked += 1
            if deadline is not None and (checked & 7) == 0 and time.perf_counter() > deadline:
                timed_out = True
                break
            if len(combo) != size or not set(combo) <= free:
                continue
            carriage = tables.seats[combo[0]].carriage
            if all(tables.seats[s].carriage == carriage for s in combo):
                if probed.get(carriage, 0) >= per_carriage_probe:
                    continue
                probed[carriage] = probed.get(carriage, 0) + 1
            if best is not None and estimate + pair_ceiling <= best_value:
                continue
            ok = mandatory_ok(unit, combo, ctx, tables.seats)
            result = tables.best_pairs(unit, combo, placed)
            if result is None:
                continue
            pairs, value, _est = result
            if not ok:
                if fallback is None or value > fallback[1]:
                    fallback = (pairs, value)
                continue
            if best is None or value > best_value:
                best, best_value = pairs, value
        if _DEBUG_SOLVER:
            picked = [
                tables.seats[s].seat_id for _p, s in (best or [])
            ]
            print(
                f"      [scan] options={len(list(options)) if isinstance(options, list) else '?'} "
                f"best={picked} value={best_value:.1f}"
            )
        return best

    result = scan(candidates)
    if _DEBUG_SOLVER:
        print(f"    [solver] {unit.unit_id} size={size} candidates={len(candidates)} "
              f"-> scan(candidates)={'None' if result is None else 'OK'}")
    if result is not None:
        return result
    # 预算已耗尽就不再试跨车厢（那一层只会更贵），直接走兜底判定。
    if not timed_out:
        cross = tables.cross_carriage(unit)
        result = scan(cross)
        if _DEBUG_SOLVER:
            print(f"    [solver] cross_carriage={len(cross)} -> "
                  f"{'None' if result is None else 'OK'} fallback={'有' if fallback else '无'}")
        if result is not None:
            return result
    if fallback is None:
        return None
    # 最后一道闸：**Tier 0 绝不放行**。
    # fallback 里存的是"违反 mandatory_ok（硬核单元被拆到不同车厢）"的放置，
    # 早期实现把它当作兜底直接返回，于是在无障碍专区不足时，
    # "轮椅旅客 + 家属团"被拆到 6 车与 7 车 —— 这是 Tier 0 事故。
    # 正确取舍：宁可这一单转入候补（并生成站车协助提示），也不拆散硬绑定。
    fallback_combo = tuple(slot for _p, slot in fallback[0])
    if not mandatory_ok(unit, fallback_combo, ctx, tables.seats):
        if _DEBUG_SOLVER:
            print(f"    [solver] 放弃 fallback：会拆散硬绑定（Tier 0 不可违反）")
        return None
    return fallback[0]


def wheelchair_waitlist(
    placed: dict[str, Seat],
    ctx: OrderContext,
    config: EngineConfig,
    notes: list[str],
    bay_slot_ids: Collection[str] | None = None,
) -> list[str]:
    """轮椅保底策略 —— **仅在显式配置下才拒票**。

    默认（``waitlist_wheelchair_without_accessible = False``）**照常出票**：
    轮椅停放位售罄时给普通座位，并交由 :mod:`smartrail.notices` 生成
    "请站车协助"的提示。理由是运营现实：拒票直接损失客票收入，而轮椅旅客
    在普通车厢同样可以乘车（需要站车协助上下车与踏板衔接）。

    只有在把该配置设为 ``True`` 时，才退回"绝不发放普通座位"的保守策略。
    """
    if not config.waitlist_wheelchair_without_accessible:
        return []
    out: list[str] = []
    for pid, seat in list(placed.items()):
        if ctx.passengers[pid].is_mobility_impaired and not is_wheelchair_seat(
            seat, bay_slot_ids
        ):
            del placed[pid]
            out.append(pid)
            notes.append(
                f"轮椅乘客 {pid} 无可用无障碍座位，按**保守策略**转入候补"
                "（配置 waitlist_wheelchair_without_accessible=True）。"
                "默认策略是出票普通座位 + 站车协助。"
            )
    return out


# ---------------------------------------------------------------------------
# 模式二：分支限界
# ---------------------------------------------------------------------------


def self_partial_placement(
    tables: "SolverTables", unit: PassengerUnit, free: set[int], placed: dict[str, int]
) -> tuple[list[tuple[Passenger, int]], float] | None:
    """余票不足时为单元做**部分安置**：按优先级把能坐的人先安排下去。

    返回 (指派, 亲和度)；无可行座位时返回 None。
    """
    if not free:
        return None
    # 照护人优先落座，保证弱势成员还有人陪
    order = tables.ctx.order
    priority = sorted(
        unit.passengers,
        key=lambda p: (
            0 if order.caregivers_of(p.passenger_id) else 1,
            0 if p.is_caregiver else 1,
            -p.age,
        ),
    )
    slots = sorted(free)
    chosen: list[tuple[Passenger, int]] = []
    used: set[int] = set()
    for passenger in priority:
        available = [s for s in slots if s not in used]
        if not available:
            break

        # 单人级别选择：个体分 + 与已选成员的关系
        def value_of(
            slot: int,
            passenger: Passenger = passenger,
            chosen: list[tuple[Passenger, int]] = chosen,
        ) -> float:
            total = tables.fs.row(passenger.passenger_id)[slot]
            for other, other_slot in chosen:
                total += tables.fs.pair(
                    passenger.passenger_id, other.passenger_id, slot, other_slot
                )
            return total

        chosen_slot = max(available, key=value_of)
        used.add(chosen_slot)
        chosen.append((passenger, chosen_slot))
    if not chosen:
        return None
    value = tables.local_affinity(chosen) + sum(
        tables.fs.pair(p.passenger_id, oid, slot, oslot)
        for p, slot in chosen
        for oid, oslot in placed.items()
        if tables.ctx.unit_of.get(oid) is not unit
    )
    return chosen, value


def assign_exact(
    ctx: OrderContext,
    state: BookingState,
    config: EngineConfig,
    mode: str = "smart",
    time_budget_ms: float | None = None,
    node_limit: int | None = None,
    accessible_available: bool = True,
) -> Solution:
    """分支限界搜索全局最优解；预算耗尽时返回当前最优解。"""
    start = time.perf_counter()
    tables = SolverTables(ctx, state, config)
    # 评分器必须带上轮椅停放位信息，否则**事后评估**会把"坐在停放位上"
    # 误判成"轮椅旅客拿到了普通座位"，凭空产生 Tier 0 违规
    # （实测：座位明明是停放位 04车01A，违规却说"不是轮椅停放位"）。
    scorer = Scorer(
        config,
        accessible_available=accessible_available,
        wheelchair_bays_free=len(tables.bay_slot_ids),
        bay_slot_ids=tables.bay_slot_ids,
    )
    budget_ms = config.default_exactness_budget if time_budget_ms is None else time_budget_ms
    node_limit = node_limit or config.max_branch_nodes
    units = _priority_units(ctx)
    notes: list[str] = []

    incumbent = assign_greedy(
        ctx,
        state,
        config,
        mode=mode,
        scorer=scorer,
        tables=tables,
        accessible_available=accessible_available,
    )
    best_affinity = incumbent.total_affinity
    best_placed: dict[str, int] = {
        pid: tables.slot[a.seat_id] for pid, a in incumbent.assignments.items()
    }
    nodes = 0
    truncated = False

    # 自适应预算：大团体订单的分支因子极高（组合爆炸），继续搜索基本只会
    # 烧掉 CPU 而拿不到更好解；此时直接采用贪心解，把时延让给吞吐。
    if len(ctx.passengers) > config.max_exact_order_size:
        incumbent.solver = "greedy(bulk)"
        incumbent.notes.append(
            f"订单 {len(ctx.passengers)} 人超过精确搜索适用范围"
            f"（{config.max_exact_order_size} 人），按自适应预算直接采用贪心解。"
        )
        return incumbent

    free_all = set(range(len(tables.seats)))
    if len(free_all) < sum(u.size for u in units):
        notes.append("余票不足以容纳整单，部分乘客进入候补。")

    fs = tables.fs

    def upper_bound(current: float, placed: dict[str, int], remaining: Sequence[PassengerUnit]) -> float:
        """乐观上界（剪枝用），必须可采纳：不得低于任何可行解的真实亲和度。

        构成：已实现亲和度 + 未放置乘客的单项最大亲和度 + 单元级加成上限
        （安全底线 50 分/需照护人）。单元级加成必须显式计入，否则会剪掉更优解。
        """
        bound = current
        care_bonus = 0.0
        for unit in remaining:
            for passenger in unit.passengers:
                pid = passenger.passenger_id
                if pid in placed:
                    continue
                bound += fs.max_individual_global(pid)
                if is_care_dependent(passenger, ctx.order):
                    care_bonus += config.b5_child_adjacent
        return bound + care_bonus

    def search(index: int, placed: dict[str, int], current: float, free: set[int]) -> None:
        nonlocal nodes, best_affinity, best_placed, truncated
        if truncated:
            return
        nodes += 1
        if nodes > node_limit:
            truncated = True
            return
        if (nodes & 0x07) == 0 and (time.perf_counter() - start) * 1000.0 > budget_ms:
            truncated = True
            return
        if index >= len(units):
            if current > best_affinity:
                best_affinity = current
                best_placed = dict(placed)
            return
        remaining = units[index:]
        if upper_bound(current, placed, remaining) <= best_affinity:
            return
        unit = units[index]
        if len(free) < unit.size:
            # 余票装不下整个单元：尽量安置其中一部分（与贪心保持一致），
            # 剩余成员自然进入候补，而不是把整单一起放弃。
            partial = self_partial_placement(tables, unit, free, placed)
            if partial:
                pairs, value = partial
                for _, slot in pairs:
                    free.discard(slot)
                for passenger, slot in pairs:
                    placed[passenger.passenger_id] = slot
                search(index + 1, placed, current + value, free)
                for passenger, _ in pairs:
                    placed.pop(passenger.passenger_id, None)
                for _, slot in pairs:
                    free.add(slot)
            else:
                search(index + 1, placed, current, free)
            return

        scored: list[tuple[float, tuple[int, ...], bool]] = []
        # 只对最有希望的若干候选做完整成对评估：大单元（≥5 人）的候选池
        # 若全量精算，单个节点就要上百次成对打分。
        #
        # **但这个上限不能卡在"最前面 14 个"上。**
        # ``tables.candidates()`` 是按**个体分估计**排序的，而个体分完全
        # 不反映乘客之间的距离。实测后果：2 成人 + 2 儿童的候选池里，
        # "同一排 4 座"（真正想要的）排在"跨 3 排的散座"之后 ——
        # 前者恰好落在第 14 名之外，**从来没被评估过**，
        # 于是分支限界每次都在众多"跨排"方案里挑一个，用户看到
        # "你把大人小孩分开了"（15 单里 14 单跨排）。
        #
        # 因此这里的 probes 只是**评估次数预算**，候选按"先少分排、再高分"
        # 排序后再取前 N 个，保证"坐在一起"的方案一定进入评估。
        probe_limit = 20 if len(unit.passengers) <= 4 else 10
        probes = 0
        for combo, _estimate in _by_row_span(tables, tables.candidates(unit)):
            if not set(combo) <= free:
                continue
            if probes >= probe_limit:
                break
            probes += 1
            result = tables.best_pairs(unit, combo, placed)
            if result is None:
                continue
            pairs, value, _est = result
            scored.append(
                (value, tuple(slot for _, slot in pairs), mandatory_ok(unit, combo, ctx, tables.seats))
            )
        if not scored:
            search(index + 1, placed, current, free)
            return
        scored.sort(key=lambda item: (not item[2], -item[0]))  # Tier 0 优先，其次最优在前
        for value, combo, _ok in scored:
            if truncated:
                return
            pairs = [(unit.passengers[i], slot) for i, slot in enumerate(combo)]
            for _, slot in pairs:
                free.discard(slot)
            for passenger, slot in pairs:
                placed[passenger.passenger_id] = slot
            search(index + 1, placed, current + value, free)
            for passenger, _ in pairs:
                placed.pop(passenger.passenger_id, None)
            for _, slot in pairs:
                free.add(slot)

    search(0, {}, 0.0, free_all)

    if truncated:
        notes.append(
            f"分支限界在预算内未收敛（节点 {nodes} / {budget_ms:.1f}ms），已返回当前最优解。"
        )
    placed_seats = {pid: tables.seats[slot] for pid, slot in best_placed.items()}
    waitlisted = [pid for pid in ctx.passengers if pid not in placed_seats]
    waitlisted.extend(wheelchair_waitlist(
        placed_seats, ctx, config, notes, tables.bay_slot_ids
    ))
    cost, violations = evaluate_placement(placed_seats, ctx, scorer)
    # incumbent（贪心解）在候选更优时可能已被替换；这里补齐它的可解释性备注，
    # 否则"为什么某位乘客进了候补"会在最终结果里丢失。
    for note in incumbent.notes:
        if note not in notes:
            notes.append(note)
    solution = Solution(
        assignments=_to_assignments(placed_seats, ctx, violations),
        waitlisted=sorted(set(waitlisted)),
        total_affinity=cost,
        violations=violations,
        solver="branch-and-bound",
        elapsed_ms=(time.perf_counter() - start) * 1000.0,
        notes=notes,
        mode=mode,
    )
    solution.breakdown = _breakdown(violations)
    solution.nodes = nodes  # type: ignore[attr-defined]
    return solution


# ---------------------------------------------------------------------------
# 统一入口与权威评估
# ---------------------------------------------------------------------------


def solve(
    order: Order,
    state: BookingState,
    config: EngineConfig,
    mode: str = "smart",
    time_budget_ms: float | None = None,
    accessible_available: bool | None = None,
) -> Solution:
    """统一入口。``mode`` ∈ {free, smart, degraded}。

    ``accessible_available``：无障碍专区在**本单开始前**是否还有可用座位。
    它决定"轮椅旅客拿到普通座位"被记成 Tier 0 违规（专区有空位却没给）
    还是运营例外（专区确实售罄，出票 + 站车协助）。不传则由当前状态推断。
    """
    if accessible_available is None:
        accessible_available = accessible_zone_has_free_seat(
            state.formation.seats, state.occupied
        )
    ctx = build_context(order, config)
    if mode == "degraded":
        return assign_greedy(
            ctx, state, config, mode=mode, accessible_available=accessible_available
        )
    if mode == "free":
        from .free_seat import validate_free_selection

        return validate_free_selection(
            order, state, config, accessible_available=accessible_available
        )
    return assign_exact(
        ctx,
        state,
        config,
        mode=mode,
        time_budget_ms=time_budget_ms,
        accessible_available=accessible_available,
    )


def evaluate_pairs(
    pairs: Sequence[tuple[Passenger, Seat]],
    placed: dict[str, Seat],
    ctx: OrderContext,
    scorer: Scorer,
) -> tuple[float, list[Violation]]:
    """权威增量评估（构造 Violation，用于可解释性）。"""
    total = 0.0
    violations: list[Violation] = []
    placed_here = list(pairs)
    here_carriage = {p.passenger_id: seat.carriage for p, seat in placed_here}

    for passenger, seat in placed_here:
        value, terms = scorer.individual_cost(passenger, seat)
        total += value
        violations.extend(t.to_violation() for t in terms)
    for (pa, sa), (pb, sb) in combinations(placed_here, 2):
        value, terms = scorer.pair_cost(pa, sa, pb, sb, ctx.order.bond_of(pa.passenger_id, pb.passenger_id))
        total += value
        violations.extend(t.to_violation() for t in terms)

    # 静音车厢：按出行单元整体计价（Tier 3）
    if len(placed_here) > 1 and placed_here[0][1].is_quiet_carriage:
        group_extra = scorer.cfg.t3_quiet_group_extra * (len(placed_here) - 1)
        total += group_extra
        violations.append(
            CostTerm(
                QUIET_GROUP_OVERUSE,
                3,
                group_extra,
                tuple(p.passenger_id for p, _ in placed_here),
                f"{len(placed_here)} 人同行进入静音车厢，占用稀缺公共资源",
            ).to_violation()
        )

    # Tier 0：孕晚期必须与同行人同车厢（"无同行人"是更严重的一种形态）
    for passenger, seat in placed_here:
        if SupportNeed.PREGNANT_LATE not in passenger.support_needs:
            continue
        helpers = ctx.helpers.get(passenger.passenger_id, frozenset())
        if not helpers:
            total += scorer.cfg.t0_pregnant_no_companion
            violations.append(
                CostTerm(
                    PREGNANT_ALONE,
                    0,
                    scorer.cfg.t0_pregnant_no_companion,
                    (passenger.passenger_id,),
                    "孕晚期旅客本单无同行人（硬约束：孕晚期必须有人陪同）",
                ).to_violation()
            )
            continue
        same_carriage = any(
            other_id in helpers
            and (
                placed[other_id].carriage == seat.carriage
                if other_id in placed
                else here_carriage.get(other_id) == seat.carriage
            )
            for other_id in helpers
        )
        if not same_carriage:
            total += scorer.cfg.t0_pregnant_no_companion
            violations.append(
                CostTerm(
                    PREGNANT_ALONE,
                    0,
                    scorer.cfg.t0_pregnant_no_companion,
                    (passenger.passenger_id,),
                    "孕晚期旅客的同行人不在同一车厢",
                ).to_violation()
            )

    # 安全底线：需照护者周围必须有支持人
    here_ids = {p.passenger_id for p, _ in placed_here}
    for passenger, seat in placed_here:
        if not is_care_dependent(passenger, ctx.order):
            continue
        helpers = support_people_of(passenger, ctx.order)
        if not helpers:
            continue
        adjacent = any(
            other.passenger_id != passenger.passenger_id
            and other.passenger_id in helpers
            and seat.manhattan_to(other_seat) <= CARE_ADJACENT_TOLERANCE
            for other, other_seat in placed_here
        ) or any(
            other_id in helpers
            and other_id not in here_ids
            and seat.manhattan_to(other_seat) <= CARE_ADJACENT_TOLERANCE
            for other_id, other_seat in placed.items()
        )
        if adjacent:
            total += scorer.cfg.b5_child_adjacent
            violations.append(
                CostTerm(
                    CHILD_ADJACENT, 5, scorer.cfg.b5_child_adjacent,
                    (passenger.passenger_id,), "需照护者身边有支持人，安全底线满足",
                ).to_violation()
            )
        else:
            total += scorer.cfg.t1_isolated_care_member
            violations.append(
                CostTerm(
                    ISOLATED_CARE, 1, scorer.cfg.t1_isolated_care_member,
                    (passenger.passenger_id,), "需照护者周围没有任何支持人（被孤立）",
                ).to_violation()
            )

    # 跨组关系
    for passenger, seat in placed_here:
        for other_id, other_seat in placed.items():
            bond = ctx.order.bond_of(passenger.passenger_id, other_id)
            value, terms = scorer.pair_cost(
                passenger, seat, ctx.passengers[other_id], other_seat, bond
            )
            total += value
            violations.extend(t.to_violation() for t in terms)
    return total, violations


def evaluate_placement(
    placed: dict[str, Seat],
    ctx: OrderContext,
    scorer: Scorer,
    partial: bool = False,
) -> tuple[float, list[Violation]]:
    """对给定座位映射计算全局亲和度，按出行单元调用 :func:`evaluate_pairs`。"""
    _ = partial
    total = 0.0
    violations: list[Violation] = []
    seen: set[tuple[str, int, tuple[str, ...], str]] = set()
    for unit in ctx.units:
        pairs = [
            (passenger, placed[passenger.passenger_id])
            for passenger in unit.passengers
            if passenger.passenger_id in placed
        ]
        if not pairs:
            continue
        others = {pid: seat for pid, seat in placed.items() if ctx.unit_of.get(pid) is not unit}
        value, terms = evaluate_pairs(pairs, others, ctx, scorer)
        total += value
        for term in terms:
            key = (term.code, term.tier, term.passengers, term.detail)
            if key in seen:
                continue
            seen.add(key)
            violations.append(term)
    return total, violations


def _to_assignments(
    placed: dict[str, Seat],
    ctx: OrderContext,
    violations: Sequence[Violation] | None = None,
) -> dict[str, Assignment]:
    """把"乘客 -> 座位"映射转成对外结果。

    这里强制校验**一人一座**：座位 ID 冲突会让两位乘客拿到同一个座位，
    属于绝对不能静默通过的错误（历史上曾因编组座位 ID 重复而触发）。
    """
    violations = violations or []
    seat_ids = [seat.seat_id for seat in placed.values()]
    if len(set(seat_ids)) != len(seat_ids):
        duplicates = sorted({sid for sid in seat_ids if seat_ids.count(sid) > 1})
        raise ValueError(f"座位分配冲突：{duplicates} 被分配给多位乘客")
    per_passenger: dict[str, list[Violation]] = {}
    for v in violations:
        for pid in v.passengers:
            per_passenger.setdefault(pid, []).append(v)
    out: dict[str, Assignment] = {}
    for pid, seat in placed.items():
        unit = ctx.unit_of.get(pid)
        out[pid] = Assignment(
            passenger_id=pid,
            seat_id=seat.seat_id,
            carriage=seat.carriage,
            row=seat.row,
            col=seat.col,
            quiet_carriage=seat.is_quiet_carriage,
            reason=Scorer.explain(per_passenger.get(pid, [])),
            unit_id=unit.unit_id if unit else "",
        )
    return dict(sorted(out.items(), key=lambda kv: seat_key(kv[1].seat_id)))


def _breakdown(violations: Sequence[Violation]) -> dict[str, float]:
    """按"Tier:代码"聚合亲和度（内部口径，负=惩罚、正=奖励）。"""
    out: dict[str, float] = {}
    for v in violations:
        key = f"tier{v.tier}:{v.code}"
        out[key] = out.get(key, 0.0) + v.affinity
    return dict(sorted(out.items(), key=lambda kv: kv[1]))
