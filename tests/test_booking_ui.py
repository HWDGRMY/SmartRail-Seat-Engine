"""交互式选座页（12306 风格）的契约与交互链路测试。

这个页面与另外两个页面的根本区别：**没有任何预置回放**。
用户自己填乘客、自己点座位、自己按提交，所以必须验证"用户操作 → 后端 → 结果"
这条链路真的走得通，而不是只检查 HTML 里有没有关键字。

测试顺序刻意模仿真实用户操作：
1. 打开页面 → 2. 重置车厢（选上座率）→ 3. 填乘客（含预置场景）→
4. 提交订单 → 5. 自选座位 → 6. 被拦截时能否看懂原因。
"""

from __future__ import annotations

import json
import os
import sys
import threading
import urllib.error
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


def _client(base: str):
    def get(path: str):
        with urllib.request.urlopen(base + path, timeout=60) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def post(path: str, payload: dict):
        request = urllib.request.Request(
            base + path,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=120) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    return get, post


def test_page_is_purely_interactive() -> None:
    """页面必须是真交互：无预置快照，且用户能改乘客/座位/上座率。"""
    print("[交互页] 页面是纯交互（无预置回放）")
    text = PAGE.read_text(encoding="utf-8")
    check(len(text) > 5000, f"页面内容完整（{len(text)} 字符）")
    for token, label in (
        ("提交订单", "有提交按钮"),
        ("/api/book", "调用真实下单接口"),
        ("/api/reset", "支持重置车厢"),
        ("/api/snapshot", "读取真实座位状态"),
        ("/api/scenario/passengers/", "预置场景从后端拉取（不重复维护乘客定义）"),
        ("addPassenger", "支持用户自行添加乘客"),
        ("support_needs", "支持特殊需求（轮椅/孕晚期等）"),
        ("mandatory", "支持硬绑定关系"),
        ('id="fill"', "支持调整车厢上座率"),
    ):
        check(token in text, label)
    check("__SNAPSHOT" not in text and "__TF_" not in text, "不含任何预置快照占位符")
    for name, open_ch, close_ch in (("花括号", "{", "}"), ("圆括号", "(", ")"), ("方括号", "[", "]")):
        check(
            text.count(open_ch) == text.count(close_ch),
            f"{name}平衡（{text.count(open_ch)} vs {text.count(close_ch)}）",
        )


def test_full_interaction_chain() -> None:
    """完整交互链路：重置 → 填表 → 提交 → 出票。"""
    print("[交互页] 用户操作链路端到端")
    httpd = serve("127.0.0.1", 0)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    get, post = _client(base)
    try:
        with urllib.request.urlopen(base + "/booking", timeout=30) as response:
            html = response.read().decode("utf-8")
        check(response.status == 200 and "提交订单" in html, "GET /booking 返回交互页")

        _, config = get("/api/config")
        check(bool(config.get("scenarios")), f"预置场景列表可用（{len(config.get('scenarios', []))} 个）")

        _, scenario = get("/api/scenario/passengers/family_with_child")
        check(bool(scenario.get("passengers")), "预置场景可导出为表单数据")
        check(
            any(b["bond"] == "mandatory" for b in scenario["bonds"]),
            "带娃家庭场景含硬绑定关系",
        )

        _, snap = post("/api/reset", {"formation": "crh16", "fill": 0.9, "seed": 7})
        free = snap["total_seats"] - snap["occupied_count"]
        check(0 < free < snap["total_seats"], f"上座率设置生效（空位 {free}）")

        status, body = post(
            "/api/book",
            {
                "order_id": "O-UI-FAMILY",
                "mode": "smart",
                "passengers": [
                    {"passenger_id": "A1", "name": "张伟", "age": 36},
                    {"passenger_id": "A2", "name": "李娜", "age": 34},
                    {"passenger_id": "C1", "name": "张小雨", "age": 5, "ticket_type": "child"},
                ],
                "bonds": [
                    {"a": "A1", "b": "C1", "bond": "mandatory"},
                    {"a": "A1", "b": "A2", "bond": "strong"},
                ],
            },
        )
        check(status == 200, "提交订单成功")
        check(len(body["assignments"]) == 3, f"3 人全部出票（实际 {len(body['assignments'])}）")
        check(not body["waitlisted"], "无人被拒票（九成上座率下仍然出票）")
        for field in ("notices", "adjacency", "explanation", "breakdown", "elapsed_ms", "total_cost"):
            check(field in body, f"返回 {field}")
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_free_seat_selection_contract() -> None:
    """自选座位的映射方向必须是 {乘客号: 座位号}。

    方向写反会得到 ``V_UNKNOWN_PASSENGER``（"乘客 01车01A 不属于本订单"），
    报错信息完全不会提示是方向问题 —— 这是本项目真实踩过的坑，
    因此必须用测试锁住契约方向。
    """
    print("[交互页] 自选座位契约方向")
    httpd = serve("127.0.0.1", 0)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    get, post = _client(base)
    try:
        _, snapshot = get("/api/snapshot")
        seats = [s["seat_id"] for s in snapshot["seats"] if not s["occupied"]][:2]
        check(len(seats) == 2, "取到两个空位")

        status, body = post(
            "/api/book",
            {
                "order_id": "O-UI-FREE",
                "mode": "free",
                "passengers": [
                    {"passenger_id": "S1", "name": "旅客一", "age": 30},
                    {"passenger_id": "S2", "name": "旅客二", "age": 28},
                ],
                "bonds": [],
                "chosen_seats": {"S1": seats[0], "S2": seats[1]},
            },
        )
        check(status == 200, "自选座位下单成功")
        assigned = {a["passenger_id"]: a["seat_id"] for a in body["assignments"]}
        check(assigned.get("S1") == seats[0], f"S1 落在自选座位 {seats[0]}")
        check(assigned.get("S2") == seats[1], f"S2 落在自选座位 {seats[1]}")
        check(body.get("mode") == "free", "返回 mode=free")

        # 反向映射必须被明确拒绝（而不是静默错配）
        try:
            post(
                "/api/book",
                {
                    "order_id": "O-UI-REVERSED",
                    "mode": "free",
                    "passengers": [{"passenger_id": "S9", "name": "旅客九", "age": 30}],
                    "bonds": [],
                    "chosen_seats": {seats[0]: "S9"},
                },
            )
            check(False, "反向映射应被拒绝，但通过了")
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8")
            check(error.code == 409, f"反向映射被拒绝（HTTP {error.code}）")
            check("V_UNKNOWN_PASSENGER" in detail, "拒绝理由为乘客不属于本订单")
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_wheelchair_self_selection_is_blocked_with_readable_reason() -> None:
    """轮椅自选普通座位必须被拦截，且理由要让人看得懂。"""
    print("[交互页] 轮椅自选普通座位被拦截且理由可读")
    httpd = serve("127.0.0.1", 0)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    get, post = _client(base)
    try:
        _, snapshot = get("/api/snapshot")
        candidates = [
            s["seat_id"]
            for s in snapshot["seats"]
            if not s["occupied"] and not s["accessible"]
        ]
        check(bool(candidates), f"存在可尝试的普通空位（{len(candidates)} 个）")
        got_right_reason = False
        for seat_id in candidates[:12]:
            try:
                post(
                    "/api/book",
                    {
                        "order_id": "O-UI-BLOCK",
                        "mode": "free",
                        "passengers": [
                            {
                                "passenger_id": "W1",
                                "name": "轮椅旅客",
                                "age": 68,
                                "support_needs": ["wheelchair"],
                            }
                        ],
                        "bonds": [],
                        "chosen_seats": {"W1": seat_id},
                    },
                )
                check(False, f"轮椅自选普通座位 {seat_id} 应被拦截，但通过了")
                break
            except urllib.error.HTTPError as error:
                detail = error.read().decode("utf-8")
                if "WHEELCHAIR_NO_ACCESSIBLE" in detail and "无障碍专区" in detail:
                    got_right_reason = True
                    break
        check(got_right_reason, "拦截理由是无障碍约束本身，且含中文说明")
    finally:
        httpd.shutdown()
        httpd.server_close()


def main() -> int:
    tests = [
        test_page_is_purely_interactive,
        test_full_interaction_chain,
        test_free_seat_selection_contract,
        test_wheelchair_self_selection_is_blocked_with_readable_reason,
    ]
    for test in tests:
        if UNDER_PYTEST:
            test()
        else:
            try:
                test()
            except AssertionError:
                pass
    print("-" * 72)
    if _FAILURES:
        print(f"失败 {len(_FAILURES)} 项：")
        for item in _FAILURES:
            print(f"  - {item}")
        return 1
    print("全部 4 组断言通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
