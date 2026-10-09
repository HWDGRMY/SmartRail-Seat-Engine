"""12306 风格购票页面的 HTTP 端到端验收。

覆盖需求逐条：车次列表无座位图、选座只一排、静音勾选、必须先加乘车人、
余票不足才分票、开发者改余票实时生效、重置系统。
"""

from __future__ import annotations

import json
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from smartrail.api.stdlib_server import serve  # noqa: E402

httpd = serve("127.0.0.1", 0)
port = httpd.server_address[1]
threading.Thread(target=httpd.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{port}"
problems: list[str] = []


def check(ok: bool, message: str) -> None:
    print(("  PASS  " if ok else "  FAIL  ") + message)
    if not ok:
        problems.append(message)


def call(path: str, payload: dict | None = None, method: str = "GET"):
    """请求接口。**同时兼容 JSON 与 HTML 响应** —— 页面路由返回的是 HTML，
    早期实现对它调 ``json.loads`` 会抛 JSONDecodeError。"""
    url = BASE + path
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        method = "POST"
    request = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            raw_body = response.read().decode("utf-8")
            status = response.status
    except urllib.error.HTTPError as error:
        raw_body = error.read().decode("utf-8")
        status = error.code
    try:
        return status, json.loads(raw_body)
    except json.JSONDecodeError:
        return status, {"_html": raw_body}


def raw(path: str) -> str:
    with urllib.request.urlopen(BASE + path, timeout=60) as response:
        return response.read().decode("utf-8")


try:
    print("=== 1) 页面可访问 ===")
    for path, keyword in (("/ticket", "确认订单"), ("/dev", "全局座位图")):
        status, _ = call(path)
        page = raw(path)
        check(status == 200 and keyword in page,
              f"{path} 返回 200 且含「{keyword}」（{len(page)} 字符）")
    user_page = raw("/ticket")
    dev_page = raw("/dev")
    # 用户模式绝不能出现整列座位图相关的渲染逻辑
    check("seatgrid" not in user_page, "用户模式页面**不含**座位图容器")
    check("seatgrid" in dev_page, "开发者模式页面含座位图容器")
    check("/api/dev/" not in user_page, "用户模式页面不调用任何 /api/dev 接口")

    print()
    print("=== 2) 重置到干净状态 ===")
    status, body = call("/api/dev/reset", {"passengers": True})
    check(status == 200 and body["reset"], "重置成功")
    check(body["occupied_count"] == 0, f"无占用（{body['occupied_count']}）")
    check(body["passenger_count"] == 16,
          f"重置后预制乘车人 {body['passenger_count']} 位（仅开发者模式可见）")
    check(body.get("user_passenger_count", 0) == 0,
          f"重置后用户添加的乘车人清空（{body.get('user_passenger_count')}）")

    print()
    print("=== 3) 乘车人：用户模式隔离 + 类型目录 + 增删 ===")
    # **需求**：预制乘车人只给开发者模式，用户模式必须看不到
    status, user_side = call("/api/passengers")
    check(status == 200 and user_side["count"] == 0,
          f"用户模式起始 0 位（{user_side.get('count')}）")
    check(user_side["empty"] is True, "用户模式为空 -> 界面引导添加")
    status, body = call("/api/passengers?scope=dev")
    check(status == 200 and body["count"] == 16, f"开发者模式预制 {body.get('count')} 位")
    check(body["empty"] is False, "开发者模式列表非空")
    types = {p["type_id"] for p in body["passengers"]}
    check(len(types) == 16, f"覆盖 {len(types)} 种人群类型")
    status, catalog = call("/api/passengers/types")
    check(len(catalog["types"]) == 16, f"类型目录 {len(catalog['types'])} 种")
    check(len(catalog["groups"]) == 5, f"类型分组 {len(catalog['groups'])} 组")

    status, created = call("/api/passengers/add",
                           {"name": "接口测试", "type_id": "wheelchair"})
    check(status == 201, f"添加返回 201（实际 {status}）")
    new_id = created["profile_id"]
    check(new_id == "C017", f"新编号不撞预制（{new_id}）")
    # 新加的算"用户添加"，因此用户模式看得见、开发者模式也看得见
    status, body = call("/api/passengers")
    check(body["count"] == 1,
          f"用户模式看得到自己添加的 1 位（{body['count']}）")
    check(all(p["source"] != "preset" for p in body["passengers"]),
          "用户模式返回的都是用户添加的，无预制")
    status, body = call("/api/passengers?scope=dev")
    check(body["count"] == 17, f"开发者模式 17 位（{body['count']}）")
    status, body = call("/api/passengers/remove", {"profile_id": new_id})
    check(status == 200 and body["count"] == 16,
          f"移除后开发者模式回到 16 位（{body['count']}）")
    status, body = call("/api/passengers")
    check(body["count"] == 0, f"移除后用户模式回到 0 位（{body['count']}）")

    print()
    print("=== 4) 车次列表：有余票、无座位图 ===")
    status, body = call("/api/trains")
    trains = body["trains"]
    check(len(trains) == 3, f"{len(trains)} 个车次")
    first = trains[0]
    check(first["train_code"] == "G25", f"首个 {first['train_code']}")
    check(first["total_seats"] == 1238, f"定员 {first['total_seats']}")
    check("seats" not in first and "carriages" not in first,
          "车次列表**不含**座位/车厢明细")
    check(first["quiet_carriages"] == [3, 11],
          f"静音车厢 {first['quiet_carriages']}")
    names = [c["class_code"] for c in first["classes"]]
    check(names == ["二等座", "一等座", "商务座"], f"席别 {names}")

    print()
    print("=== 5) 选座服务：只有一排 ===")
    # 中文查询参数必须 URL 编码：浏览器会自动做，测试里要显式做，
    # 否则 http.client 在构造请求行时报 UnicodeEncodeError（ascii 编码失败）。
    status, body = call(
        "/api/trains/seat-row?class_code=" + urllib.parse.quote("二等座"))
    columns = [c["col"] for c in body["columns"]]
    check(columns == ["A", "B", "C", "D", "F"], f"列 {columns}")
    check(all("row" not in c and "carriage" not in c and "seat_id" not in c
              for c in body["columns"]), "不含排号/车厢/座位号")
    feature = {c["col"]: c["feature"] for c in body["columns"]}
    check(feature["A"] == "window" and feature["C"] == "aisle",
          f"特征 {feature}")

    print()
    print("=== 6) 必须先添加乘车人 ===")
    status, body = call("/api/trains/evaluate",
                        {"class_code": "二等座", "profile_ids": []})
    check(any("请先添加乘车人" in e for e in body["errors"]),
          f"空选择报错（{body['errors']}）")
    status, body = call("/api/tickets/book",
                        {"class_code": "二等座", "profile_ids": []})
    check(status == 422, f"提交被拒（{status}）")

    print()
    print("=== 7) 单人下单：出票并进入台账 ===")
    status, body = call("/api/tickets/book",
                        {"class_code": "二等座", "profile_ids": ["C001"],
                         "preference": {"columns": ["A"], "quiet": False}})
    check(status == 200 and body["ok"], "出票成功")
    order = body["order"]
    check(order["seated"] == 1, f"出票 {order['seated']} 张")
    seat = order["passengers"][0]
    check(seat["col"] == "A", f"满足选座偏好（列 {seat['col']}）")
    check(bool(seat["seat_id"]), f"座位号 {seat['seat_id']}")

    status, snap = call("/api/dev/snapshot")
    check(snap["occupied_count"] == 1, f"开发者座位图已同步（{snap['occupied_count']}）")
    sold = [s for s in snap["seats"] if s["source"] == "sold"]
    check(len(sold) == 1 and sold[0]["order_id"] == order["order_id"],
          f"占用归属正确（{sold[0]['order_id']}）")
    check(sold[0]["passenger_name"] != "", "占用带乘客姓名（供分票映射展示）")

    print()
    print("=== 8) 余票不足 -> 自动分票 ===")
    call("/api/dev/reset", {"passengers": False})
    status, body = call("/api/dev/remaining", {"class_code": "二等座", "remaining": 0})
    check(body["remaining"] == 0, f"二等座售罄（余 {body['remaining']}）")
    status, trains = call("/api/trains")
    second = [c for c in trains["trains"][0]["classes"]
              if c["class_code"] == "二等座"][0]
    check(second["status"] == "无" and not second["bookable"],
          f"车次列表显示「{second['status']}」，不可订")
    status, verdict = call("/api/trains/evaluate",
                           {"class_code": "二等座",
                            "profile_ids": ["C001", "C005"],
                            "preference": {"columns": [], "quiet": False}})
    check(verdict["verdict"]["split_needed"], f"判定需要分票（{verdict['verdict']['reason']}）")

    print()
    print("=== 9) 空车多人：不触发分票 ===")
    call("/api/dev/reset", {"passengers": False})
    status, verdict = call("/api/trains/evaluate",
                           {"class_code": "二等座",
                            "profile_ids": ["C001", "C004", "C005"],
                            "preference": {"columns": [], "quiet": False}})
    check(not verdict["verdict"]["split_needed"],
          f"空车 3 人同排可行（最长连续 {verdict['verdict']['largest_run']}）")
    status, body = call("/api/tickets/book",
                        {"class_code": "二等座",
                         "profile_ids": ["C001", "C004", "C005"],
                         "preference": {"columns": [], "quiet": False}})
    check(body["ok"] and body["order"]["seated"] == 3, "3 人全部出票")
    rows = body["order"]["rows"]
    check(len(rows) == 1, f"安排在同一排（{rows}）")

    print()
    print("=== 10) 静音车厢勾选 ===")
    call("/api/dev/reset", {"passengers": False})
    status, body = call("/api/tickets/book",
                        {"class_code": "二等座", "profile_ids": ["C001"],
                         "preference": {"columns": [], "quiet": True}})
    seat = body["order"]["passengers"][0]
    check(seat["quiet"] is True, f"分配到静音车厢（{seat['seat_id']}）")
    check(seat["carriage"] in (3, 11), f"车厢 {seat['carriage']} 属于 03/11")
    # 注意：上面两条在"静音车厢恰好最先遍历到"时**恒真**，不能证明偏好生效。
    # 真正能区分的是"同一个座位、只改偏好"的得分差，这里由
    # tests/test_ticketing.py::test_quiet_preference_changes_scoring 覆盖：
    # 勾选后静音座位亲和度 50 -> 60，不勾选则不变。

    print()
    print("=== 11) 开发者手动锁定座位 ===")
    call("/api/dev/reset", {"passengers": False})
    status, body = call("/api/dev/seat/toggle", {"seat_id": "02车01A"})
    check(status == 200 and body["action"] == "locked", "锁定成功")
    status, body = call("/api/dev/seat/toggle", {"seat_id": "02车01A"})
    check(status == 200 and body["action"] == "unlocked", "解锁成功")
    status, body = call("/api/dev/seat/toggle", {"seat_id": "99车99Z"})
    check(status == 404, f"不存在的座位返回 404（{status}）")

    print()
    print("=== 12) 用户已售座位不可手动解锁 ===")
    call("/api/dev/reset", {"passengers": False})
    status, booked = call("/api/tickets/book",
                          {"class_code": "二等座", "profile_ids": ["C001"]})
    sold_seat = booked["order"]["passengers"][0]["seat_id"]
    status, body = call("/api/dev/seat/toggle", {"seat_id": sold_seat})
    check(status == 409, f"拒绝解锁已售座位（{status}：{body.get('detail')}）")

    print()
    print("=== 13) 上座率压测与重置 ===")
    status, body = call("/api/dev/fill", {"ratio": 0.6})
    check(abs(body["actual_ratio"] - 0.6) < 0.02,
          f"卖到 {body['actual_ratio']:.1%}（目标 60%）")
    status, snap = call("/api/dev/snapshot")
    check(snap["occupied_count"] > 700, f"座位图已售 {snap['occupied_count']} 个")
    status, body = call("/api/dev/reset", {"passengers": True})
    check(body["occupied_count"] == 0 and body["order_count"] == 0, "重置清空")
    check(body["remaining"]["二等座"] == 1152,
          f"二等座余票回到 {body['remaining']['二等座']}")

    print()
    print("=== 14) 页面脚本可执行（Node 实跑） ===")
    import subprocess

    harness = ROOT / ".np2.js"
    harness.write_text(
        "const fs=require('fs');const html=fs.readFileSync(process.argv[2],'utf8');"
        "const s=html.indexOf('<script>')+8,e=html.lastIndexOf('</script>');"
        "try{new Function(html.slice(s,e));console.log('OK');}"
        "catch(err){console.log('FAIL '+err.message);}",
        encoding="utf-8",
    )
    for path, name in (("/ticket", "ticketing.html"), ("/dev", "developer.html")):
        page = ROOT / "smartrail" / "web" / name
        result = subprocess.run(["node", str(harness), str(page)],
                                capture_output=True, text=True)
        check(result.stdout.strip() == "OK",
              f"{name} 语法解析通过（{result.stdout.strip()}）")
    harness.unlink(missing_ok=True)

finally:
    httpd.shutdown()
    httpd.server_close()

print()
if problems:
    print(f"失败 {len(problems)} 项：")
    for item in problems:
        print("  -", item)
    raise SystemExit(1)
print("12306 购票流程 HTTP 验收全部通过。")
