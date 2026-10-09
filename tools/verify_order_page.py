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
    for name, o, c in (("花括号", "{", "}"), ("方括号", "[", "]")):
        # 不对整份 HTML 数 ASCII 圆括号：正文里的中文全角括号（U+FF08/9）
        # 会被误统计。JS 段的精确配对检查交给 smartrail.web.bracket_check。
        check(html.count(o) == html.count(c), f"{name}平衡（{html.count(o)} vs {html.count(c)}）")
    from smartrail.web.bracket_check import check_js_brackets

    issues = check_js_brackets(html)
    check(not issues, f"JS 括号精确配对（问题：{issues[:2] or '无'}）")

    print()
    print("=== 2) 类型清单接口 ===")
    status, types = get("/api/orders/types")
    check(status == 200 and len(types["archetypes"]) >= 15,
          f"返回 {len(types.get('archetypes', []))} 种乘客类型")
    check(
        "outcome_levels" in types and len(types["outcome_levels"]) >= 4,
        f"返回结果分档定义（{len(types.get('outcome_levels', {}))} 档）",
    )
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
    print("=== 4) 出票优先：特殊席位不够也照常出票 + 提示/询问 ===")
    # 单次请求内自足：先把无障碍专区占满，再放溢出的单。
    # 不能依赖"上一节刚好占掉了座位"——那是测试之间的隐式耦合。
    #
    # 专区分两处（04 车与 12 车，各 2 排 = 10 座），合计 **20 座**。
    # 早期这里写死"7 张单占满 10 座"，是按"只有 04 车"的旧编组算的；
    # 编组改成 04+12 后就占不满了，于是"站车协助提示"不再触发。
    # 现在按实际座位数生成填满订单，编组再变也不会失效。
    from smartrail.api import service as _service

    engine = _service.create_engine()
    zone_seats = sum(1 for s in engine.formation.seats if s.in_accessible_zone())
    print(f"      无障碍专区实际座位数：{zone_seats}")
    # 每张填满单放 2 位轮椅 -> 需要的订单数
    per_order = 2
    fill_orders = -(-zone_seats // per_order)
    orders = [
        {
            "order_id": f"FILL-{n}",
            "note": f"第 {n} 张轮椅订单",
            "passengers": [{"key": "wheelchair"}] * per_order
            + [{"key": "caregiver"}] * per_order,
        }
        for n in range(1, fill_orders + 1)
    ]
    orders.append(
        {
            "order_id": "LATE-ONE",
            "note": "专区满后到来的轮椅旅客",
            "passengers": [{"key": "wheelchair"}, {"key": "caregiver"}],
        }
    )
    orders.append(
        {
            "order_id": "LATE-MANY",
            "note": "一张单 4 位轮椅",
            "passengers": [{"key": "wheelchair"}] * 4 + [{"key": "caregiver"}] * 4,
        }
    )
    status, overflow = post("/api/orders/submit", {"orders": orders})
    summary = overflow["summary"]
    check(status == 200, "接口可用")
    check(
        summary["seated_passengers"] == summary["requested_passengers"],
        f"专区售罄后仍然全员出票"
        f"（{summary['seated_passengers']}/{summary['requested_passengers']}）",
    )
    check(summary["waitlisted_passengers"] == 0, "没有因专区售罄而候补")
    check(summary["impossible_orders"] == 0, "没有订单被判『无座可发』")
    late = [item for item in overflow["orders"] if item["order_id"] == "LATE-ONE"][0]
    check(late["seated"] == late["requested"], "溢出的轮椅订单也出票了")
    notices = " ".join(n["message"] for n in late["notices"])
    check("无障碍专区已满" in notices, f"生成站车协助提示（{notices[:48]}…）")
    check(
        any(item["order_id"] == "LATE-ONE" for item in overflow["confirmations"]),
        "给出需要用户确认的问题（提示/询问后出票）",
    )
    check(
        any(item["order_id"] == "LATE-MANY" for item in overflow["confirmations"]),
        "超配额的轮椅大单也给出确认询问",
    )

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
