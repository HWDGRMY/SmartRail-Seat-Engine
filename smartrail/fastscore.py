"""高速打分器：与 :class:`smartrail.scoring.Scorer` **数值等价**，但为热路径优化。

设计要点
--------
``Scorer`` 是权威定义（逐项构造 ``CostTerm``，便于可解释性与单元测试）；
但在分支限界的内层循环里，每个候选都要重算成对项，构造对象成了主要开销。
``FastScorer`` 因此：

1. 预计算 ``indiv[passenger_slot][seat_slot]`` 矩阵（O(N·S) 次，而不是每次候选重算）；
2. 成对项按 (乘客对, 座位对) 惰性缓存；
3. 只返回 float，不构造任何对象；可解释性所需的 ``Violation`` 仅对**最终胜出**
   的方案用权威 ``Scorer`` 重算一次。

两者一致性由 ``tests/test_fastscore_equivalence.py`` 逐项断言。
"""

from __future__ import annotations

from typing import Collection

from .config import EngineConfig
from .scoring import is_wheelchair_seat
from .models import BondType, DeclaredBehavior, Order, Passenger, Seat, SeatFeature, SupportNeed


class FastScorer:
    """数值等价、零对象分配的打分器。"""

    def __init__(
        self,
        order: Order,
        passengers: dict[str, Passenger],
        seats: list[Seat],
        config: EngineConfig,
        unit_of: dict[str, object] | None = None,
        bay_slot_ids: Collection[str] | None = None,
    ) -> None:
        self.order = order
        self.passengers = passengers
        self.seats = seats
        self.cfg = config
        self.unit_of = unit_of or {}
        self.slot: dict[str, int] = {pid: i for i, pid in enumerate(passengers)}
        self.seat_slot = {s.seat_id: i for i, s in enumerate(seats)}
        n = len(passengers)
        m = len(seats)
        self._m = m
        # 单项亲和度矩阵**按需惰性计算**：不是每位乘客、每个座位都会被评估
        # （多数订单只关心几节车厢），全量预算会让 16 节编组下每单固定多花
        # 十几毫秒。行切片语义见 ``row()``。
        self._rows: dict[int, list[float]] = {}
        # 座位属性以并行数组预展开：热循环里直接下标取值，避免反复做
        # frozenset 成员测试与对象属性查找（profiler 显示这是第一热点）。
        self._quiet_flag: list[bool] = [s.is_quiet_carriage for s in seats]
        # 轮椅落点：有停放位信息时只认停放位槽，否则退回无障碍专区口径。
        # 两处（此处与 SolverTables.zone_slots / is_wheelchair_seat）必须口径一致，
        # 否则会出现"快速打分器认为某座位可用、候选池却排除了它"这类矛盾。
        self._accessible: list[bool] = [
            is_wheelchair_seat(s, bay_slot_ids) for s in seats
        ]
        self._aisle: list[bool] = [s.is_aisle for s in seats]
        self._is_window: list[bool] = [SeatFeature.WINDOW in s.features for s in seats]
        self._near_door: list[bool] = [SeatFeature.NEAR_DOOR in s.features for s in seats]
        self._near_toilet: list[bool] = [SeatFeature.NEAR_TOILET in s.features for s in seats]
        self._pair_cache: dict[tuple[int, int, int, int], float] = {}
        self._max_cache: dict[int, float] = {}
        # 座位级曼哈顿距离缓存：避免每次成对评估重复计算
        self._dist: dict[tuple[int, int], int] = {}

    def row(self, passenger_id: str) -> list[float]:
        """取某乘客的单项亲和度行（首次访问时惰性计算，随后复用）。"""
        index = self.slot[passenger_id]
        cached = self._rows.get(index)
        if cached is None:
            cached = self._build_row(self.passengers[passenger_id])
            self._rows[index] = cached
        return cached

    def _attr(self, passenger_id: str, attr: str) -> Any:
        return getattr(self.passengers[passenger_id], attr)

    def _build_row(self, p: Passenger) -> list[float]:
        """向量化构造一整行：把逐座位的 Python 函数调用换成按属性的列表推导。

        座位亲和度是若干"属性 × 常量"的和，因此可以按属性列批量计算后相加，
        16 节编组（1156 座）下单行耗时从 ~0.9ms 降到 ~0.15ms。
        """
        cfg = self.cfg
        m = self._m
        total = [0.0] * m
        aisle = self._aisle
        accessible = self._accessible
        window = self._is_window
        near_door = self._near_door
        near_toilet = self._near_toilet
        moving = p.is_mobility_impaired
        # Tier 0 / Tier 3
        if moving:
            total = [v + (0.0 if accessible[j] else cfg.t0_wheelchair_no_accessible) for j, v in enumerate(total)]
        if p.quietness_score < 60.0:
            total = [v + (cfg.t3_quiet_blocked_credit if self._quiet_flag[j] else 0.0) for j, v in enumerate(total)]
        if p.quiet_repulsion > 0:
            quiet_penalty = cfg.t3_quiet_family_base * p.quiet_repulsion
            total = [v + (quiet_penalty if self._quiet_flag[j] else 0.0) for j, v in enumerate(total)]
        if cfg.t3_accessible_misuse and not moving:
            total = [v + (cfg.t3_accessible_misuse if accessible[j] else 0.0) for j, v in enumerate(total)]

        # Tier 5：设施匹配
        if moving:
            total = [v + (cfg.b5_accessible_match if accessible[j] else 0.0) for j, v in enumerate(total)]
        if p.preference_aisle:
            total = [v + (cfg.b5_facility_match if aisle[j] else 0.0) for j, v in enumerate(total)]
        if p.preference_window:
            total = [v + (cfg.b5_facility_match if window[j] else 0.0) for j, v in enumerate(total)]
        needs = p.support_needs
        if SupportNeed.INDEPENDENT_BLIND in needs:
            total = [
                v + (cfg.b5_facility_match if aisle[j] else 0.0) + (cfg.b5_facility_match if near_door[j] else 0.0)
                for j, v in enumerate(total)
            ]
        if SupportNeed.PREGNANT_LATE in needs:
            total = [
                v + (cfg.b5_facility_match if near_toilet[j] else 0.0) + (cfg.b5_facility_match if aisle[j] else 0.0)
                for j, v in enumerate(total)
            ]
        if p.age >= 70:
            total = [v + (cfg.b5_facility_match * 0.5 if near_door[j] else 0.0) for j, v in enumerate(total)]
        if SupportNeed.ELDERLY in needs:
            total = [v + (cfg.b5_facility_match * 0.5 if aisle[j] else 0.0) for j, v in enumerate(total)]
        # 普通成人单人旅客进入静音车厢的奖励
        if not p.is_child and not needs and p.quietness_score >= 60.0 and p.declared_behavior is not DeclaredBehavior.LIVELY:
            total = [v + (cfg.t5_quiet_solo_adult if self._quiet_flag[j] else 0.0) for j, v in enumerate(total)]
        # 与权威 Scorer 一致的奖励地板与硬上限：
        # * 低于 min_floor_reward 的碎片化奖励不发（避免为凑分牺牲真实匹配）；
        # * 高于 max_reward_per_passenger 的奖励封顶（保证约束优先于偏好）。
        floor = cfg.min_floor_reward
        cap = cfg.max_reward_per_passenger
        return [0.0 if 0.0 < v < floor else (cap if v > cap else v) for v in total]

    # ------------------------------------------------------------------
    # 单项（与 Scorer.individual_cost 数值等价，走同一条向量化路径）
    # ------------------------------------------------------------------
    def _individual(self, p: Passenger, seat: Seat) -> float:
        """按 Seat 对象取单项亲和度（等价性测试与可读性用）。"""
        return self._build_row(p)[self.seat_slot[seat.seat_id]]

    def individual(self, passenger_id: str, seat_slot: int) -> float:
        return self.row(passenger_id)[seat_slot]

    def max_individual(self, passenger_id: str, seat_slots: list[int]) -> float:
        row = self.row(passenger_id)
        return max((row[j] for j in seat_slots), default=0.0)

    def max_individual_global(self, passenger_id: str) -> float:
        """全局单项最大亲和度（对所有座位取最大，含已占用座位）。

        由于"限定可用座位"只会让最大值不增，这个全局值天然是可采纳上界，
        且可 O(1) 命中缓存，避免在内层循环里重复扫描座位集合。
        """
        index = self.slot[passenger_id]
        cached = self._max_cache.get(index)
        if cached is None:
            cached = max(self.row(passenger_id), default=0.0)
            self._max_cache[index] = cached
        return cached

    # ------------------------------------------------------------------
    # 成对项
    # ------------------------------------------------------------------
    def pair(self, a_id: str, b_id: str, ia: int, ib: int) -> float:
        key = (self.slot[a_id], self.slot[b_id], ia, ib)
        cached = self._pair_cache.get(key)
        if cached is None:
            cached = self._pair_compute(a_id, b_id, ia, ib)
            self._pair_cache[key] = cached
        return cached

    def distance(self, ia: int, ib: int) -> int:
        """座位间曼哈顿距离（带缓存）。"""
        if ia > ib:
            ia, ib = ib, ia
        key = (ia, ib)
        value = self._dist.get(key)
        if value is None:
            value = self.seats[ia].manhattan_to(self.seats[ib])
            self._dist[key] = value
        return value

    def _pair_compute(self, a_id: str, b_id: str, ia: int, ib: int) -> float:
        cfg = self.cfg
        sa, sb = self.seats[ia], self.seats[ib]
        pa, pb = self.passengers[a_id], self.passengers[b_id]
        bond = self.order.bond_of(a_id, b_id)
        total = 0.0
        if sa.carriage != sb.carriage:
            if bond is BondType.MANDATORY:
                total += cfg.t0_separated_care_bond
            elif bond is BondType.STRONG:
                total += (
                    cfg.t1_care_unit_split_carriage
                    if (pa.is_vulnerable or pb.is_vulnerable)
                    else cfg.t2_adult_cross_carriage
                )
            else:
                total += (
                    cfg.t1_vulnerable_cross_carriage
                    if (pa.is_vulnerable or pb.is_vulnerable)
                    else cfg.t2_adult_cross_carriage
                )
        else:
            distance = self.distance(ia, ib)
            if bond is BondType.MANDATORY:
                if distance == 1:
                    total += cfg.b5_companion_together
                else:
                    total += cfg.t1_care_bond_same_carriage
            elif bond is BondType.STRONG:
                if distance == 1:
                    total += cfg.b5_companion_together
                else:
                    # 就近梯度必须与 Scorer.pair_cost 完全一致，否则两个打分器会漂移。
                    row_gap = abs(sa.row - sb.row)
                    if cfg.prefer_adjacent_for_weak_bonds and row_gap == 1:
                        total += cfg.b5_same_carriage_adjacent_row
                    elif cfg.prefer_adjacent_for_weak_bonds and row_gap == 0:
                        total += cfg.b5_near_row_together
                    else:
                        total += cfg.t4_not_together
            else:
                if _aisle_separated(sa, sb):
                    total += cfg.t4_aisle_separated
                elif sa.row == sb.row and abs(sa.col_index - sb.col_index) == 1:
                    total += cfg.b5_companion_together * 0.5
        # Tier 1：冲突群体（轮椅/独立视障/智力障碍）被塞进静音车厢
        if sb.is_quiet_carriage and pa.support_needs & _QUIET_CONFLICT_NEEDS:
            total += cfg.t1_conflict_isolation
        elif sa.is_quiet_carriage and pb.support_needs & _QUIET_CONFLICT_NEEDS:
            total += cfg.t1_conflict_isolation
        return total

    @property
    def pair_cache_size(self) -> int:
        return len(self._pair_cache)


_QUIET_CONFLICT_NEEDS = frozenset(
    {
        SupportNeed.WHEELCHAIR,
        SupportNeed.INDEPENDENT_BLIND,
        SupportNeed.INTELLECTUAL_DISABILITY,
    }
)


def _aisle_separated(seat_a: Seat, seat_b: Seat) -> bool:
    if seat_a.carriage != seat_b.carriage or seat_a.row != seat_b.row:
        return False
    low, high = sorted((seat_a.col_index, seat_b.col_index))
    return (high - low) == 2 and not seat_a.is_aisle and not seat_b.is_aisle


__all__ = ["FastScorer"]
