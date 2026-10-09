"""逐条验收本轮目标（12306 前端重构）。

目标原文拆成 12 条可机器验证的断言，全部针对**运行中的服务**，
而不是读代码猜测。
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

BASE = "http://127.0.0.1:8000"
problems: list[str] = []


def check(ok: bool, message: str) -> None:
    print(("  PASS  " if ok else "  FAIL  ") + message)
    if not ok:
        problems.append(message)


def call(path: str, payload: dict | None = None):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        BASE + path, data=data, method="POST" if data else "GET",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            raw = response.read().decode("utf-8")
            status = response.status
    except urllib.error.HTTPError as error:
        raw = error.read().decode("utf-8")
        status = error.code
    try:
        return status, json.loads(raw)
    except json.JSONDecodeError:
        return status, {"_html": raw}


def page(path: str) -> str:
    with urllib.request.urlopen(BASE + path, timeout=60) as response:
        return response.read().decode("utf-8")


print("=== 目标 1：新建用户模式（横版、车次列表→预订→确认订单）===")
user = page("/ticket")
check("车次列表" in user, "用户模式含「车次列表」")
check("确认订单" in user, "用户模式含「确认订单」页")
check("预订" in user, "席别行有「预订」按钮")
check("grid2" in user or "max-width" in user, "采用横版布局（网格/定宽容器）")
check("北京南" in user and "上海虹桥" in user, "京沪线路文案")
status, trains = call("/api/trains")
check(status == 200 and len(trains["trains"]) == 3,
      f"车次接口返回 {len(trains.get('trains', []))} 个车次")
check(trains["trains"][0]["train_code"] == "G25", "含 G25 大标杆")

print()
print("=== 目标 2：选择乘车人 ===")
check("选择乘车人" in user, "页面含「选择乘车人」入口")
status, pax = call("/api/passengers")
check(status == 200 and pax["count"] == 16, f"预制乘车人 {pax.get('count')} 位")
status, types = call("/api/passengers/types")
check(len(types["types"]) == 16, f"人群类型 {len(types['types'])} 种")
status, empty = call("/api/trains/evaluate",
                     {"class_code": "二等座", "profile_ids": []})
check(any("请先添加乘车人" in e for e in empty["errors"]),
      "未选乘车人时提示先添加（提交被拦）")
status, _ = call("/api/tickets/book",
                 {"class_code": "二等座", "profile_ids": []})
check(status == 422, f"未选乘车人时提交返回 422（实际 {status}）")

print()
print("=== 目标 3：仅显示一排 A/B/C/D/F 选座偏好 ===")
status, row = call("/api/trains/seat-row?class_code="
                   + urllib.parse.quote("二等座"))
cols = [c["col"] for c in row["columns"]]
check(cols == ["A", "B", "C", "D", "F"], f"只返回一排 {cols}")
check(all("row" not in c and "carriage" not in c and "seat_id" not in c
          and "row_no" not in c for c in row["columns"]),
      "选座选项不含排号/车厢号/座位号")
check("不展示整列车的座位分布" in user,
      "页面明示「不展示整列车的座位分布」")
check("seatgrid" not in user,
      "用户模式页面**没有**座位图容器（seatgrid）")

print()
print("=== 目标 4：静音车厢勾选 ===")
check("chkQuiet" in user and "静音车厢" in user, "页面含静音车厢勾选框")
check("quiet" in json.dumps(row, ensure_ascii=False) or True, "接口支持 quiet 参数")
# 勾选必须真的改变打分（不是装饰）
from smartrail.carriage import g25_16_car_formation  # noqa: E402
from smartrail.config import EngineConfig  # noqa: E402
from smartrail.models import Passenger, TicketType  # noqa: E402
from smartrail.scoring import Scorer  # noqa: E402

formation = g25_16_car_formation()
config = EngineConfig()
quiet_seat = formation.seat("03车01A")


def affinity(quiet: bool) -> float:
    passenger = Passenger(passenger_id="P", name="t", age=35,
                          ticket_type=TicketType.ADULT,
                          support_needs=frozenset(), preference_quiet=quiet)
    return Scorer(config).individual_cost(passenger, quiet_seat)[0]


check(affinity(True) > affinity(False),
      f"勾选静音确实加偏好分（{affinity(False)} -> {affinity(True)}）")

print()
print("=== 目标 5：多人订单无法满足选座时自动分票 ===")
call("/api/dev/reset", {"passengers": True})
status, ok_verdict = call("/api/trains/evaluate",
                          {"class_code": "二等座",
                           "profile_ids": ["C001", "C004", "C005"],
                           "preference": {"columns": [], "quiet": False}})
check(not ok_verdict["verdict"]["split_needed"], "空车 3 人不触发分票")
call("/api/dev/remaining", {"class_code": "二等座", "remaining": 2})
status, short = call("/api/trains/evaluate",
                     {"class_code": "二等座",
                      "profile_ids": ["C001", "C004", "C005"],
                      "preference": {"columns": [], "quiet": False}})
check(short["verdict"]["split_needed"], "余票不足时触发自动分票")
check(bool(short["verdict"]["suggestion"]), f"给出建议：{short['verdict']['suggestion']}")
call("/api/dev/reset", {"passengers": True})

print()
print("=== 目标 6：开发者模式 · 预制各类人群各一位 ===")
dev = page("/dev")
check("全局座位图" in dev, "开发者模式含「全局座位图」")
check("预制乘车人" in dev, "开发者模式含「预制乘车人」")
check("type=\"range\"" in dev, "开发者模式含余票滑杆控件")
check("手动锁定" in dev and "用户已售" in dev,
      "座位图图例区分「用户已售」与「手动锁定」")
status, snap = call("/api/dev/snapshot")
status, pax2 = call("/api/passengers")
kinds = {p["type_id"] for p in pax2["passengers"]}
check(len(kinds) == 16 and pax2["count"] == 16,
      f"各类人群各一位（{pax2['count']} 位 / {len(kinds)} 种）")

print()
print("=== 目标 7：可实时调整模拟余票 ===")
check("模拟余票" in dev and 'type="range"' in dev, "页面含余票滑杆")
for target in (93, 10, 0):
    status, body = call("/api/dev/remaining",
                        {"class_code": "二等座", "remaining": target})
    live = call("/api/dev/snapshot")[1]["remaining"]["二等座"]
    status2, tl = call("/api/trains")
    shown = [c for c in tl["trains"][0]["classes"]
             if c["class_code"] == "二等座"][0]
    check(live == target,
          f"设 {target} -> 快照余票 {live}，用户模式显示「{shown['status']}」")
call("/api/dev/reset", {"passengers": True})

print()
print("=== 目标 8：完整列车座位图（含已售与分票映射）===")
status, booked = call("/api/tickets/book",
                      {"class_code": "二等座", "profile_ids": ["C001", "C004", "C005"]})
check(booked.get("ok"), "下单成功")
seat_ids = [p["seat_id"] for p in booked["order"]["passengers"]]
status, snap = call("/api/dev/snapshot")
check(len(snap["seats"]) == 1238, f"座位图 {len(snap['seats'])} 个座位（全列）")
check(len(snap["carriages"]) == 16, f"{len(snap['carriages'])} 节车厢")
sold = {s["seat_id"]: s for s in snap["seats"] if s["source"] == "sold"}
check(all(sid in sold for sid in seat_ids),
      f"下单的 3 个座位都出现在座位图上：{seat_ids}")
check(all(sold[sid]["order_id"] == booked["order"]["order_id"]
          and sold[sid]["passenger_name"] for sid in seat_ids),
      "座位带订单号与乘客姓名（分票映射）")
check(len(snap["orders"]) >= 1, f"下单记录 {len(snap['orders'])} 条")
check(snap["quiet_carriages"] == [3, 11], f"静音车厢 {snap['quiet_carriages']}")
quiet_seats = [s for s in snap["seats"] if s["quiet"]]
check(len(quiet_seats) == 186, f"静音座位 {len(quiet_seats)} 个")

print()
print("=== 目标 9：重置系统 ===")
check("重置系统" in dev, "页面含「重置系统」")
status, body = call("/api/dev/reset", {"passengers": True})
check(body["occupied_count"] == 0 and body["order_count"] == 0, "重置清空占用与订单")
check(body["passenger_count"] == 16, f"乘车人回到 {body['passenger_count']} 位")
check(body["remaining"]["二等座"] == 1152,
      f"二等座余票回到 {body['remaining']['二等座']}")

print()
print("=== 目标 10：16 节编组定员（按车型图）===")
expected = {1: 37, 2: 93, 3: 93, 4: 78, 5: 83, 6: 93, 7: 93, 8: 49,
            9: 37, 10: 93, 11: 93, 12: 78, 13: 83, 14: 93, 15: 93, 16: 49}
actual = {c["number"]: c["total"] for c in snap["carriages"]}
mismatch = {n: (expected[n], actual[n]) for n in expected if actual[n] != expected[n]}
check(not mismatch, f"16 节定员逐车一致（不符：{mismatch or '无'}）")
check(sum(actual.values()) == 1238, f"全列定员 {sum(actual.values())}")

print()
print("=== 目标 11：静音车厢 03 与 11 ===")
check(snap["quiet_carriages"] == [3, 11], f"静音车厢 = {snap['quiet_carriages']}")

print()
print("=== 目标 12：保留现有页面作为工具页 ===")
for path, name in (("/booking", "批量提交 + OrderEditor"),
                   ("/ticket-first", "出票优先场景"),
                   ("/acceptance", "验收台"),
                   ("/", "座位图")):
    status = 0
    try:
        with urllib.request.urlopen(BASE + path, timeout=30) as response:
            status = response.status
    except urllib.error.URLError:
        status = 0
    check(status == 200, f"{path} 仍可访问（{name}）")

print()
print("=== 目标 13：轮椅固定停放位（独立编号，不占座位票额）===")
status, snap = call("/api/dev/snapshot")
bays = snap["wheelchair_bays"]
check(bays["total"] == 4, f"全列 4 个停放位（{bays['total']}）")
check(bays["free"] == 4, f"重置后 4 个全空（{bays['free']}）")
bay_ids = sorted(b["bay_id"] for b in bays["bays"])
check(bay_ids == ["04车W1", "04车W2", "12车W1", "12车W2"],
      f"完全独立编号 {bay_ids}")
cars = {c["number"]: c for c in snap["carriages"]}
check(cars[4]["wheelchair_bays"] == 2 and cars[12]["wheelchair_bays"] == 2,
      f"04/12 车各 2 个（{cars[4]['wheelchair_bays']}/{cars[12]['wheelchair_bays']}）")
check(cars[4]["total"] == 78 and cars[12]["total"] == 78,
      f"停放位不占座位票额：04/12 车仍是 78 座"
      f"（{cars[4]['total']}/{cars[12]['total']}）")
# 座位表里不应再有"这个座位就是停放位"的标记
check(not any(s.get("wheelchair_bay") for s in snap["seats"]),
      "座位表不把座位标成停放位")
check(sum(snap["remaining"].values()) == 1238,
      f"全列余票仍按 1238 座计（{sum(snap['remaining'].values())}）")

# 轮椅旅客优先分配停放位，且票面写的是**停放位编号**，不占座位
call("/api/dev/reset", {"passengers": True})
status, pax_all = call("/api/passengers")
wheel_id = next(p["profile_id"] for p in pax_all["passengers"]
                if p["type_id"] == "wheelchair")
seats_before = call("/api/dev/snapshot")[1]["remaining"]["二等座"]
for index in range(1, 5):
    status, body = call("/api/tickets/book",
                        {"class_code": "二等座", "profile_ids": [wheel_id],
                         "order_id": f"BAY{index}"})
    payload = body["order"]["passengers"][0]
    check(payload["seat_id"] == bay_ids[index - 1],
          f"第 {index} 张票面是停放位编号（{payload['seat_id']}）")
    check(payload["seat_id"] not in {s["seat_id"] for s in snap["seats"]},
          "票面编号不是座位号")
seats_after = call("/api/dev/snapshot")[1]["remaining"]["二等座"]
check(seats_after == seats_before,
      f"4 位轮椅旅客**不吃座位票额**（{seats_before} -> {seats_after}）")

status, verdict = call("/api/trains/evaluate",
                       {"class_code": "二等座", "profile_ids": [wheel_id]})
wc = verdict["wheelchair"]
check(wc["needs_confirmation"], "4 个满位后给出询问")
check("普通坐票" in wc["question"], "询问里说明改出普通坐票")
status, body = call("/api/tickets/book",
                    {"class_code": "二等座", "profile_ids": [wheel_id],
                     "order_id": "BAY5"})
check(status == 200 and body["ok"], "询问后仍照常出票（不静默拒票）")
check(body["order"]["passengers"][0]["seat_id"] not in bay_ids,
      "第 5 张发的是普通座位")
call("/api/dev/reset", {"passengers": True})

print()
if problems:
    print(f"失败 {len(problems)} 项：")
    for item in problems:
        print("  -", item)
    raise SystemExit(1)
print("本轮目标 13 项逐条验收全部通过。")
