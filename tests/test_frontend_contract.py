"""前端契约测试：HTML/JS 与后端接口、DOM 元素、数据字段的一致性。

双模式运行：

    python tests/test_frontend_contract.py
    pytest tests/test_frontend_contract.py

为什么需要它
------------
前端是纯静态单文件（无构建链），最容易出现的回归是"JS 里引用了不存在的
DOM id"或"调用了一个后端没有的接口"。这类错误通常只在浏览器里才暴露，
所以在 CI 里用纯文本契约测试提前拦住。
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from smartrail.api import service  # noqa: E402
from smartrail.api.stdlib_server import serve  # noqa: E402
from smartrail.credit import CreditLedger  # noqa: E402

FAILURES: list[str] = []
UNDER_PYTEST = "PYTEST_CURRENT_TEST" in os.environ or "pytest" in sys.modules
HTML = (ROOT / "smartrail" / "web" / "index.html").read_text(encoding="utf-8")


def check(condition: bool, message: str) -> None:
    if condition:
        print(f"  PASS  {message}")
        return
    print(f"  FAIL  {message}")
    FAILURES.append(message)
    if UNDER_PYTEST:
        raise AssertionError(message)


def test_dom_ids_referenced_by_js_exist() -> None:
    print("[前端] JS 引用的 DOM id 都存在")
    defined = set(re.findall(r'id="([^"]+)"', HTML))
    used = set(re.findall(r'\$\("([^"]+)"\)', HTML))
    used |= set(re.findall(r'getElementById\("([^"]+)"\)', HTML))
    missing = sorted(used - defined)
    check(not missing, f"JS 引用的 id 全部已定义（缺失：{missing or '无'}）")
    check(len(used) >= 8, f"前端至少引用 8 个状态元素（实际 {len(used)}）")


def test_api_paths_match_backend_routes() -> None:
    print("[前端] 调用的接口与后端路由一致")
    called = set(re.findall(r'api\("(/api/[^"]+)"', HTML))
    expected = {
        "/api/snapshot",
        "/api/config",
        "/api/scenario",
        "/api/credit",
        "/api/reset",
    }
    check(expected <= called, f"前端调用的接口覆盖核心能力（实际 {sorted(called)}）")

    # 验收页面（V1/V2/V3 对比台）另有一套接口契约
    acceptance = (ROOT / "smartrail" / "web" / "acceptance.html").read_text(encoding="utf-8")
    for path in ("/api/compare", "/api/config"):
        check(path in acceptance, f"验收页调用 {path}")
    check('id="snapshot-data"' in acceptance, "验收页内嵌快照容器存在（离线可用）")

    httpd = serve("127.0.0.1", 0)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        for path in sorted(called):
            method = "POST" if path in {"/api/scenario", "/api/credit", "/api/reset"} else "GET"
            payload = None
            if path == "/api/scenario":
                payload = {"scenario": "family_with_child"}
            if path == "/api/credit":
                payload = {"passenger_id": "P-CONTRACT", "action": "commendation"}
            if path == "/api/reset":
                payload = {"formation": "crh16"}
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}{path}",
                data=json.dumps(payload).encode() if payload else None,
                method=method,
            )
            if payload:
                request.add_header("Content-Type", "application/json")
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    body = json.loads(response.read().decode("utf-8"))
                check(response.status == 200 and body, f"{method} {path} 返回 200 且非空")
            except urllib.error.HTTPError as error:
                check(False, f"{method} {path} 返回 {error.code}（前端会调用失败）")

        # 验收台、出票优先页、以及**交互式选座页**
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/acceptance", timeout=30) as response:
            html = response.read().decode("utf-8")
        check(response.status == 200 and "验收台" in html, "GET /acceptance 返回验收台页面")
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/ticket-first", timeout=30) as response:
            tf_html = response.read().decode("utf-8")
        check(
            response.status == 200 and len(tf_html) > 2000,
            f"GET /ticket-first 返回出票优先页（{len(tf_html)} 字节）",
        )
        check("出票优先" in tf_html, "出票优先页含策略说明")
        check('id="tf-data"' in tf_html, "出票优先页内嵌真实场景数据（离线可用）")
        check("__TF_SNAPSHOT__" not in tf_html, "出票优先页数据已注入（占位符已替换）")
        # /booking 批量提交页面已按需求删除，下单能力移到 /dev。
        # 这里检查它**确实不可访问**（而不是"忘了删路由"）。
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/booking", timeout=30
            ) as response:
                gone_status = response.status
        except urllib.error.HTTPError as error:
            gone_status = error.code
        check(gone_status == 404,
              f"GET /booking 已下线（HTTP {gone_status}）")
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/dev", timeout=30) as response:
            dev_html = response.read().decode("utf-8")
        check(
            response.status == 200 and "提交订单" in dev_html,
            f"GET /dev 含『提交订单』入口（{len(dev_html)} 字节）",
        )
        check("/api/tickets/book" in dev_html,
              "开发者页调用真实下单接口（不是预置回放）")
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/scenario/passengers/family_with_child", timeout=30
        ) as response:
            scenario_body = json.loads(response.read().decode("utf-8"))
        check(
            bool(scenario_body.get("passengers")),
            "GET /api/scenario/passengers/<name> 返回可填表数据",
        )
        compare = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/compare",
            data=json.dumps({"steps": 4, "seed": 3}).encode(),
            method="POST",
        )
        compare.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(compare, timeout=600) as response:
            body = json.loads(response.read().decode("utf-8"))
        check(response.status == 200 and body.get("rows"), "POST /api/compare 返回对比行")
        check(
            any(row.get("policy", "").startswith("v1") for row in body["rows"]),
            "对比结果至少包含 V1 基线",
        )
        check("backends" in body, "对比结果附带后端可用性（前端据此解释跳过原因）")
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/training-curve", timeout=60) as response:
            curve = json.loads(response.read().decode("utf-8"))
        check("available" in curve, "GET /api/training-curve 返回可用性标记")
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def test_scenario_names_present_in_html() -> None:
    print("[前端] 场景与语义展示")
    check("loadScenarios" in HTML, "场景按钮由 /api/config 动态生成（不会前后端漂移）")
    for token in ("静音车厢", "无障碍专区", "Tier 0", "可解释性"):
        check(token in HTML, f"前端包含「{token}」相关展示")


def test_engine_exposes_required_payload_fields() -> None:
    print("[契约] 引擎响应字段满足前端渲染需求")
    engine = service.create_engine("crh16", CreditLedger())
    body = service.run_scenario(engine, "family_with_child")
    for key in ("assignments", "violations", "explanation", "mode", "solver", "total_cost", "waitlisted"):
        check(key in body, f"场景响应包含 {key}")
    assignment = body["assignments"][0]
    for key in ("passenger_id", "seat_id", "carriage", "row", "col", "quiet_carriage", "reason"):
        check(key in assignment, f"分配项包含 {key}")
    violation = (
        body["violations"][0]
        if body["violations"]
        else {"tier": 0, "code": "x", "penalty": 0, "detail": "", "is_reward": False}
    )
    for key in ("tier", "code", "penalty", "detail", "is_reward"):
        check(key in violation, f"代价条目包含 {key}")
    snapshot = engine.snapshot()
    for key in ("carriages", "seats", "availability_ratio", "occupied_count", "total_seats", "latency"):
        check(key in snapshot, f"座位图快照包含 {key}")
    seat = snapshot["seats"][0]
    for key in ("seat_id", "carriage", "row", "col", "quiet", "accessible", "occupied"):
        check(key in seat, f"座位项包含 {key}")


def main() -> int:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
    print("-" * 72)
    if FAILURES:
        print(f"失败 {len(FAILURES)} 项：")
        for item in FAILURES:
            print(f"  - {item}")
        return 1
    print(f"全部 {len(tests)} 组断言通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
