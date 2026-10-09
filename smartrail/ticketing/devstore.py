"""开发者模式台账：模拟余票调整、全局座位视图、系统重置。

设计要点
--------
**余票调整直接落在"座位占用"上，而不是维护一个独立的数字。**

需求原文："开发者可在后台自由调整该车次的模拟余票数量，修改需实时影响用户模式。"

早期很容易写成"维护一个 ``remaining = 93`` 的计数器"，但那样会有两个问题：

1. 计数器与真实座位图谱会**逐渐对不上** —— 用户模式卖出座位后忘了减、或者减了
   两次，用户就会看到"显示有票但下单失败"这种最招人骂的状态；
2. 座位图（开发者要看的那个）无法反映"这 5 个座位是我手动锁掉的"。

所以这里只维护**座位级的占用集合**，余票一律由
``总座位 − 已占用`` 实时算出。单一数据源，不可能漂移。
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from ..models import TrainFormation

#: 占用来源（开发者座位图上用不同颜色区分）
SOURCE_SOLD = "sold"            # 用户模式下单售出
SOURCE_MANUAL = "manual"        # 开发者手动锁定/解锁
SOURCE_PRESET = "preset"        # 开发者一键"卖到 X 成"


@dataclass
class Occupancy:
    """一个座位的占用记录。"""

    seat_id: str
    source: str
    order_id: str = ""
    passenger_name: str = ""
    color_index: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "seat_id": self.seat_id,
            "source": self.source,
            "order_id": self.order_id,
            "passenger_name": self.passenger_name,
            "color_index": self.color_index,
        }


def _scatter_order(
    seats: Sequence[Any], rng: random.Random, adjacent_ratio: float = 0.3
) -> list[Any]:
    """把座位排成"像真实售票那样"的占用顺序。

    为什么不能按顺序占
    ------------------
    早期实现直接取"前 N 个座位"当已售，结果**余票永远是车厢末尾一整块连续座位** ——
    于是"余票紧张时凑不出连座、需要自动分票"这个最该被压测到的场景
    **永远触发不了**：随便一单都能在那一大块里坐下。
    这不是"少测了一个边界"，而是**把要测的东西测没了**。

    真实售票是散开的：多数人零散买、少数人（家庭/同行）买连座。
    因此这里按 ``adjacent_ratio`` 的比例生成"相邻对"，
    其余**随机打散**到全列各车厢，形成真实的碎片化余票格局。
    """
    pool = list(seats)
    rng.shuffle(pool)
    ordered: list[Any] = []
    used: set[str] = set()

    # 先按比例制造"相邻座位"（模拟同行旅客买连座）
    quota = int(len(pool) * max(0.0, min(1.0, adjacent_ratio)))
    if quota >= 2:
        by_row: dict[tuple[int, int], list[Any]] = {}
        for seat in pool:
            by_row.setdefault((seat.carriage, seat.row), []).append(seat)
        rows = list(by_row.values())
        rng.shuffle(rows)
        made = 0
        for group in rows:
            if made >= quota:
                break
            group = sorted(group, key=lambda s: s.col_index)
            for left, right in zip(group, group[1:]):
                if made >= quota:
                    break
                if left.seat_id in used or right.seat_id in used:
                    continue
                ordered.extend((left, right))
                used.update({left.seat_id, right.seat_id})
                made += 2
    # 其余按打乱后的顺序补齐（保证覆盖率 100% 且分布随机）
    ordered.extend(seat for seat in pool if seat.seat_id not in used)
    return ordered


@dataclass
class DevStore:
    """开发者模式的内存台账。"""

    formation: TrainFormation
    occupied: dict[str, Occupancy] = field(default_factory=dict)
    #: 已占用的轮椅停放位，键是**对外编号**（``04车W1``），与座位占用互不影响
    occupied_bays: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: 停放位的内部落点座位。它们**不是票务占用**（不减少余票），
    #: 只是"这个落点当前被某个停放位占着"，避免求解器把同一个落点
    #: 分给第二位轮椅旅客。
    blocked_slots: set[str] = field(default_factory=set)
    orders: list[dict[str, Any]] = field(default_factory=list)
    _color_seq: int = 0

    @property
    def engine_occupied(self) -> set[str]:
        """交给求解器的"不可用座位"集合 = 真实售出 + 停放位落点。"""
        return set(self.occupied) | set(self.blocked_slots)

    # -- 余票 ----------------------------------------------------------
    def is_occupied(self, seat_id: str) -> bool:
        return seat_id in self.occupied

    def free_seats(self, class_code: str | None = None) -> list[str]:
        return [
            seat.seat_id
            for seat in self.formation.seats
            if seat.seat_id not in self.occupied
            and (class_code is None or seat.class_code == class_code)
        ]

    def total_seats(self, class_code: str | None = None) -> int:
        return sum(
            1 for seat in self.formation.seats
            if class_code is None or seat.class_code == class_code
        )

    def remaining(self, class_code: str | None = None) -> dict[str, int]:
        """各类席别的实时余票（**由座位级占用算出，不是独立计数器**）。"""
        result: dict[str, int] = {}
        for seat in self.formation.seats:
            if class_code is not None and seat.class_code != class_code:
                continue
            bucket = result.setdefault(seat.class_code, 0)
            if seat.seat_id not in self.occupied:
                result[seat.class_code] = bucket + 1
        return result

    def remaining_by_carriage(self, class_code: str | None = None) -> dict[int, int]:
        result: dict[int, int] = {}
        for seat in self.formation.seats:
            if class_code is not None and seat.class_code != class_code:
                continue
            if seat.seat_id not in self.occupied:
                result[seat.carriage] = result.get(seat.carriage, 0) + 1
        return result

    # -- 轮椅停放位（独立资源、独立编号、不占座位票额）------------------
    def bay_slot_ids(self) -> frozenset[str]:
        """停放位的**内部落点座位**集合（仅供与求解器对接）。

        注意：这不是票务概念，也不作为座位号展示。对外编号一律用
        :meth:`bay_ids` 里的 ``04车W1`` 形式。
        """
        return frozenset(
            bay.slot_seat_id for bay in self.formation.wheelchair_bays
            if bay.slot_seat_id
        )

    def bay_ids(self) -> frozenset[str]:
        """停放位的**对外编号**集合（``04车W1`` 等）。"""
        return frozenset(bay.bay_id for bay in self.formation.wheelchair_bays)

    def occupied_bay_ids(self) -> set[str]:
        return set(self.occupied_bays)

    def free_bays(self) -> list[dict[str, Any]]:
        """还空着的轮椅停放位。"""
        return [
            bay.to_dict()
            for bay in self.formation.wheelchair_bays
            if bay.bay_id not in self.occupied_bays
        ]

    def occupied_bay_records(self) -> list[dict[str, Any]]:
        return [
            {**bay.to_dict(), **self.occupied_bays[bay.bay_id]}
            for bay in self.formation.wheelchair_bays
            if bay.bay_id in self.occupied_bays
        ]

    def occupy_bays(
        self,
        bay_ids: Iterable[str],
        order_id: str = "",
        passenger_name: str = "",
        color_index: int = 0,
    ) -> list[str]:
        """占用停放位（按**独立编号**记账，与座位占用互不影响）。

        停放位不占座位票额 —— 所以这里**不会**往 ``self.occupied``
        里加任何座位（那会减少余票）。只把该停放位的**内部落点**记进
        ``blocked_slots``，防止求解器把同一个落点重复分配。
        """
        taken: list[str] = []
        by_id = {bay.bay_id: bay for bay in self.formation.wheelchair_bays}
        for bay_id in bay_ids:
            if bay_id in self.occupied_bays:
                continue
            self.occupied_bays[bay_id] = {
                "order_id": order_id,
                "passenger_name": passenger_name,
                "color_index": color_index,
            }
            bay = by_id.get(bay_id)
            if bay is not None and bay.slot_seat_id:
                self.blocked_slots.add(bay.slot_seat_id)
            taken.append(bay_id)
        return taken

    def release_bays(self, bay_ids: Iterable[str]) -> int:
        removed = 0
        by_id = {bay.bay_id: bay for bay in self.formation.wheelchair_bays}
        for bay_id in bay_ids:
            if self.occupied_bays.pop(bay_id, None) is not None:
                bay = by_id.get(bay_id)
                if bay is not None and bay.slot_seat_id:
                    self.blocked_slots.discard(bay.slot_seat_id)
                removed += 1
        return removed

    def wheelchair_bays_summary(self) -> dict[str, Any]:
        """停放位总览（用户模式要据此提示"还剩几个轮椅位"）。"""
        total = len(self.formation.wheelchair_bays)
        occupied = len(self.occupied_bays)
        return {
            "total": total,
            "free": total - occupied,
            "occupied": occupied,
            "bays": [
                {
                    **bay.to_dict(),
                    "occupied": bay.bay_id in self.occupied_bays,
                    **self.occupied_bays.get(bay.bay_id, {}),
                }
                for bay in self.formation.wheelchair_bays
            ],
            "note": "轮椅固定停放位，独立编号（如 04车W1），不占座位票额；"
                    "满位后可经确认改出普通坐票",
        }

    # -- 占用 ----------------------------------------------------------
    def occupy(
        self,
        seat_ids: Iterable[str],
        source: str = SOURCE_MANUAL,
        order_id: str = "",
        passenger_name: str = "",
        color_index: int = 0,
    ) -> int:
        """占用若干座位；已占用的跳过（不报错，便于幂等调用）。"""
        added = 0
        for seat_id in seat_ids:
            if seat_id in self.occupied:
                continue
            self.occupied[seat_id] = Occupancy(
                seat_id=seat_id,
                source=source,
                order_id=order_id,
                passenger_name=passenger_name,
                color_index=color_index,
            )
            added += 1
        return added

    def release(self, seat_ids: Iterable[str]) -> int:
        removed = 0
        for seat_id in seat_ids:
            if self.occupied.pop(seat_id, None) is not None:
                removed += 1
        return removed

    def next_color(self) -> int:
        self._color_seq += 1
        return self._color_seq

    # -- 开发者操作 ----------------------------------------------------
    def set_remaining(
        self,
        class_code: str,
        target: int,
        *,
        from_front: bool = False,
        seed: int = 20261019,
    ) -> dict[str, Any]:
        """把某席别的余票调整到 ``target``。

        默认 **``from_front=False``，即打散占用** —— 这样余票是碎片化的，
        能真实压测"凑不出连座 -> 自动分票"。设为 ``True`` 则退化为
        "占用最靠前的座位"，便于人工核对座位图。

        早期默认是 ``from_front=True``，结果是余票永远为车厢末尾一整块，
        最该被测到的分票场景**永远触发不到**。
        """
        candidates = [
            seat for seat in self.formation.seats if seat.class_code == class_code
        ]
        if not candidates:
            raise ValueError(f"编组里没有席别：{class_code}")
        total = len(candidates)
        target = max(0, min(total, int(target)))
        sold_target = total - target

        # 当前非手动占用的座位（用户售出 + 预设）已经占掉一部分
        auto_occupied = [
            seat for seat in candidates
            if seat.seat_id in self.occupied
            and self.occupied[seat.seat_id].source != SOURCE_MANUAL
        ]
        manual_needed = max(0, sold_target - len(auto_occupied))

        manual_now = [
            seat for seat in candidates
            if seat.seat_id in self.occupied
            and self.occupied[seat.seat_id].source == SOURCE_MANUAL
        ]
        # 真源优先：让"已售"保持原样，只调整手动锁定的部分
        manual_now_ids = {seat.seat_id for seat in manual_now}
        free = [seat for seat in candidates if seat.seat_id not in self.occupied]
        if not from_front:
            free = _scatter_order(free, random.Random(seed))

        # 需要更多手动占用
        if manual_needed > len(manual_now):
            need = manual_needed - len(manual_now)
            pool = free if not from_front else free
            if from_front:
                pool = list(free)
            for seat in pool[:need]:
                self.occupied[seat.seat_id] = Occupancy(
                    seat_id=seat.seat_id, source=SOURCE_MANUAL
                )
        # 需要更少手动占用
        elif manual_needed < len(manual_now):
            drop = len(manual_now) - manual_needed
            # 释放时同样尽量散开：随机挑，而不是从排头砍
            ordered = sorted(manual_now_ids)
            if not from_front:
                random.Random(seed).shuffle(ordered)
            elif not from_front:
                ordered = list(reversed(ordered))
            for seat_id in ordered[:drop]:
                self.occupied.pop(seat_id, None)

        return {
            "class_code": class_code,
            "total": total,
            "target": target,
            "remaining": self.remaining(class_code).get(class_code, 0),
            "manual_locked": sum(
                1 for item in self.occupied.values() if item.source == SOURCE_MANUAL
            ),
            # 碎片化程度：能连坐的排数。压测时最关键的一个数 ——
            # 余票数量相同但"能否凑出 N 连座"可以完全不同。
            "fragmentation": self.fragmentation(class_code),
        }

    def fragmentation(self, class_code: str | None = None) -> dict[str, Any]:
        """余票的碎片化程度：各车厢的"最长连续空座"分布。

        为什么需要这个指标：同样剩 100 张票，可能是"一整排 100 连座"，
        也可能是"100 个互不相邻的单座"。前者任何订单都能满足，
        后者连 2 人同行都要分票 —— 只报"余票 100"会完全掩盖这个差别。

        **必须按物理列位判断相邻**：早期实现按 ``col_index`` 排序后
        只数"有几个自由座位"，没有检查下标是否真正相邻，于是
        "A 座 + D 座"这种隔着过道的组合也被算成 2 连座。
        实测该 bug 会把最长连座从 2 报成 5，直接让这个指标失效 ——
        而它正是判断"是否需要自动分票"的依据。
        """
        by_row: dict[tuple[int, int], list[int]] = {}
        for seat in self.formation.seats:
            if class_code is not None and seat.class_code != class_code:
                continue
            if seat.seat_id in self.occupied:
                continue
            by_row.setdefault((seat.carriage, seat.row), []).append(seat.col_index)
        runs: list[int] = []
        for columns in by_row.values():
            columns.sort()
            run = 1
            for left, right in zip(columns, columns[1:]):
                if right == left + 1:
                    run += 1
                else:
                    runs.append(run)
                    run = 1
            runs.append(run)
        return {
            "rows_with_free_seats": len(by_row),
            "longest_run": max(runs, default=0),
            "runs_at_least_2": sum(1 for r in runs if r >= 2),
            "runs_at_least_3": sum(1 for r in runs if r >= 3),
            "total_runs": len(runs),
        }

    def fill_to_ratio(
        self, ratio: float, *, scatter: bool = True, seed: int = 20261019
    ) -> dict[str, Any]:
        """把整列车卖到指定上座率（开发者压测用）。

        ``scatter=True``（默认）按真实售票的样子**打散**占用，
        从而产生碎片化余票 —— 这正是"多人订单凑不出连座、需要自动分票"
        的触发条件。``scatter=False`` 保留旧的"按顺序占"行为，
        便于人工核对座位图。

        默认带固定 ``seed``，保证同一上座率每次得到同一份布局（可复现）。
        """
        ratio = max(0.0, min(1.0, float(ratio)))
        seats = list(self.formation.seats)
        target = int(round(len(seats) * ratio))
        current = len(self.occupied)
        if current < target:
            free = [seat for seat in seats if seat.seat_id not in self.occupied]
            if scatter:
                free = _scatter_order(free, random.Random(seed))
            for seat in free[: target - current]:
                self.occupied[seat.seat_id] = Occupancy(
                    seat_id=seat.seat_id, source=SOURCE_PRESET
                )
        elif current > target:
            removable = [
                seat_id for seat_id, item in self.occupied.items()
                if item.source == SOURCE_PRESET
            ]
            for seat_id in removable[: current - target]:
                self.occupied.pop(seat_id, None)
        return {
            "ratio": ratio,
            "occupied": len(self.occupied),
            "total": len(seats),
            "actual_ratio": round(len(self.occupied) / max(1, len(seats)), 4),
        }

    def record_order(self, payload: Mapping[str, Any]) -> None:
        self.orders.append(dict(payload))

    def snapshot(self) -> dict[str, Any]:
        """开发者模式的全局座位视图。"""
        bay_slots = self.bay_slot_ids()
        accessible_carriages = {
            carriage.number for carriage in self.formation.carriages
            if carriage.has_accessible_zone
        }
        seats: list[dict[str, Any]] = []
        for seat in self.formation.seats:
            record = self.occupied.get(seat.seat_id)
            seats.append({
                "seat_id": seat.seat_id,
                "carriage": seat.carriage,
                "row": seat.row,
                "col": seat.col,
                "class_code": seat.class_code,
                "quiet": seat.is_quiet_carriage,
                "accessible": seat.carriage in accessible_carriages,
                # 该座位紧邻某个轮椅停放位（**它仍是普通座位，可正常发售**）。
                # 停放位本身是独立资源、独立编号，不在座位表里 ——
                # 所以这里不叫 wheelchair_bay，避免又被理解成"这个座位就是停放位"。
                "bay_slot": seat.seat_id in bay_slots,
                "aisle": seat.is_aisle,
                "occupied": record is not None,
                "source": record.source if record else "",
                "order_id": record.order_id if record else "",
                "passenger_name": record.passenger_name if record else "",
                "color_index": record.color_index if record else 0,
            })
        carriages = []
        for carriage in self.formation.carriages:
            rows = [s for s in self.formation.seats if s.carriage == carriage.number]
            # 逐席别计数：混合车厢（01车=一等32+商务5、08车=二等43+商务6）
            # 只显示"主席别 + 总定员"是看不出构成的，需求给的车型图正是
            # 用 "32/5" 这种写法标注的。
            class_totals: dict[str, int] = {}
            class_remaining: dict[str, int] = {}
            for seat in rows:
                class_totals[seat.class_code] = class_totals.get(seat.class_code, 0) + 1
                if seat.seat_id not in self.occupied:
                    class_remaining[seat.class_code] = (
                        class_remaining.get(seat.class_code, 0) + 1
                    )
            carriages.append({
                "number": carriage.number,
                "class_code": carriage.class_code,
                "columns": list(carriage.columns),
                "rows": carriage.rows,
                "quiet": carriage.is_quiet_carriage,
                "accessible": carriage.has_accessible_zone,
                "toilet": carriage.has_toilet,
                "wheelchair_bays": carriage.wheelchair_bays,
                "total": len(rows),
                "remaining": sum(
                    1 for s in rows if s.seat_id not in self.occupied
                ),
                "class_totals": class_totals,
                "class_remaining": class_remaining,
                # 车型图的写法："一等座32+商务座5"
                "class_summary": "+".join(
                    f"{name}{count}" for name, count in class_totals.items()
                ),
            })
        return {
            "train_code": self.formation.train_code,
            "total_seats": len(self.formation.seats),
            "occupied_count": len(self.occupied),
            "remaining": self.remaining(),
            "remaining_by_carriage": self.remaining_by_carriage(),
            "wheelchair_bays": self.wheelchair_bays_summary(),
            "carriages": carriages,
            "seats": seats,
            "orders": list(self.orders),
            "quiet_carriages": sorted(
                c.number for c in self.formation.carriages if c.is_quiet_carriage
            ),
        }

    def reset(self) -> None:
        self.occupied.clear()
        self.occupied_bays.clear()
        self.blocked_slots.clear()
        self.orders.clear()
        self._color_seq = 0


_STORE: DevStore | None = None


def get_dev_store(formation: TrainFormation | None = None) -> DevStore:
    global _STORE
    if _STORE is None:
        if formation is None:
            from ..carriage import g25_16_car_formation

            formation = g25_16_car_formation()
        _STORE = DevStore(formation=formation)
    return _STORE


def reset_dev_store(formation: TrainFormation | None = None) -> DevStore:
    global _STORE
    if formation is None:
        from ..carriage import g25_16_car_formation

        formation = g25_16_car_formation()
    _STORE = DevStore(formation=formation)
    return _STORE


__all__ = [
    "SOURCE_MANUAL",
    "SOURCE_PRESET",
    "SOURCE_SOLD",
    "DevStore",
    "Occupancy",
    "get_dev_store",
    "reset_dev_store",
]
