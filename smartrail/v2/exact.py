"""V2.0（零依赖分支）：把 V1 的分支限界包装成统一后端。

这个文件的来历值得记录
----------------------
README 的 V2.0 设想是"引入 OR-Tools CP-SAT 的运筹学求解"。在无法安装第三方库的
环境里，我尝试**自己写一个精确求解器**（匈牙利松弛 + 分支定界），并在候选池上
用暴力枚举做交叉验证。结果是：

* 匈牙利算法本身写对了（120 组随机矩阵与暴力枚举一致，见测试）；
* 但**搜索的代理目标反复与线上权威代价函数漂移**：先后漏掉
  - 单元级安全底线 ``T1_ISOLATED_CARE_MEMBER``（返回 -3470 分方案）、
  - 静音车厢团体项 ``T3_QUIET_GROUP_OVERUSE``（返回 -2890 分方案）、
  - "未放置 ↔ 已放置"的成对额度（返回 105，而真实最优 130）。

每一次都靠暴力枚举才发现。结论很明确：**自研精确求解器若要与权威代价函数
逐节点保持一致，必须把整套代价函数增量实现一遍，任何遗漏都会让搜索系统性地
选错方案**。这属于"看起来能跑、实际给错答案"的危险区域。

因此最终形态是：本后端**直接复用 V1 已被 37 条断言守护的分支限界搜索**，
只做接口适配。V2.0 真正的运筹学实现是 :mod:`.cpsat`（OR-Tools CP-SAT）——
把约束与目标显式交给成熟求解器，不存在"代理目标漂移"。

后续若要继续做自研精确求解，正确路线是：把 :mod:`smartrail.solver` 的
``evaluate_placement`` 增量化为"放置回调 + 撤销回调"，让搜索与权威评估共用同一份
实现，而不是维护两份。
"""

from __future__ import annotations

from typing import Any, Sequence

from ..clustering import BookingState
from ..config import EngineConfig
from ..models import Order, Solution
from ..solver import build_context

EXPERIMENTAL_BRUTE_FORCE_VERIFIED = True
"""标注：自研分支定界的**算法件**（匈牙利、候选池）已通过暴力枚举交叉验证，
但完整搜索目标未达"逐节点一致"，故不再对外声称最优性。"""


def solve_exact_pure(
    ctx: Any,
    state: BookingState,
    config: EngineConfig,
    mode: str = "smart",
    time_budget_ms: float | None = None,
    per_passenger_candidates: int | None = None,
    node_limit: int | None = None,
) -> Solution:
    """零依赖精确求解：复用 V1 分支限界（评测口径与 V1 完全一致）。

    保留函数签名是为了让调用方与测试不必改动；``per_passenger_candidates`` /
    ``node_limit`` 在此实现中不生效（V1 内部有自己的候选池与节点预算）。
    """
    from ..solver import assign_exact

    _ = (per_passenger_candidates, node_limit)
    solution = assign_exact(ctx, state, config, mode=mode, time_budget_ms=time_budget_ms)
    solution.solver = "exact-pure(=v1 branch&bound)"
    solution.notes = [
        "V2（零依赖）复用 V1 分支限界：自研代理目标曾多次与权威代价漂移，"
        "故改为共用同一套评估实现，保证口径一致。工业级精确求解请用 v2_cpsat。"
    ] + list(solution.notes)
    return solution


def hungarian(cost: Sequence[Sequence[float]]) -> tuple[float, list[int]]:
    """最小代价指派（保留为独立可用件：120 组随机矩阵与暴力枚举一致）。

    约定：行数 ≤ 列数；行数多于列数时补"不可行"列扩成方阵。
    """
    a = [list(row) for row in cost]
    if not a:
        return 0.0, []
    n = len(a)
    n_cols = len(a[0])
    if n_cols == 0:
        return 0.0, [-1] * n
    pad_from = n_cols + 1
    if n > n_cols:
        for row in a:
            row.extend([PAD_COST] * (n - n_cols))
        n_cols = n

    INF = float("inf")
    u = [0.0] * (n + 1)
    v = [0.0] * (n_cols + 1)
    p = [0] * (n_cols + 1)
    way = [0] * (n_cols + 1)
    for i in range(1, n + 1):
        p[0] = i
        j0 = 0
        minv = [INF] * (n_cols + 1)
        used = [False] * (n_cols + 1)
        while True:
            used[j0] = True
            i0 = p[j0]
            delta = INF
            j1 = 0
            for j in range(1, n_cols + 1):
                if used[j] or j >= pad_from:
                    continue
                cur = a[i0 - 1][j - 1] - u[i0] - v[j]
                if cur < minv[j]:
                    minv[j] = cur
                    way[j] = j0
                if minv[j] < delta:
                    delta = minv[j]
                    j1 = j
            for j in range(n_cols + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while True:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
            if j0 == 0:
                break

    assignment = [-1] * n
    total = 0.0
    for j in range(1, n_cols + 1):
        if j >= pad_from:
            continue
        row = p[j]
        if row:
            assignment[row - 1] = j - 1
            total += a[row - 1][j - 1]
    if -1 in assignment:
        return float("-inf"), assignment
    return total, assignment


PAD_COST = 1.0e9
"""为补方阵添加的列的代价（正大值，保证填充列永不被真实行选中）。"""


class ExactPureBackend:
    """V2.0（零依赖分支）：复用 V1 分支限界的零依赖后端。"""

    name = "v2_exact"
    description = (
        "V2.0 运筹学（零依赖）：复用 V1 分支限界，评测口径与 V1 一致；"
        "工业级精确求解请用 v2_cpsat（OR-Tools CP-SAT）"
    )
    requires: tuple[str, ...] = ()

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
        return solve_exact_pure(
            ctx, state, config, mode=mode, time_budget_ms=time_budget_ms, **kwargs
        )


__all__ = [
    "EXPERIMENTAL_BRUTE_FORCE_VERIFIED",
    "ExactPureBackend",
    "hungarian",
    "solve_exact_pure",
]
