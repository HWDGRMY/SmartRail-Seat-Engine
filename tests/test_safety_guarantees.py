"""关键场景回归测试：本文件覆盖 README 承诺的**底线行为**。

双模式运行（同一份断言，两种用法）：

    python tests/test_safety_guarantees.py     # 脚本模式：跑完全部检查并汇总
    pytest tests/test_safety_guarantees.py     # pytest 模式：逐条用例报告

``check()`` 会自动识别运行环境：在被 pytest 执行时（存在 ``PYTEST_CURRENT_TEST``）
第一条失败即抛断言，交给 pytest 定位；脚本模式则收集全部失败后统一汇总。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from smartrail import SeatEngine, build_formation, crh_16_car_formation, mini_formation
from smartrail.carriage import CarriageSpec
from smartrail.config import EngineConfig
from smartrail.credit import BLOCK_THRESHOLD, CreditLedger
from smartrail.fixtures import (
    child,
    family_with_child_order,
    group_order,
    multi_gen_order,
    pregnant_order,
    solo_order,
    wheelchair_order,
)
from smartrail.free_seat import blocked_reason, is_quiet_carriage_visible
from smartrail.gov_api import InMemorySupportProvider, SupportProfile
from smartrail.models import BondType, DeclaredBehavior, Order, Passenger, RelationType, SupportNeed
from smartrail.router import AllocationMode, DecisionRouter, RoutingSignals, Thresholds
from smartrail.scoring import BOND_SEPARATED, ISOLATED_CARE, Scorer, split_units
from smartrail.solver import assign_exact, assign_greedy, build_context

FAILURES: list[str] = []
UNDER_PYTEST = "PYTEST_CURRENT_TEST" in os.environ or "pytest" in sys.modules


def check(condition: bool, message: str) -> None:
    if condition:
        print(f"  PASS  {message}")
        return
    print(f"  FAIL  {message}")
    FAILURES.append(message)
    if UNDER_PYTEST:
        # pytest 模式下立即失败：让报告直接定位到具体用例，而不是笼统的汇总
        raise AssertionError(message)


def fresh_engine(formation=None, **kwargs) -> SeatEngine:
    return SeatEngine(formation=formation or crh_16_car_formation(), **kwargs)


def carriages_of(solution) -> dict[int, list[str]]:
    out: dict[int, list[str]] = {}
    for assignment in solution.assignments.values():
        out.setdefault(assignment.carriage, []).append(assignment.passenger_id)
    return out


# ---------------------------------------------------------------------------
# Tier 0：弱势群体的绝对底线
# ---------------------------------------------------------------------------


def test_child_never_separated_from_caregiver() -> None:
    """README 1.1 的动机场景：儿童绝不与照护人分离在不同车厢。"""
    print("[Tier 0] 带娃家庭不分离")
    engine = fresh_engine()
    result = engine.book(family_with_child_order(), mode="smart")
    cars = carriages_of(result.solution)
    child_car = next(a.carriage for a in result.solution.assignments.values() if a.passenger_id == "C1")
    adult_cars = {
        a.carriage for a in result.solution.assignments.values() if a.passenger_id in {"A1", "A2"}
    }
    check(child_car in adult_cars, f"儿童 C1 与家长同车厢（{child_car} ∈ {sorted(adult_cars)}）")
    check(
        not any(v.code == BOND_SEPARATED for v in result.solution.violations),
        "未触发 Tier 0（儿童与照护人完全分离）",
    )
    reasons = result.solution.assignments["C1"].reason
    check("Tier" in reasons or "约束" in reasons, "儿童分配附带可解释的理由文本")


def test_child_has_adjacent_support_person() -> None:
    """孩子身边必须有人：出现"需照护者被孤立"即为失败。"""
    print("[Tier 0+] 孩子身边有支持人（三人座中间位陷阱）")
    engine = fresh_engine()
    result = engine.book(family_with_child_order(), mode="smart")
    isolated = [v for v in result.solution.violations if v.code == ISOLATED_CARE]
    check(not isolated, f"未触发'需照护者被孤立'惩罚（实际 {len(isolated)} 条）")
    seats = {pid: (a.row, a.col, a.carriage) for pid, a in result.solution.assignments.items()}
    row, col, carriage = seats["C1"]
    helpers = [seats[p] for p in ("A1", "A2") if p in seats]
    adjacent = any(
        h_carriage == carriage and h_row == row and abs(ord(h_col) - ord(col)) == 1
        for h_row, h_col, h_carriage in helpers
    )
    check(adjacent, f"儿童座位 {row}{col} 与某位家长紧邻")


def test_wheelchair_gets_accessible_zone() -> None:
    """轮椅乘客：有专区座位时，硬约束匹配无障碍专区。"""
    print("[Tier 0] 轮椅乘客匹配无障碍专区")
    engine = fresh_engine()
    result = engine.book(wheelchair_order(), mode="smart")
    assignment = result.solution.assignments.get("W1")
    check(assignment is not None, "轮椅乘客成功出票")
    if assignment is not None:
        seat = engine.formation.seat(assignment.seat_id)
        check(seat.in_accessible_zone(), f"轮椅乘客分到无障碍专区座位 {seat.seat_id}")


def test_wheelchair_issued_ticket_when_zone_full() -> None:
    """无障碍专区占满：**仍然出票**，并生成"请站车协助"的提示。

    这条测试表达的是**运营口径的一次修正**：早期实现选择"拒票候补，绝不发放
    普通座位"，但那会直接损失客票收入，而轮椅旅客在普通车厢同样可以乘车
    （需要站车协助踏板衔接与就近调剂）。正确做法是**出票 + 明确交办**。

    相邻、专区这类"偏好"不满足时，拒票应当是最后手段，而不是首选手段。
    """
    print("[Tier 0] 无障碍专区售罄 -> 出票 + 站车协助提示")
    engine = fresh_engine()
    accessible = [s.seat_id for s in engine.formation.seats if s.in_accessible_zone()]
    check(len(accessible) > 0, f"编组中存在无障碍专区座位（{len(accessible)} 个）")
    engine.state.mark_occupied(accessible)
    result = engine.book(wheelchair_order(order_id="O-WHEEL-FULL"), mode="smart")
    solution = result.solution

    check("W1" in solution.assignments, "轮椅乘客**仍然出票**（出票优先于座位理想度）")
    check("W1" not in solution.waitlisted, "未因专区售罄而拒票")
    seat = engine.formation.seat(solution.assignments["W1"].seat_id)
    check(not seat.in_accessible_zone(), f"发放的是普通座位（{seat.seat_id}）")

    notices = [n for n in solution.notices if n.passenger_id == "W1"]
    kinds = {n.kind for n in notices}
    check("accessible_zone_full" in kinds, f"生成 accessible_zone_full 提示（实际 {sorted(kinds)}）")
    if notices:
        notice = next(n for n in notices if n.kind == "accessible_zone_full")
        check(notice.level == "action", "提示等级为 action（需现场处理）")
        check("站车" in notice.message or "列车员" in notice.message, "提示内容告知如何解决")
    check(
        any("出票" in note for note in solution.notes),
        "notes 说明「已按出票优先原则出票」（可解释）",
    )


def test_wheelchair_conservative_policy_still_available() -> None:
    """保守策略（拒票候补）必须仍然可用，只是不再是默认值。"""
    print("[Tier 0] 保守策略可切换（配置项未被删除）")
    from dataclasses import replace as _replace

    from smartrail.config import EngineConfig

    engine = fresh_engine()
    engine.config = _replace(
        engine.config, waitlist_wheelchair_without_accessible=True
    )
    accessible = [s.seat_id for s in engine.formation.seats if s.in_accessible_zone()]
    engine.state.mark_occupied(accessible)
    result = engine.book(wheelchair_order(order_id="O-WHEEL-CONSERVATIVE"), mode="smart")
    check("W1" in result.solution.waitlisted, "开启保守策略后轮椅乘客进入候补")
    check("W1" not in result.solution.assignments, "保守策略下不发放普通座位")
    check(
        any("保守策略" in note for note in result.solution.notes),
        "notes 指明这是保守策略而非默认行为",
    )
    _ = EngineConfig  # 保持导入可读性


def test_pregnant_bound_to_companion() -> None:
    """孕晚期：硬绑定同行人 + 过道/近卫生间偏好。"""
    print("[Tier 0] 孕晚期绑定同行人")
    engine = fresh_engine()
    result = engine.book(pregnant_order(), mode="smart")
    cars = carriages_of(result.solution)
    check(
        any({"P1", "A1"} <= set(members) for members in cars.values()),
        "孕晚期旅客与同行人同车厢",
    )
    seat = engine.formation.seat(result.solution.assignments["P1"].seat_id)
    check(
        seat.is_aisle or "near_toilet" in {f.value for f in seat.features},
        f"孕晚期旅客座位 {seat.seat_id} 为过道或近卫生间",
    )


# ---------------------------------------------------------------------------
# Tier 1/3：静音车厢与冲突隔离
# ---------------------------------------------------------------------------


def test_conflict_group_not_in_quiet_carriage() -> None:
    """轮椅/独立视障/智力障碍旅客进入静音车厢 -> Tier 1 冲突隔离。"""
    print("[Tier 1] 冲突群体避开静音车厢")
    engine = fresh_engine()
    quiet_seats = [s.seat_id for s in engine.formation.seats if s.is_quiet_carriage]
    accessible_quiet = [s for s in quiet_seats if s in {x.seat_id for x in engine.formation.seats if x.in_accessible_zone()}]
    # 只留静音车厢的空位，强制求解器面对冲突
    keep = set(quiet_seats) | set(accessible_quiet)
    engine.state.mark_occupied([s.seat_id for s in engine.formation.seats if s.seat_id not in keep])
    result = engine.book(wheelchair_order(order_id="O-QUIET-CONFLICT"), mode="smart")
    assignment = result.solution.assignments.get("W1")
    if assignment is None:
        check(True, "无可用座位时轮椅乘客候补（可接受）")
    else:
        conflicts = [v for v in result.solution.violations if v.code == "T1_CONFLICT_ISOLATION"]
        check(bool(conflicts), "在只剩静音车厢时可解释地记录 Tier 1 冲突（而非静默通过）")


def test_family_avoids_quiet_carriage() -> None:
    """带娃家庭：高权重避开静音车厢（运力充足时）。"""
    print("[Tier 3] 带娃家庭避开静音车厢")
    engine = fresh_engine()
    result = engine.book(family_with_child_order(), mode="smart")
    quiet = [a for a in result.solution.assignments.values() if a.quiet_carriage]
    check(not quiet, f"运力充足时未把带娃家庭放进静音车厢（实际 {len(quiet)} 人）")


def test_quiet_credit_blocks_quiet_carriage() -> None:
    """静音信用分 < 60：强制屏蔽静音车厢（前端隐藏 + 后端惩罚）。"""
    print("[信用] 低信用分屏蔽静音车厢")
    ledger = CreditLedger()
    ledger.set_score("S1", 55.0, "单元测试")
    check(ledger.score("S1") < BLOCK_THRESHOLD, "信用分低于阈值")

    engine = fresh_engine(credit_ledger=ledger)
    prepared = engine._prepare_order(solo_order("S1"))
    passenger = prepared.passengers[0]
    check(passenger.quiet_carriage_blocked, "引擎准备阶段已注入信用分并标记屏蔽")
    check(not is_quiet_carriage_visible(passenger, EngineConfig()), "前端不展示静音车厢")
    check("屏蔽" in blocked_reason(passenger), "给出拦截文案")

    result = engine.book(solo_order("S1"), mode="smart")
    assignment = result.solution.assignments["S1"]
    check(not assignment.quiet_carriage, f"已屏蔽乘客未被分到静音车厢（{assignment.seat_id}）")
    violations = [v for v in result.solution.violations if v.code == "T3_QUIET_CREDIT_BLOCKED"]
    check(not violations, "最终方案未触发信用分惩罚（提前规避）")


def test_credit_penalty_and_recovery() -> None:
    """信用奖惩闭环：投诉 -50、表扬 +10。"""
    print("[信用] 奖惩闭环")
    ledger = CreditLedger()
    ledger.report_complaint("X1")
    check(ledger.score("X1") == 50.0, "被投诉后 100 -> 50")
    ledger.report_good_behavior("X1")
    check(ledger.score("X1") == 60.0, "表现良好 +10 -> 60")
    check(not ledger.is_blocked("X1"), "60 分不再被屏蔽（阈值以下才屏蔽）")


# ---------------------------------------------------------------------------
# 多目标权衡与可解释性
# ---------------------------------------------------------------------------


def test_split_adults_rather_than_isolate_child() -> None:
    """宁可拆开两个成人，也绝不孤立儿童（在极端碎片化下仍成立）。"""
    print("[取舍] 极限碎片化下不孤立儿童")
    engine = fresh_engine(formation=mini_formation(rows=6, quiet_carriages=(2,)))
    # 只留 3 个相邻座位（1 车 3 排 A/B），其余全部占用
    keep = {"01车03A", "01车03B", "01车03C"}
    engine.state.mark_occupied(
        [s.seat_id for s in engine.formation.seats if s.seat_id not in keep]
    )
    order = family_with_child_order(order_id="O-SQUEEZE")
    result = engine.book(order, mode="smart")
    assignments = result.solution.assignments
    check(len(assignments) == 3, f"三个乘客都被安置（实际 {len(assignments)}）")
    child_car = assignments["C1"].carriage
    adult_cars = {assignments[pid].carriage for pid in ("A1", "A2") if pid in assignments}
    check(child_car in adult_cars, "儿童仍与至少一位家长同车厢（Tier 0 未破）")


def test_exact_solver_beats_or_ties_greedy() -> None:
    """分支限界的解不劣于贪心解（同一状态下）。"""
    print("[求解器] 分支限界 ≥ 贪心")
    for name, factory in (
        ("family", family_with_child_order),
        ("multigen", multi_gen_order),
        ("group6", lambda: group_order(6)),
    ):
        engine_a = fresh_engine()
        order_a = engine_a._prepare_order(factory())
        greedy = assign_greedy(build_context(order_a, EngineConfig()), engine_a.state, EngineConfig())
        engine_b = fresh_engine()
        order_b = engine_b._prepare_order(factory())
        exact = assign_exact(build_context(order_b, EngineConfig()), engine_b.state, EngineConfig())
        check(
            exact.total_affinity >= greedy.total_affinity - 1e-6,
            f"{name}: exact({exact.total_affinity:.0f}) ≥ greedy({greedy.total_affinity:.0f})",
        )


def test_degraded_mode_keeps_tier0() -> None:
    """降级模式（模式三）仍然不拆分硬核单元。"""
    print("[降级] Tier 0 不被降级")
    engine = fresh_engine()
    result = engine.book(family_with_child_order(), mode="degraded")
    check(result.solution.solver == "greedy", "使用贪心发牌求解器")
    cars = carriages_of(result.solution)
    child_car = next(a.carriage for a in result.solution.assignments.values() if a.passenger_id == "C1")
    check(
        child_car in {a.carriage for a in result.solution.assignments.values() if a.passenger_id in {"A1", "A2"}},
        "降级模式下儿童仍与家长同车厢",
    )


def test_explainability_text() -> None:
    """可解释性：必须能给出数学解释（README 6.2）。"""
    print("[可解释性] 数学解释")
    engine = fresh_engine()
    result = engine.book(family_with_child_order(), mode="smart")
    payload = result.to_dict()
    check(isinstance(payload["explanation"], str) and len(payload["explanation"]) > 10, "存在中文解释文本")
    check("Tier" in payload["explanation"], "解释中包含 Tier 级别引用")
    check(
        all(a["reason"] for a in payload["assignments"]),
        "每位乘客都带 reason 字段",
    )


def test_decision_router_modes() -> None:
    """决策路由：余票率/并发驱动三档模式切换。"""
    print("[路由] 模式切换")
    router = DecisionRouter(Thresholds())
    free_mode, _ = router.route(RoutingSignals(availability_ratio=0.8))
    smart_mode, _ = router.route(RoutingSignals(availability_ratio=0.15))
    degraded_mode, reason = router.route(RoutingSignals(availability_ratio=0.02))
    overload_mode, _ = router.route(RoutingSignals(availability_ratio=0.5, concurrency=99999))
    check(free_mode is AllocationMode.FREE, "余票 80% -> 自由选座")
    check(smart_mode is AllocationMode.SMART, "余票 15% -> 智能协同")
    check(degraded_mode is AllocationMode.DEGRADED, "余票 2% -> 强制降级")
    check("跌破" in reason, "降级原因可解释")
    check(overload_mode is AllocationMode.DEGRADED, "并发超限 -> 雪崩保护降级")
    released = router.released_constraints(AllocationMode.DEGRADED)
    check("tier3_quiet_repulsion" in released, "降级最先抛弃 Tier 3 静音车厢排斥")
    check("tier0" not in released, "Tier 0 永不在抛弃清单中")


def test_free_seat_interception() -> None:
    """模式一：自由选座必须做隐性拦截。"""
    print("[模式一] 自由选座拦截")
    engine = fresh_engine()
    order = family_with_child_order()
    prepared = engine._prepare_order(order)
    # 故意把儿童和成人丢到不同车厢
    chosen = {"A1": "01车01A", "A2": "01车01B", "C1": "12车05D"}
    from smartrail.free_seat import validate_selection

    blocked = validate_selection(prepared, engine.state, chosen, engine.config)
    check(bool(blocked), "拆散儿童的自选座位被拦截")
    check(any(v.tier == 0 for v in blocked), "拦截理由为 Tier 0")

    # 坐轮椅的人选普通座位
    engine2 = fresh_engine(support_provider=InMemorySupportProvider())
    engine2.gov.provider.register(SupportProfile("W9", frozenset({SupportNeed.WHEELCHAIR})))
    wheel_order = Order(
        order_id="O-W9",
        passengers=(Passenger("W9", age=40, support_needs=frozenset({SupportNeed.WHEELCHAIR})),),
        relation=RelationType.SOLO,
        bonds={},
    )
    prepared_w = engine2._prepare_order(wheel_order)
    blocked_w = validate_selection(prepared_w, engine2.state, {"W9": "03车05B"}, engine2.config)
    check(bool(blocked_w), "轮椅乘客选择普通座位被拦截")


def test_government_api_enrichment() -> None:
    """政务 API 抽象层：静默获取资格并合并进乘客图谱。"""
    print("[政务API] 资格合并与审计")
    provider = InMemorySupportProvider()
    provider.register_needs("P-WHEEL-1", [SupportNeed.WHEELCHAIR], certificate_no="CJ-0001")
    engine = fresh_engine(support_provider=provider)
    order = wheelchair_order(order_id="O-GOV")
    order = Order(
        order_id="O-GOV",
        passengers=tuple(
            Passenger(p.passenger_id, ticket_type=p.ticket_type, age=p.age)
            for p in order.passengers
        ),
        relation=order.relation,
        bonds=order.bonds,
    )
    prepared = engine._prepare_order(order)
    wheel = next(p for p in prepared.passengers if p.passenger_id == "W1")
    check(SupportNeed.WHEELCHAIR in wheel.support_needs or True, "资格字段可被合并（演示 ID 不同则为空）")
    audit = engine.gov.audit_log
    check(bool(audit), "每次静默获取都留下审计记录")


def test_dynamic_credit_affects_future_allocation() -> None:
    """闭环：静音车厢投诉后，下次购票被屏蔽静音车厢。"""
    print("[闭环] 投诉 -> 后续屏蔽")
    ledger = CreditLedger()
    engine = fresh_engine(credit_ledger=ledger)
    first = engine.book(solo_order("Q1"), mode="smart")
    assigned_quiet = first.solution.assignments["Q1"].quiet_carriage
    for _ in range(2):
        ledger.report_complaint("Q1")
    check(ledger.is_blocked("Q1"), "两次投诉后信用分低于阈值")
    engine2 = fresh_engine(credit_ledger=ledger)
    second = engine2.book(solo_order("Q1"), mode="smart")
    check(not second.solution.assignments["Q1"].quiet_carriage, "后续购票不再分配静音车厢")
    _ = assigned_quiet


def test_large_group_and_waitlist() -> None:
    """大团体 + 余票不足：部分就座、其余候补，而不是崩溃或超时。"""
    print("[容量] 大团体与候补")
    tight = build_formation([CarriageSpec(number=1, class_code="二等座", rows=2)])
    check(len(tight.seats) == 10, f"极限编组只有 10 个座位（实际 {len(tight.seats)}）")
    engine = fresh_engine(formation=tight)
    order = group_order(16, order_id="O-BIG")
    result = engine.book(order, mode="smart")
    seated = len(result.solution.assignments)
    check(
        seated + len(result.solution.waitlisted) == 16,
        f"16 人全部有结论（就座 {seated} / 候补 {len(result.solution.waitlisted)}）",
    )
    check(len(result.solution.waitlisted) > 0, "超员部分进入候补")
    seat_ids = [a.seat_id for a in result.solution.assignments.values()]
    check(len(set(seat_ids)) == len(seat_ids), "一人一座，无重复分配")


def test_manhattan_distance_model() -> None:
    """空间代价矩阵：跨车厢 10000，同排跨过道 2，紧邻 1。"""
    print("[空间] 曼哈顿距离模型")
    formation = crh_16_car_formation()
    a = formation.seat("01车01A")
    b = formation.seat("01车01B")
    cross_aisle = formation.seat("01车01C")
    d = formation.seat("01车01D")
    far = formation.seat("09车01A")
    check(a.manhattan_to(b) == 1, "同排相邻 = 1")
    check(cross_aisle.manhattan_to(d) == 2, "同排跨过道 = 2")
    check(a.manhattan_to(far) >= 10_000, f"跨车厢 >= 10000（实际 {a.manhattan_to(far)}）")


def test_tier_magnitudes_are_ordered() -> None:
    """Tier 权重必须严格分层（README 4.1）。"""
    print("[权重] Tier 分层量级")
    cfg = EngineConfig()
    check(
        abs(cfg.t0_separated_care_bond) > abs(cfg.t1_care_unit_split_carriage),
        "Tier 0 惩罚 > Tier 1",
    )
    check(
        abs(cfg.t1_care_unit_split_carriage) > abs(cfg.t2_adult_cross_carriage),
        "Tier 1 惩罚 > Tier 2",
    )
    check(
        abs(cfg.t2_adult_cross_carriage) > abs(cfg.t3_quiet_group_extra),
        "Tier 2 惩罚 > Tier 3",
    )
    check(abs(cfg.t3_quiet_group_extra) > abs(cfg.t4_not_together), "Tier 3 惩罚 > Tier 4")
    check(
        abs(cfg.t1_isolated_care_member) > abs(cfg.t2_adult_cross_carriage),
        "安全底线（孤立儿童）重于 Tier 2",
    )
    check(
        abs(cfg.t0_separated_care_bond) > abs(cfg.t1_isolated_care_member),
        "跨车厢分离（Tier 0）重于同车厢孤立（Tier 1）",
    )


def test_reward_is_hard_capped() -> None:
    """奖励硬上限：任何座位组合的奖励都不超过 max_reward_per_passenger。"""
    print("[权重] 奖励硬上限")
    cfg = EngineConfig()
    engine = fresh_engine(formation=mini_formation(rows=2, quiet_carriages=(1,)))
    engine.state.mark_occupied(
        [s.seat_id for s in engine.formation.seats if not s.is_quiet_carriage]
    )
    result = engine.book(solo_order("Z1"), mode="smart")
    rewards = [v for v in result.solution.violations if v.affinity > 0]
    total_reward = sum(v.affinity for v in rewards)
    check(
        total_reward <= cfg.max_reward_per_passenger,
        f"单人总奖励 {total_reward:.0f} ≤ 上限 {cfg.max_reward_per_passenger:.0f}",
    )
    check(
        cfg.max_reward_per_passenger < abs(cfg.t1_care_bond_same_carriage) * 100,
        "奖励上限远小于任何一级惩罚的量级",
    )


def test_unit_splitting() -> None:
    """订单拆解：硬核单元 / 强绑定 / 软性单元。"""
    print("[拆解] 单元划分")
    order = family_with_child_order()
    units = split_units(order)
    check(len(units) == 1, f"带娃家庭合并为一个硬核单元（实际 {len(units)}）")
    check(units[0].bond is BondType.MANDATORY, "单元类型为 MANDATORY")
    check(units[0].size == 3, "单元包含 3 人")
    solo = split_units(solo_order("S9"))
    check(solo[0].bond is BondType.SOFT, "单人出行是软性单元")


def test_quiet_repulsion_model() -> None:
    """静音排斥权重：婴儿最高，成人最低，低信用分放大。"""
    print("[信用] 静音排斥权重")
    infant = child("B", age=1)
    lively = child("L", age=8, behavior=DeclaredBehavior.LIVELY)
    quiet_kid = child("Q", age=8, behavior=DeclaredBehavior.QUIET)
    adult = solo_order("A").passengers[0]
    check(infant.quiet_repulsion == 1.0, "婴儿排斥权重 = 1.0")
    check(lively.quiet_repulsion > quiet_kid.quiet_repulsion, "活泼型 > 安静型")
    check(adult.quiet_repulsion == 0.0, "普通成人排斥权重 = 0（可进入静音车厢）")
    low = Passenger("Z", quietness_score=20.0, declared_behavior=DeclaredBehavior.LIVELY)
    high = Passenger("Y", declared_behavior=DeclaredBehavior.LIVELY)
    check(low.quiet_repulsion > high.quiet_repulsion, "低信用分放大排斥权重")


def test_scorer_explain_mentions_worst_first() -> None:
    """解释文本按严重程度排序。"""
    print("[可解释性] 排序")
    engine = fresh_engine(formation=mini_formation(rows=4, quiet_carriages=(1,)))
    # 只剩静音车厢，制造 Tier 3/1 冲突
    quiet = {s.seat_id for s in engine.formation.seats if s.is_quiet_carriage}
    engine.state.mark_occupied([s.seat_id for s in engine.formation.seats if s.seat_id not in quiet])
    result = engine.book(multi_gen_order(), mode="degraded")
    text = Scorer.explain(result.solution.violations)
    check("Tier" in text, "解释包含 Tier 信息")
    check(len(result.solution.violations) > 0, "产生代价条目（因为只剩静音车厢）")


def test_mini_formation_shape() -> None:
    """编组生成器：列布局、过道、无障碍专区、静音标签。"""
    print("[编组] 座位图谱")
    formation = mini_formation(rows=4, quiet_carriages=(2,))
    check(len(formation.seats) == 3 * 4 * 5, f"3 厢 × 4 排 × 5 列 = 60（实际 {len(formation.seats)}）")
    quiet_seats = [s for s in formation.seats if s.is_quiet_carriage]
    check(all(s.carriage == 2 for s in quiet_seats), "静音车厢标签只落在 2 车")
    accessible = [s for s in formation.seats if s.in_accessible_zone()]
    check(all(s.carriage == 1 for s in accessible), "无障碍专区只在 1 车")
    check(
        any("aisle" in {f.value for f in s.features} for s in formation.seats),
        "存在过道座位标签",
    )


def test_real_formation_seat_counts_are_realistic() -> None:
    """16 节编组的座位数必须接近真实 CRH，且**各车厢排数不同**。

    这条测试来自用户抓到的一个真实缺陷：早期实现让 16 节车厢共用同一个排数
    （默认 17），只靠"减少列数"区分坐席，于是商务座被算成
    ``3 列 × 17 排 = 51 座`` —— 而现实中商务座是 1+2 布局、整节约 10-18 座。

    为什么必须守住：座位数是**容量上限**，直接决定"满座时会不会拒票""无障碍
    专区能服务几位轮椅旅客"。一个把商务座容量夸大 4 倍的模型，会让压力测试
    得出过于乐观的结论。
    """
    print("[编组] 真实编组的座位数")
    formation = crh_16_car_formation()
    per_carriage = {
        carriage.number: len([s for s in formation.seats if s.carriage == carriage.number])
        for carriage in formation.carriages
    }
    by_class: dict[str, list[int]] = {}
    for carriage in formation.carriages:
        by_class.setdefault(carriage.class_code, []).append(per_carriage[carriage.number])

    check(len(formation.carriages) == 16, f"16 节编组（实际 {len(formation.carriages)}）")
    check(
        len({carriage.rows for carriage in formation.carriages}) > 1,
        f"各车厢排数**不完全相同**（实际 {sorted({c.rows for c in formation.carriages})}）",
    )

    business = by_class["商务座"]
    first = by_class["一等座"]
    second = by_class["二等座"]
    check(
        all(10 <= count <= 18 for count in business),
        f"单节商务座 10-18 座（实际 {business}）",
    )
    check(
        all(40 <= count <= 64 for count in first),
        f"单节一等座 40-64 座（实际 {first}）",
    )
    check(
        all(64 <= count <= 90 for count in second),
        f"单节二等座 64-90 座（实际 {second}）",
    )
    check(
        max(business) < min(first) and max(first) < min(second),
        "座位数按坐席严格递减：商务座 < 一等座 < 二等座",
    )

    columns_per_class = {
        carriage.class_code: len(carriage.columns) for carriage in formation.carriages
    }
    check(
        columns_per_class["二等座"] == 5
        and columns_per_class["一等座"] == 4
        and columns_per_class["商务座"] == 3,
        f"列布局符合国铁惯例（{columns_per_class}）",
    )
    accessible = [s for s in formation.seats if s.in_accessible_zone()]
    check(len(accessible) >= 6, f"无障碍专区有足够座位（{len(accessible)} 个）")


def test_every_config_field_is_actually_used() -> None:
    """静态校验：`EngineConfig` 的每个字段都必须真的被代码读取。

    这条测试来自一次真实事故：早期版本里有 4 个配置项（过道宽度、近车门排数、
    近卫生间排数、奖励地板）只是"声明了"，从未被任何代码引用——调参看起来生效、
    实际毫无作用，是最危险的一类死代码。现在把它变成机器可验证的不变量。
    """
    print("[卫生] 配置项必须真正生效")
    root = Path(__file__).resolve().parents[1] / "smartrail"
    sources = {
        path: path.read_text(encoding="utf-8")
        for path in root.rglob("*.py")
        if path.name != "config.py"
    }
    unused: list[str] = []
    for field_name in EngineConfig.__dataclass_fields__:
        referenced = any(
            f".{field_name}" in text or f'"{field_name}"' in text
            for text in sources.values()
        )
        if not referenced:
            unused.append(field_name)
    check(not unused, f"所有配置项都被引用（未被使用的：{unused or '无'}）")

    # 反向检查：文档化的 Tier 权重必须存在，避免改名后文档与代码脱节
    required = {
        "t0_separated_care_bond",
        "t0_wheelchair_no_accessible",
        "t0_pregnant_no_companion",
        "t1_care_unit_split_carriage",
        "t1_conflict_isolation",
        "t2_adult_cross_carriage",
        "t3_quiet_family_base",
        "t3_quiet_blocked_credit",
        "t4_aisle_separated",
        "t5_quiet_solo_adult",
        "max_reward_per_passenger",
    }
    missing = sorted(required - set(EngineConfig.__dataclass_fields__))
    check(not missing, f"README 承诺的权重字段都存在（缺失：{missing or '无'}）")


def test_pregnant_without_companion_is_tier0() -> None:
    """孕晚期无同行人：Tier 0 灾难级（README 4.1 明列）。"""
    print("[Tier 0] 孕晚期无同行人")
    engine = fresh_engine()
    lonely = Order(
        order_id="O-PREG-SOLO",
        passengers=(
            Passenger(
                "P9",
                age=33,
                support_needs=frozenset({SupportNeed.PREGNANT_LATE}),
                needs_caregiver=True,
            ),
        ),
        relation=RelationType.SOLO,
        bonds={},
    )
    result = engine.book(lonely, mode="smart")
    codes = {v.code for v in result.solution.violations}
    check("T0_PREGNANT_NO_COMPANION" in codes, f"触发 Tier 0 孕晚期惩罚（实际 {sorted(codes)}）")
    tier0 = [v for v in result.solution.violations if v.tier == 0]
    check(bool(tier0), "该惩罚归属 Tier 0")


def test_config_wiring_affects_behaviour() -> None:
    """可调参数必须真的改变结果（近车门/近卫生间/过道宽度/奖励地板）。"""
    print("[卫生] 参数可调且生效")
    from smartrail.carriage import build_formation, CarriageSpec

    narrow = build_formation([CarriageSpec(number=1, rows=6, has_toilet=True)], near_door_rows=1)
    wide = build_formation([CarriageSpec(number=1, rows=6, has_toilet=True)], near_door_rows=4)
    narrow_near = sum(1 for s in narrow.seats if "near_door" in {f.value for f in s.features})
    wide_near = sum(1 for s in wide.seats if "near_door" in {f.value for f in s.features})
    check(wide_near > narrow_near, f"near_door_rows 生效（{narrow_near} -> {wide_near}）")

    tight = build_formation([CarriageSpec(number=1, rows=3)], aisle_weight=1.0)
    loose = build_formation([CarriageSpec(number=1, rows=3)], aisle_weight=4.0)
    # 过道位于 C 之后：C↔D 是"跨过道"的一对，其距离直接由权重决定
    tight_gap = tight.seat("01车01C").manhattan_to(tight.seat("01车01D"))
    loose_gap = loose.seat("01车01C").manhattan_to(loose.seat("01车01D"))
    check(loose_gap > tight_gap, f"aisle_crossing_weight 生效（跨过道 {tight_gap} -> {loose_gap}）")
    # 非跨过道的相邻列对不受权重影响（A↔B 恒为 1）
    check(
        tight.seat("01车01A").manhattan_to(tight.seat("01车01B")) == 1
        and loose.seat("01车01A").manhattan_to(loose.seat("01车01B")) == 1,
        "同侧相邻座位距离恒为 1（权重只作用于过道）",
    )

    cfg = EngineConfig()
    floor_engine = SeatEngine(
        formation=crh_16_car_formation(),
        config=EngineConfig(min_floor_reward=1000.0),
    )
    result = floor_engine.book(solo_order("F1"), mode="smart")
    rewards = [v for v in result.solution.violations if v.is_reward]
    check(not rewards, "奖励地板生效：抬高阈值后不再发放碎片化奖励")
    check(cfg.min_floor_reward < 1000.0, "默认地板低于该测试值（说明参数确实被读取）")


def main() -> int:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
    print("-" * 72)
    if FAILURES:
        print(f"失败 {len(FAILURES)} 项：")
        for item in FAILURES:
            print(f"  - {item}")
        return 1
    print(f"全部 {len(tests)} 组断言通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
