"""出票优先策略的专项测试。

背景：本项目最初把"相邻座位"当成近似硬性的发牌条件 —— 无障碍专区满了就
**拒票候补**（"绝不发放普通座位"）。这在运营上是错的：

* 拒票 = 直接损失客票收入；
* 而"一家人没挨着"在高铁上是可以通过**列车员现场调剂**解决的（换座是常态）；
* 轮椅旅客在普通车厢同样可以乘车，只是需要站车协助踏板衔接。

因此策略修正为 **出票优先于座位理想度**：
能出票就出票，把"需要人处理的事"明确交办出去；拒票只保留给"确实无座"。

本套件守护这条策略不被后续改动悄悄改回去。
"""

from __future__ import annotations

import os
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from smartrail import crh_16_car_formation  # noqa: E402
from smartrail.api import service  # noqa: E402
from smartrail.engine import SeatEngine  # noqa: E402
from smartrail.fixtures import (  # noqa: E402
    family_with_child_order,
    multi_gen_order,
    pregnant_order,
    wheelchair_order,
)
from smartrail.notices import assess_adjacency, is_adjacent  # noqa: E402
from smartrail.scoring import Scorer  # noqa: E402
from smartrail.solver import build_context, evaluate_placement  # noqa: E402

UNDER_PYTEST = "PYTEST_CURRENT_TEST" in os.environ or "pytest" in sys.modules
_FAILURES: list[str] = []


def check(condition: bool, message: str) -> None:
    if condition:
        print(f"  PASS  {message}")
        return
    print(f"  FAIL  {message}")
    if UNDER_PYTEST:
        raise AssertionError(message)
    _FAILURES.append(message)


def fresh_engine() -> SeatEngine:
    return SeatEngine(formation=crh_16_car_formation())


def scatter_all_adjacency(engine: SeatEngine) -> None:
    """占用"每一对相邻座位中的一个"，使车厢内**不存在任何相邻空位**。

    做法：按 (车厢, 排, 列) 分组，每两列占掉一列；同时占掉相邻排的同一列，
    保证纵向也不相邻。这样无论求解器怎么选，都不可能凑出相邻座位 ——
    正是"实在没有相邻座位"的极端场景。
    """
    seats = list(engine.state.formation.seats)
    for seat in seats:
        if (seat.col_index % 2 == 1) or (seat.row % 2 == 1):
            engine.state.occupied.add(seat.seat_id)


def test_no_adjacent_seats_still_issues_tickets() -> None:
    """极端场景：车厢内不存在任何相邻空位 —— 仍必须全部出票。"""
    print("[出票优先] 无任何相邻空位时仍出票")
    engine = fresh_engine()
    scatter_all_adjacency(engine)
    free = len(engine.state.available_seats)
    check(free > 3, f"仍有余票可发（{free} 个空位）")

    # 先确认这个场景确实"无相邻空位"
    free_seats = list(engine.state.available_seats)
    adjacent_pairs = [
        (a, b)
        for index, a in enumerate(free_seats)
        for b in free_seats[index + 1 :]
        if is_adjacent(a, b)
    ]
    check(not adjacent_pairs, f"场景构造正确：无相邻空位（相邻对数 {len(adjacent_pairs)}）")

    result = engine.book(family_with_child_order(order_id="O-NO-ADJ"), mode="smart")
    solution = result.solution
    check(
        len(solution.assignments) == 3,
        f"3 人全部出票（实际 {len(solution.assignments)}）",
    )
    check(not solution.waitlisted, "无人被拒票")
    check(solution.adjacency.level == "impossible", f"相邻结论为 impossible（实际 {solution.adjacency.level}）")
    kinds = {n.kind for n in solution.notices}
    check(
        bool(kinds & {"caregiver_split", "scattered"}),
        f"生成需现场处理的提示（实际 {sorted(kinds)}）",
    )
    messages = " ".join(n.message for n in solution.notices)
    check(
        "列车员" in messages or "站车" in messages,
        "提示明确告诉旅客/乘务员如何处理",
    )


def test_adjacency_never_blocks_ticketing() -> None:
    """不变量：只要有足够余票，**任何场景都不应产生候补**。"""
    print("[出票优先] 有余票就不候补（不变量）")
    scenarios = (
        ("带娃家庭", family_with_child_order),
        ("多代同堂", multi_gen_order),
        ("轮椅旅客", wheelchair_order),
        ("孕晚期", pregnant_order),
    )
    for ratio in (0.5, 0.8):  # 越往后空位越零散
        engine = fresh_engine()
        seats = list(engine.state.formation.seats)
        step = max(2, int(round(1 / ratio)))
        for index, seat in enumerate(seats):
            if index % step != 0:
                engine.state.occupied.add(seat.seat_id)
        for label, factory in scenarios:
            order = factory()
            if len(order.passengers) > len(engine.state.available_seats):
                continue
            probe = fresh_engine()
            probe.state.occupied |= set(engine.state.occupied)
            result = probe.book(order, mode="smart")
            check(
                not result.solution.waitlisted,
                f"空位比例 {ratio:.0%} 时 {label} 未被拒票"
                f"（候补 {result.solution.waitlisted}）",
            )


def test_wheelchair_zone_full_issues_ticket_with_notice() -> None:
    """无障碍专区售罄：出票 + 站车协助提示（不是拒票）。"""
    print("[出票优先] 无障碍专区售罄 -> 出票 + 提示")
    engine = fresh_engine()
    accessible = [s.seat_id for s in engine.formation.seats if s.in_accessible_zone()]
    engine.state.mark_occupied(accessible)
    result = engine.book(wheelchair_order(order_id="O-WF"), mode="smart")
    solution = result.solution
    check("W1" in solution.assignments, "轮椅旅客出票")
    notices = [n for n in solution.notices if n.kind == "accessible_zone_full"]
    check(bool(notices), "生成 accessible_zone_full 提示")
    if notices:
        check(notices[0].level == "action", "提示等级 action（需现场处理）")
        check(notices[0].carriage is not None, "提示带车厢信息（便于站车定位）")


def test_notices_are_serialisable_and_in_api() -> None:
    """提示必须能通过 API 完整送达前端。"""
    print("[出票优先] 提示可序列化并出现在 API 返回中")
    engine = service.create_engine("crh16", service.CreditLedger())
    payload = {
        "order_id": "O-NOTICE-API",
        "relation": "nuclear_family",
        "passengers": [
            {"passenger_id": "A1", "age": 36},
            {"passenger_id": "A2", "age": 34},
            {"passenger_id": "C1", "age": 5, "ticket_type": "child"},
        ],
        "bonds": [
            {"a": "A1", "b": "C1", "bond": "mandatory"},
            {"a": "A1", "b": "A2", "bond": "strong"},
        ],
    }
    order = service.order_from_payload(payload)
    # 把列车挤到只剩零散座位
    scatter_all_adjacency(engine)
    response = service.book_order(engine, {**payload, "mode": "smart"})
    body = response["body"]
    check(response["ok"] and response["status"] == 200, "下单成功（未被拒票）")
    check("notices" in body, "API 返回包含 notices 字段")
    check("adjacency" in body, "API 返回包含 adjacency 字段")
    check(
        isinstance(body["notices"], list) and body["notices"],
        f"notices 非空（{len(body.get('notices', []))} 条）",
    )
    for notice in body["notices"]:
        for field in ("passenger_id", "kind", "level", "message"):
            check(field in notice, f"提示字段完整：{field}")
        break  # 检查一条即可，避免刷屏


def test_adjacency_gradient_pulls_family_together() -> None:
    """就近梯度必须真的生效：无法紧邻时，优选项应当是"相邻排"而非"隔很多排"。

    这条测试守护的是"分数之外还有实际行为"：只加权重不算数，
    要能看到求解器把一家人往一起收。
    """
    print("[出票优先] 就近梯度确实把同行人拉近")
    engine = fresh_engine()
    scatter_all_adjacency(engine)
    result = engine.book(family_with_child_order(order_id="O-GRAD"), mode="smart")
    placed = {
        pid: engine.formation.seat(a.seat_id)
        for pid, a in result.solution.assignments.items()
    }
    check(len(placed) == 3, "3 人全部出票")
    rows = sorted(seat.row for seat in placed.values())
    carriages = {seat.carriage for seat in placed.values()}
    check(len(carriages) == 1, f"全部在同一车厢（{sorted(carriages)}）")
    check(
        rows[-1] - rows[0] <= 3,
        f"排号跨度受限（rows={rows}，跨度 {rows[-1] - rows[0]} ≤ 3）"
        " —— 就近梯度生效，没有被撒到很远",
    )


def test_adjacency_assessment_matches_reality() -> None:
    """相邻结论必须与真实座位距离一致（防止"报告说相邻、实际不挨着"）。"""
    print("[出票优先] 相邻结论与真实距离一致")
    engine = fresh_engine()
    order = family_with_child_order(order_id="O-ADJ-CHECK")
    prepared = engine.prepare_order(order)
    result = engine.book(prepared, mode="smart")
    solution = result.solution
    placed = {
        pid: engine.formation.seat(a.seat_id) for pid, a in solution.assignments.items()
    }
    ctx = build_context(prepared, engine.config, all_seats=engine.formation.seats)
    status = assess_adjacency(placed, ctx, engine.config)

    # 独立重算一遍"是否有未相邻的绑定对"
    def bond_of(a: str, b: str):
        return prepared.bond_of(a, b).value

    manual_unsatisfied = []
    ids = sorted(placed)
    for index, a in enumerate(ids):
        for b in ids[index + 1 :]:
            if bond_of(a, b) in ("mandatory", "strong"):
                if not is_adjacent(placed[a], placed[b]):
                    manual_unsatisfied.append((a, b))
    expected_level = "satisfied" if not manual_unsatisfied else status.level
    check(
        (status.level == "satisfied") == (not manual_unsatisfied),
        f"结论 {status.level} 与手工核对一致（未相邻绑定对 {manual_unsatisfied}）",
    )
    check(expected_level in {"satisfied", "compromised", "impossible"}, "结论取值合法")

    # 全部出票 + 亲和度可重算
    value, _ = evaluate_placement(placed, ctx, Scorer(engine.config))
    check(
        abs(value - solution.total_affinity) < 1e-6,
        f"亲和度可由权威打分器重算（{value:.1f}）",
    )


def test_conservative_policy_is_opt_in_only() -> None:
    """保守策略必须显式开启，且默认值为出票。"""
    print("[出票优先] 默认出票，保守策略需显式开启")
    from smartrail.config import EngineConfig

    check(
        EngineConfig().waitlist_wheelchair_without_accessible is False,
        "默认配置为出票（waitlist_wheelchair_without_accessible=False）",
    )
    engine = fresh_engine()
    engine.config = replace(engine.config, waitlist_wheelchair_without_accessible=True)
    accessible = [s.seat_id for s in engine.formation.seats if s.in_accessible_zone()]
    engine.state.mark_occupied(accessible)
    result = engine.book(wheelchair_order(order_id="O-CONS"), mode="smart")
    check("W1" in result.solution.waitlisted, "显式开启后才拒票候补")


def main() -> int:
    tests = [
        test_no_adjacent_seats_still_issues_tickets,
        test_adjacency_never_blocks_ticketing,
        test_wheelchair_zone_full_issues_ticket_with_notice,
        test_notices_are_serialisable_and_in_api,
        test_adjacency_gradient_pulls_family_together,
        test_adjacency_assessment_matches_reality,
        test_conservative_policy_is_opt_in_only,
    ]
    for test in tests:
        if UNDER_PYTEST:
            test()
        else:
            try:
                test()
            except AssertionError:
                pass
    print("-" * 72)
    if _FAILURES:
        print(f"失败 {len(_FAILURES)} 项：")
        for item in _FAILURES:
            print(f"  - {item}")
        return 1
    print("全部 7 组断言通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
