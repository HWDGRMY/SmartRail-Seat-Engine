"""页面级验收：OrderEditor 的构成接口在真实 HTTP 上工作。"""

from __future__ import annotations

import json
import sys
import threading
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from smartrail.api.stdlib_server import serve  # noqa: E402

httpd = serve("127.0.0.1", 0)
port = httpd.server_address[1]
threading.Thread(target=httpd.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{port}"
failures: list[str] = []


def check(ok: bool, message: str) -> None:
    print(("  PASS  " if ok else "  FAIL  ") + message)
    if not ok:
        failures.append(message)


def post(path: str, payload: dict):
    request = urllib.request.Request(
        base + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=600) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def get(path: str):
    with urllib.request.urlopen(base + path, timeout=60) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


try:
    print("=== 1) schema 接口 ===")
    status, schema = get("/api/composition/schema")
    check(status == 200, "GET /api/composition/schema 可用")
    check(len(schema["base_groups"]) == 5,
          f"基础分组 5 档（{len(schema['base_groups'])}）")
    check([g["id"] for g in schema["base_groups"]]
          == ["adult", "youth", "child", "toddler", "infant"],
          "基础分组顺序与原规格一致")
    check(len(schema["child_sub_groups"]) == 2,
          f"儿童细分 2 档（安静/吵闹）")
    check(len(schema["disability_levels"]) == 4,
          f"残疾程度 4 档（{len(schema['disability_levels'])}）")
    check(len(schema["pregnant_stages"]) == 4,
          f"孕妇孕期 4 档（{len(schema['pregnant_stages'])}）")
    check(schema["total_formula"] == "adult + youth + child + toddler + infant",
          "总人数公式与规格一致")
    check(schema["excluded_from_total"] == ["child_sub", "disability", "pregnant"],
          "标明三个不计入总人数的维度")
    check(len(schema["age_bands"]) == 5, "年龄段 5 档")

    print()
    print("=== 2) 实时校验接口（不求解）===")
    status, checked = post(
        "/api/composition/check",
        {
            "orders": [
                {"base": {"adult": 1, "child": 1}},
                {"base": {"infant": 1}},
                {"base": {"youth": 1}},
            ]
        },
    )
    check(status == 200, "POST /api/composition/check 可用")
    check(checked["blocked_count"] == 1,
          f"拦下 1 张（{checked['blocked_count']}）")
    check(checked["orders"][0]["ok"], "成人+儿童 -> 通过")
    check(not checked["orders"][1]["ok"], "婴儿单独 -> 不通过")
    check(checked["orders"][2]["ok"], "青少年单独 -> 通过")
    check(checked["orders"][1]["total_passengers"] == 1,
          "校验返回总人数（供前端展示）")

    print()
    print("=== 3) 构成提交：先校验、通过才求解 ===")
    status, result = post(
        "/api/composition/submit",
        {
            "orders": [
                {"note": "正常一家",
                 "base": {"adult": 2, "child": 1},
                 "child_sub": {"child_quiet": 1}},
                {"note": "婴儿单独（应拒票）", "base": {"infant": 1}},
                {"note": "重度残疾未约重点旅客（应拒票）",
                 "base": {"adult": 1, "child": 1},
                 "disability": {"severe": {"child": 1}}},
                {"note": "重度残疾已约重点旅客",
                 "base": {"adult": 2, "child": 1},
                 "disability": {"severe": {"child": 1}},
                 "key_passenger_service": True},
            ]
        },
    )
    check(status == 200, "POST /api/composition/submit 可用")
    summary = result["summary"]
    check(summary["orders"] == 4, f"收到 4 张订单（{summary['orders']}）")
    check(summary["blocked_orders"] == 2, f"拦下 2 张（{summary['blocked_orders']}）")
    check(summary["submitted_orders"] == 2, f"提交 2 张（{summary['submitted_orders']}）")
    blocked_ids = [item["order_id"] for item in result["blocked"]]
    check(blocked_ids == ["COMP-2", "COMP-3"], f"拦下的是预期两张（{blocked_ids}）")
    check(summary["tier0_violations"] == 0,
          f"Tier 0 为 0（{summary['tier0_violations']}）")
    ok_orders = [o for o in result["orders"] if not o["blocked"]]
    check(all(o["level"] == "fulfilled" for o in ok_orders),
          "通过校验的订单全部出票")
    check(sum(o["total_passengers"] for o in ok_orders) == 6,
          f"通过的订单合计 6 人（3+3，实际 "
          f"{sum(o['total_passengers'] for o in ok_orders)}）")

    print()
    print("=== 4) 总人数只由基础分组求和 ===")
    # 注意：这一单的儿童细分必须是 1 人（儿童只有 1 位）。
    # 早期版本这里写了 child_quiet=1 + child_noisy=1，被
    # 「细分合计不得超过儿童人数」正确拦下 —— 是校验对，不是用例对。
    status, result = post(
        "/api/composition/submit",
        {
            "orders": [{
                "base": {"adult": 2, "youth": 1, "child": 1, "toddler": 1, "infant": 1},
                "child_sub": {"child_quiet": 1},
                "disability": {"mild": {"adult": 1}, "severe": {"child": 1}},
                "pregnant": {"early": {"adult": 1}},
                "key_passenger_service": True,
            }]
        },
    )
    order = result["orders"][0]
    check(order["total_passengers"] == 6,
          f"总人数 = 2+1+1+1+1 = {order['total_passengers']}（细分/残疾/孕妇不计入）")
    check(len(order["seats"]) == 6, f"出票 6 人（{len(order['seats'])}）")

    print()
    print("=== 4b) 细分超过儿童人数时必须被拦下 ===")
    status, over = post(
        "/api/composition/check",
        {
            "orders": [{
                "base": {"adult": 1, "child": 1},
                "child_sub": {"child_quiet": 1, "child_noisy": 1},
            }]
        },
    )
    check(not over["orders"][0]["ok"], "儿童1 却有 2 个行为标签 -> 不通过")
    check(any("行为细分" in e for e in over["orders"][0]["errors"]),
          "原因指向行为细分")

    print()
    print("=== 5) 多订单数据隔离 ===")
    status, result = post(
        "/api/composition/check",
        {
            "orders": [
                {"base": {"adult": 2}, "disability": {"severe": {"adult": 1}},
                 "key_passenger_service": True},
                {"base": {"adult": 1, "child": 1}},
            ]
        },
    )
    first, second = result["orders"]
    check(first["total_passengers"] == 2 and second["total_passengers"] == 2,
          "两张订单各自独立计数")
    check(second["healthy_adults"] == 1,
          f"第二张的可用健康成人为 1（未被第一张影响）")

    print()
    print("=== 6) 页面引用了构成接口 ===")
    with urllib.request.urlopen(base + "/booking", timeout=30) as response:
        html = response.read().decode("utf-8")
    for token, label in (
        ("/api/composition/schema", "schema 接口"),
        ("/api/composition/check", "校验接口"),
        ("/api/composition/submit", "提交接口"),
        ("base-group", "BaseGroup"),
        ("disability-group", "DisabilityGroup"),
        ("pregnant-group", "PregnantGroup"),
        ("order-switcher", "OrderSwitcher"),
    ):
        check(token in html, f"页面包含 {label}")
finally:
    httpd.shutdown()
    httpd.server_close()

print()
if failures:
    print(f"失败 {len(failures)} 项：")
    for item in failures:
        print("  -", item)
    raise SystemExit(1)
print("OrderEditor 页面级验收全部通过。")
