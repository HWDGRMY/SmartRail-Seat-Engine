"""固定 V2 缺陷的现场证据（供规范文档与回归测试引用）。

不改任何生产代码，只做取证：
  1. V2 在空车上对 4 人单返回 0 分配 + 全员候补，却报 OPTIMAL；
  2. 同一套"同车厢"编码手写的最小模型能坐下 4 人 -> 编码本身没错；
  3. 因此缺陷在 solve_cpsat 的建模细节里，需要单独排查。

为什么值得单独取证：这类"报最优却什么都没做"的结果**完全符合所有既有断言**
（没有任何测试期望它出票），只能靠规范 C1/K4 抓。
"""

import sys

sys.path.insert(0, r"F:\PycharmProjects\SmartRail-Seat-Engine")

import dataclasses

from smartrail.carriage import g25_16_car_formation
from smartrail.clustering import BookingState
from smartrail.composition import OrderComposition, PlatformPolicy, composition_schema
from smartrail.config import EngineConfig
from smartrail.mapping import build_order
from smartrail.scoring import Scorer
from smartrail.solver import build_context, solve
from smartrail.validation import check_solution
from smartrail.v2 import cpsat as M

schema = composition_schema()
bands = [b["id"] for b in schema["age_bands"]]
comp = OrderComposition.from_dict({
    "class_code": "二等座",
    "base": {"adult": 2, "child": 2, "youth": 0, "toddler": 0, "infant": 0},
    "child_sub": {g["id"]: 0 for g in schema["child_sub_groups"]},
    "disability": {lv["id"]: {b: 0 for b in bands}
                   for lv in schema["disability_levels"]},
    "pregnant": {st["id"]: {b: 0 for b in bands}
                 for st in schema["pregnant_stages"]},
})
order = dataclasses.replace(
    build_order(comp, PlatformPolicy()).order, class_code="二等座")
formation = g25_16_car_formation()
config = EngineConfig()
ctx = build_context(order, config, all_seats=formation.seats, occupied_seats=set())
state = BookingState(formation=formation)
scorer = Scorer(config)

print("=" * 78)
print("证据 1：V1 正常出票（同一 ctx / 同一判据）")
print("=" * 78)
sol_v1 = solve(order, state, config)
report_v1 = check_solution(order, sol_v1, formation, occupied=set(),
                           scorer=scorer, ctx=ctx)
print(" ", report_v1.explain())

print()
print("=" * 78)
print("证据 2：V2 空车上全员候补，却报 OPTIMAL")
print("=" * 78)
sol_v2 = M.solve_cpsat(ctx, state, config)
print(f"  assignments = {len(sol_v2.assignments)}")
print(f"  waitlisted  = {len(sol_v2.waitlisted)}  {sorted(sol_v2.waitlisted)}")
print(f"  affinity    = {sol_v2.total_affinity}")
print(f"  solver      = {sol_v2.solver}")
for note in sol_v2.notes:
    print(f"  note        = {note}")
report_v2 = check_solution(order, sol_v2, formation, occupied=set(),
                           scorer=scorer, ctx=ctx)
print()
print(" ", report_v2.explain())

print()
print("=" * 78)
print("证据 3：放宽 strict_hard 就能坐下 -> 说明硬约束在判死")
print("=" * 78)
sol_relaxed = M.solve_cpsat(ctx, state, config, strict_hard=False)
print(f"  strict_hard=False -> assignments={len(sol_relaxed.assignments)} "
      f"waitlisted={len(sol_relaxed.waitlisted)}")
report_relaxed = check_solution(order, sol_relaxed, formation, occupied=set(),
                                scorer=scorer, ctx=ctx)
print(" ", report_relaxed.explain())

print()
print("=" * 78)
print("证据 4：加病态解守卫后，V2 不再流出『报最优却什么都没做』")
print("=" * 78)
sol_guarded = M.solve_cpsat(ctx, state, config)
print(f"  solver = {sol_guarded.solver}")
print(f"  assignments = {len(sol_guarded.assignments)} "
      f"waitlisted = {len(sol_guarded.waitlisted)}")
report_guarded = check_solution(order, sol_guarded, formation, occupied=set(),
                                scorer=scorer, ctx=ctx)
print(" ", report_guarded.explain())

print()
print("=" * 78)
print("结论")
print("=" * 78)
print("  V1 通过全部 18 条不变量。")
print(f"  V2 加了守卫后通过（违反 {report_guarded.codes()}）。")
print("  但**根因仍未消除**：CP-SAT 在 strict_hard 下把『全员候补』"
      "当成最优解（目标值 -0.0），")
print("  而同一份约束手写最小模型能坐下这 4 人 —— 属建模细节问题，待单独排查。")
print("  守卫只是保证病态解不外流（V1 已通过全部不变量），"
      "并留下可追溯的 note。")
