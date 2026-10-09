"""轮椅固定停放位的验收（独立资源，不占座位票额）。

需求原文：
    "每一节无障碍车厢（4，12）各有两个轮椅固定停放位，不占正常座位票额，
     如果 4 个轮椅位都卖完了，可在询问后出正常坐票。"

逐条守护：

1. 每节无障碍车厢 **2 个**停放位，全列 **4 个**（不是 20 个）；
2. 停放位 **不占座位票额** —— 04 车仍是 78 个二等座，可照常卖给别人；
3. 轮椅旅客**优先**拿到停放位（4 张单依次落在 04车W1/W2 → 12车W1/W2）；
4. 4 个满位后**给出询问**，而不是静默拒票；
5. 询问后**照常出普通坐票**（出票优先）；
6. 普通旅客占用停放位会被 Tier 3 排斥；
7. 停放位空着时可卖给普通旅客。

双模式：``python tests/test_wheelchair_bays.py`` 或 ``pytest``。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from smartrail.carriage import G25_EXPECTED_SEATS, g25_16_car_formation  # noqa: E402
from smartrail.config import EngineConfig  # noqa: E402
from smartrail.models import (  # noqa: E402
    Passenger,
    SupportNeed,
    TicketType,
)
from smartrail.scoring import (  # noqa: E402
    Scorer,
    is_wheelchair_seat,
    wheelchair_bay_slot_ids,
    wheelchair_bays_free,
)
from smartrail.ticketing import (  # noqa: E402
    book_ticket_order,
    reset_dev_store,
    reset_passenger_store,
)
from smartrail.ticketing.booking import evaluate_wheelchair_bays  # noqa: E402

UNDER_PYTEST = "PYTEST_CURRENT_TEST" in os.environ or "pytest" in sys.modules
_FAILURES: list[str] = []

WHEELCHAIR_PROFILE = "C012"   # 刘建国（轮椅）
PLAIN_PROFILE = "C001"        # 周昊（成人）


def check(condition: bool, message: str) -> None:
    if condition:
        print(f"  PASS  {message}")
        return
    print(f"  FAIL  {message}")
    if UNDER_PYTEST:
        raise AssertionError(message)
    _FAILURES.append(message)


def test_two_bays_per_accessible_carriage() -> None:
    """每节无障碍车厢 2 个停放位，全列 4 个。"""
    print("[轮椅位] 数量与分布")
    formation = g25_16_car_formation()
    bays = formation.wheelchair_bays
    check(len(bays) == 4, f"全列 4 个停放位（实际 {len(bays)}）")
    by_car: dict[int, int] = {}
    for bay in bays:
        by_car[bay.carriage] = by_car.get(bay.carriage, 0) + 1
    check(by_car == {4: 2, 12: 2}, f"04 车 2 个、12 车 2 个（实际 {by_car}）")
    check(all(bay.slot_seat_id for bay in bays), "每个停放位都有账目座位槽")
    slots = [bay.slot_seat_id for bay in bays]
    check(len(set(slots)) == 4, f"槽位互不重复（{slots}）")
    check(all(any(s.seat_id == sid for s in formation.seats) for sid in slots),
          "槽位都是编组里的真实座位")


def test_bays_do_not_consume_seat_inventory() -> None:
    """停放位**不占座位票额**：各车厢定员与车型图完全一致。"""
    print("[轮椅位] 不占座位票额")
    formation = g25_16_car_formation()
    actual: dict[int, int] = {}
    for seat in formation.seats:
        actual[seat.carriage] = actual.get(seat.carriage, 0) + 1
    mismatch = {
        number: (want, actual.get(number))
        for number, want in G25_EXPECTED_SEATS.items()
        if actual.get(number) != want
    }
    check(not mismatch, f"16 节定员仍与车型图一致（不符：{mismatch or '无'}）")
    check(actual[4] == 78 and actual[12] == 78,
          f"04/12 车仍为 78 座（实际 {actual[4]}/{actual[12]}）")
    check(len(formation.seats) == 1238, f"座位总数仍为 1238（{len(formation.seats)}）")
    check(formation.total_wheelchair_bays == 4, "停放位单独计数，不混入座位")
    # 停放位空着时，槽位座位是可售的
    check(not any(s.accessible_zone for s in formation.seats),
          "没有座位被标成『无障碍专区』（旧模型已移除）")


def test_wheelchair_prefers_bays() -> None:
    """轮椅旅客优先拿到停放位（4 张单依次占满）。"""
    print("[轮椅位] 优先分配")
    store = reset_dev_store()
    pax = reset_passenger_store()
    slots = store.bay_slot_ids()
    for index in range(1, 5):
        before = evaluate_wheelchair_bays(store, pax.by_ids([WHEELCHAIR_PROFILE]))
        check(not before.needs_confirmation,
              f"第 {index} 张：空位 {before.bays_free} -> 不需询问")
        result = book_ticket_order(store, pax.by_ids([WHEELCHAIR_PROFILE]),
                                   class_code="二等座", order_id=f"W{index}")
        seat_id = result["order"]["passengers"][0]["seat_id"]
        check(seat_id in slots, f"第 {index} 张落在停放位（{seat_id}）")
    check(len(store.free_bays()) == 0, "4 张单后停放位全部占用")


def test_overflow_asks_then_issues() -> None:
    """4 个满位后：**先询问**，确认后**照常出普通坐票**。"""
    print("[轮椅位] 满位后询问并出票")
    store = reset_dev_store()
    pax = reset_passenger_store()
    for index in range(1, 5):
        book_ticket_order(store, pax.by_ids([WHEELCHAIR_PROFILE]),
                          class_code="二等座", order_id=f"FILL{index}")
    check(len(store.free_bays()) == 0, "停放位已满")

    verdict = evaluate_wheelchair_bays(store, pax.by_ids([WHEELCHAIR_PROFILE]))
    check(verdict.needs_confirmation, "给出询问（needs_confirmation=True）")
    check(bool(verdict.question), f"询问文案非空：{verdict.question[:40]}…")
    for keyword in ("轮椅固定停放位", "普通坐票", "站车"):
        check(keyword in verdict.question, f"询问里说明「{keyword}」")

    result = book_ticket_order(store, pax.by_ids([WHEELCHAIR_PROFILE]),
                               class_code="二等座", order_id="OVER1")
    check(result["ok"], "仍然出票（不静默拒票）")
    seat_id = result["order"]["passengers"][0]["seat_id"]
    check(seat_id not in store.bay_slot_ids(),
          f"发的是普通座位（{seat_id}）")
    check(result["wheelchair"]["needs_confirmation"], "订单里记录需要确认")


def test_large_wheelchair_order_counts_shortfall() -> None:
    """一张单里 6 位轮椅：4 个进停放位、2 位需确认。"""
    print("[轮椅位] 大单缺口")
    store = reset_dev_store()
    pax = reset_passenger_store()
    profiles = [pax.get(WHEELCHAIR_PROFILE)]
    # 同一档案只能选一次，这里直接构造 6 位轮椅旅客的评估
    from smartrail.ticketing.passenger_store import PassengerProfile

    many = [PassengerProfile(profile_id=f"W{i}", name=f"轮椅{i}",
                             type_id="wheelchair") for i in range(6)]
    verdict = evaluate_wheelchair_bays(store, many)
    check(verdict.requested == 6, f"识别 6 位轮椅（{verdict.requested}）")
    check(verdict.bays_free == 4, f"可用停放位 4（{verdict.bays_free}）")
    check(verdict.needs_confirmation, "需要确认")
    check("2 位" in verdict.question, f"询问里指出缺 2 位：{verdict.question[:60]}…")
    _ = profiles


def test_ordinary_passenger_is_pushed_away() -> None:
    """普通旅客占用停放位槽会被 Tier 3 排斥。"""
    print("[轮椅位] 普通旅客排斥")
    formation = g25_16_car_formation()
    config = EngineConfig()
    slots = wheelchair_bay_slot_ids(formation)
    plain = Passenger(passenger_id="A1", name="成人", age=35)
    wheel = Passenger(
        passenger_id="W1", name="轮椅", age=45,
        ticket_type=TicketType.DISABLED_VETERAN,
        support_needs=frozenset({SupportNeed.WHEELCHAIR}),
    )
    Bay = formation.seat("04车01A")
    plain_seat = formation.seat("02车01A")

    plain_cost, plain_terms = Scorer(
        config, wheelchair_bays_free=4, bay_slot_ids=slots
    ).individual_cost(plain, Bay)
    plain_normal, _ = Scorer(
        config, wheelchair_bays_free=4, bay_slot_ids=slots
    ).individual_cost(plain, plain_seat)
    check(plain_cost < plain_normal,
          f"普通旅客占停放位更差（{plain_cost} < {plain_normal}）")
    check(any("停放位" in term.detail for term in plain_terms),
          f"给出可解释原因：{plain_terms[0].detail if plain_terms else '无'}")

    wheel_bay, _ = Scorer(
        config, wheelchair_bays_free=4, bay_slot_ids=slots
    ).individual_cost(wheel, Bay)
    wheel_plain, wheel_terms = Scorer(
        config, wheelchair_bays_free=4, bay_slot_ids=slots
    ).individual_cost(wheel, plain_seat)
    check(wheel_bay > wheel_plain,
          f"轮椅旅客进停放位更好（{wheel_bay} > {wheel_plain}）")
    check(any(term.tier == 0 for term in wheel_terms),
          "停放位空着却给普通座位 -> 记 Tier 0")


def test_sold_out_is_service_not_failure() -> None:
    """停放位售罄时给普通座位**不算 Tier 0**（是运营事实，不是求解器失误）。"""
    print("[轮椅位] 售罄记服务例外")
    formation = g25_16_car_formation()
    config = EngineConfig()
    slots = wheelchair_bay_slot_ids(formation)
    wheel = Passenger(
        passenger_id="W1", name="轮椅", age=45,
        ticket_type=TicketType.DISABLED_VETERAN,
        support_needs=frozenset({SupportNeed.WHEELCHAIR}),
    )
    plain_seat = formation.seat("02车01A")
    cost, terms = Scorer(
        config, wheelchair_bays_free=0, bay_slot_ids=slots
    ).individual_cost(wheel, plain_seat)
    check(not any(term.tier == 0 for term in terms),
          f"满位时不记 Tier 0（terms={[(t.tier, t.value) for t in terms]}）")
    check(cost > -1000, f"代价不爆炸（{cost}）")

    # 反证：停放位还空着时给普通座位必须记 Tier 0
    cost2, terms2 = Scorer(
        config, wheelchair_bays_free=2, bay_slot_ids=slots
    ).individual_cost(wheel, plain_seat)
    check(any(term.tier == 0 for term in terms2),
          "有空位却给普通座位 -> 记 Tier 0")


def test_helper_functions_agree() -> None:
    """``is_wheelchair_seat`` / ``wheelchair_bays_free`` 口径一致。"""
    print("[轮椅位] 辅助函数")
    formation = g25_16_car_formation()
    slots = wheelchair_bay_slot_ids(formation)
    bay = formation.seat("04车01A")
    normal = formation.seat("02车01A")
    check(is_wheelchair_seat(bay, slots), "停放位槽被认作轮椅落点")
    check(not is_wheelchair_seat(normal, slots), "普通座位不被认作轮椅落点")
    check(wheelchair_bays_free(formation, set()) == 4, "空车 4 个停放位")
    check(wheelchair_bays_free(formation, {"04车01A"}) == 3,
          "占用 1 个后剩 3 个")
    check(wheelchair_bays_free(formation, slots) == 0, "全占用后剩 0 个")
    # 没有停放位信息时退回无障碍专区口径（向后兼容）
    check(is_wheelchair_seat(bay, None) == bay.in_accessible_zone(),
          "无停放位信息时退回旧判据")


def test_dev_snapshot_marks_bays() -> None:
    """开发者座位图标出停放位。"""
    print("[轮椅位] 座位图标记")
    store = reset_dev_store()
    snapshot = store.snapshot()
    marked = {s["seat_id"] for s in snapshot["seats"] if s.get("wheelchair_bay")}
    check(marked == set(store.bay_slot_ids()),
          f"座位图标出 4 个停放位（{sorted(marked)}）")
    summary = snapshot["wheelchair_bays"]
    check(summary["total"] == 4 and summary["free"] == 4,
          f"总览 4 个全空（{summary['total']}/{summary['free']}）")
    cars = {c["number"]: c for c in snapshot["carriages"]}
    check(cars[4]["wheelchair_bays"] == 2 and cars[12]["wheelchair_bays"] == 2,
          f"04/12 车各 2 个（{cars[4]['wheelchair_bays']}/{cars[12]['wheelchair_bays']}）")


def main() -> int:
    tests = [
        test_two_bays_per_accessible_carriage,
        test_bays_do_not_consume_seat_inventory,
        test_wheelchair_prefers_bays,
        test_overflow_asks_then_issues,
        test_large_wheelchair_order_counts_shortfall,
        test_ordinary_passenger_is_pushed_away,
        test_sold_out_is_service_not_failure,
        test_helper_functions_agree,
        test_dev_snapshot_marks_bays,
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
