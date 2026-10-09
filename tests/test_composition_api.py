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


def main() -> int:
    tests = [
        test_blocked_orders_are_not_submitted,
        test_severe_disability_gate,
        test_term_pregnancy_gate,
        test_multi_order_isolation,
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
