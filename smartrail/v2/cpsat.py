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

import os
import time
from itertools import combinations
from typing import Any, Sequence

from ..clustering import BookingState
from ..config import EngineConfig
from ..fastscore import FastScorer
from ..models import BondType, Order, Seat, Solution
from ..scoring import (
    Scorer,
    is_wheelchair_seat,
    wheelchair_bay_slot_ids,
)
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

AFFINITY_SCALE = 10
"""亲和度的整数缩放倍数。

CP-SAT 的目标必须是整数，而亲和度是浮点。**缩放倍数不能太大**：
目标项是"系数 × 变量"再求和，系数越大量级越容易逼近整数上限，
求解器会静默截断，于是"谁都不坐"又变成最优 —— 实测 100 倍缩放配合
10^7 的"坐人奖励"会溢出；改成 10 倍 + 2×10^5 才稳定。
"""

SEAT_ONE_PASSENGER = 200_000
"""把"多坐一个人"折算成多少个亲和度单位。

必须**远大于任何单人所可能获得的全部亲和度之和**（T5 奖励有 60/人的硬上限，
实际总亲和度在数百量级），否则"少坐一人、换更高亲和度"仍会被选中。
这是"出票优先"在 CP-SAT 里的落地方式。
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
    state: BookingState | None = None,
) -> dict[str, list[int]]:
    """为每位乘客生成候选座位槽（已内嵌 Tier 0 硬约束）。

    **同车厢约束必须在候选池这一层保证**
    ------------------------------------
    硬绑定的两人（家长-儿童）必须坐同一车厢。这条约束在模型里是
    "两人落在车厢 c 的指示量互相蕴含"，但它只有在**两人都有同一车厢的候选**
    时才可能被满足。

    早期实现让每位乘客各自按亲和度取前 N 个候选，结果两位乘客的候选
    落在**互不重叠的车厢集合**里：

    ==========  ==============================
    A1（家长）  {1: 13, 2: 1, 3: 13, 11: 13}
    C1（儿童）  {1: 13, 2: 13, 4: 13, 5: 1}
    ==========  ==============================

    共同车厢只有 1 和 2，其中车厢 2 在 A1 侧只剩 1 个候选 —— 模型交付了
    "A1 在 03 车、儿童在 02 车"这种把一家人拆开的解，而且报 OPTIMAL。
    CP-SAT 没有错：**是候选池让它无解可施**。

    这与 V1 那次"轮椅旅客排最后就永远拿不到停放位"是同一类问题，
    所以在候选池里就把共同车厢固定下来。
    """
    # **席别是硬约束，必须在候选池这一层过滤。**
    #
    # 调用方可传 `state=None`（此时 `seats` 由调用方给全列座位），
    # 这条路径原先**完全没有席别过滤** —— 实测"商务座"订单拿到
    # `11车01C`（二等座），违反规范 R8。
    # V1 的 `SolverTables` 有这道过滤，V2 漏了，属于版本间漂移。
    wanted_class = str(getattr(ctx.order, "class_code", "") or "")
    if wanted_class:
        seats = [seat for seat in seats if seat.class_code == wanted_class]
    if not seats:
        return {}
    fs = FastScorer(ctx.order, ctx.passengers, seats, config, ctx.unit_of)
    # 轮椅停放位的落点座位（独立资源，全列 4 个）。取不到编组信息时
    # 退回"无障碍专区"口径，保证迷你编组等旧路径仍可用。
    formation = getattr(state, "formation", None) if state is not None else None
    bay_slots = wheelchair_bay_slot_ids(formation)
    if not bay_slots:
        bay_slots = frozenset(
            seat.seat_id for seat in seats if seat.in_accessible_zone()
        )
    out: dict[str, list[int]] = {}

    def base_allowed(passenger: Any) -> list[int]:
        if passenger.is_mobility_impaired:
            # 约束 3：轮椅乘客只允许**轮椅固定停放位**的落点座位
            return [i for i, seat in enumerate(seats) if is_wheelchair_seat(seat, bay_slots)]
        return list(range(len(seats)))

    # 计算每人的"可坐车厢"集合，供硬绑定求交集
    allowed_carriages: dict[str, set[int]] = {}
    for passenger in ctx.order.passengers:
        allowed_carriages[passenger.passenger_id] = {
            seats[i].carriage for i in base_allowed(passenger)
        }

    for passenger in ctx.order.passengers:
        pid = passenger.passenger_id
        row = fs.row(pid)
        allowed = base_allowed(passenger)

        mandatory = [
            h
            for h in ctx.helpers.get(pid, frozenset())
            if ctx.order.bond_of(pid, h) is BondType.MANDATORY
        ]
        if mandatory:
            # 与所有硬绑定同伴求**共同可坐车厢**，并把候选限制在这个交集里。
            # 只在交集为空时才退化为不限制（那种情况本来就无解，
            # 由模型层去候补，而不是悄悄拆开）。
            shared = set(allowed_carriages.get(pid, set()))
            for helper in mandatory:
                shared &= allowed_carriages.get(helper, set())
            if shared:
                allowed = [i for i in allowed if seats[i].carriage in shared]

        out[pid] = _rank_with_per_carriage_quota(allowed, row, seats, per_passenger)
    return out


def _rank_with_per_carriage_quota(
    allowed: list[int],
    affinity_row: Sequence[float],
    seats: list[Seat],
    per_passenger: int,
) -> list[int]:
    """候选池构造：**保证每节可坐车厢都有代表**，再按个体分排序。

    为什么不能"把预算分给最好的几节车厢"
    ------------------------------------
    早期实现取 ``per_carriage = per_passenger // 3``（40 // 3 = 13），
    然后按车厢亲和度依次填满：13 + 13 + 13 = 39 —— 预算用完，
    **第 4 节及以后的车厢一个候选都拿不到**。

    后果不是"候选少一点"，而是**硬绑定的家长与儿童被拆开**：

    ==========  =====================================
    A1（家长）  {1: 13, 2: 1, 3: 13, 11: 13}   ← 车厢 4 消失
    C1（儿童）  {1: 13, 2: 13, 4: 13, 5: 1}
    ==========  =====================================

    两人可能同处的车厢 4 在 A1 侧没有候选，于是模型里那条"同车厢"
    蕴含约束无处施加，CP-SAT 交出了"A1 在 03 车、儿童在 02 车"的解，
    还报 OPTIMAL。**CP-SAT 没错，是候选池让它无解可施。**

    正确做法是**均衡轮转**：先把每节车厢的最好几个座位各取一个，
    循环直到预算用尽。这样每节车厢都有代表，共同车厢不会被挤掉，
    同时高亲和度的车厢仍能多拿（因为它们排在前、轮次更多）。
    """
    if len(allowed) <= per_passenger:
        # 候选比预算还少：全都要，不必削减
        return sorted(allowed, key=lambda i: (-affinity_row[i], i))

    by_carriage: dict[int, list[int]] = {}
    for slot in allowed:
        by_carriage.setdefault(seats[slot].carriage, []).append(slot)
    for group in by_carriage.values():
        group.sort(key=lambda i: (-affinity_row[i], i))
    # 亲和度高的车厢排在前面（轮转时先被取到，因此拿到的候选更多）
    ranked = sorted(by_carriage.items(), key=lambda kv: -affinity_row[kv[1][0]])

    # 高亲和度车厢的"加厚"轮数：单靠轮转每节车厢各拿一样多，会让
    # CP-SAT 在亲和度等价的方案之间随便挑一节车厢（实测挑了 15 车，
    # 而 2 车在运营上更自然）。给排名靠前的车厢多几轮，
    # 既保留"每节车厢都有代表"（硬绑定交集不丢），又让搜索偏向好车厢。
    bonus_rounds = 3

    picked: list[int] = []
    seen: set[int] = set()
    for _carriage, group in ranked[:4]:
        for slot in group[:bonus_rounds]:
            if slot in seen:
                continue
            seen.add(slot)
            picked.append(slot)

    depth = 0
    # 轮转：第 0 轮每节车厢取最好的 1 个，第 1 轮取次好的 1 个……
    while len(picked) < per_passenger:
        added = False
        for _carriage, group in ranked:
            if depth >= len(group):
                continue
            slot = group[depth]
            if slot in seen:
                continue
            seen.add(slot)
            picked.append(slot)
            added = True
            if len(picked) >= per_passenger:
                break
        if not added:
            break   # 所有车厢都取完了
        depth += 1
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
    # **评价用的 Scorer 必须带上停放位与无障碍专区信息。**
    #
    # 原写法是裸的 ``Scorer(config)``，于是事后评估会把"坐在停放位上"
    # 误判成"轮椅旅客拿到了普通座位"，**凭空产生 Tier 0 违规**：
    # 实测报告亲和度 70，独立复算却是 −99970（差 −100040，正好一条
    # ``t0_separated_care_bond`` / 无障碍类惩罚）。
    #
    # V1 的两处 Scorer 都带了这三个参数，这里是复制粘贴时的漂移 ——
    # 规范 K1（报告亲和度必须可复算）专门抓这一类。
    seats = list(state.available_seats)
    # **席别过滤必须与 `build_candidates` 用同一份列表。**
    #
    # 候选池返回的是**索引**，而索引是相对它自己那份座位列表算的。
    # 若这里不过滤、`build_candidates` 内部过滤了，两边索引就会错位 ——
    # 实测"商务座"订单拿到了 `01车06C`（**一等座**）：候选池里第 N 个
    # 是某节商务座车的位置，而这里用未过滤列表解释同一个 N。
    # 规范 R8 抓到的就是这个。
    wanted_class = str(getattr(ctx.order, "class_code", "") or "")
    if wanted_class:
        seats = [seat for seat in seats if seat.class_code == wanted_class]
    bay_slots_for_scoring = wheelchair_bay_slot_ids(
        getattr(state, "formation", None)
    )
    scorer = Scorer(
        config,
        accessible_available=True,
        wheelchair_bays_free=len(bay_slots_for_scoring),
        bay_slot_ids=bay_slots_for_scoring,
    )
    notes: list[str] = []

    if not seats:
        return _empty_solution(list(ctx.passengers), mode, start, ["余票为空，全部候补。"])

    candidates = build_candidates(
        ctx, seats, config, per_passenger_candidates, state=state
    )
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

    def _indicator(name: str, variables: list[Any]) -> Any | None:
        """构造"这一组座位里**至少有一个**被选中"的**布尔变量**。

        为什么必须返回布尔变量而不是 ``sum(variables)``
        ------------------------------------------------
        这个 bug 直到装好 OR-Tools 才暴露：``sum(...)`` 在 0/1 个元素时是
        **Python int**、在多个元素时是 ``SumArray`` —— 两者都没有 ``.Not()``，
        所以 ``model.AddBoolOr([wa.Not(), ...])`` 直接抛
        ``AttributeError: 'SumArray' object has no attribute 'Not'``。

        更根本的问题是：CP-SAT 的 ``AddBoolOr`` 要求**字面量**（布尔变量或其取反），
        而 ``sum`` 是线性表达式，语义上也不该当字面量用。正确做法是显式建一个
        布尔变量并约束 ``ind <= sum(vars) <= n * ind``：
        有任何一个被选中时 ``ind`` 必为真，全不选时必须为假。

        这类错误只有**真正跑一次**才会发现 —— 静态检查、类型标注都看不出来。
        """
        if not variables:
            return None
        if len(variables) == 1:
            return variables[0]
        indicator = model.NewBoolVar(name)
        total = sum(variables)
        model.Add(total >= indicator)
        model.Add(total <= len(variables) * indicator)
        return indicator

    def carriage_indicator(pid: str, carriage: int) -> Any | None:
        variables = [
            x[(pid, s)] for s in candidates.get(pid, []) if seats[s].carriage == carriage
        ]
        return _indicator(f"car_{pid}_{carriage}", variables)

    def column_indicator(pid: str, carriage: int, col: str) -> Any | None:
        variables = [
            x[(pid, s)]
            for s in candidates.get(pid, [])
            if seats[s].carriage == carriage and seats[s].col == col
        ]
        return _indicator(f"col_{pid}_{carriage}_{col}", variables)

    # -- 约束 4/6：强绑定同车厢 + 硬核单元整体就座 -------------------------
    bound_pairs: list[tuple[str, str, BondType]] = []
    for a, b in combinations([p.passenger_id for p in ctx.order.passengers], 2):
        bond = ctx.order.bond_of(a, b)
        if bond in (BondType.MANDATORY, BondType.STRONG):
            bound_pairs.append((a, b, bond))

    # -- "是否落座"指示量 --------------------------------------------------
    # 它既用于目标函数（出票优先），也用于硬约束（**只在两人都落座时才要求
    # 同车厢**）。必须在这里先建好，后面两处共用同一批变量。
    rides: dict[str, Any] = {}
    for pid in ctx.passengers:
        slots = candidates.get(pid) or []
        if not slots:
            continue
        flag = model.NewBoolVar(f"rides_{pid}")
        model.Add(sum(x[(pid, s)] for s in slots) == flag)
        rides[pid] = flag
    stats.variables += len(rides)

    if strict_hard:
        for a, b, bond in bound_pairs:
            if not candidates.get(a) or not candidates.get(b):
                continue
            # **同车厢必须写成"两人同处某一车厢"的析取，不能写成
            # 逐车厢的双向蕴含。**
            #
            # 原写法对每个车厢 c 加 ``left_c -> right_c`` 与
            # ``right_c -> left_c``，其中 left_c 是"a 落在 c"的指示量。
            # 两人**都没落座**时所有 left_c/right_c 全为 0，蕴含平凡成立 ——
            # 看似无害，实际把"谁都不坐"也纳入了合法解空间，
            # 而它恰好是唯一能绕开全部约束的解。
            #
            # 实测后果：整列车空着，2 成人 + 2 儿童全部候补，
            # 而 CP-SAT 报 **OPTIMAL、目标值 -0.0** —— 把"什么都不做"
            # 当成了最优（规范 C1/K4 专门抓这个）。
            #
            # 正确编码：引入 both_c = "两人同在车厢 c"，令
            #   both_c -> rides_a、both_c -> rides_b、rides_a + rides_b <= both_c + 1
            # 于是"两人都坐"就必须落在同一车厢，"都不坐"则不受约束。
            both_any: list[Any] = []
            for carriage in by_carriage:
                left = carriage_indicator(a, carriage)
                right = carriage_indicator(b, carriage)
                if left is None or right is None:
                    continue
                both = model.NewBoolVar(f"both_{a}_{b}_{carriage}")
                stats.variables += 1
                model.AddImplication(both, left)
                model.AddImplication(both, right)
                both_any.append(both)
            if both_any:
                # **关键的一步**：两人都落座时，必须有某一个 both_c 为真。
                # 少了这条，"谁都不坐"仍然是可行解 —— 而它是唯一能绕开
                # 全部约束的解，于是 CP-SAT 会挑它并报 OPTIMAL。
                model.Add(sum(both_any) == rides[a])
                if bond is BondType.MANDATORY:
                    # 硬绑定：两人的"落座"同步（杜绝"孩子上车、家长候补"）
                    model.Add(rides[a] == rides[b])

    # -- 目标函数：个体项 --------------------------------------------------
    objective: list[Any] = []
    # **先把"尽量多坐人"放进目标，且量级压倒亲和度。**
    #
    # 原实现只累加个体分与座对项，于是"谁都不安排"（目标值 0）在数学上
    # 就是最优解之一 —— CP-SAT 会老老实实返回
    #
    #     assignments=0, waitlisted=全部, notes=['CP-SAT OPTIMAL … 目标值 -0.0']
    #
    # 也就是**把"不可行 / 什么都不做"误报成"最优"**，整列车空着却全员候补。
    # 这与项目"出票优先"的底线直接冲突，也是规范 C1/K4 专门要抓的东西。
    #
    # 权重取模块级常量 ``SEAT_ONE_PASSENGER``（量级说明见文件头）。
    seated_vars = list(rides.values())
    for flag in seated_vars:
        objective.append(SEAT_ONE_PASSENGER * flag)
    if os.environ.get("SMARTRAIL_CPSAT_DEBUG"):
        print(f"    [cpsat] 坐人项 {len(seated_vars)} 个，"
              f"系数 {SEAT_ONE_PASSENGER}，目标项合计 {len(objective)}")

    for (pid, slot), var in x.items():
        weight = fs.row(pid)[slot]
        if weight:
            objective.append(int(round(weight * AFFINITY_SCALE)) * var)

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
                    objective.append(int(round(value * AFFINITY_SCALE)) * both)
                    stats.pair_terms += 1

    if objective:
        model.Maximize(sum(objective))

    # 诊断探针：直接在**同一个模型**上问"强制全员落座可行吗"。
    # 这一步能把"建模写错"与"问题真的不可行"区分开 ——
    # 实测强行置 rides=1 后模型仍报 OPTIMAL（见 SMARTRAIL_CPSAT_DEBUG 输出），
    # 说明约束并不阻止落座，是**目标函数没把落座算进去**。
    if os.environ.get("SMARTRAIL_CPSAT_FORCE_RIDES") and rides:
        for flag in rides.values():
            model.Add(flag == 1)

    # -- 求解 --------------------------------------------------------------
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = max(0.05, budget_ms / 1000.0)
    solver.parameters.num_search_workers = 4
    status = solver.Solve(model)
    status_name = solver.StatusName(status)
    elapsed_ms = (time.perf_counter() - start) * 1000.0

    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        seated = {pid: slot for (pid, slot), var in x.items() if solver.Value(var) == 1}
        if os.environ.get("SMARTRAIL_CPSAT_DEBUG"):
            print("    [cpsat] rides =",
                  {pid: solver.Value(flag) for pid, flag in rides.items()})
            print("    [cpsat] seated =", seated)
            print("    [cpsat] objective =", solver.ObjectiveValue(),
                  "best =", solver.BestObjectiveBound())
        notes.append(
            f"CP-SAT {status_name}：变量 {stats.variables}，座对项 {stats.pair_terms}，"
            f"目标值 {solver.ObjectiveValue() / AFFINITY_SCALE:.1f}，用时 {elapsed_ms:.0f}ms"
        )
    else:
        notes.append(f"CP-SAT {status_name}：按硬约束不可行，回退 V1.0 启发式。")
        return _fallback(ctx, state, config, mode, time_budget_ms, notes, start)

    # ---- 病态解守卫：把"全体候补"当作求解失败来对待 ----
    #
    # 需求底线是"**以出票为目的**"：整列车空着却把所有人放进候补，
    # 无论求解器怎么解释都是不可接受的输出。规范 C1/K4 会把它判为事故。
    #
    # 触发场景（实测）：空车上对"2 成人 + 2 儿童"返回
    # ``assignments=0, waitlisted=4``，而 notes 写着
    # ``CP-SAT OPTIMAL ... 目标值 -0.0``。
    # 同一份约束手写最小模型能坐下这 4 人，所以这是建模细节问题，
    # 不是问题真的不可行 —— 但**在查清之前不能让病态解流出去**。
    #
    # 处置：CP-SAT 一个人都没安排、而订单确实有人时，改用 V1 的结果。
    # V1 已通过全部 18 条不变量；只有当 V1 也安排不了（真没座位）才接受空解。
    if ctx.passengers and not seated:
        fallback_solution = _fallback(
            ctx, state, config, mode, time_budget_ms, notes, start,
        )
        if fallback_solution.assignments:
            notes.append(
                "CP-SAT 返回『全员候补』（一个人都没安排），而订单非空 —— "
                "已改用 V1.0 结果，避免『报最优却什么都没做』的病态输出。"
            )
            fallback_solution.notes = notes
            fallback_solution.solver = "cp-sat(→v1 兜底)"
            return fallback_solution
        solution = Solution(
            assignments={},
            waitlisted=sorted({pid for pid in ctx.passengers}),
            total_affinity=0.0,
            violations=[],
            solver="cp-sat",
            elapsed_ms=elapsed_ms,
            notes=notes + ["CP-SAT 与 V1.0 都无法安排本单，进入候补。"],
            mode=mode,
        )
        solution.breakdown = _breakdown([])
        solution.cpsat_status = status_name  # type: ignore[attr-defined]
        return solution

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
