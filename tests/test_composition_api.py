"""构成提交接口的验收：先校验、通过才求解、多订单隔离。

对应规格第六节的出票规则：校验不通过的订单**不予出票**，
而且不该被送进求解器（省掉无意义的计算）。

双模式：``python tests/test_composition_api.py`` 或 ``pytest``。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from smartrail.api import service  # noqa: E402
from smartrail.ticketing import get_dev_store, reset_dev_store  # noqa: E402

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


def comp(**base: int) -> dict:
    return {"base": {k: base.get(k, 0) for k in
                     ("adult", "youth", "child", "toddler", "infant")}}


def test_blocked_orders_are_not_submitted() -> None:
    """校验不通过的订单被拦下，不提交求解器。"""
    print("[构成接口] 校验拦截")
    result = service.submit_compositions(
        {
            "orders": [
                {**comp(infant=1), "note": "婴儿单独"},
                {**comp(adult=1, child=2), "note": "正常一家"},
                {**comp(youth=1), "note": "青少年单独"},
            ]
        }
    )
    summary = result["summary"]
    check(summary["orders"] == 3, f"收到 3 张订单（{summary['orders']}）")
    check(summary["blocked_orders"] == 1, f"拦截 1 张（{summary['blocked_orders']}）")
    check(summary["submitted_orders"] == 2, f"提交 2 张（{summary['submitted_orders']}）")
    check([item["order_id"] for item in result["blocked"]] == ["COMP-1"],
          "被拦下的是婴儿单独那张")
    check("未满14周岁" in result["blocked"][0]["errors"][0],
          f"拦截原因正确（{result['blocked'][0]['errors'][0]}）")
    for item in result["orders"]:
        print(f"        {item['order_id']:<8} {item['level_label']:<24} "
              f"总人数={item['total_passengers']} 出票={len(item['seats'])}")
    ok_order = [i for i in result["orders"] if i["order_id"] == "COMP-2"][0]
    check(len(ok_order["seats"]) == 3, f"通过校验的 3 人全部出票（{len(ok_order['seats'])}）")
    check(ok_order["level"] == "fulfilled", f"档位 {ok_order['level']}")


def test_severe_disability_gate() -> None:
    """重度残疾未约重点旅客 -> 拦截；补上后放行并出票。"""
    print("[构成接口] 重度残疾闭环")
    severe = {
        "base": {"adult": 1, "child": 1},
        "disability": {"severe": {"child": 1}},
        "key_passenger_service": False,
    }
    result = service.submit_compositions({"orders": [severe]})
    check(result["summary"]["blocked_orders"] == 1, "未约重点旅客 -> 拦截")
    check("重点旅客" in result["blocked"][0]["errors"][0],
          f"原因正确（{result['blocked'][0]['errors'][0]}）")

    severe_ok = dict(severe)
    severe_ok["key_passenger_service"] = True
    result = service.submit_compositions({"orders": [severe_ok]})
    check(result["summary"]["blocked_orders"] == 0, "补上重点旅客服务 -> 通过")
    check(len(result["orders"][0]["seats"]) == 2,
          f"2 人出票（{len(result['orders'][0]['seats'])}）")


def test_term_pregnancy_gate() -> None:
    """足月孕妇未约重点旅客 -> 拦截；补上后放行。"""
    print("[构成接口] 足月孕妇闭环")
    term = {
        "base": {"adult": 2},
        "pregnant": {"term": {"adult": 1}},
        "key_passenger_service": False,
    }
    result = service.submit_compositions({"orders": [term]})
    check(result["summary"]["blocked_orders"] == 1, "未约重点旅客 -> 拦截")
    term["key_passenger_service"] = True
    result = service.submit_compositions({"orders": [term]})
    check(result["summary"]["blocked_orders"] == 0, "补上重点旅客服务 -> 通过")


def test_multi_order_isolation() -> None:
    """多订单数据隔离：A 单的残疾数据不串到 B 单。"""
    print("[构成接口] 多订单数据隔离")
    # 订单号现在按**台账已有单数**续号（否则每次都是 COMP-1，
    # 记录无法区分）。所以这里先重置，编号才是确定的 COMP-1/COMP-2。
    reset_dev_store()
    first = {**comp(adult=2), "note": "A 单"}
    first["disability"] = {"severe": {"adult": 1}}
    first["key_passenger_service"] = True
    second = {**comp(adult=1, child=1), "note": "B 单"}
    result = service.submit_compositions({"orders": [first, second]})
    check(result["summary"]["blocked_orders"] == 0, "两张订单各自独立通过")
    by_id = {item["order_id"]: item for item in result["orders"]}
    check(len(by_id) == 2, f"两张订单编号互不相同（{sorted(by_id)}）")
    ids = sorted(by_id)
    check(by_id[ids[0]]["check"]["total_passengers"] == 2, "A 单总人数 2")
    check(by_id[ids[1]]["check"]["total_passengers"] == 2, "B 单总人数 2")
    check(
        by_id[ids[0]]["info"]["disability"]["severe"]["adult"] == 1
        and by_id[ids[1]]["info"]["disability"]["severe"]["adult"] == 0,
        "残疾数据未串单",
    )
    check(by_id[ids[1]]["check"]["healthy_adults"] == 1,
          "B 单可用健康成人 1（未被 A 单影响）")
    # 提交的订单必须被记进台账（需求："开发者提交的订单难道不用保留吗"）
    store = get_dev_store()
    check(len(store.orders) == 2,
          f"两张单都记进台账（{len(store.orders)}）")
    check([o["order_id"] for o in store.orders] == ids,
          "台账里的订单号与返回一致（编号唯一，不再重复）")
    check(all(o["source"] == "dev-composition" for o in store.orders),
          "台账标出来源是开发者组单")
    check(store.orders[0]["base_desc"] == "adult×2",
          f"台账记录了基础分组（{store.orders[0]['base_desc']}）")


def test_schema_returned() -> None:
    """返回 schema 供前端渲染，且标明哪些维度不计入总人数。"""
    print("[构成接口] schema")
    result = service.submit_compositions({"orders": [{**comp(adult=1)}]})
    check("schema" in result and result["schema"]["base_groups"], "返回基础分组定义")
    check(len(result["schema"]["base_groups"]) == 5,
          f"基础分组 5 档（{len(result['schema']['base_groups'])}）")
    check(result["schema"]["excluded_from_total"] == ["child_sub", "disability", "pregnant"],
          "schema 标明哪些维度不计入总人数")


def test_class_code_reaches_the_solver() -> None:
    """构成组单的**席别必须一路带到 Order**，否则会"买二等座出一等座"。

    真实事故：开发者页选"二等座"提交 2 成人 2 儿童，
    结果发的是 ``01车01A/C/D/F`` —— **一等座**。

    根因有两层，都在"重建对象时漏字段"：
      1. ``OrderComposition`` 没有 ``class_code`` 字段（先补上）；
      2. ``orders_from_design`` 里 ``default_bond`` 覆盖时**重建了 Order**
         却没带 ``class_code``（与 ``engine._prepare_order`` 漏字段同一类错误）。

    这里同时守住"写在订单上"与"写在请求顶层"两种写法，
    以及"空席别 = 不限"这条向后兼容语义。
    """
    print("[构成接口] 席别是硬约束")
    from smartrail.ticketing import reset_dev_store as _reset

    expectations = {
        "二等座": "二等座",
        "一等座": "一等座",
        "商务座": "商务座",
    }
    for want, expect in expectations.items():
        _reset()
        payload = {**comp(adult=2, child=2), "class_code": want}
        result = service.submit_compositions({"orders": [payload]})
        order = result["orders"][0]
        seats = sorted((order.get("seats") or {}).values())
        check(bool(seats), f"{want}：确实出票了（{len(seats)} 座）")
        # 座位号首两位是车厢号，用编组反查席别
        from smartrail.carriage import g25_16_car_formation

        formation = g25_16_car_formation()
        got = {formation.seat(seat_id).class_code
               for seat_id in seats if formation.seat(seat_id)}
        check(got == {expect},
              f"下单 {want} -> 实际席别 {sorted(got)}（{seats}）")

    # 顶层 class_code 也生效（批量提交接口的写法）
    _reset()
    payload = {**comp(adult=1)}
    result = service.submit_compositions(
        {"orders": [payload], "class_code": "一等座"})
    order = result["orders"][0]
    got = {s[-1] for s in (order.get("seats") or {}).values()}
    check(bool(got), "顶层席别也出票了")
    from smartrail.carriage import g25_16_car_formation

    formation = g25_16_car_formation()
    classes = {formation.seat(seat_id).class_code
               for seat_id in (order.get("seats") or {}).values()}
    check(classes == {"一等座"},
          f"顶层 class_code 生效（{sorted(classes)}）")

    # 空席别 = 不限（压测/仿真场景的向后兼容语义）
    _reset()
    payload = {**comp(adult=1), "class_code": ""}
    result = service.submit_compositions({"orders": [payload]})
    check(bool(result["orders"][0].get("seats")),
          "空席别表示不限，仍能出票（未把过滤写成必然失败）")

    # default_bond 覆盖路径不能丢 class_code
    _reset()
    result = service.submit_compositions({
        "orders": [{**comp(adult=2), "class_code": "二等座"}],
        "same_order_bond": "soft",
    })
    classes = {formation.seat(seat_id).class_code
               for seat_id in (result["orders"][0].get("seats") or {}).values()}
    check(classes == {"二等座"} or not classes,
          f"same_order_bond 覆盖后席别仍生效（{sorted(classes)}）")


def test_composition_respects_ledger_inventory() -> None:
    """构成组单必须看到**台账的真实库存**，不能超卖。

    真实事故（用户报"这里出票怎么不受有没有座位影响？"）：
    ``submit_orders`` 无条件 ``reset_engine()`` 建**空车**引擎，
    于是这条路径完全看不到开发者页设定的余票与已售座位。实测：

    ==================  ====================================
    二等座余票 172      4 人单「全部出票」（应为需现场处理）
    二等座余票 **2**    4 人单**仍然全部出票，发了 4 张票** ← 超卖 2 张
    组单后余票          1152 -> 1152（出了票却不扣减）
    ==================  ====================================

    现在改成用台账引擎，并把出票结果写回台账。
    """
    print("[构成接口] 库存约束（不得超卖）")
    from smartrail.ticketing import reset_dev_store as _reset

    # ① 余票充足：出票成功，且出票后余票减少
    _reset()
    before = len(_snapshot_seats())
    payload = {**comp(adult=2, child=2), "class_code": "二等座"}
    result = service.submit_compositions({"orders": [payload]})
    order = result["orders"][0]
    seats = sorted((order.get("seats") or {}).values())
    check(len(seats) == 4, f"余票充足时 4 人全部出票（{len(seats)}）")
    check(len(_snapshot_seats()) == before - 4,
          f"出票后台账少了 4 个空位（{before} -> {len(_snapshot_seats())}）")

    # ② 余票不足：**不得超卖**
    _reset()
    store = get_dev_store()
    store.set_remaining("二等座", 2)
    payload = {**comp(adult=2, child=2), "class_code": "二等座"}
    result = service.submit_compositions({"orders": [payload]})
    order = result["orders"][0]
    seats = sorted((order.get("seats") or {}).values())
    check(len(seats) <= 2,
          f"余票 2 时最多出 2 座（实际 {len(seats)}：{seats}）")
    check(store.remaining("二等座")["二等座"] >= 0,
          f"余票不为负（{store.remaining('二等座')['二等座']}）")

    # ③ 余票为 0：不出票，且给出原因（不是静默失败）
    _reset()
    # **必须重新取 store**：_reset() 换的是全局单例，手里的旧引用
    # 指向已废弃的实例（踩过：写漏这一行，測试拿旧台账、结果假失败）。
    store = get_dev_store()
    store.set_remaining("二等座", 0)
    check(store.remaining("二等座")["二等座"] == 0,
          f"余票已置 0（{store.remaining('二等座')['二等座']}）")
    payload = {**comp(adult=1), "class_code": "二等座"}
    result = service.submit_compositions({"orders": [payload]})
    order = result["orders"][0]
    check(not (order.get("seats") or {}),
          f"余票 0 时不发座（{order.get('seats')}）")
    check(order.get("level_label"),
          f"给出分档说明（{order.get('level_label')}）")


def _snapshot_seats() -> list[str]:
    """台账里还空着的二等座（用于核对余票增减）。"""
    store = get_dev_store()
    return [seat.seat_id for seat in store.formation.seats
            if seat.class_code == "二等座" and not store.is_occupied(seat.seat_id)]


def test_same_order_sits_in_one_row_when_possible() -> None:
    """余票够时，同一订单必须坐在**同一排**（不是仅仅同车厢）。

    真实事故（用户报"你把大人小孩分开了"）：
    2 成人 + 2 儿童是一个 mandatory 单元，却拿到

        06车01A / 06车01B  +  06车04D / 06车04F

    ——每对"成人+儿童"挨着，但**两对之间隔了三排**，一眼就能看出被拆开。

    根因是**候选池的排序**：``tables.candidates()`` 按"个体分之和"排序，
    而个体分**完全不反映乘客之间的距离**。分支限界只评估前 N 个候选，
    于是"同一排 4 座"这种真正想要的方案排在"跨 3 排的散座"之后，
    从来没被评估过 —— 算法只能在众多"跨排"方案里挑一个。

    修正：候选按"占排数升序、个体分降序"重排后再取前 N 个，
    保证"坐在一起"的方案一定进入评估。

    实测（余票 300，连续 12 单 2 成人 2 儿童）：同排率 0% -> 67%。
    """
    print("[构成接口] 同订单应坐同一排")
    from smartrail.ticketing import reset_dev_store as _reset

    # ① 空车：必然同排
    _reset()
    payload = {**comp(adult=2, child=2), "class_code": "二等座"}
    result = service.submit_compositions({"orders": [payload]})
    seats = sorted((result["orders"][0].get("seats") or {}).values())
    check(len(seats) == 4, f"空车 4 人出票（{len(seats)}）")
    check(len({s[:2] + "车" + s[3:5] for s in seats}) == 1,
          f"空车 4 人同一排（{seats}）")

    # ② 余票 300（碎片化但仍有若干整排）：同排率必须显著高于"随机"
    _reset()
    store = get_dev_store()
    store.set_remaining("二等座", 300)
    trials = 12
    same_row = 0
    for _ in range(trials):
        result = service.submit_compositions({"orders": [dict(payload)]})
        got = (result["orders"][0].get("seats") or {}).values()
        if len({s[:2] + "车" + s[3:5] for s in got}) == 1:
            same_row += 1
    check(same_row >= trials * 0.5,
          f"余票 300 时同排率 {same_row}/{trials}"
          f"（修复前为 0/{trials}，要求 ≥ {trials // 2}）")

    # ③ 反证：库存确实很紧时，跨排是允许的 —— 但必须**明示**
    #    （不能默默把一家人拆开还说"全部出票"）
    _reset()
    store.set_remaining("二等座", 40)
    result = service.submit_compositions({"orders": [dict(payload)]})
    order = result["orders"][0]
    seats = sorted((order.get("seats") or {}).values())
    rows = {s[:2] + "车" + s[3:5] for s in seats}
    if len(rows) > 1:
        check(bool(order.get("level_label")),
              f"跨排时给出分档说明（{order.get('level_label')}）")
    else:
        check(True, "余票 40 时仍坐同一排（更好）")


def main() -> int:
    tests = [
        test_blocked_orders_are_not_submitted,
        test_severe_disability_gate,
        test_term_pregnancy_gate,
        test_multi_order_isolation,
        test_class_code_reaches_the_solver,
        test_composition_respects_ledger_inventory,
        test_same_order_sits_in_one_row_when_possible,
        test_schema_returned,
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
