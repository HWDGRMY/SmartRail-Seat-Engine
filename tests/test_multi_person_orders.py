"""各种多人组合订单的验收（按人数 × 类型 × 席别 × 余票状态交叉）。

需求："各种多人组合订单测试"。这个文件把它做成一张**组合矩阵**，
而不是零散挑几个例子 —— 因为漏测的往往正是"没被列进例子的那一格"。

矩阵维度
--------
* **人数**：1 / 2 / 3 / 5 / 8 人（覆盖"一排 5 座装不下 8 人"这个结构性难点）
* **人群构成**：全成人、带儿童、带婴儿（硬绑定）、轮椅+照护、
  老人+孕妇、多代同堂、视障需导盲
* **席别**：二等座 / 一等座 / 商务座（硬约束，不能坐错）
* **余票状态**：空车 / 打散到只剩碎片 / 满座
* **订单数**：单张 vs 多张连续提交（顺序处理，非并发）

每条组合都验证四件事：
1. 不出现 Tier 0（安全底线）；
2. 硬绑定的人**同车厢**；
3. 席别正确（付什么钱坐什么座）；
4. 要么全员出票，要么给出**可操作**的说明（不静默失败）。

双模式：``python tests/test_multi_person_orders.py`` 或 ``pytest``。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from smartrail.ticketing import (  # noqa: E402
    SeatPreference,
    book_ticket_order,
    reset_dev_store,
    reset_passenger_store,
)
from smartrail.ticketing.passenger_store import PassengerProfile  # noqa: E402

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


def make(*type_ids: str) -> list[PassengerProfile]:
    """按人群类型临时构造一张订单的乘客（数量可以任意多）。

    注意：下单流程会**重新编号**乘客 ID（``{order_id}-P1`` 形式），
    所以断言里不能按这里给的 ``profile_id`` 去找人，要按**顺序**索引。
    """
    return [
        PassengerProfile(profile_id=f"T{index}", name=f"{kind}{index}",
                         type_id=kind, source="user")
        for index, kind in enumerate(type_ids)
    ]


def find(store, result, index: int) -> dict:
    """按**下单时的顺序**取乘客结果（ID 会被流程重编号）。"""
    return result["order"]["passengers"][index]


#: 组合矩阵：名称 -> (人群类型序列, 期望同车厢的**下标对**)
#: 下标对应 ``make()`` 的入参顺序（下单流程会重新编号乘客 ID，
#: 所以这里用顺序下标而不是 ID）
COMBINATIONS: tuple[tuple[str, tuple[str, ...], tuple[tuple[int, int], ...]], ...] = (
    ("单人", ("adult",), ()),
    ("两人·成人同行", ("adult", "adult"), ((0, 1),)),
    ("父母 + 儿童", ("adult", "adult", "child"), ((0, 2), (1, 2))),
    ("父母 + 婴儿", ("adult", "adult", "infant"), ((0, 2), (1, 2))),
    ("父母 + 幼儿 + 儿童", ("adult", "adult", "toddler", "child"),
     ((0, 2), (0, 3))),
    ("老人 + 孕妇（足月）", ("elderly", "pregnant_term"), ((0, 1),)),
    ("轮椅 + 照护人", ("wheelchair", "caregiver"), ((0, 1),)),
    ("视障需导盲 + 照护", ("blind_with_guide", "caregiver"), ((0, 1),)),
    ("智力障碍 + 照护", ("intellectual", "caregiver"), ((0, 1),)),
    ("多代同堂 5 人", ("adult", "adult", "elderly", "child", "toddler"),
     ((0, 3), (0, 4))),
    ("朋友团 5 人", ("adult",) * 5, ()),
    ("大家庭 8 人（一排装不下）",
     ("adult", "adult", "elderly", "elderly", "child", "child", "toddler", "infant"),
     ((0, 4), (0, 5), (0, 7))),
)

#: 席别（硬约束：不能坐错）
CLASSES: tuple[str, ...] = ("二等座", "一等座", "商务座")


def _book(store, profiles, class_code: str, order_id: str):
    return book_ticket_order(store, profiles, class_code=class_code,
                             preference=SeatPreference(), order_id=order_id)


def _seat_class(store, seat_id: str) -> str:
    for seat in store.formation.seats:
        if seat.seat_id == seat_id:
            return seat.class_code
    return "?"


def test_combination_matrix() -> None:
    """人数 × 类型 × 席别的完整组合矩阵（空车）。"""
    print("[组合] 人数 × 类型 × 席别矩阵")
    checked = 0
    for name, types, _bonds in COMBINATIONS:
        profiles = make(*types)
        for class_code in CLASSES:
            store = reset_dev_store()
            result = _book(store, profiles, class_code, f"{name}-{class_code}")
            order = result["order"]
            ok = result["ok"]
            tier0 = result["tier0"]
            check(tier0 == 0, f"{name} / {class_code}：Tier 0 = 0")
            if not ok:
                check(bool(result["errors"] or order["waitlisted"]),
                      f"{name} / {class_code}：未出票时给出说明（不静默失败）")
                continue
            # 席别硬约束
            wrong = [
                item["seat_id"] for item in order["passengers"]
                if item["seat_id"] and not item["seat_id"].startswith("0")
                and item["seat_id"] not in store.bay_ids()
                and _seat_class(store, item["seat_id"]) != class_code
            ]
            check(not wrong,
                  f"{name} / {class_code}：席别全部正确"
                  f"（例外：{wrong or '无'}）")
            checked += 1
    check(checked >= 30, f"完成了 {checked} 个组合的求解")


def test_hard_bonds_share_carriage() -> None:
    """硬绑定的人必须同车厢（带娃家庭不被拆分）。"""
    print("[组合] 硬绑定同车厢")
    for name, types, bonds in COMBINATIONS:
        if not bonds:
            continue
        profiles = make(*types)
        store = reset_dev_store()
        result = _book(store, profiles, "二等座", f"BOND-{name}")
        if not result["ok"]:
            continue
        items = result["order"]["passengers"]
        for left_index, right_index in bonds:
            left = items[left_index]["carriage"]
            right = items[right_index]["carriage"]
            check(left is not None and left == right,
                  f"{name}：第 {left_index + 1} 位与第 {right_index + 1} 位"
                  f"同车厢（{left} vs {right}）")


def test_multiple_orders_sequential() -> None:
    """多张订单连续提交（顺序处理）：互不干扰、逐单可解释。"""
    print("[组合] 多张订单连续提交")
    store = reset_dev_store()
    pax = reset_passenger_store()
    plans = [
        ("O1", ["C001"], "二等座"),
        ("O2", ["C002", "C005"], "二等座"),
        ("O3", ["C012", "C016"], "二等座"),      # 轮椅 + 照护
        ("O4", ["C003", "C004"], "一等座"),      # 老人 + 青少年
        ("O5", ["C001"], "商务座"),
        ("O6", ["C006", "C007", "C008"], "二等座"),  # 儿童 + 幼儿 + 婴儿
    ]
    for order_id, ids, class_code in plans:
        result = _book(store, pax.by_ids(ids), class_code, order_id)
        check(result["tier0"] == 0, f"{order_id}：Tier 0 = 0")
        check(result["ok"], f"{order_id}：出票成功"
                            f"（{result['order']['seated']} 人）")
        check(result["order"]["seated"] == len(ids),
              f"{order_id}：{len(ids)} 人全部出票")
    check(len(store.orders) == len(plans),
          f"台账记录 {len(store.orders)} 张订单（期望 {len(plans)}）")


def test_scattered_inventory_forces_split() -> None:
    """余票被打散后，大单必须走自动分票且仍然全员出票。"""
    print("[组合] 碎片化余票下的多人单")
    store = reset_dev_store()
    before = store.set_remaining("二等座", 40)
    frag = before["fragmentation"]
    print(f"      碎片化：自由座位 {before['remaining']} 张，"
          f"最长连座 {frag['longest_run']}，"
          f"≥2 连座 {frag['runs_at_least_2']} 处，"
          f"≥3 连座 {frag['runs_at_least_3']} 处")
    check(frag["longest_run"] <= 3,
          f"碎片化确实生效（最长连座 {frag['longest_run']} <= 3）")
    profiles = make("adult", "adult", "child", "toddler")
    result = _book(store, profiles, "二等座", "SPLIT-1")
    check(result["ok"], "碎片化余票下仍然全员出票")
    check(result["order"]["seated"] == 4, "4 人全部出票")
    check(result["tier0"] == 0, "Tier 0 = 0")
    if result["order"]["split"]:
        check(bool(result["verdict"]["reason"]) or bool(result["order"]["rows"]),
              "自动分票时给出排布说明")
    # 大单在碎片化余票下也应出票（可能分票）
    big = make("adult", "adult", "adult", "adult", "adult", "adult", "adult", "adult")
    result = _book(store, big, "二等座", "SPLIT-8")
    check(result["tier0"] == 0, "8 人单在碎片化余票下 Tier 0 = 0")
    check(result["order"]["seated"] == 8,
          f"8 人单全部出票（{result['order']['seated']}）")


def test_sold_out_is_explained() -> None:
    """满座时不能静默失败：必须有可操作的原因。"""
    print("[组合] 满座时的说明")
    store = reset_dev_store()
    store.fill_to_ratio(1.0)
    check(store.remaining("二等座")["二等座"] == 0, "二等座已售罄")
    profiles = make("adult", "adult")
    result = _book(store, profiles, "二等座", "FULL-1")
    check(not result["ok"], "满座时不出票")
    check(bool(result["errors"]), f"给出原因：{result['errors']}")
    check(any("不足" in e or "无" in e for e in result["errors"]),
          "原因说明余票不足")


def test_wheelchair_bay_in_multi_person_order() -> None:
    """轮椅旅客混在多人单里，仍按独立编号拿停放位。"""
    print("[组合] 多人单里的轮椅停放位")
    store = reset_dev_store()
    # 轮椅 + 2 位照护 + 1 位儿童：停放位不占座位，其余 3 人坐座位
    profiles = make("wheelchair", "caregiver", "caregiver", "child")
    seats_before = store.remaining("二等座")["二等座"]
    result = _book(store, profiles, "二等座", "MIX-W")
    check(result["ok"], f"4 人单出票（{result['order']['seated']}）")
    check(result["tier0"] == 0, "Tier 0 = 0")
    wheel = result["order"]["passengers"][0]
    check(wheel["seat_id"] in store.bay_ids(),
          f"轮椅旅客拿到停放位编号（{wheel['seat_id']}）")
    # 只有非轮椅的 3 人占座位
    after = store.remaining("二等座")["二等座"]
    check(after == seats_before - 3,
          f"只扣 3 个座位（{seats_before} -> {after}），停放位不吃票额")


def test_bond_and_class_together() -> None:
    """硬绑定 + 席别约束同时成立（两个约束不能互相破坏）。"""
    print("[组合] 硬绑定与席别同时成立")
    for class_code in CLASSES:
        store = reset_dev_store()
        profiles = make("adult", "child", "infant")
        result = _book(store, profiles, class_code, f"BOTH-{class_code}")
        if not result["ok"]:
            continue
        items = result["order"]["passengers"]
        carriages = {items[0]["carriage"], items[1]["carriage"],
                     items[2]["carriage"]}
        check(len(carriages) == 1,
              f"{class_code}：全家同车厢（{carriages}）")
        wrong = [item["seat_id"] for item in items
                 if item["seat_id"] and not item["seat_id"].startswith("0")
                 and _seat_class(store, item["seat_id"]) != class_code]
        check(not wrong, f"{class_code}：席别正确（例外：{wrong or '无'}）")


def test_fragmentation_metric_uses_physical_columns() -> None:
    """碎片化指标必须按**物理列位**判断相邻。

    真实 bug：早期实现按 ``col_index`` 排序后只数"有几个自由座位"，
    没有比较下标是否真正相邻，于是"A 座 + D 座"（隔着过道）也被算成连座。
    实测把最长连座从真实的 2 报成 5 —— 而这个数正是"是否需要自动分票"
    的判定依据，报大了就会漏掉分票场景。

    这里构造两种**已知答案**的布局：
    ``A + D``（隔过道）必须是 1 连座；``A + B``（相邻）必须是 2 连座。
    """
    print("[组合] 碎片化指标按物理列位判相邻")
    from smartrail.ticketing.devstore import SOURCE_MANUAL, Occupancy

    def only_free(seat_ids: set[str]):
        """占掉所有二等座，只留 ``seat_ids`` 自由。"""
        store = reset_dev_store()
        for seat in store.formation.seats:
            if seat.class_code != "二等座" or seat.seat_id in seat_ids:
                continue
            store.occupied[seat.seat_id] = Occupancy(
                seat_id=seat.seat_id, source=SOURCE_MANUAL
            )
        return store

    # **必须共用同一份编组**：reset_dev_store() 每次都会新建 store，
    # 如果每个座位各查一次，得到的行号组合可能对不上（第一版就踩了这个）。
    # 取样排要挑**五座齐全的二等座排**：01 车是一等/商务座车，
    # A 座所在排根本没有 B 列，直接取第一个 A 会 StopIteration。
    reference = reset_dev_store()
    seats = list(reference.formation.seats)
    by_row: dict[tuple[int, int], dict[str, object]] = {}
    for seat in seats:
        if seat.class_code == "二等座":
            by_row.setdefault((seat.carriage, seat.row), {})[seat.col] = seat
    probe_row = next(
        columns for _key, columns in sorted(by_row.items())
        if set("ABCDF") <= set(columns)
    )
    probe = probe_row["A"]

    def seat_in_row(col: str):
        return probe_row[col]

    a_seat, b_seat, d_seat = seat_in_row("A"), seat_in_row("B"), seat_in_row("D")
    print(f"      取样排：{a_seat.carriage:02d} 车 {a_seat.row} 排"
          f"（A={a_seat.seat_id} B={b_seat.seat_id} D={d_seat.seat_id}）")

    # A + D：隔着过道，不是连座
    gap = only_free({a_seat.seat_id, d_seat.seat_id}).fragmentation("二等座")
    check(gap["rows_with_free_seats"] == 1, "只留 1 排有自由座位")
    check(gap["longest_run"] == 1,
          f"A+D（隔过道）不算连座，最长连座 = {gap['longest_run']}（应为 1）")
    check(gap["runs_at_least_2"] == 0,
          f"没有真正的 2 连座（{gap['runs_at_least_2']}）")

    # A + B：相邻，是 2 连座
    adjacent = only_free({a_seat.seat_id, b_seat.seat_id}).fragmentation("二等座")
    check(adjacent["longest_run"] == 2,
          f"A+B 相邻算 2 连座（{adjacent['longest_run']}）")
    check(adjacent["runs_at_least_2"] == 1,
          f"恰好 1 处 2 连座（{adjacent['runs_at_least_2']}）")
    check(adjacent["runs_at_least_3"] == 0, "没有 3 连座")


def test_pressure_test_scatters_seats() -> None:
    """压测占位必须打散，否则"凑不出连座"永远测不到。"""
    print("[组合] 压测占位打散")
    sequential = reset_dev_store()
    sequential.set_remaining("二等座", 100, from_front=True)
    scattered = reset_dev_store()
    scattered.set_remaining("二等座", 100)
    seq = sequential.fragmentation("二等座")
    sca = scattered.fragmentation("二等座")
    check(sequential.remaining("二等座")["二等座"] == 100
          and scattered.remaining("二等座")["二等座"] == 100,
          "两种方式余票数量一致（都是 100）")
    check(seq["longest_run"] >= 5,
          f"按顺序占留下 {seq['longest_run']} 连座（必然测不到分票）")
    check(sca["longest_run"] <= 2,
          f"打散后最长连座 {sca['longest_run']} <= 2（分票会被触发）")
    check(sca["rows_with_free_seats"] > seq["rows_with_free_seats"],
          f"打散后散布更广（{sca['rows_with_free_seats']} > "
          f"{seq['rows_with_free_seats']}）")
    check(sca["runs_at_least_3"] < seq["runs_at_least_3"],
          f"打散后 3 连座更少（{sca['runs_at_least_3']} < "
          f"{seq['runs_at_least_3']}）")
    # 可复现：同一 seed 得到同一布局
    again = reset_dev_store()
    again.set_remaining("二等座", 100)
    check(again.fragmentation("二等座") == sca, "同一种子结果可复现")


def main() -> int:
    tests = [
        test_combination_matrix,
        test_hard_bonds_share_carriage,
        test_multiple_orders_sequential,
        test_scattered_inventory_forces_split,
        test_sold_out_is_explained,
        test_wheelchair_bay_in_multi_person_order,
        test_bond_and_class_together,
        test_fragmentation_metric_uses_physical_columns,
        test_pressure_test_scatters_seats,
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
