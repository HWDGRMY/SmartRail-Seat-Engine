"""测试规范在 pytest 下的回归守护。

与 `tools/verify_conformance.py` 的分工：
* 那个是**执行器**，跑场景矩阵 + 模糊测试，用于人工验收与迭代；
* 这个是**回归守护**，只跑最有价值的一小撮场景，
  保证规范本身与关键条款不会在后续改动中退化。

设计要点：**规范必须对"故意做错"的结果报警** ——
否则它只是个恒真的装饰。所以除了正面场景，这里还构造了几组
"人为篡改的解"，断言核验器一定能抓到。
"""

from __future__ import annotations

import dataclasses
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from smartrail.carriage import g25_16_car_formation  # noqa: E402
from smartrail.clustering import BookingState  # noqa: E402
from smartrail.composition import composition_schema  # noqa: E402
from smartrail.config import EngineConfig  # noqa: E402
from smartrail.mapping import build_order  # noqa: E402
from smartrail.composition import OrderComposition, PlatformPolicy  # noqa: E402
from smartrail.models import Assignment, BondType, Solution  # noqa: E402
from smartrail.solver import solve  # noqa: E402
from smartrail.validation import (  # noqa: E402
    INVARIANTS, check_solution, invariant_catalog,
)

UNDER_PYTEST = "PYTEST_CURRENT_TEST" in os.environ or "pytest" in sys.modules
_FAILURES: list[str] = []


def check(condition: bool, message: str) -> None:
    if condition:
        print(f"  PASS  {message}")
        return
    print(f"  FAIL  {message}")
    _FAILURES.append(message)
    if UNDER_PYTEST:
        raise AssertionError(message)


SCHEMA = composition_schema()
BANDS = [b["id"] for b in SCHEMA["age_bands"]]
LEVELS = [lv["id"] for lv in SCHEMA["disability_levels"]]
STAGES = [st["id"] for st in SCHEMA["pregnant_stages"]]


def make_order(adult=2, child=2, class_code="二等座", wheelchair=0):
    payload = {
        "class_code": class_code,
        "wheelchair_count": wheelchair,
        "key_passenger_service": bool(wheelchair),
        "base": {"adult": adult, "child": child, "youth": 0,
                 "toddler": 0, "infant": 0},
        "child_sub": {g["id"]: 0 for g in SCHEMA["child_sub_groups"]},
        "disability": {lv: {b: 0 for b in BANDS} for lv in LEVELS},
        "pregnant": {st: {b: 0 for b in BANDS} for st in STAGES},
    }
    order = build_order(OrderComposition.from_dict(payload),
                        PlatformPolicy()).order
    return dataclasses.replace(order, class_code=class_code)


def test_catalog_is_wellformed() -> None:
    """规范本身要自洽：编号唯一、等级合法、说明非空。"""
    print("[规范] 条款登记表")
    check(len(INVARIANTS) >= 18,
          f"条款不少于 18 条（{len(INVARIANTS)}）")
    codes = [item.code for item in INVARIANTS]
    check(len(codes) == len(set(codes)), f"条款编号唯一（{len(codes)} 条）")
    valid = {"RESOURCE", "COMPLETENESS", "SAFETY", "CONSISTENCY"}
    check(all(item.severity in valid for item in INVARIANTS),
          f"等级都在 {sorted(valid)} 内")
    check(all(item.title and item.detail for item in INVARIANTS),
          "每条都有标题与说明")
    catalog = invariant_catalog()
    check(len(catalog) == len(INVARIANTS), "导出清单与登记表一致")


def test_v1_passes_all_invariants() -> None:
    """V1 在典型场景下必须通过全部条款。"""
    print("[规范] V1 通过全部条款")
    formation = g25_16_car_formation()
    config = EngineConfig()
    for label, order in (
        ("两成人", make_order(2, 0)),
        ("两成人+两儿童", make_order(2, 2)),
        ("商务座单人", make_order(1, 0, class_code="商务座")),
        ("一位轮椅+家属", make_order(2, 0, wheelchair=1)),
    ):
        state = BookingState(formation=formation)
        solution = solve(order, state, config)
        report = check_solution(order, solution, formation, occupied=set(),
                                class_code=order.class_code)
        check(report.ok, f"{label}：{report.explain()[:120]}")


def test_spec_catches_tampered_solutions() -> None:
    """**规范必须对"故意做错"报警** —— 否则它只是装饰。

    这几组篡改对应真实发生过的缺陷类型，逐条确认核验器抓得住。
    """
    print("[规范] 篡改检测（规范不能恒真）")
    formation = g25_16_car_formation()
    config = EngineConfig()
    order = make_order(2, 2)
    state = BookingState(formation=formation)
    good = solve(order, state, config)
    check(len(good.assignments) == 4, "基准解 4 人就座")

    base = {pid: a.seat_id for pid, a in good.assignments.items()}
    free_seats = [s.seat_id for s in formation.seats
                  if s.seat_id not in base]

    def as_solution(mapping, waitlisted=()):
        assignments = {}
        for pid, seat_id in mapping.items():
            # ``formation.seat`` 对未知座位会抛 KeyError，
            # 而"故意篡改成不存在的座位"正是要测的情形 —— 先自己兜住。
            try:
                seat = formation.seat(seat_id)
            except KeyError:
                seat = None
            if seat is None:
                # 故意篡改成"不存在的座位"时，仍要能构造出解，
                # 好让规范 R3 去抓它 —— 这里给一组占位坐标。
                assignments[pid] = Assignment(
                    passenger_id=pid, seat_id=seat_id, carriage=99,
                    row=99, col="Z", quiet_carriage=False,
                    reason="", unit_id="",
                )
                continue
            assignments[pid] = Assignment(
                passenger_id=pid, seat_id=seat_id, carriage=seat.carriage,
                row=seat.row, col=seat.col,
                quiet_carriage=seat.is_quiet_carriage,
                reason="", unit_id="",
            )
        return Solution(
            assignments=assignments,
            waitlisted=list(waitlisted),
            total_affinity=0.0, violations=[], solver="tampered",
        )

    # ① 一个座位卖给两个人 -> R1
    pid_a, pid_b = list(base)[0], list(base)[1]
    dup = dict(base)
    dup[pid_b] = dup[pid_a]
    report = check_solution(order, as_solution(dup), formation)
    check("R1" in report.codes(), f"座位重复被抓到（{report.codes()}）")

    # ② 分到不存在的座位 -> R3
    bad = dict(base)
    bad[pid_a] = "99车99Z"
    report = check_solution(order, as_solution(bad), formation)
    check("R3" in report.codes(), f"虚构座位被抓到（{report.codes()}）")

    # ③ 席别不符 -> R8
    first_class = [s.seat_id for s in formation.seats
                   if s.class_code == "一等座"]
    wrong = dict(base)
    wrong[pid_a] = first_class[0]
    report = check_solution(order, as_solution(wrong), formation)
    check("R8" in report.codes(), f"席别不符被抓到（{report.codes()}）")

    # ④ 硬绑定被拆到不同车厢 -> S1
    other_car = next(s.seat_id for s in formation.seats
                     if s.carriage not in {formation.seat(v).carriage
                                           for v in base.values()})
    split = dict(base)
    split[pid_a] = other_car
    report = check_solution(order, as_solution(split), formation)
    check("S1" in report.codes() or "S3" in report.codes(),
          f"同行人被拆散被抓到（{report.codes()}）")

    # ⑤ 有空座却全员候补 -> C1
    report = check_solution(order, as_solution({}, waitlisted=list(base)),
                            formation)
    check("C1" in report.codes(), f"空车候补被抓到（{report.codes()}）")

    # ⑥ 既不分配也不候补 -> C3
    report = check_solution(order, as_solution({}), formation)
    check("C3" in report.codes(), f"凭空少人被抓到（{report.codes()}）")

    # ⑦ 报最优却什么都没做 -> K4
    stuck = Solution(assignments={}, waitlisted=list(base),
                     total_affinity=0.0, violations=[], solver="cp-sat",
                     notes=["CP-SAT OPTIMAL：目标值 -0.0"])
    report = check_solution(order, stuck, formation)
    check("K4" in report.codes(), f"把不可行报成最优被抓到（{report.codes()}）")


def test_v2_never_returns_pathological_result() -> None:
    """V2 不得流出"订单非空却一个人都不安排"的病态解。

    现场（规范建立时抓到）：空车上对 2 成人 + 2 儿童返回
    ``assignments=0, waitlisted=4``，而 notes 写着
    ``CP-SAT OPTIMAL ... 目标值 -0.0``。

    守住的是**契约**（不得流出病态解），不是某一条具体实现路径：

    * 修好 Scorer 配置与候选池索引错位之后，CP-SAT **自己**就能坐满 4 人；
    * 万一哪天又退化，病态解守卫会改用 V1 结果（``solver`` 里带 ``v1``）。

    两种情况都算通过 —— 不允许的是"订单非空却零分配"。
    """
    print("[规范] V2 不得流出病态解")
    from smartrail.v2.cpsat import solve_cpsat
    from smartrail.solver import build_context

    formation = g25_16_car_formation()
    config = EngineConfig()
    for label, order in (
        ("两成人+两儿童", make_order(2, 2)),
        ("两成人+两婴儿", make_order(2, 0)),
        ("商务座单人", make_order(1, 0, class_code="商务座")),
    ):
        state = BookingState(formation=formation)
        ctx = build_context(order, config, all_seats=formation.seats,
                            occupied_seats=set())
        solution = solve_cpsat(ctx, state, config)
        people = len(order.passengers)
        check(len(solution.assignments) > 0,
              f"{label}：V2 不得零分配（实际 {len(solution.assignments)} 人，"
              f"solver={solution.solver}）")
        report = check_solution(order, solution, formation, occupied=set(),
                                class_code=order.class_code)
        check(report.ok, f"{label}：通过全部条款 {report.explain()[:110]}")
        if len(solution.assignments) < people:
            # 没坐满时必须给出可解释的原因，不能静默
            check(bool(solution.waitlisted),
                  f"{label}：未就座的人必须在候补名单里"
                  f"（{len(solution.assignments)}/{people}）")


def test_class_code_is_enforced_in_both_versions() -> None:
    """席别是硬约束：两个版本都不得发出别的席别。

    现场：V2 的"商务座"订单发出 `01车06C`（一等座）——
    因为候选池按席别过滤了座位列表，而 `solve_cpsat` 用**未过滤**的
    列表按索引取座位，索引整体错位。规范 R8 抓到的就是这个。
    """
    print("[规范] 席别硬约束（V1/V2）")
    from smartrail.v2.cpsat import solve_cpsat
    from smartrail.solver import build_context

    formation = g25_16_car_formation()
    config = EngineConfig()
    for class_code in ("商务座", "一等座", "二等座"):
        order = make_order(1, 0, class_code=class_code)
        # V1
        state = BookingState(formation=formation)
        v1 = solve(order, state, config)
        v1_classes = {formation.seat(a.seat_id).class_code
                      for a in v1.assignments.values()}
        check(v1_classes <= {class_code},
              f"V1 下单 {class_code} -> 实际 {sorted(v1_classes)}")
        # V2
        state2 = BookingState(formation=formation)
        ctx = build_context(order, config, all_seats=formation.seats,
                            occupied_seats=set())
        v2 = solve_cpsat(ctx, state2, config)
        v2_classes = {formation.seat(a.seat_id).class_code
                      for a in v2.assignments.values()}
        check(v2_classes <= {class_code},
              f"V2 下单 {class_code} -> 实际 {sorted(v2_classes)}")


def test_cost_tiers_cannot_be_overturned() -> None:
    """代价分层必须**结构性地**保证硬约束压得住软偏好。

    这是本项目最容易反复踩的一类结构性问题：把"必须满足"的约束写成罚分，
    再靠"罚分足够大"保证它不被违反。只要有人调整某个奖励的量级，
    分层就可能悄悄失效 —— 而**结果看起来完全正常**
    （求解器仍然返回解、仍然报 OPTIMAL），只有出票结果不合理。

    真实事故（就在建立这套规范时发生）：为修"空车全员候补"（C1），
    V2 的"多坐一人"奖励一度被设成 ``200_000``，**比一条 Tier 0
    （100000）还大 2 倍** —— 等于告诉求解器"多坐 1 个人比避免 1 条
    硬约束违规更值"。规范条款 M1/M3 就是为这件事立的。
    """
    print("[规范] 代价分层不得被掀翻")
    from smartrail.validation import check_cost_magnitudes

    report = check_cost_magnitudes()
    check(report.ok, f"分层关系成立：{report.explain()[:150]}")

    # 反向验证：把坐人奖励改回错误量级，条款必须报警
    try:
        from smartrail import v2  # noqa: F401
        from smartrail.v2 import cpsat as cpsat_module
    except Exception:  # noqa: BLE001 - 没装 ortools 时跳过
        print("  SKIP  未安装 ortools，跳过反向验证")
        return

    original = cpsat_module.SEAT_ONE_PASSENGER
    try:
        cpsat_module.SEAT_ONE_PASSENGER = 200_000
        broken = check_cost_magnitudes()
        check(not broken.ok and "M3" in broken.codes(),
              f"坐人奖励超过 Tier 0 时被条款 M3 抓到（{broken.codes()}）")
    finally:
        cpsat_module.SEAT_ONE_PASSENGER = original


def main() -> int:
    tests = [
        test_catalog_is_wellformed,
        test_v1_passes_all_invariants,
        test_spec_catches_tampered_solutions,
        test_v2_never_returns_pathological_result,
        test_class_code_is_enforced_in_both_versions,
        test_cost_tiers_cannot_be_overturned,
    ]
    if UNDER_PYTEST:
        for test in tests:
            test()
        return 0
    passed = 0
    for test in tests:
        before = len(_FAILURES)
        try:
            test()
        except AssertionError:
            continue
        if len(_FAILURES) == before:
            passed += 1
        print()
    print("-" * 72)
    if _FAILURES:
        print(f"失败 {len(_FAILURES)} 项：")
        for item in _FAILURES:
            print("  -", item)
        return 1
    print(f"全部 {passed} 组断言通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
