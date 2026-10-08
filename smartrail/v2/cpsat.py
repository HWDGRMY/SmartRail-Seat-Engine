"""V2.0：Google OR-Tools CP-SAT 求解器（MILP 精确建模）。

建模思路（对应 README 第 7 节 V2.0 "转化为混合整数线性规划"）
-----------------------------------------------------------
问题本质是一个**带约束的二部图匹配**：乘客集合 × 座位集合。

决策变量
    ``x[p,s] ∈ {0,1}``：乘客 p 是否坐在候选座位 s 上。

硬约束（Tier 0 / Tier 1 —— 对应 README 的"加入轮椅、孕妇等硬约束"）
    1. 每位乘客**至多**一个座位（用 ``≤1`` 而非 ``=1``：装不下就候补，
       而不是让整个模型不可行）；
    2. 每个座位至多一位乘客；
    3. 轮椅乘客的候选集**只含**无障碍专区座位（建模前就排除违规可能）；
    4. 强绑定（MANDATORY / STRONG）必须**同车厢**：
       ``sum_{s∈car_c} x[p,s] == sum_{s∈car_c} x[q,s]`` 对每个车厢 c 成立；
    5. 孕晚期旅客必须与同行人同车厢（由第 4 条覆盖，因为其绑定为 MANDATORY）；
    6. 硬核单元的 MANDATORY 成员**整体就座或整体候补**：
       ``sum_s x[p,s] == sum_s x[q,s]``，杜绝"孩子上车、家长候补"。

线性化技巧（本模块最关键的一步）
    目标里的座对项在朴素写法下是二次的（``x[p,s]·x[q,t]``）。这里用两个手段
    把它**完全线性化**：

    * **跨车厢惩罚不出现在目标函数里**：强绑定乘客的候选池只保留共同车厢的座位，
      "跨车厢"被结构性地排除，而不是靠巨额负分去惩罚；
    * **同车厢内的座对项**用标准线性化变量
      ``y ≤ x[p,s]``、``y ≤ x[q,t]``、``y ≥ x[p,s]+x[q,t]-1``。
      分离惩罚（同车厢但不相邻）则先引入"乘客 p 在车厢 c 的列指示量"
      ``w[p,c,k] = Σ_{s∈c, col=k} x[p,s]``，再对 ``w[p,c,k]·w[q,c,l]`` 施加同样的
      线性化 —— 变量的列数远小于座位数，因此这一层额外开销很小。

目标函数（软约束）
    最大化 ``Σ indiv[p,s]·x[p,s] + Σ pair[·]·y[·]``，其中 ``indiv`` / ``pair``
    与 V1.0 的代价函数**同源**（复用 :class:`smartrail.fastscore.FastScorer`），
    保证三个版本可直接比较。

求解与回退
    预算内求出 OPTIMAL 即返回；超时返回 CP-SAT 的最好可行解（FEASIBLE）；
    模型不可行则回退 V1.0 启发式，并在 ``notes`` 中说明原因 —— 服务可用性
    永远优先于"必须用运筹学解"。
"""

from __future__ import annotations

import time
from itertools import combinations
from typing import Any, Sequence

from ..clustering import BookingState
from ..config import EngineConfig
from ..fastscore import FastScorer
from ..models import BondType, Order, Seat, Solution
from ..scoring import Scorer
from ..solver import (
    OrderContext,
    _to_assignments,
    build_context,
    evaluate_placement,
    wheelchair_waitlist,
)

DEFAULT_TIME_BUDGET_MS = 900.0
"""CP-SAT 默认时间预算。运筹学求解器比启发式慢一个量级，换来全局最优性；
在线链路可用 ``time_budget_ms`` 收紧到 100ms 以内。"""

CANDIDATES_PER_PASSENGER = 48
"""每位乘客保留的候选座位数（精确性与规模的权衡）。

1156 座 × 20 人若全量建模会产生 2 万多个布尔变量；压缩到 48/人后约 1 千个，
CP-SAT 能在百毫秒级给出最优解。
"""


class CpSatUnavailable(RuntimeError):
    """未安装 OR-Tools。"""


def _require_cp_model() -> Any:
    try:
        from ortools.sat.python import cp_model  # type: ignore
    except ImportError as error:  # pragma: no cover - 取决于运行环境
        raise CpSatUnavailable("V2.0 求解器需要 OR-Tools：pip install ortools") from error
    return cp_model


# ---------------------------------------------------------------------------
# 候选池：把硬约束"内嵌"进候选集合
# ---------------------------------------------------------------------------


def build_candidates(
    ctx: OrderContext,
    seats: list[Seat],
    config: EngineConfig,
    per_passenger: int = CANDIDATES_PER_PASSENGER,
) -> dict[str, list[int]]:
    """为每位乘客生成候选座位槽（已内嵌 Tier 0 硬约束）。"""
    fs = FastScorer(ctx.order, ctx.passengers, seats, config, ctx.unit_of)
    out: dict[str, list[int]] = {}

    for passenger in ctx.order.passengers:
        pid = passenger.passenger_id
        row = fs.row(pid)

        if passenger.is_mobility_impaired:
            # 约束 3：轮椅乘客只允许无障碍专区座位
            allowed = [i for i, seat in enumerate(seats) if seat.in_accessible_zone()]
        else:
            allowed = list(range(len(seats)))
            mandatory = [
                h
                for h in ctx.helpers.get(pid, frozenset())
                if ctx.order.bond_of(pid, h) is BondType.MANDATORY
            ]
            if mandatory:
                shared: set[int] | None = None
                for helper in mandatory:
                    helper_passenger = ctx.passengers[helper]
                    carriages = {
                        seat.carriage
                        for seat in seats
                        if not (
                            helper_passenger.is_mobility_impaired
                            and not seat.in_accessible_zone()
                        )
                    }
                    shared = carriages if shared is None else (shared & carriages)
                if shared:
                    allowed = [i for i in allowed if seats[i].carriage in shared]

        ranked = _rank_with_per_carriage_quota(allowed, row, seats, per_passenger)
        out[pid] = ranked
    return out


def _rank_with_per_carriage_quota(
    allowed: list[int],
    affinity_row: Sequence[float],
    seats: list[Seat],
    per_passenger: int,
) -> list[int]:
    """候选池构造：**把预算分给最好的车厢，而不是平均撒到每节车厢**。

    两难之处：
    * 若"全局取前 N"，候选会全部挤在一两节车厢 —— 硬绑定双方的交集会变空，
      "同车厢"约束反而无法成立；
    * 若"每节车厢等额配额"（早期做法），158 个可用座位会被切成 16 份，
      每车厢只剩 1 个候选，连"3 人坐一起"都排不出来。

    因此这里按车厢聚合，先按"该车厢的最好个体分"给车厢排序，再按顺序
    逐车厢把配额填满（``per_carriage`` 个/车厢），末尾用少量其他车厢的候选兜底。
    """
    if len(allowed) <= per_passenger:
        return sorted(allowed, key=lambda i: (-affinity_row[i], i))

    by_carriage: dict[int, list[int]] = {}
    for slot in allowed:
        by_carriage.setdefault(seats[slot].carriage, []).append(slot)
    for group in by_carriage.values():
        group.sort(key=lambda i: (-affinity_row[i], i))
    ranked_carriages = sorted(
        by_carriage.items(), key=lambda kv: -affinity_row[kv[1][0]]
    )

    per_carriage = max(1, per_passenger // 3)
    picked: list[int] = []
    seen: set[int] = set()
    for _carriage, group in ranked_carriages:
        if len(picked) >= per_passenger:
            break
        for slot in group[:per_carriage]:
            if slot in seen:
                continue
            seen.add(slot)
            picked.append(slot)
            if len(picked) >= per_passenger:
                break
    # 兜底：若仍有空位（车厢数少），按全局亲和度补齐
    if len(picked) < per_passenger:
        rest = [slot for slot in allowed if slot not in seen]
        rest.sort(key=lambda i: (-affinity_row[i], i))
        picked.extend(rest[: per_passenger - len(picked)])
    return sorted(picked, key=lambda i: (-affinity_row[i], i))


# ---------------------------------------------------------------------------
# 建模
# ---------------------------------------------------------------------------


class _ModelBundle:
    """一次 CP-SAT 建模的中间产物（便于测试与打印统计）。"""

    def __init__(self) -> None:
        self.variables = 0
        self.pair_terms = 0


def solve_cpsat(
    ctx: OrderContext,
    state: BookingState,
    config: EngineConfig,
    mode: str = "smart",
    time_budget_ms: float | None = None,
    per_passenger_candidates: int = CANDIDATES_PER_PASSENGER,
    strict_hard: bool = True,
    adjacency_terms: bool = True,
) -> Solution:
    """用 CP-SAT 求解一次分配，返回与 V1 同构的 :class:`Solution`。"""
    cp_model = _require_cp_model()
    start = time.perf_counter()
    budget_ms = DEFAULT_TIME_BUDGET_MS if time_budget_ms is None else time_budget_ms
    scorer = Scorer(config)
    seats = list(state.available_seats)
    notes: list[str] = []

    if not seats:
        return _empty_solution(list(ctx.passengers), mode, start, ["余票为空，全部候补。"])

    candidates = build_candidates(ctx, seats, config, per_passenger_candidates)
    fs = FastScorer(ctx.order, ctx.passengers, seats, config, ctx.unit_of)
    model = cp_model.CpModel()
    stats = _ModelBundle()

    x: dict[tuple[str, int], Any] = {}
    for pid, slots in candidates.items():
        for slot in slots:
            x[(pid, slot)] = model.NewBoolVar(f"x_{pid}_{slot}")
    stats.variables = len(x)

    # -- 约束 1/2：每人至多一座、每座至多一人 ------------------------------
    for pid, slots in candidates.items():
        if slots:
            model.Add(sum(x[(pid, s)] for s in slots) <= 1)
    by_seat: dict[int, list[Any]] = {}
    for (_, slot), var in x.items():
        by_seat.setdefault(slot, []).append(var)
    for variables in by_seat.values():
        model.Add(sum(variables) <= 1)

    # -- 车厢 / 列指示量 ---------------------------------------------------
    by_carriage: dict[int, list[int]] = {}
    columns: dict[int, list[str]] = {}
    for index, seat in enumerate(seats):
        by_carriage.setdefault(seat.carriage, []).append(index)
        columns.setdefault(seat.carriage, [])
        if seat.col not in columns[seat.carriage]:
            columns[seat.carriage].append(seat.col)

    def carriage_indicator(pid: str, carriage: int) -> Any | None:
        variables = [x[(pid, s)] for s in candidates.get(pid, []) if seats[s].carriage == carriage]
        return sum(variables) if variables else None

    def column_indicator(pid: str, carriage: int, col: str) -> Any | None:
        variables = [
            x[(pid, s)]
            for s in candidates.get(pid, [])
            if seats[s].carriage == carriage and seats[s].col == col
        ]
        return sum(variables) if variables else None

    # -- 约束 4/6：强绑定同车厢 + 硬核单元整体就座 -------------------------
    bound_pairs: list[tuple[str, str, BondType]] = []
    for a, b in combinations([p.passenger_id for p in ctx.order.passengers], 2):
        bond = ctx.order.bond_of(a, b)
        if bond in (BondType.MANDATORY, BondType.STRONG):
            bound_pairs.append((a, b, bond))

    if strict_hard:
        for a, b, bond in bound_pairs:
            if not candidates.get(a) or not candidates.get(b):
                continue
            for carriage in by_carriage:
                left = carriage_indicator(a, carriage)
                right = carriage_indicator(b, carriage)
                if left is None or right is None:
                    continue
                if isinstance(left, int) and isinstance(right, int):
                    continue
                model.Add(left == right)
            if bond is BondType.MANDATORY:
                model.Add(
                    sum(x[(a, s)] for s in candidates[a]) == sum(x[(b, s)] for s in candidates[b])
                )

    # -- 目标函数：个体项 --------------------------------------------------
    objective: list[Any] = []
    for (pid, slot), var in x.items():
        weight = fs.row(pid)[slot]
        if weight:
            objective.append(int(round(weight * 100)) * var)

    # -- 目标函数：同车厢座对项（线性化） ---------------------------------
    for a, b, bond in bound_pairs:
        slots_a = [s for s in candidates.get(a, [])]
        slots_b = [s for s in candidates.get(b, [])]
        if not slots_a or not slots_b:
            continue
        for slot_a in slots_a:
            for slot_b in slots_b:
                if slot_a == slot_b:
                    continue
                seat_a, seat_b = seats[slot_a], seats[slot_b]
                if seat_a.carriage != seat_b.carriage:
                    continue  # 跨车厢已被候选池排除，无需建模
                distance = seat_a.manhattan_to(seat_b)
                if adjacency_terms and distance > 1:
                    continue  # 分离惩罚交给下方的"列指示量"层处理
                value = fs.pair(a, b, slot_a, slot_b)
                if not value:
                    continue
                var_a, var_b = x[(a, slot_a)], x[(b, slot_b)]
                both = model.NewBoolVar(f"y_{stats.pair_terms}")
                model.AddBoolOr([var_a.Not(), var_b.Not(), both])
                model.AddImplication(both, var_a)
                model.AddImplication(both, var_b)
                objective.append(int(round(value * 100)) * both)
                stats.pair_terms += 1

        # 分离惩罚：同车厢但不同列（未紧邻）
        if not adjacency_terms:
            continue
        for carriage in by_carriage:
            columns_here = columns.get(carriage, [])
            for i, col_a in enumerate(columns_here):
                wa = column_indicator(a, carriage, col_a)
                if wa is None:
                    continue
                for col_b in columns_here[i:]:
                    wb = column_indicator(b, carriage, col_b)
                    if wb is None:
                        continue
                    # 仅对"非紧邻列对"计惩罚；紧邻列对已由上面的 y 变量给奖励
                    if _columns_adjacent(seats, carriage, col_a, col_b):
                        continue
                    value = fs.pair(
                        a,
                        b,
                        _first_slot(seats, carriage, col_a),
                        _first_slot(seats, carriage, col_b),
                    )
                    if not value:
                        continue
                    both = model.NewBoolVar(f"z_{stats.pair_terms}")
                    model.AddBoolOr([wa.Not(), wb.Not(), both])
                    model.AddImplication(both, wa)
                    model.AddImplication(both, wb)
                    objective.append(int(round(value * 100)) * both)
                    stats.pair_terms += 1

    if objective:
        model.Maximize(sum(objective))

    # -- 求解 --------------------------------------------------------------
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = max(0.05, budget_ms / 1000.0)
    solver.parameters.num_search_workers = 4
    status = solver.Solve(model)
    status_name = solver.StatusName(status)
    elapsed_ms = (time.perf_counter() - start) * 1000.0

    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        seated = {pid: slot for (pid, slot), var in x.items() if solver.Value(var) == 1}
        notes.append(
            f"CP-SAT {status_name}：变量 {stats.variables}，座对项 {stats.pair_terms}，"
            f"目标值 {solver.ObjectiveValue() / 100:.1f}，用时 {elapsed_ms:.0f}ms"
        )
    else:
        notes.append(f"CP-SAT {status_name}：按硬约束不可行，回退 V1.0 启发式。")
        return _fallback(ctx, state, config, mode, time_budget_ms, notes, start)

    placed = {pid: seats[slot] for pid, slot in seated.items()}
    waitlisted = [pid for pid in ctx.passengers if pid not in placed]
    waitlisted.extend(wheelchair_waitlist(placed, ctx, config, notes))
    cost, violations = evaluate_placement(placed, ctx, scorer)
    solution = Solution(
        assignments=_to_assignments(placed, ctx, violations),
        waitlisted=sorted(set(waitlisted)),
        total_affinity=cost,
        violations=violations,
        solver="cp-sat",
        elapsed_ms=elapsed_ms,
        notes=notes,
        mode=mode,
    )
    solution.breakdown = _breakdown(violations)
    solution.cpsat_status = status_name  # type: ignore[attr-defined]
    solution.cpsat_variables = stats.variables  # type: ignore[attr-defined]
    solution.cpsat_pair_terms = stats.pair_terms  # type: ignore[attr-defined]
    return solution


def _first_slot(seats: list[Seat], carriage: int, col: str) -> int:
    for index, seat in enumerate(seats):
        if seat.carriage == carriage and seat.col == col:
            return index
    return 0


def _columns_adjacent(seats: list[Seat], carriage: int, col_a: str, col_b: str) -> bool:
    """两个列是否属于"紧邻"（同排相邻座位或同列）。"""
    if col_a == col_b:
        return True
    sample_a = next((s for s in seats if s.carriage == carriage and s.col == col_a), None)
    sample_b = next((s for s in seats if s.carriage == carriage and s.col == col_b), None)
    if sample_a is None or sample_b is None:
        return False
    return sample_a.manhattan_to(sample_b) == 1


def _fallback(
    ctx: OrderContext,
    state: BookingState,
    config: EngineConfig,
    mode: str,
    time_budget_ms: float | None,
    notes: list[str],
    start: float,
) -> Solution:
    """CP-SAT 不可行时回退到 V1.0 启发式（服务可用性优先）。"""
    from ..solver import assign_exact

    solution = assign_exact(ctx, state, config, mode=mode, time_budget_ms=time_budget_ms)
    solution.solver = "cp-sat(fallback->v1)"
    solution.notes = notes + list(solution.notes)
    solution.elapsed_ms = (time.perf_counter() - start) * 1000.0
    return solution


def _empty_solution(
    waiting: list[str], mode: str, start: float, notes: list[str]
) -> Solution:
    solution = Solution(
        waitlisted=sorted(waiting),
        solver="cp-sat",
        elapsed_ms=(time.perf_counter() - start) * 1000.0,
        notes=notes,
        mode=mode,
    )
    solution.breakdown = {}
    return solution


def _breakdown(violations: Sequence[Any]) -> dict[str, float]:
    out: dict[str, float] = {}
    for v in violations:
        key = f"tier{v.tier}:{v.code}"
        out[key] = out.get(key, 0.0) + v.affinity
    return dict(sorted(out.items(), key=lambda kv: kv[1]))


class CpSatBackend:
    """V2.0：OR-Tools CP-SAT（MILP）后端。"""

    name = "v2_cpsat"
    description = "V2.0 运筹学：OR-Tools CP-SAT，Tier 0/1 建模为硬约束、软约束为目标函数"
    requires: tuple[str, ...] = ("ortools",)

    def __call__(
        self,
        order: Order,
        state: BookingState,
        config: EngineConfig,
        mode: str = "smart",
        time_budget_ms: float | None = None,
        **kwargs: object,
    ) -> Solution:
        ctx = build_context(order, config)
        return solve_cpsat(
            ctx,
            state,
            config,
            mode=mode,
            time_budget_ms=time_budget_ms,
            per_passenger_candidates=int(
                kwargs.get("per_passenger_candidates", CANDIDATES_PER_PASSENGER)
            ),
            strict_hard=bool(kwargs.get("strict_hard", True)),
            adjacency_terms=bool(kwargs.get("adjacency_terms", True)),
        )


__all__ = [
    "CANDIDATES_PER_PASSENGER",
    "CpSatBackend",
    "CpSatUnavailable",
    "DEFAULT_TIME_BUDGET_MS",
    "build_candidates",
    "solve_cpsat",
]
