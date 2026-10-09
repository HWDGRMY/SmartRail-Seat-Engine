"""出票结果的**形式化规范**：把"合不合理"变成可机检的不变量。

为什么需要这个模块
==================

本项目原有的 700+ 条断言都是**期望式**的："我期望 A，看是否得到 A"。
它们能守住已知行为，但守不住"**涌现出来的不合理结果**"——
因为没人预先写下"这里不该这样"。

真实代价（写这个模块时立刻抓到）：V2 的 CP-SAT 在
``strict_hard=True`` 下对"2 成人 + 2 儿童"返回

    assignments=0, waitlisted=4, total_affinity=0.0
    notes=['CP-SAT OPTIMAL：变量 192，座对项 468，目标值 -0.0']

**整列车是空的，却把 4 个人全部候补，还声称"最优"。**
它违反的是一条谁都没有显式断言过的常识：

    **有空座时不得把人放进候补。**

所以这里把这类常识集中写成**不变量（invariant）**，由
:func:`check_solution` 对任意求解器（V1 / V2 / V3）的任意解统一核验。
判据与被测对象解耦：**规范只描述"结果必须满足什么"，不关心谁产出的。**

不变量清单
==========

分为四级：

``RESOURCE`` 资源一致性（违反即事故）
    R1 同一座位不得分给两个人
    R2 一人最多一个座位
    R3 座位必须真实存在
    R4 已占用的座位不得再分配
    R5 轮椅停放位不得重复分配
    R6 轮椅旅客不得占用普通座位（停放位尚有空余时）
    R7 普通旅客不得占用轮椅停放位
    R8 座位席别必须符合订单要求

``COMPLETENESS`` 出票完整性（违反即事故）
    C1 **有空座时不得候补**（"出票优先"的机器判据）
    C2 候补与已分配不得重叠
    C3 结果必须覆盖订单里的每一个人（不能凭空少人）

``SAFETY`` 安全底线（违反即事故）
    S1 硬绑定配对必须同车厢（Tier 0）
    S2 需照护者必须与照护人同车厢
    S3 订单不得跨车厢（同订单默认同车厢）

``CONSISTENCY`` 自洽性（违反即事故）
    K1 报告的总亲和度必须等于独立复算的结果
    K2 声明的 Tier 0 违规数必须与逐条复算一致
    K3 已出票人数必须与座位数一致
    K4 求解器不得同时声称"最优"和"什么都没做"

用法
====

    from smartrail.validation import check_solution, InvariantReport

    report = check_solution(order, solution, formation, occupied=set())
    if not report.ok:
        print(report.explain())

:mod:`tools.verify_conformance` 用它在场景矩阵与随机模糊测试上跑三套求解器。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from .models import (
    BondType,
    Seat,
    Solution,
    SupportNeed,
    TrainFormation,
)

__all__ = [
    "Invariant",
    "InvariantReport",
    "INVARIANTS",
    "check_solution",
    "check_allocation",
    "invariant_catalog",
]


# ---------------------------------------------------------------------------
# 不变量登记表
# ---------------------------------------------------------------------------

#: 违反即事故的等级。``RESOURCE`` / ``COMPLETENESS`` / ``SAFETY`` / ``CONSISTENCY``
SEVERITIES = ("RESOURCE", "COMPLETENESS", "SAFETY", "CONSISTENCY")


@dataclass(frozen=True)
class Invariant:
    """一条可机检的规范条款。"""

    code: str
    severity: str
    title: str
    detail: str

    def to_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "severity": self.severity,
            "title": self.title,
            "detail": self.detail,
        }


INVARIANTS: tuple[Invariant, ...] = (
    # ---- 资源一致性 ----
    Invariant("R1", "RESOURCE", "一个座位不能卖给两个人",
              "分配结果里同一 seat_id 只能出现一次；重复即超卖。"),
    Invariant("R2", "RESOURCE", "一个人最多一个座位",
              "同一 passenger_id 不得出现在多个分配里。"),
    Invariant("R3", "RESOURCE", "座位必须真实存在",
              "分配引用的 seat_id 必须在编组座位表里能找到。"),
    Invariant("R4", "RESOURCE", "已占用的座位不得再分配",
              "分配不得落在进单前已被占用的座位上。"),
    Invariant("R5", "RESOURCE", "轮椅停放位不得重复分配",
              "同一 bay_id 只能分配给一位轮椅旅客。"),
    Invariant("R6", "RESOURCE", "停放位有余时轮椅旅客不得坐普通座位",
              "全列停放位尚有空余时，轮椅旅客必须进入停放位；"
              "只有停放位售罄才允许『询问后改出普通坐票』。"),
    Invariant("R7", "RESOURCE", "普通旅客不得占用轮椅停放位",
              "停放位是轮椅旅客的专用资源，普通旅客落在上面记 Tier 0。"),
    Invariant("R8", "RESOURCE", "席别必须符合订单要求",
              "订单指定了 class_code 时，分配座位的席别必须与之相同。"),
    # ---- 出票完整性 ----
    Invariant("C1", "COMPLETENESS", "有空座时不得候补",
              "只要该席别还有空座，就不允许把人放进候补 —— "
              "这是『出票优先』的机器判据。V2 曾在空车上把 4 人全部候补"
              "并声称 OPTIMAL，就是被这一条抓住的。"),
    Invariant("C2", "COMPLETENESS", "候补与已分配不得重叠",
              "同一个人不能既已分配座位又在候补名单里。"),
    Invariant("C3", "COMPLETENESS", "不得凭空少人",
              "订单里的每位乘客必须要么有座位、要么在候补名单里。"),
    # ---- 安全底线 ----
    Invariant("S1", "SAFETY", "硬绑定配对必须同车厢",
              "MANDATORY 绑定的两人被拆到不同车厢即为 Tier 0 事故。"),
    Invariant("S2", "SAFETY", "需照护者必须与照护人同车厢",
              "needs_caregiver 的乘客不得独自落在没有照护人的车厢。"),
    Invariant("S3", "SAFETY", "同一订单不得跨车厢",
              "同订单默认同车厢；跨车厢即为把同行人拆散。"),
    # ---- 自洽性 ----
    Invariant("K1", "CONSISTENCY", "报告亲和度必须可复算（选入）",
              "solution.total_affinity 必须等于用**同一代价函数配置**独立复算的值。"
              "注意：只有调用方拿得到与求解器**完全一致**的 Scorer 时才有意义 —— "
              "配置稍有差别（例如是否带停放位信息）就会产生假违规，"
              "因此这条是**选入**的（传 scorer/ctx 才核验）。"),
    Invariant("K2", "CONSISTENCY", "Tier 0 计数必须与逐条复算一致（选入）",
              "不得少报硬约束违规。与 K1 同理，需要一致的 Scorer 配置。"),
    Invariant("K3", "CONSISTENCY", "出票人数必须与座位数一致",
              "seated 与 assignments 长度必须相等。"),
    Invariant("K4", "CONSISTENCY", "不得声称最优却什么都没做",
              "订单非空、车上有空座，却分配 0 人并声称 OPTIMAL/UNKNOWN，"
              "属于把『不可行』误报成『最优』。"),
)

INVARIANT_BY_CODE: dict[str, Invariant] = {item.code: item for item in INVARIANTS}


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------


@dataclass
class Breach:
    """一条被违反的规范。"""

    code: str
    severity: str
    title: str
    message: str
    context: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "title": self.title,
            "message": self.message,
            "context": self.context,
        }


@dataclass
class InvariantReport:
    """一次核验的完整结论。"""

    solver: str = ""
    order_id: str = ""
    passengers: int = 0
    seated: int = 0
    waitlisted: int = 0
    breaches: list[Breach] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.breaches

    @property
    def fatal(self) -> list[Breach]:
        """资源 / 完整性 / 安全 三类 —— 这些属于事故，不是"不够好"。"""
        return [b for b in self.breaches
                if b.severity in ("RESOURCE", "COMPLETENESS", "SAFETY")]

    def codes(self) -> list[str]:
        return sorted({b.code for b in self.breaches})

    def explain(self) -> str:
        if self.ok:
            return (f"[{self.solver}] {self.order_id}: 全部 "
                    f"{len(INVARIANTS)} 条不变量通过"
                    f"（{self.seated}/{self.passengers} 就座，"
                    f"候补 {self.waitlisted}）")
        lines = [
            f"[{self.solver}] {self.order_id}: 违反 {len(self.breaches)} 条"
            f"（{self.seated}/{self.passengers} 就座，候补 {self.waitlisted}）"
        ]
        for breach in self.breaches:
            lines.append(f"    [{breach.severity}] {breach.code} "
                         f"{breach.title} —— {breach.message}")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "solver": self.solver,
            "order_id": self.order_id,
            "passengers": self.passengers,
            "seated": self.seated,
            "waitlisted": self.waitlisted,
            "ok": self.ok,
            "codes": self.codes(),
            "breaches": [b.to_dict() for b in self.breaches],
        }


# ---------------------------------------------------------------------------
# 核心校验
# ---------------------------------------------------------------------------


def check_solution(
    order: Any,
    solution: Solution,
    formation: TrainFormation,
    *,
    occupied: Iterable[str] = (),
    class_code: str = "",
    bay_slot_ids: Iterable[str] = (),
    assigned_bays: Iterable[str] = (),
    free_bays: int | None = None,
    scorer: Any = None,
    ctx: Any = None,
) -> InvariantReport:
    """对任意求解器产出的 :class:`Solution` 逐条核验规范。

    参数
    ----
    order
        本次下单的订单（需有 ``order_id`` / ``passengers`` / ``bond_of``）。
    solution
        求解器产出。V1 / V2 共用同一 ``Solution`` 结构。
    formation
        编组（座位表 + 轮椅停放位）。
    occupied
        进单**之前**已被占用的座位号。用于 R4。
    class_code
        订单要求的席别；空字符串表示不限。
    bay_slot_ids
        停放位对应的内部座位号（求解器把停放位表达成座位）。
    assigned_bays
        已经分配出去的停放位编号（用于 R5 去重）。
    free_bays
        本单开始时**仍空余**的停放位数；``None`` 表示不检查 R6。
    scorer / ctx
        传入时额外复算亲和度（K1）与 Tier 0（K2）。
    """
    report = InvariantReport(
        solver=getattr(solution, "solver", "") or "",
        order_id=str(getattr(order, "order_id", "") or ""),
        passengers=len(getattr(order, "passengers", ()) or ()),
        seated=len(getattr(solution, "assignments", {}) or {}),
        waitlisted=len(getattr(solution, "waitlisted", ()) or ()),
    )

    seats: Mapping[str, Seat] = formation.by_id()
    occupied_set = set(occupied)
    bay_slots = set(bay_slot_ids)
    assigned = dict(getattr(solution, "assignments", {}) or {})
    waitlisted = list(getattr(solution, "waitlisted", ()) or ())
    passengers = list(getattr(order, "passengers", ()) or ())
    by_pid = {p.passenger_id: p for p in passengers}
    # 席别以**订单上登记的**为准 —— 调用方漏传 class_code 时不能因此漏检 R8
    # （踩过：测试脚本建单时没设 class_code、却给核验传了"二等座"，
    #  结果 V1 拿了 4 条假违规，白查一轮）。
    declared_class = str(class_code or getattr(order, "class_code", "") or "")

    # ---------------- R1 / R2：唯一性 ----------------
    seen: dict[str, str] = {}
    for pid, assignment in assigned.items():
        seat_id = getattr(assignment, "seat_id", "")
        if seat_id in seen:
            _breach(report, "R1",
                    f"{seat_id} 同时分给 {seen[seat_id]} 与 {pid}",
                    {"seat_id": seat_id, "passengers": [seen[seat_id], pid]})
        else:
            seen[seat_id] = pid
    # assigned 是 dict，天然满足"一人一座"；但求解器可能返回列表型结构
    if not isinstance(getattr(solution, "assignments", {}), dict):
        _breach(report, "R2", "assignments 不是 {passenger_id: seat} 映射")

    # ---------------- R3 / R4 / R8：座位合法性 ----------------
    for pid, assignment in assigned.items():
        seat_id = getattr(assignment, "seat_id", "")
        seat = seats.get(seat_id)
        if seat is None:
            _breach(report, "R3", f"{pid} 分到不存在的座位 {seat_id}",
                    {"passenger_id": pid, "seat_id": seat_id})
            continue
        if seat_id in occupied_set:
            _breach(report, "R4",
                    f"{pid} 分到进单前已占用的座位 {seat_id}",
                    {"passenger_id": pid, "seat_id": seat_id})
        if declared_class and seat.class_code != declared_class:
            _breach(report, "R8",
                    f"订单要求 {declared_class}，{pid} 却分到 "
                    f"{seat.class_code}（{seat_id}）",
                    {"passenger_id": pid, "seat_id": seat_id,
                     "want": declared_class, "got": seat.class_code})

    # ---------------- R5 / R6 / R7：轮椅停放位 ----------------
    if bay_slots:
        bay_used: dict[str, str] = {}
        for pid, assignment in assigned.items():
            seat_id = getattr(assignment, "seat_id", "")
            if seat_id not in bay_slots:
                continue
            bay_key = _canonical_bay(formation, seat_id)
            if bay_key in bay_used:
                _breach(report, "R5",
                        f"停放位 {bay_key} 同时分给 "
                        f"{bay_used[bay_key]} 与 {pid}",
                        {"bay": bay_key,
                         "passengers": [bay_used[bay_key], pid]})
            else:
                bay_used[bay_key] = pid
            passenger = by_pid.get(pid)
            if passenger is not None and not _is_wheelchair(passenger):
                _breach(report, "R7",
                        f"非轮椅旅客 {pid} 占用停放位 {bay_key}",
                        {"passenger_id": pid, "bay": bay_key})

        if free_bays is not None and free_bays > 0:
            for pid, assignment in assigned.items():
                passenger = by_pid.get(pid)
                if passenger is None or not _is_wheelchair(passenger):
                    continue
                seat_id = getattr(assignment, "seat_id", "")
                if seat_id not in bay_slots:
                    _breach(report, "R6",
                            f"{pid} 是轮椅旅客，但拿到普通座位 {seat_id}"
                            f"（当时仍有 {free_bays} 个停放位空余）",
                            {"passenger_id": pid, "seat_id": seat_id,
                             "free_bays": free_bays})

    # ---------------- C1：有空座不得候补 ----------------
    if waitlisted:
        placed_ids = {getattr(a, "seat_id", "") for a in assigned.values()}
        still_free = _free_seats(formation, occupied_set, placed_ids)
        if declared_class:
            still_free = [x for x in still_free
                          if x.class_code == declared_class]
        if len(still_free) >= len(waitlisted):
            label = f"『{declared_class}』" if declared_class else ""
            _breach(report, "C1",
                    f"{len(waitlisted)} 人进入候补，但车上还有 "
                    f"{len(still_free)} 个{label}空座 —— "
                    f"『出票优先』要求先出票",
                    {"waitlisted": waitlisted,
                     "free_seats": len(still_free)})

    # ---------------- C2 / C3：候补与覆盖 ----------------
    overlap = [pid for pid in waitlisted if pid in assigned]
    if overlap:
        _breach(report, "C2", f"{overlap} 既已分配又在候补名单", {"pids": overlap})
    missing = [p.passenger_id for p in passengers
               if p.passenger_id not in assigned
               and p.passenger_id not in set(waitlisted)]
    if missing:
        _breach(report, "C3",
                f"{len(missing)} 位乘客既没座位也不在候补名单：{missing[:6]}",
                {"missing": missing})

    # ---------------- S1 / S2 / S3：安全底线 ----------------
    if passengers:
        carriages = {}
        for pid, assignment in assigned.items():
            carriages[pid] = getattr(assignment, "carriage", None)
        # S1 硬绑定同车厢
        for index, a in enumerate(passengers):
            for b in passengers[index + 1:]:
                bond = _bond_of(order, a.passenger_id, b.passenger_id)
                if bond is not BondType.MANDATORY:
                    continue
                ca, cb = carriages.get(a.passenger_id), carriages.get(b.passenger_id)
                if ca is None or cb is None:
                    continue
                if ca != cb:
                    _breach(report, "S1",
                            f"硬绑定 {a.passenger_id}({ca}车) 与 "
                            f"{b.passenger_id}({cb}车) 被拆到不同车厢",
                            {"a": a.passenger_id, "b": b.passenger_id,
                             "carriage_a": ca, "carriage_b": cb})
        # S2 需照护者不得落单
        caregivers = {carriages[p.passenger_id]
                      for p in passengers
                      if getattr(p, "is_caregiver", False)
                      and p.passenger_id in carriages}
        for passenger in passengers:
            if not getattr(passenger, "needs_caregiver", False):
                continue
            home = carriages.get(passenger.passenger_id)
            if home is None:
                continue
            if home not in caregivers:
                _breach(report, "S2",
                        f"需照护的 {passenger.passenger_id} 在 {home} 车，"
                        f"该车厢没有任何照护人",
                        {"passenger_id": passenger.passenger_id,
                         "carriage": home})
        # S3 同一订单不得跨车厢
        used = {c for c in carriages.values() if c is not None}
        if len(used) > 1:
            _breach(report, "S3",
                    f"同一订单的座位跨了 {len(used)} 节车厢：{sorted(used)}",
                    {"carriages": sorted(used)})

    # ---------------- K1 / K2：复算 ----------------
    if scorer is not None and ctx is not None:
        try:
            from .solver import evaluate_placement

            seats_of = {pid: seats[a.seat_id] for pid, a in assigned.items()
                        if a.seat_id in seats}
            recomputed, violations = evaluate_placement(seats_of, ctx, scorer)
            declared = float(getattr(solution, "total_affinity", 0.0) or 0.0)
            if abs(recomputed - declared) > 1.0:
                _breach(report, "K1",
                        f"报告亲和度 {declared:.1f}，独立复算 {recomputed:.1f}"
                        f"（差 {recomputed - declared:+.1f}）",
                        {"declared": declared, "recomputed": recomputed})
            tier0 = [v for v in violations if getattr(v, "tier", 9) == 0]
            if tier0 and not getattr(solution, "violations", None):
                _breach(report, "K2",
                        f"独立复算发现 {len(tier0)} 条 Tier 0 违规，"
                        f"但 solution 未报告任何违规",
                        {"recomputed_tier0": len(tier0),
                         "first": str(getattr(tier0[0], "detail", ""))[:70]})
        except Exception as exc:  # noqa: BLE001 - 复算失败本身要报出来
            _breach(report, "K1", f"亲和度复算失败：{type(exc).__name__}: {exc}")

    # ---------------- K3：seated 与座位数一致 ----------------
    # ``Solution`` 上没有 seated 字段时跳过；有则必须是整数且与分配数一致。
    seated_attr = getattr(solution, "seated", None)
    if isinstance(seated_attr, bool) or (
        seated_attr is not None and not isinstance(seated_attr, int)
    ):
        seated_attr = None
    if seated_attr is not None and int(seated_attr) != len(assigned):
        _breach(report, "K3",
                f"solution.seated={seated_attr} 与 "
                f"assignments={len(assigned)} 不一致")

    # ---------------- K4：不得"声称最优却什么都没做" ----------------
    if passengers and not assigned and not waitlisted:
        _breach(report, "K4", "订单有乘客，却既没分配也没候补（结果为空）")
    elif passengers and not assigned and waitlisted:
        notes = " ".join(str(n) for n in (getattr(solution, "notes", ()) or ()))
        claims_best = any(word in notes.upper()
                          for word in ("OPTIMAL", "FEASIBLE", "UNKNOWN"))
        free_here = _free_seats(formation, occupied_set, set())
        if declared_class:
            free_here = [x for x in free_here
                         if x.class_code == declared_class]
        if claims_best and free_here:
            _breach(report, "K4",
                    f"全部 {len(waitlisted)} 人候补，却声称最优"
                    f"（notes 含 OPTIMAL/UNKNOWN），且车上有 "
                    f"{len(free_here)} 个空座 —— 把『不可行』误报成『最优』",
                    {"notes": notes[:120], "free_seats": len(free_here)})

    return report


def check_allocation(
    order: Any,
    allocation: Mapping[str, str],
    formation: TrainFormation,
    **kwargs: Any,
) -> InvariantReport:
    """便利封装：直接对 ``{passenger_id: seat_id}`` 做核验。

    供不产出 :class:`Solution` 的路径使用（例如 HTTP 接口返回的 ``seats``）。
    """
    from .models import Assignment

    solution = Solution(
        assignments={
            pid: Assignment(
                passenger_id=pid, seat_id=seat_id,
                carriage=formation.seat(seat_id).carriage if formation.seat(seat_id) else 0,
                row=formation.seat(seat_id).row if formation.seat(seat_id) else 0,
                col=formation.seat(seat_id).col if formation.seat(seat_id) else "",
            )
            for pid, seat_id in allocation.items()
        },
        waitlisted=list(kwargs.pop("waitlisted", ()) or ()),
        total_affinity=float(kwargs.pop("total_affinity", 0.0) or 0.0),
        violations=list(kwargs.pop("violations", ()) or ()),
        solver=str(kwargs.pop("solver", "http")),
    )
    return check_solution(order, solution, formation, **kwargs)


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def _breach(report: InvariantReport, code: str, message: str,
            context: dict[str, Any] | None = None) -> None:
    meta = INVARIANT_BY_CODE[code]
    report.breaches.append(Breach(
        code=code, severity=meta.severity, title=meta.title,
        message=message, context=context or {},
    ))


def _free_seats(formation: TrainFormation, occupied: set[str],
                assigned: set[str]) -> list[Seat]:
    return [s for s in formation.seats
            if s.seat_id not in occupied and s.seat_id not in assigned]


def _canonical_bay(formation: TrainFormation, seat_id: str) -> str:
    """把停放位的内部座位号映射回停放位编号（票面编号）。"""
    for bay in formation.wheelchair_bays:
        if bay.slot_seat_id == seat_id:
            return bay.bay_id
    return seat_id


def _is_wheelchair(passenger: Any) -> bool:
    needs = getattr(passenger, "support_needs", None) or frozenset()
    return SupportNeed.WHEELCHAIR in needs


def _bond_of(order: Any, a: str, b: str) -> BondType:
    fn = getattr(order, "bond_of", None)
    if callable(fn):
        return fn(a, b)
    return BondType.SOFT


def invariant_catalog() -> list[dict[str, str]]:
    """规范清单（供文档与验收页展示）。"""
    return [item.to_dict() for item in INVARIANTS]
