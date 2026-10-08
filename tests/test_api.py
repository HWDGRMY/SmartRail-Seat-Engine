"""API 层测试：既跑"业务核心"（零依赖），也跑真实 HTTP 服务。

双模式运行：

    python tests/test_api.py     # 脚本模式：跑完全部检查并汇总
    pytest tests/test_api.py     # pytest 模式：逐条用例报告
"""

from __future__ import annotations

import json
import os
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from smartrail.api import service
from smartrail.api.stdlib_server import serve
from smartrail.credit import CreditLedger

FAILURES: list[str] = []
UNDER_PYTEST = "PYTEST_CURRENT_TEST" in os.environ or "pytest" in sys.modules


def check(condition: bool, message: str) -> None:
    if condition:
        print(f"  PASS  {message}")
        return
    print(f"  FAIL  {message}")
    FAILURES.append(message)
    if UNDER_PYTEST:
        raise AssertionError(message)


# ---------------------------------------------------------------------------
# 业务核心（不依赖 Web 框架）
# ---------------------------------------------------------------------------


def test_service_book_family() -> None:
    print("[service] 购票请求 -> 分配结果")
    engine = service.create_engine("crh16", CreditLedger())
    outcome = service.book_order(
        engine,
        {
            "order_id": "O-API-FAM",
            "relation": "nuclear_family",
            "passengers": [
                {"passenger_id": "A1", "age": 36, "name": "家长A"},
                {"passenger_id": "A2", "age": 34, "name": "家长B"},
                {"passenger_id": "C1", "age": 5, "ticket_type": "child", "declared_behavior": "lively"},
            ],
            "bonds": [
                {"a": "A1", "b": "C1", "bond": "mandatory"},
                {"a": "A1", "b": "A2", "bond": "strong"},
                {"a": "A2", "b": "C1", "bond": "strong"},
            ],
        },
    )
    body = outcome["body"]
    check(outcome["status"] == 200, "返回 200")
    check(len(body["assignments"]) == 3, "3 人全部分配")
    cars = {a["carriage"] for a in body["assignments"]}
    child_car = next(a["carriage"] for a in body["assignments"] if a["passenger_id"] == "C1")
    check(child_car in cars and len(cars) == 1, "儿童与家长同车厢")
    check(not [v for v in body["violations"] if v["tier"] == 0], "无 Tier 0 违规")
    check("Tier" in body["explanation"], "响应包含可解释文本")


def test_service_validation() -> None:
    print("[service] 非法输入校验")
    engine = service.create_engine("mini", CreditLedger())
    _ = engine
    try:
        service.order_from_payload({"passengers": []})
        check(False, "空乘客列表应报错")
    except ValueError as error:
        check("passengers" in str(error), f"空乘客列表报错：{error}")
    try:
        service.order_from_payload(
            {"passengers": [{"passenger_id": "X", "support_needs": ["not_a_need"]}]}
        )
        check(False, "非法枚举应报错")
    except ValueError as error:
        check("非法取值" in str(error), f"非法 support_needs 报错：{error}")


def test_service_scenarios_and_credit() -> None:
    print("[service] 场景与信用闭环")
    engine = service.create_engine("crh16", CreditLedger())
    names = service.scenario_names()
    check("family_with_child" in names, f"场景列表包含带娃家庭（{len(names)} 个场景）")
    for name in names:
        body = service.run_scenario(engine, name)
        if not body["assignments"] and not body["waitlisted"]:
            check(False, f"场景 {name} 既无分配也无候补")
            return
    check(True, f"全部 {len(names)} 个场景均返回结论")
    credited = service.update_credit(engine, "S1", "complaint")
    check(credited["score"] == 50.0 and credited["blocked"], "投诉后信用分 50 且被屏蔽")


def test_service_config_payload() -> None:
    print("[service] 配置暴露")
    engine = service.create_engine("crh16", CreditLedger())
    payload = service.config_payload(engine)
    config = payload["engine_config"]
    check(config["t0_separated_care_bond"] == -100_000.0, "Tier 0 权重对外可见")
    check(payload["quiet_credit_block_threshold"] == 60.0, "信用阈值对外可见")
    check("free" in payload["modes"], "模式列表对外可见")


# ---------------------------------------------------------------------------
# 真实 HTTP（零依赖服务器）
# ---------------------------------------------------------------------------


def _http(method: str, url: str, payload: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))


def test_stdlib_server_end_to_end() -> None:
    print("[http] 标准库服务器端到端")
    httpd = serve("127.0.0.1", 0)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    try:
        status, snapshot = _http("GET", f"{base}/api/snapshot")
        # 按**真实编组**校验，而不是写死一个数字：
        # 座位数由各车厢的列布局 × 排数决定，一等座/商务座的车厢排数更少。
        # 写死数字会让"把商务座容量夸大成 51 座"这类错误一路绿灯通过。
        from smartrail import crh_16_car_formation

        expected_total = len(crh_16_car_formation().seats)
        check(
            status == 200 and snapshot["total_seats"] == expected_total,
            f"GET /api/snapshot 座位数与编组一致（{snapshot['total_seats']} 座）",
        )
        by_class: dict[str, int] = {}
        for seat in crh_16_car_formation().seats:
            by_class[seat.class_code] = by_class.get(seat.class_code, 0) + 1
        check(
            by_class.get("商务座", 0) < by_class.get("一等座", 0) < by_class.get("二等座", 0),
            f"座位数按坐席递减：二等座 {by_class.get('二等座')} > "
            f"一等座 {by_class.get('一等座')} > 商务座 {by_class.get('商务座')}",
        )
        check(
            10 <= by_class.get("商务座", 0) / 4 <= 20,
            f"单节商务座约 10-20 座（实际 {by_class.get('商务座', 0) / 4:.1f}）",
        )

        status, config = _http("GET", f"{base}/api/config")
        check(status == 200 and "engine_config" in config, "GET /api/config 返回权重")

        status, body = _http("POST", f"{base}/api/scenario", {"scenario": "family_with_child"})
        check(status == 200 and len(body["assignments"]) == 3, "POST /api/scenario 返回分配")
        check(body["mode"] in {"free", "smart", "degraded"}, f"模式字段合法（{body['mode']}）")
        check("explanation" in body, "返回可解释文本")
        assigned_seats = [a["seat_id"] for a in body["assignments"]]
        check(len(set(assigned_seats)) == len(assigned_seats), "一人一座")

        status, _ = _http("POST", f"{base}/api/scenario", {"scenario": "does_not_exist"})
        check(status == 404, "未知场景返回 404")

        status, _ = _http("POST", f"{base}/api/book", {"passengers": []})
        check(status == 422, "非法请求返回 422")

        with urllib.request.urlopen(f"{base}/", timeout=30) as response:
            html = response.read().decode("utf-8")
        check("SmartRail-Seat-Engine" in html and "<script>" in html, "GET / 返回可视化前端")

        status, released = _http("POST", f"{base}/api/release", {"order_id": "O-WEB"})
        check(status == 200 and "released" in released, "POST /api/release 可用")

        status, reset = _http("POST", f"{base}/api/reset", {"formation": "mini"})
        check(status == 200 and reset["total_seats"] < 100, "POST /api/reset 可切换到小规模编组")
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


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
