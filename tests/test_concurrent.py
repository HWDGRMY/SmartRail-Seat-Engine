"""并发下单模拟的契约测试。

验收场景：用户勾选"任意数量 × 任意人群类型"，点一次「同时下单」，
所有订单一次性压给引擎，结果要能直接标在座位图上。

本套件守护四件事：
1. 人群类型清单**覆盖全部支持需求**（否则"看起来测了很多、其实没测到重点旅客"）；
2. 并发结果里的**逐单可着色**（页面按订单上色的前提）；
3. **同订单同车厢**（"没人跟陌生人拼单买票"这条产品常识）；
4. Tier 0 为 0（并发压力下底线不破）。
"""

from __future__ import annotations

import json
import os
import sys
import threading
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from smartrail.api.stdlib_server import serve  # noqa: E402

UNDER_PYTEST = "PYTEST_CURRENT_TEST" in os.environ or "pytest" in sys.modules
_FAILURES: list[str] = []
PAGE = ROOT / "smartrail" / "web" / "booking.html"


def check(condition: bool, message: str) -> None:
    if condition:
        print(f"  PASS  {message}")
        return
    print(f"  FAIL  {message}")
    if UNDER_PYTEST:
        raise AssertionError(message)
    _FAILURES.append(message)


def test_preset_library_covers_every_support_need() -> None:
    """人群类型必须覆盖全部支持需求 —— 这是"全覆盖"的唯一证据。"""
    print("[并发] 人群类型覆盖全部支持需求")
    from smartrail.models import SupportNeed
    from smartrail.v3.presets import PRESETS, build_order, coverage_report

    check(len(PRESETS) >= 12, f"人群类型足够丰富（{len(PRESETS)} 种）")
    orders = [build_order(preset, f"O-{i}", index=i) for i, preset in enumerate(PRESETS)]
    report = coverage_report(orders)
    check(
        not report["missing_support_needs"],
        f"全部支持需求都被覆盖（缺失 {report['missing_support_needs']}）",
    )
    check(
        set(report["support_needs"]) == {need.value for need in SupportNeed},
        f"覆盖集合与枚举一致（{len(report['support_needs'])} 项）",
    )
    check(
        report["bond_levels"]["mandatory"] > 0 and report["bond_levels"]["strong"] > 0,
        f"硬绑定与强绑定都有样例（{report['bond_levels']}）",
    )
    check("child" in report["ticket_types"], "覆盖儿童票")


def test_same_order_default_bond_is_strong() -> None:
    """同订单默认同座：未声明关系的人默认按同行人处理。"""
    print("[并发] 同订单默认同座")
    from smartrail.api import service
    from smartrail.models import BondType

    payload = {
        "order_id": "O-DEFAULT",
        "passengers": [
            {"passenger_id": "D1", "age": 30},
            {"passenger_id": "D2", "age": 31},
        ],
    }
    order = service.order_from_payload(payload)
    check(
        order.bond_of("D1", "D2") is BondType.STRONG,
        f"未声明关系默认 STRONG（实际 {order.bond_of('D1', 'D2')}）",
    )
    relaxed = service.order_from_payload({**payload, "same_order_bond": "soft"})
    check(
        relaxed.bond_of("D1", "D2") is BondType.SOFT,
        "可显式取消（公司代订、多人各自出差）",
    )
    explicit = service.order_from_payload(
        {**payload, "bonds": [{"a": "D1", "b": "D2", "bond": "mandatory"}]}
    )
    check(
        explicit.bond_of("D1", "D2") is BondType.MANDATORY,
        "显式声明优先于默认",
    )


def test_concurrent_api_contract() -> None:
    """并发接口契约：清单、汇总、覆盖、座位快照、可着色。"""
    print("[并发] 接口契约")
    httpd = serve("127.0.0.1", 0)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"

    def post(path: str, payload: dict):
        request = urllib.request.Request(
            base + path,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=300) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    try:
        status, single = post("/api/simulate/concurrent", {"presets": {"solo": 1}})
        check(status == 200, "接口可用")
        check(bool(single.get("catalog")), "返回人群类型清单")
        check("summary" in single and "coverage" in single, "返回汇总与覆盖报告")
        check(bool(single["train"].get("seats")), "返回座位快照（供按订单着色）")

        status, full = post("/api/simulate/concurrent", {"presets": {}})
        summary = full["summary"]
        check(summary["orders"] >= 15, f"全人群订单数（{summary['orders']}）")
        check(
            summary["tier0_violations"] == 0,
            f"Tier 0 为 0（实际 {summary['tier0_violations']}）",
        )
        check(
            summary["orders_same_carriage"] == summary["multi_passenger_orders"],
            f"同订单同车厢 {summary['orders_same_carriage']}/{summary['multi_passenger_orders']}",
        )
        check(
            summary["seat_rate"] >= 0.9,
            f"就座率不低于 90%（{summary['seat_rate']:.3f}）",
        )

        status, combo = post(
            "/api/simulate/concurrent",
            {"presets": {"family_child": 3, "wheelchair": 2, "group6": 1}},
        )
        check(combo["summary"]["orders"] == 6, f"任意数量组合生效（{combo['summary']['orders']}）")
        check(
            all(isinstance(o.get("color_index"), int) for o in combo["orders"]),
            "每张订单都有颜色索引",
        )
        check(
            all("seats" in o and "carriages" in o for o in combo["orders"]),
            "每张订单都有座位与车厢信息",
        )
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_modular_archetypes_and_auto_grouping() -> None:
    """模块化乘客类型 + 自动组单：验收时按"人"自由组合的核心保障。

    三条不变量（每一条都对应一个真实缺陷）：

    1. **人群全覆盖** —— 全部 7 项支持需求都能被勾选到；
    2. **被照护者不孤身** —— 儿童/婴儿/轮椅旅客必须与照护人在同一张订单里。
       早期版本生成过"儿童（6 岁）1 人订单"，照护关系完全没测到；
    3. **零散旅客不拼陌生人** —— 默认一人一单，可用 ``group_friends`` 显式拼团。
    """
    print("[并发] 模块化乘客类型与自动组单")
    from smartrail.models import SupportNeed
    from smartrail.v3.archetypes import (
        archetype_catalog,
        build_orders_from_archetypes,
        coverage_of,
    )

    catalog = archetype_catalog()
    check(len(catalog) >= 15, f"乘客类型足够丰富（{len(catalog)} 种）")
    covered = {need for item in catalog for need in item["support_needs"]}
    check(
        covered == {need.value for need in SupportNeed},
        f"类型清单覆盖全部支持需求（缺 {sorted({n.value for n in SupportNeed} - covered)}）",
    )
    for role in ("adult", "child", "vulnerable", "caregiver"):
        check(any(item["role"] == role for item in catalog), f"包含角色：{role}")

    # 全部类型各 1 位：必须全覆盖，且没有孤身的被照护者
    counts = {item["key"]: 1 for item in catalog}
    built = build_orders_from_archetypes(counts)
    report = coverage_of([item.order for item in built])
    check(
        not report["missing_support_needs"],
        f"全类型覆盖无缺失（缺 {report['missing_support_needs']}）",
    )
    lone = [
        item for item in built
        if item.size == 1 and item.order.passengers[0].needs_caregiver
    ]
    check(not lone, f"没有被照护者孤身成单（实际 {len(lone)} 张）")
    check(
        report["bond_levels"]["mandatory"] >= 6,
        f"生成足够的硬绑定（{report['bond_levels']['mandatory']} 条）",
    )

    # 零散旅客默认不拼陌生人
    solo = build_orders_from_archetypes({"adult": 9})
    check(len(solo) == 9, f"9 位成人默认 9 张订单（实际 {len(solo)}）")
    friends = build_orders_from_archetypes({"adult": 9}, group_friends=True)
    check(
        [item.size for item in friends] == [4, 4, 1],
        f"显式拼团按 4 人一组（实际 {[item.size for item in friends]}）",
    )


def test_modular_api_contract() -> None:
    """模块化接口：任意数量 × 任意类型，空车必须全员出票。"""
    print("[并发] 模块化接口契约")
    from smartrail.api import service

    result = service.concurrent_simulation(
        {"archetypes": {"adult": 6, "wheelchair": 2, "infant": 1, "child": 2}, "seed": 7}
    )
    summary = result["summary"]
    check(summary["seated_passengers"] == summary["requested_passengers"],
          f"空车全员出票（{summary['seated_passengers']}/{summary['requested_passengers']}）")
    check(summary["tier0_violations"] == 0, f"Tier 0 为 0（{summary['tier0_violations']}）")
    check(
        summary["orders_same_carriage"] == summary["multi_passenger_orders"],
        f"同订单同车厢 {summary['orders_same_carriage']}/{summary['multi_passenger_orders']}",
    )
    check(
        all("passengers" in order for order in result["orders"]),
        "逐单结果带乘客类型标签（页面用来显示『这张单里都是谁』）",
    )
    check(bool(result.get("input", {}).get("archetypes")), "回显输入便于复现")


def test_manual_order_submission() -> None:
    """**手工构造订单**：任意张数 × 每单任意人数与类型，未满足的必须给提示。

    这是验收的核心诉求，守护四条不变量：

    1. 任意张数、每单任意人数的订单都能提交并被逐单求解；
    2. 结果分四档（完全满足 / 部分满足 / 需现场处理 / 无法满足），不是笼统成败；
    3. **无法满足的订单必须给出可操作的原因**（例如"专区仅剩 0 个空位"）；
    4. Tier 0 在批量提交下依然为 0。
    """
    print("[提交] 手工构造订单与未满足提示")
    from smartrail.api import service

    # 1) 任意张数 × 任意人数
    status = service.submit_orders(
        {
            "orders": [
                {"order_id": "S1", "passengers": [{"key": "adult"}]},
                {"order_id": "S2", "passengers": [{"key": "adult"}, {"key": "child"}]},
                {"order_id": "S3", "passengers": [{"key": "wheelchair"}, {"key": "caregiver"}]},
                {"order_id": "S4", "passengers": [{"key": "adult"}] * 5},
            ]
        }
    )
    summary = status["summary"]
    check(summary["orders"] == 4, f"接受 4 张订单（{summary['orders']}）")
    check(
        summary["requested_passengers"] == 10,
        f"接受 10 位乘客（1+2+2+5，实际 {summary['requested_passengers']}）",
    )
    check(summary["seated_passengers"] == 10, "空车全部出票")
    check(summary["tier0_violations"] == 0, f"Tier 0 为 0（{summary['tier0_violations']}）")
    check(
        all("level" in item and "reasons" in item for item in status["orders"]),
        "逐单返回分档与原因",
    )

    # 2) **出票优先**：特殊席位不够时照常出票 + 生成提示/询问，而不是拒票。
    #    这是产品的第一原则 —— 拒票（候补）只应发生在"全车确实没有空座"时。
    orders = [
        {
            "order_id": f"FILL-{n}",
            "passengers": [{"key": "wheelchair"}, {"key": "caregiver"}],
        }
        for n in range(1, 8)
    ]
    orders.append(
        {"order_id": "LATE", "passengers": [{"key": "wheelchair"}, {"key": "caregiver"}]}
    )
    overflow = service.submit_orders({"orders": orders})
    summary = overflow["summary"]
    check(
        summary["seated_passengers"] == summary["requested_passengers"],
        f"专区售罄后仍然全员出票"
        f"（{summary['seated_passengers']}/{summary['requested_passengers']}）",
    )
    check(summary["waitlisted_passengers"] == 0, "没有因专区售罄而候补")
    check(summary["impossible_orders"] == 0, "没有订单被判『无座可发』")
    late = [item for item in overflow["orders"] if item["order_id"] == "LATE"][0]
    check(late["seated"] == late["requested"], "溢出的轮椅订单也出票了")
    check(
        late["level"] == "action_required",
        f"分档为『已出票，需现场处理』（{late['level']}）",
    )
    notices = " ".join(n["message"] for n in late["notices"])
    check("无障碍专区已满" in notices, f"生成了站车协助提示（{notices[:40]}…）")
    check(
        any(item["order_id"] == "LATE" for item in overflow["confirmations"]),
        "同时给出需要用户确认的问题（提示 / 询问后出票）",
    )

    # 3) 单张订单轮椅人数超建议上限：仍然出票，但要询问
    quota = service.submit_orders(
        {"orders": [{"order_id": "W8", "passengers": [{"key": "wheelchair"}] * 8}]}
    )
    wide = quota["orders"][0]
    check(wide["seated"] == 8, f"8 位轮椅一单仍然全部出票（{wide['seated']}/8）")
    check(bool(wide["question"]), "给出了需要用户确认的问题")
    check(
        wide["feasibility"]["code"] in ("WHEELCHAIR_OVER_QUOTA", "NO_WHEELCHAIR_SLOT"),
        f"原因码正确（{wide['feasibility']['code']}）",
    )
    check(
        quota["summary"]["tier0_violations"] == 0,
        f"Tier 0 仍为 0（{quota['summary']['tier0_violations']}）",
    )

    # 4) 正常单不应产生多余的询问
    normal = service.submit_orders(
        {"orders": [{"order_id": "NORMAL", "passengers": [{"key": "adult"}]}]}
    )
    check(normal["orders"][0]["level"] == "fulfilled", "正常单为『全部出票』")
    check(not normal["confirmations"], "正常单不产生确认询问")


def test_page_has_concurrent_ui() -> None:
    """页面必须有批量订单构造与未满足提示。"""
    print("[提交] 页面交互区")
    text = PAGE.read_text(encoding="utf-8")
    for token, label in (
        ("批量提交订单", "批量提交区域"),
        ("/api/orders/submit", "调用提交接口"),
        ("/api/orders/types", "调用类型清单接口"),
        ('id="palette"', "乘客类型面板"),
        ("id=\"orderList\"", "订单列表"),
        ("btnAddOrder", "『新建订单』按钮"),
        ("btnSubmitOrders", "『提交全部订单』按钮"),
        ("renderUnmet", "未满足提示渲染"),
        ("impossible", "『无法满足』分档"),
        ("ORDER_COLORS", "订单颜色表"),
        ("renderConcurrentSeatMap", "座位图按订单着色"),
    ):
        check(token in text, label)


def main() -> int:
    tests = [
        test_preset_library_covers_every_support_need,
        test_modular_archetypes_and_auto_grouping,
        test_modular_api_contract,
        test_manual_order_submission,
        test_same_order_default_bond_is_strong,
        test_concurrent_api_contract,
        test_page_has_concurrent_ui,
    ]
    passed = 0
    for test in tests:
        if UNDER_PYTEST:
            test()
            passed += 1
            continue
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
            print(f"  - {item}")
        return 1
    print(f"全部 {passed} 组断言通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
