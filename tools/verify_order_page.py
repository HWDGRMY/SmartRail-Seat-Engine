"""页面级验收：批量订单提交的接口、页面结构、以及"无法满足"提示闭环。

用法::

    python tools/verify_order_page.py

它**自带服务**（在随机端口起一个真实 HTTP 服务），不依赖你手动启动后端，
因此可以在任何环境里一条命令跑完页面级验收。
``tests/test_concurrent.py`` 覆盖的是同一批契约的单元级版本，两者互补：
单元测试保证逻辑对，本脚本保证**真实 HTTP + 真实 HTML** 也对。
"""

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


def post(path, payload):
    request = urllib.request.Request(
        base + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=600) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def get(path):
    with urllib.request.urlopen(base + path, timeout=60) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


try:
    print("=== 1) 页面结构 ===")
    with urllib.request.urlopen(base + "/booking", timeout=30) as response:
        html = response.read().decode("utf-8")
    check(response.status == 200, "GET /booking 可访问")
    for token, label in (
        ("批量提交订单", "有批量提交区域"),
        ("/api/orders/submit", "调用提交接口"),
        ("/api/orders/types", "调用类型清单接口"),
        ("id=\"palette\"", "有乘客类型面板"),
        ("id=\"orderList\"", "有订单列表"),
        ("btnAddOrder", "有『新建订单』按钮"),
        ("btnSubmitOrders", "有『提交全部订单』按钮"),
        ("renderUnmet", "有未满足提示渲染"),
        ("impossible", "有『无法满足』分档"),
    ):
        check(token in html, label)
    for name, o, c in (("花括号", "{", "}"), ("圆括号", "(", ")"), ("方括号", "[", "]")):
        check(html.count(o) == html.count(c), f"{name}平衡（{html.count(o)} vs {html.count(c)}）")

    print()
    print("=== 2) 类型清单接口 ===")
    status, types = get("/api/orders/types")
    check(status == 200 and len(types["archetypes"]) >= 15,
          f"返回 {len(types.get('archetypes', []))} 种乘客类型")
    check("outcome_levels" in types and len(types["outcome_levels"]) == 4,
          "返回四档结果定义")
    check("feasibility_codes" in types, "返回不可行原因码")

    print()
    print("=== 3) 任意张数 × 每单任意人数 ===")
    status, result = post("/api/orders/submit", {
        "orders": [
            {"note": "单人", "passengers": [{"key": "adult"}]},
            {"note": "两人", "passengers": [{"key": "adult"}, {"key": "child"}]},
            {"note": "三人", "passengers": [{"key": "wheelchair"}, {"key": "caregiver"}, {"key": "adult"}]},
            {"note": "五人", "passengers": [{"key": "adult"}] * 5},
        ],
    })
    check(status == 200, "提交接口可用")
    check(result["summary"]["orders"] == 4, f"接受 4 张订单（{result['summary']['orders']}）")
    check(result["summary"]["requested_passengers"] == 11,
          f"接受 11 位乘客（{result['summary']['requested_passengers']}）")
    check(result["summary"]["seated_passengers"] == 11, "全部出票")
    check(result["summary"]["tier0_violations"] == 0, "Tier 0 为 0")
    check(len(result["orders"]) == 4, "逐单返回结果")
    check(all("level" in o and "reasons" in o for o in result["orders"]),
          "每单带分档与原因")

    print()
    print("=== 4) 无法满足的订单必须有提示 ===")
    # 单次请求内自足：先用 7 张轮椅订单占满专区（10 座），再放两张必然溢出的单。
    # 不能依赖"上一节刚好占掉了座位"——那是测试之间的隐式耦合。
    orders = [
        {
            "order_id": f"FILL-{n}",
            "note": f"第 {n} 张轮椅订单",
            "passengers": [{"key": "wheelchair"}, {"key": "caregiver"}],
        }
        for n in range(1, 8)
    ]
    orders.append(
        {
            "order_id": "BAD-ONE",
            "note": "专区满后到来的轮椅旅客",
            "passengers": [{"key": "wheelchair"}, {"key": "caregiver"}],
        }
    )
    orders.append(
        {
            "order_id": "BAD-MANY",
            "note": "一张单 4 位轮椅",
            "passengers": [{"key": "wheelchair"}] * 4 + [{"key": "caregiver"}] * 4,
        }
    )
    status, bad = post("/api/orders/submit", {"orders": orders})
    summary = bad["summary"]
    check(status == 200, "接口可用")
    check(summary["impossible_orders"] >= 1,
          f"识别出无法满足的订单（{summary['impossible_orders']} 张）")
    check(bool(bad["unmet_orders"]), "未满足订单清单非空")
    reasons = " ".join(bad["unmet_orders"][0]["reasons"])
    check("无障碍专区" in reasons, f"原因说明具体（{reasons[:60]}）")
    check(bad["unmet_orders"][0]["level"] == "impossible", "分档为『无法满足』")
    # 溢出订单必须落在最后两张里
    bad_ids = {item["order_id"] for item in bad["unmet_orders"]}
    check(bool(bad_ids & {"BAD-ONE", "BAD-MANY"}),
          f"溢出的订单被标为无法满足（{[i['order_id'] for i in bad['unmet_orders']]}）")

    print()
    print("=== 5) 座位图可按订单着色 ===")
    check(bool(result["train"].get("seats")), "返回座位快照")
    check(len(result["seat_owner"]) == 11, f"座位归属覆盖 11 座（{len(result['seat_owner'])}）")
finally:
    httpd.shutdown()
    httpd.server_close()

print()
if failures:
    print(f"失败 {len(failures)} 项：")
    for item in failures:
        print("  -", item)
    raise SystemExit(1)
print("批量订单提交页面校验全部通过。")
