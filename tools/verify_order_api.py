"""批量提交接口的端到端验收（``/api/orders/submit``，纯接口层）。

``/booking`` 批量提交**页面**已按需求删除，下单能力集中在 ``/dev``。
但"任意张数 × 每单任意人数与类型"这个**接口能力**仍在，这里守住它。

与页面级验收的区别：本脚本只打接口，不读 HTML —— 页面那层由
``tools/check_page_js.py``（在 Node 里真跑 JS）与 ``tools/verify_served_pages.py``
（检查服务实际下发的页面）覆盖。

用法::

    python tools/verify_order_api.py
"""

from __future__ import annotations

import json
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from smartrail.api.stdlib_server import serve  # noqa: E402

FAILURES: list[str] = []


def check(condition: bool, message: str) -> None:
    print(f"  {'PASS' if condition else 'FAIL'}  {message}")
    if not condition:
        FAILURES.append(message)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def main() -> int:
    port = free_port()
    # serve() 只**构造**服务器（签名 host/port/verbose），不会开始监听，
    # 必须自己起线程跑 serve_forever()。第一版漏了这步，
    # 表现为"连不上但服务器对象存在"。
    server = serve(port=port, verbose=False)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(80):
        try:
            with urllib.request.urlopen(base + "/api/trains", timeout=5):
                break
        except (urllib.error.URLError, OSError):
            time.sleep(0.25)

    def post(path: str, payload: dict):
        request = urllib.request.Request(
            base + path,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8")
            try:
                return error.code, json.loads(body)
            except json.JSONDecodeError:
                return error.code, {"_raw": body}

    def get(path: str):
        try:
            with urllib.request.urlopen(base + path, timeout=60) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            return error.code, {}

    print("=== 1) 类型清单接口 ===")
    status, catalog = get("/api/orders/types")
    check(status == 200, f"GET /api/orders/types 可用（HTTP {status}）")
    archetypes = catalog.get("archetypes", [])
    check(len(archetypes) >= 16, f"人群原型 {len(archetypes)} 种")
    check({"adult", "child", "wheelchair", "caregiver"}
          <= {a["key"] for a in archetypes},
          "含成人 / 儿童 / 轮椅 / 照护人")
    check(len(catalog.get("outcome_levels", {})) >= 4,
          f"分档定义 {len(catalog.get('outcome_levels', {}))} 级")

    print()
    print("=== 2) 任意张数 × 每单任意人数与类型 ===")
    orders = [
        {"order_id": "DESIGN-1", "passengers": [{"key": "adult"}]},
        {"order_id": "DESIGN-2", "passengers": [{"key": "adult"},
                                                {"key": "child"}]},
        {"order_id": "DESIGN-3", "passengers": [{"key": "wheelchair"},
                                                {"key": "caregiver"},
                                                {"key": "adult"}]},
        {"order_id": "DESIGN-4", "passengers": [{"key": "adult"}] * 5},
    ]
    status, result = post("/api/orders/submit", {"orders": orders})
    summary = result.get("summary", {})
    check(status == 200, f"提交接口可用（HTTP {status}）")
    check(summary.get("orders") == 4, f"接受 {summary.get('orders')} 张订单")
    check(summary.get("requested_passengers") == 11,
          f"接受 {summary.get('requested_passengers')} 位乘客")
    check(summary.get("seated_passengers") == summary.get("requested_passengers"),
          "全部出票")
    check(summary.get("tier0_violations") == 0,
          f"Tier 0 = {summary.get('tier0_violations')}")
    check(len(result.get("orders", [])) == 4, "逐单返回结果")
    check(all("level" in o and "reasons" in o for o in result.get("orders", [])),
          "每单带分档与原因")

    print()
    print("=== 3) 出票优先：特殊席位不够也照常出票 + 提示/询问 ===")
    from smartrail.api import service as _service

    engine = _service.create_engine()
    bay_count = engine.formation.total_wheelchair_bays
    print(f"      轮椅固定停放位：{bay_count} 个"
          f"（{sorted({b.carriage for b in engine.formation.wheelchair_bays})} 车）")
    per_order = 2
    fill_orders = -(-bay_count // per_order)
    orders = [
        {"order_id": f"FILL-{n}",
         "passengers": [{"key": "wheelchair"}] * per_order
                       + [{"key": "caregiver"}] * per_order}
        for n in range(1, fill_orders + 1)
    ]
    orders.append({"order_id": "LATE-ONE",
                   "passengers": [{"key": "wheelchair"}, {"key": "caregiver"}]})
    orders.append({"order_id": "LATE-MANY",
                   "passengers": [{"key": "wheelchair"}] * 4
                                 + [{"key": "caregiver"}] * 4})
    status, overflow = post("/api/orders/submit", {"orders": orders})
    summary = overflow["summary"]
    check(status == 200, "接口可用")
    check(summary["seated_passengers"] == summary["requested_passengers"],
          f"停放位售罄后仍然全员出票"
          f"（{summary['seated_passengers']}/{summary['requested_passengers']}）")
    check(summary.get("waitlisted_passengers", 0) == 0, "没有因停放位售罄而候补")
    check(summary.get("impossible_orders", 0) == 0, "没有订单被判『无座可发』")
    late = [o for o in overflow["orders"] if o["order_id"] == "LATE-ONE"][0]
    check(late["seated"] == late["requested"], "溢出的轮椅订单也出票了")
    notices = " ".join(n["message"] for n in late["notices"])
    check("轮椅固定停放位已满" in notices,
          f"生成站车协助提示（{notices[:44]}…）")
    check(any(item["order_id"] == "LATE-ONE"
              for item in overflow.get("confirmations", [])),
          "给出需要用户确认的问题（提示/询问后出票）")

    print()
    print("=== 4) 提交结果可还原座位归属与配色 ===")
    # 批量提交走的是**主引擎**（不是开发者台账 devstore），
    # 所以这里核对提交响应里的 seat_owner，而不是 /api/dev/snapshot ——
    # 早期版本查错了台账，结果"座位归属 0 座"永远失败。
    #
    # seat_owner 的形状是 ``{座位号: 颜色下标}``（不是按乘客号索引）。
    owner = result.get("seat_owner") or {}
    check(len(owner) >= 10, f"座位归属覆盖 {len(owner)} 座")
    colors = set(owner.values())
    check(len(colors) >= 2, f"不同订单分到不同颜色（{len(colors)} 种）")
    check(all(isinstance(value, int) for value in owner.values()),
          "颜色下标是整数（座位图直接取色用）")
    # 逐单返回的 seats 必须全部落在 seat_owner 里
    flattened: list[str] = []
    for item in result.get("orders", []):
        flattened.extend((item.get("seats") or {}).values())
    missing = [seat_id for seat_id in flattened if seat_id not in owner]
    check(not missing,
          f"逐单 seats 都被 seat_owner 覆盖（缺失 {len(missing)} 个）")

    print()
    if FAILURES:
        print(f"失败 {len(FAILURES)} 项：")
        for item in FAILURES:
            print("  -", item)
        return 1
    print("批量提交接口验收全部通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
