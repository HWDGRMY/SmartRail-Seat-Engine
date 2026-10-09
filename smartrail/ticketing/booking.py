"""12306 风格的下单流程：选座偏好 → 余票判定 → 自动分票 → 出票。

需求要点（逐条对应）
--------------------
1. **用户端不可见整辆列车的座位分布图** —— 本模块对外只暴露"一排座位"
   （:func:`seat_row_options`）与聚合余票，从不返回全列座位图。
2. **选座服务仅显示一排座位（A、B、C、D、F），供用户选择偏好** ——
   走 :func:`seat_row_options`。
3. **提供"静音车厢"勾选项** —— ``SeatPreference.quiet``。
4. **仅当多人订单且当前余票无法满足用户选座要求时，自动分票逻辑才介入** ——
   走 :func:`evaluate_preference`：先判断"能不能满足"，只有不能满足时才
   降级为"尽量就近拆分"，并在结果里明示 :attr:`PreferenceVerdict.split_needed`。

为什么把"能不能满足"单独算一遍，而不是直接交给求解器
------------------------------------------------------
求解器一定会给出一个解（它总能找到座位或候补），但**它不会告诉你
"这个解其实没满足你的选座要求"**。用户需要知道的是
"我给你换了座位，因为连着的没有 3 个了" —— 这句话必须由流程层显式产生，
不能指望代价函数替用户解释。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Mapping, Sequence

from ..models import Seat
from .devstore import SOURCE_SOLD, DevStore
from .passenger_store import PassengerProfile, build_profile_order, validate_selection

#: 一等座/商务座也用同一套列名，但只有部分列存在
ROW_COLUMNS: tuple[str, ...] = ("A", "B", "C", "D", "F")


@dataclass
class SeatPreference:
    """用户的选座偏好（对应 12306 的"选座服务"）。"""

    columns: tuple[str, ...] = ()
    """偏好的列，如 ``("A", "F")`` 表示想要靠窗。空 = 无偏好。"""

    quiet: bool = False
    """是否优先静音车厢。"""

    def to_dict(self) -> dict[str, Any]:
        return {"columns": list(self.columns), "quiet": self.quiet}

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any] | None) -> SeatPreference:
        payload = payload or {}
        raw = payload.get("columns") or []
        columns = tuple(str(item).upper() for item in raw if str(item).upper() in ROW_COLUMNS)
        return cls(columns=columns, quiet=bool(payload.get("quiet", False)))


@dataclass
class PreferenceVerdict:
    """"用户的选座要求能不能满足"的判定结论。"""

    order_size: int
    same_row_possible: bool
    """能否让这一单的人坐在**同一排且连续**。"""

    quiet_possible: bool
    largest_run: int
    """同排里最长的一段连续可用座位数。"""

    split_needed: bool
    """是否需要自动分票（余票无法满足选座要求）。"""

    reason: str = ""
    suggestion: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "order_size": self.order_size,
            "same_row_possible": self.same_row_possible,
            "quiet_possible": self.quiet_possible,
            "largest_run": self.largest_run,
            "split_needed": self.split_needed,
            "reason": self.reason,
            "suggestion": self.suggestion,
        }


@dataclass
class TrainAvailability:
    """一个车次的余票概览（**不含**座位分布图）。"""

    train_code: str
    depart: str
    arrive: str
    duration: str
    classes: list[dict[str, Any]] = field(default_factory=list)
    quiet_carriages: list[int] = field(default_factory=list)
    total_seats: int = 0
    remaining_seats: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "train_code": self.train_code,
            "depart": self.depart,
            "arrive": self.arrive,
            "duration": self.duration,
            "classes": list(self.classes),
            "quiet_carriages": list(self.quiet_carriages),
            "total_seats": self.total_seats,
            "remaining_seats": self.remaining_seats,
        }


# ---------------------------------------------------------------------------
# 选座服务：只显示一排座位
# ---------------------------------------------------------------------------


def seat_row_options(
    store: DevStore, class_code: str, columns: Sequence[str] = ROW_COLUMNS
) -> list[dict[str, Any]]:
    """返回**一排座位**供用户点选偏好（A/B/C/D/F）。

    这是用户端能看到的全部座位信息 —— 不含排号、不含车厢号、不含占用明细，
    只有"这一列的余票够不够"。严格对齐 12306：用户看到的是
    "A B C 过道 D F" 这一排，而不是整列车。
    """
    class_seats = [s for s in store.formation.seats if s.class_code == class_code]
    total = len(class_seats)
    result: list[dict[str, Any]] = []
    for col in columns:
        matching = [s for s in class_seats if s.col == col]
        if not matching:
            continue
        free = [s for s in matching if not store.is_occupied(s.seat_id)]
        result.append({
            "col": col,
            "label": col,
            "total": len(matching),
            "remaining": len(free),
            "sold_out": not free,
            "feature": ("window" if col in ("A", "F") else
                        "aisle" if col in ("C", "D") else "middle"),
        })
    _ = total
    return result


# ---------------------------------------------------------------------------
# 余票判定与自动分票
# ---------------------------------------------------------------------------


def _runs_in_row(
    free_cols: set[str], columns: Sequence[str]
) -> list[list[str]]:
    """把"这一排可用的列"切成若干段连续座位。

    过道不算中断 —— 现实中 A/B/C 三人与 D/F 两人中间隔着过道，
    仍算"同一排就座"，这也是 12306 的实际行为。
    """
    runs: list[list[str]] = []
    current: list[str] = []
    for col in columns:
        if col in free_cols:
            current.append(col)
        elif current:
            runs.append(current)
            current = []
    if current:
        runs.append(current)
    return runs


def evaluate_preference(
    store: DevStore,
    class_code: str,
    order_size: int,
    preference: SeatPreference | None = None,
) -> PreferenceVerdict:
    """判断"这一单的选座要求能不能满足"，并决定是否需要自动分票。

    判定顺序（**只有不满足时才降级**）：

    1. 先看**偏好列**：如果用户指定了 A/F，而偏好列所在排凑不出连续 N 座，
       就说明"选座要求无法满足"；
    2. 没有偏好列时，看**任意列**能否凑出连续 N 座；
    3. 静音车厢勾选但静音车厢已无 N 连座 → 也算无法满足。
    """
    preference = preference or SeatPreference()
    class_seats = [s for s in store.formation.seats if s.class_code == class_code]
    if not class_seats:
        return PreferenceVerdict(
            order_size=order_size, same_row_possible=False, quiet_possible=False,
            largest_run=0, split_needed=True,
            reason=f"编组里没有席别：{class_code}",
            suggestion="请选择其它席别",
        )

    free = [s for s in class_seats if not store.is_occupied(s.seat_id)]
    if len(free) < order_size:
        return PreferenceVerdict(
            order_size=order_size, same_row_possible=False, quiet_possible=False,
            largest_run=0, split_needed=True,
            reason=f"{class_code} 仅剩 {len(free)} 张，不足 {order_size} 张",
            suggestion=self_suggest_other_class(store, class_code, order_size),
        )

    # 按 (车厢, 排) 分组，找出最长连续段
    def longest_run(seats: Iterable[Seat], columns: Sequence[str]) -> int:
        rows: dict[tuple[int, int], set[str]] = {}
        for seat in seats:
            rows.setdefault((seat.carriage, seat.row), set()).add(seat.col)
        best = 0
        for free_cols in rows.values():
            for run in _runs_in_row(free_cols, columns):
                best = max(best, len(run))
        return best

    columns = tuple(preference.columns) if preference.columns else ROW_COLUMNS
    largest = longest_run(free, columns)
    same_row = largest >= order_size

    quiet_free = [s for s in free if s.is_quiet_carriage]
    quiet_possible = (not preference.quiet) or (
        longest_run(quiet_free, columns) >= order_size
    )

    if same_row and quiet_possible:
        return PreferenceVerdict(
            order_size=order_size, same_row_possible=True, quiet_possible=True,
            largest_run=largest, split_needed=False,
            reason="余票可以满足选座要求",
            suggestion="",
        )

    if not same_row:
        reason = (
            f"所选席别最多只能凑出 {largest} 个连续座位，"
            f"不足一单 {order_size} 人"
        )
        suggestion = "系统将自动分票：尽量安排在同一车厢的邻近座位"
    else:
        reason = "静音车厢已无满足人数的连续座位"
        suggestion = "系统将自动分票：可能安排到非静音车厢"
        # 看看非静音能不能满足
        if not preference.columns:
            non_quiet = [s for s in free if not s.is_quiet_carriage]
            if longest_run(non_quiet, ROW_COLUMNS) >= order_size:
                suggestion = "非静音车厢仍有连续座位，将优先安排"
    return PreferenceVerdict(
        order_size=order_size, same_row_possible=same_row, quiet_possible=quiet_possible,
        largest_run=largest, split_needed=True, reason=reason, suggestion=suggestion,
    )


def self_suggest_other_class(store: DevStore, class_code: str, order_size: int) -> str:
    """本席别不够时，建议改乘哪个席别。"""
    for other, count in sorted(store.remaining().items(), key=lambda kv: -kv[1]):
        if other == class_code:
            continue
        if count >= order_size:
            return f"可改选 {other}（余票 {count} 张）"
    return "全列余票均不足，建议候补或改乘其它车次"


# ---------------------------------------------------------------------------
# 下单
# ---------------------------------------------------------------------------


def build_order_from_profiles(
    profiles: Sequence[PassengerProfile],
    order_id: str = "TICKET",
    preference: SeatPreference | None = None,
):
    """把乘车人变成订单，并把**选座偏好写进每位乘客**。

    这一步是必须的：偏好若只停留在流程层，求解器根本读不到它 ——
    早期实现就是这样，"勾选静音"与"不勾选"的下座**完全一样**，
    界面上的勾选框成了装饰。

    ``A``/``F`` 映射为靠窗偏好，``C``/``D`` 映射为过道偏好。
    """
    order = build_profile_order(profiles, order_id=order_id)
    preference = preference or SeatPreference()
    if not preference.columns and not preference.quiet:
        return order
    columns = set(preference.columns)
    patched = tuple(
        replace(
            passenger,
            preference_window=("A" in columns or "F" in columns),
            preference_aisle=("C" in columns or "D" in columns),
            preference_quiet=preference.quiet,
        )
        for passenger in order.passengers
    )
    return replace(order, passengers=patched)


def book_ticket_order(
    store: DevStore,
    profiles: Sequence[PassengerProfile],
    *,
    class_code: str = "二等座",
    preference: SeatPreference | None = None,
    order_id: str = "TICKET",
    engine: Any = None,
) -> dict[str, Any]:
    """按 12306 流程下一单：校验 → 判定 → 求解 → 写台账。

    ``engine`` 可传入既有 :class:`~smartrail.engine.SeatEngine`；不传则临时建一个
    并**把开发者台账里的占用同步进去**，保证"用户看到的余票"与"求解器实际可用
    座位"是同一份数据。
    """
    errors = validate_selection(profiles)
    if errors:
        return {"ok": False, "errors": errors, "verdict": None, "solution": None}

    preference = preference or SeatPreference()
    order_size = len(profiles)
    verdict = evaluate_preference(store, class_code, order_size, preference)
    wheelchair = evaluate_wheelchair_bays(store, profiles)

    order = build_order_from_profiles(profiles, order_id=order_id,
                                      preference=preference)
    # **席别硬约束**：把已购席别写进订单，求解器据此过滤候选座位。
    # 不这么做的话"下单商务座"也会被分到二等座（实测发生过）。
    order = replace(order, class_code=class_code)
    engine = engine or _engine_from_store(store)
    # AllocationMode 只有 free / smart / degraded 三种。
    # 购票流程要的是**智能分配**（带代价函数与硬约束），而不是"自由选座"
    # （那是用户自己点具体座位，本系统按需求不暴露座位图）。
    result = engine.book(order, mode="smart")

    solution = result.solution
    assignments: dict[str, str] = {}
    for passenger_id, assignment in solution.assignments.items():
        assignments[passenger_id] = assignment.seat_id

    # 座位实际归属哪节车厢/哪些排 —— 用来判断"系统是否真的分票了"
    seat_index = {s.seat_id: s for s in store.formation.seats}
    carriages = sorted({seat_index[sid].carriage for sid in assignments.values()
                        if sid in seat_index})
    rows = sorted({(seat_index[sid].carriage, seat_index[sid].row)
                   for sid in assignments.values() if sid in seat_index})
    actually_split = len(rows) > 1

    # ---- 第一步：先给轮椅旅客分配**停放位独立编号** ----
    # 他们从 assignments 里被摘出来，因此**不会占用任何座位** ——
    # 这就是"无障碍完全独立编号 / 不占座位票额"的落地点。
    color = store.next_color()
    class_seats = {s.seat_id: s for s in store.formation.seats}
    passengers_payload = []
    for passenger in order.passengers:
        seat_id = assignments.get(passenger.passenger_id)
        seat = class_seats.get(seat_id or "")
        passengers_payload.append({
            "passenger_id": passenger.passenger_id,
            "name": passenger.name,
            "ticket_type": passenger.ticket_type.value,
            "seat_id": seat_id or "",
            "carriage": seat.carriage if seat else 0,
            "row": seat.row if seat else 0,
            "col": seat.col if seat else "",
            "class_code": seat.class_code if seat else "",
            "quiet": bool(seat.is_quiet_carriage) if seat else False,
        })
    assigned_bays = _apply_bay_identity(store, order, passengers_payload,
                                        color_index=color, order_id=order_id)
    bay_by_passenger = {
        payload["passenger_id"]: payload["wheelchair_bay"]
        for payload in passengers_payload if payload.get("wheelchair_bay")
    }

    # ---- 第二步：只把"真正坐座位的人"写进座位台账 ----
    # 拿到停放位的轮椅旅客不在此列（他们不占座位票额）。
    name_by_seat: dict[str, str] = {}
    for passenger in order.passengers:
        if passenger.passenger_id in bay_by_passenger:
            continue
        seat_id = assignments.get(passenger.passenger_id)
        if seat_id:
            name_by_seat[seat_id] = passenger.name
    store.occupy(
        name_by_seat.keys(),
        source=SOURCE_SOLD,
        order_id=order_id,
        color_index=color,
    )
    for seat_id, name in name_by_seat.items():
        record = store.occupied.get(seat_id)
        if record is not None:
            record.passenger_name = name
            record.color_index = color
    order_record = {
        "order_id": order_id,
        "class_code": class_code,
        "preference": preference.to_dict(),
        "passengers": passengers_payload,
        "seated": len(assignments),
        "waitlisted": len(solution.waitlisted),
        "carriages": carriages,
        "rows": [f"{carriage}车{row}排" for carriage, row in rows],
        "split": actually_split,
        "color_index": color,
        "verdict": verdict.to_dict(),
        "wheelchair": wheelchair.to_dict(),
        "train_code": store.formation.train_code,
    }
    store.record_order(order_record)
    return {
        "ok": len(assignments) > 0,
        "errors": [],
        "verdict": verdict.to_dict(),
        "wheelchair": wheelchair.to_dict(),
        "order": order_record,
        "notes": list(getattr(solution, "notes", ()) or ()),
        "tier0": len(getattr(solution, "tier0_violations", ()) or ()),
    }


def _apply_bay_identity(
    store: DevStore,
    order: Any,
    passengers_payload: list[dict[str, Any]],
    color_index: int = 0,
    order_id: str = "",
) -> list[str]:
    """把轮椅旅客的座位号换成**停放位独立编号**并占用停放位。

    需求："无障碍要求完全独立编号"。所以：

    * 凭证上写 ``04车W1``，**不写座位号**；
    * 停放位按编号独立记账（``store.occupy_bays``），
      **不占用任何座位票额**；
    * 求解器内部给的落点座位保留在 ``internal_seat_id`` 里，
      仅供工程侧排查，不进入票面。

    返回实际分配的停放位编号列表。
    """
    from ..models import SupportNeed

    occupied_now = store.occupied_bay_ids()
    free = [
        bay for bay in store.formation.wheelchair_bays
        if bay.bay_id not in occupied_now
    ]
    assigned: list[str] = []
    for passenger, payload in zip(order.passengers, passengers_payload):
        if SupportNeed.WHEELCHAIR not in passenger.support_needs:
            continue
        if not free:
            # 停放位已满：按"询问后出普通坐票"的结论，保留座位号
            payload["wheelchair_bay"] = ""
            payload["bay_assigned"] = False
            continue
        bay = free.pop(0)
        payload["internal_seat_id"] = payload["seat_id"]
        payload["seat_id"] = bay.bay_id          # 票面写停放位编号
        payload["wheelchair_bay"] = bay.bay_id
        payload["bay_assigned"] = True
        payload["carriage"] = bay.carriage
        payload["row"] = bay.row
        payload["col"] = ""
        assigned.append(bay.bay_id)
    store.occupy_bays(assigned, order_id=order_id, color_index=color_index)
    # 回填姓名，便于开发者座位图展示
    for bay_id in assigned:
        record = store.occupied_bays.get(bay_id)
        if record is None:
            continue
        for passenger, payload in zip(order.passengers, passengers_payload):
            if payload.get("wheelchair_bay") == bay_id:
                record["passenger_name"] = passenger.name
    return assigned


def _engine_from_store(store: DevStore):
    """用台账的占用状态建一个引擎，保证余票口径一致。"""
    from ..engine import SeatEngine

    engine = SeatEngine(formation=store.formation)
    occupied = store.engine_occupied if hasattr(store, 'engine_occupied') else set(store.occupied)
    if occupied:
        # 注意必须传集合：``mark_occupied`` 的入参是座位 ID 序列，
        # 传单个字符串会被逐字符迭代（clustering 里已加显式拦截）。
        engine.state.mark_occupied(occupied)
    return engine


@dataclass
class WheelchairOutcome:
    """轮椅停放位的安排结论（用户模式要据此提示/询问）。"""

    requested: int
    """这一单里有几位轮椅旅客。"""

    bays_free: int
    """可用停放位数量（下单前）。"""

    bays_assigned: int
    """实际分配到停放位的人数。"""

    needs_confirmation: bool
    """停放位不够 -> 需要询问用户是否同意改出普通坐票。"""

    question: str = ""
    """要问用户的原话。"""

    def to_dict(self) -> dict[str, Any]:
        return {
            "requested": self.requested,
            "bays_free": self.bays_free,
            "bays_assigned": self.bays_assigned,
            "needs_confirmation": self.needs_confirmation,
            "question": self.question,
        }


def evaluate_wheelchair_bays(
    store: DevStore, profiles: Sequence[PassengerProfile]
) -> WheelchairOutcome:
    """判断这一单的轮椅停放位够不够，不够就给出询问文案。

    需求原文："每一节无障碍车厢（4，12）各有两个轮椅固定停放位，
    不占正常座位票额，如果 4 个轮椅位都卖完了，可在询问后出正常坐票。"

    注意这是**询问后出票**，不是拒票 —— 与项目一贯的"出票优先"原则一致。
    """
    from ..models import SupportNeed

    needed = sum(
        1 for profile in profiles
        if SupportNeed.WHEELCHAIR in profile.support_needs()
    )
    free = len(store.free_bays())
    if needed == 0:
        return WheelchairOutcome(0, free, 0, False)
    assigned = min(needed, free)
    if needed <= free:
        return WheelchairOutcome(needed, free, assigned, False)
    short = needed - free
    total = len(store.formation.wheelchair_bays)
    question = (
        f"本单有 {needed} 位轮椅旅客，但轮椅固定停放位仅剩 {free} 个"
        f"（全列共 {total} 个，分布在 04 车与 12 车）。"
        f"是否同意其中 {short} 位改出**普通坐票**？"
        f"（轮椅停放位独立于座位票额，普通坐票仍可正常出票，"
        f"站车将协助上下车）"
    )
    return WheelchairOutcome(needed, free, assigned, True, question)


# ---------------------------------------------------------------------------
# 车次列表（12306 的查询结果页）
# ---------------------------------------------------------------------------

#: 京沪高铁的典型车次（列表页展示用）
TRAIN_SCHEDULE: tuple[dict[str, str], ...] = (
    {"train_code": "G25", "depart": "17:00", "arrive": "21:18",
     "duration": "4小时18分", "from": "北京南", "to": "上海虹桥", "tag": "复兴号"},
    {"train_code": "G35", "depart": "19:24", "arrive": "23:51",
     "duration": "4小时27分", "from": "北京南", "to": "上海虹桥", "tag": "复兴号"},
    {"train_code": "G531", "depart": "06:08", "arrive": "12:04",
     "duration": "5小时56分", "from": "北京南", "to": "上海虹桥", "tag": "复兴号"},
)

#: 席别票价（按二等座为基准的倍数，仅用于界面展示）
CLASS_PRICES: dict[str, dict[str, Any]] = {
    "二等座": {"base": 661, "discount": "8.4折"},
    "一等座": {"base": 1058, "discount": "8.4折"},
    "商务座": {"base": 2315, "discount": "8.4折"},
}


def available_trains(store: DevStore) -> list[dict[str, Any]]:
    """车次列表：每个车次带各席别余票（**只有数字，没有座位图**）。"""
    remaining = store.remaining()
    trains: list[dict[str, Any]] = []
    for item in TRAIN_SCHEDULE:
        classes = []
        for class_code, price in CLASS_PRICES.items():
            count = remaining.get(class_code, 0)
            classes.append({
                "class_code": class_code,
                "remaining": count,
                "total": store.total_seats(class_code),
                "price": price["base"],
                "discount": price["discount"],
                # 12306 的措辞：有票 / N 张 / 无
                "status": "有票" if count >= 20 else (f"{count}张" if count > 0 else "无"),
                "bookable": count > 0,
            })
        trains.append({
            **item,
            "classes": classes,
            "remaining_seats": sum(remaining.values()),
            "total_seats": store.total_seats(),
            "quiet_carriages": sorted(
                c.number for c in store.formation.carriages if c.is_quiet_carriage
            ),
            "seat_row": [
                {"col": col, "feature": ("window" if col in ("A", "F") else
                                         "aisle" if col in ("C", "D") else "middle")}
                for col in ROW_COLUMNS
            ],
        })
    return trains


__all__ = [
    "CLASS_PRICES",
    "ROW_COLUMNS",
    "TRAIN_SCHEDULE",
    "PreferenceVerdict",
    "SeatPreference",
    "TrainAvailability",
    "available_trains",
    "book_ticket_order",
    "build_order_from_profiles",
    "evaluate_preference",
    "seat_row_options",
]
