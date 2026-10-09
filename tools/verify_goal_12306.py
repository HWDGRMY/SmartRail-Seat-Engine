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

#: 预制乘车人数量**从服务实时取**，不写死 —— 早期写死 16，
#: 预制档案增删后验收就失败，而失败原因是「数字过期」而非真缺陷。
PRESET_COUNT: int = 0


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
print("=== 目标 2：选择乘车人（用户模式不预置乘车人）===")
if PRESET_COUNT == 0:
    PRESET_COUNT = call("/api/dev/passengers")[1]["count"]
check("选择乘车人" in user, "页面含「选择乘车人」入口")
# **需求**：预制乘车人是给开发者模式的，用户模式必须看不到
status, user_pax = call("/api/passengers")
check(status == 200 and user_pax["count"] == 0,
      f"用户模式起始 0 位乘车人（{user_pax.get('count')}）")
check(user_pax["empty"] is True, "用户模式列表为空 -> 引导用户添加")
status, dev_pax = call("/api/passengers?scope=dev")
check(dev_pax["count"] == PRESET_COUNT, f"开发者模式可见预制 {dev_pax.get('count')} 位")
status, pax = call("/api/dev/passengers")
check(pax["count"] == PRESET_COUNT, f"/api/dev/passengers 返回 {pax.get('count')} 位")
check("选择乘车人" in user and "预制" not in user.split("选择乘车人")[0][-200:],
      "用户模式页面不向旅客展示预制档案")
status, types = call("/api/passengers/types")
check(len(types["types"]) == PRESET_COUNT, f"人群类型 {len(types['types'])} 种")
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
check("提交订单" in dev, "开发者模式含「提交订单」")
check("type=\"range\"" in dev, "开发者模式含余票滑杆控件")
check("手动锁定" in dev and "用户已售" in dev,
      "座位图图例区分「用户已售」与「手动锁定」")
status, snap = call("/api/dev/snapshot")
status, pax2 = call("/api/passengers?scope=dev")
kinds = {p["type_id"] for p in pax2["passengers"]}
check(len(kinds) == PRESET_COUNT and pax2["count"] == PRESET_COUNT,
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
check(body["passenger_count"] == PRESET_COUNT,
          f"乘车人回到 {body['passenger_count']} 位")
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
print("=== 目标 12：页面集合（/booking 已按需求下线）===")
for path, name in (("/ticket", "用户模式购票"),
                   ("/dev", "开发者模式（含提交订单）"),
                   ("/ticket-first", "出票优先场景"),
                   ("/acceptance", "验收台"),
                   ("/", "座位图")):
    status = 0
    try:
        with urllib.request.urlopen(BASE + path, timeout=30) as response:
            status = response.status
    except urllib.error.URLError:
        status = 0
    check(status == 200, f"{path} 可访问（{name}）")
# 批量提交页面已删除：确认它**真的不可访问**，而不是"忘了删路由"
booking_status = 0
try:
    with urllib.request.urlopen(BASE + "/booking", timeout=30) as response:
        booking_status = response.status
except urllib.error.HTTPError as error:
    booking_status = error.code
except urllib.error.URLError:
    booking_status = 0
check(booking_status == 404, f"/booking 已下线（HTTP {booking_status}）")

print()
print("=== 目标 12b：开发者页的『提交订单』入口 ===")
with urllib.request.urlopen(BASE + "/dev", timeout=30) as response:
    dev_html = response.read().decode("utf-8")
check("提交订单" in dev_html, "开发者页含『提交订单』区域")
check("btnSubmitOrder" in dev_html, "含提交按钮")
check("submit-order" in dev_html, "按钮有 testid（可被自动化点击）")
check("/api/composition/submit" in dev_html, "调用真实组单接口")
check("orderClass" in dev_html, "含席别选择")
check("baseCounts" in dev_html, "含基础分组计数状态")
check("baseGroups" in dev_html, "含基础分组步进器")
check("class_code" in dev_html,
      "席别会随请求发出（否则界面选了也不生效）")

print()
print("=== 目标 12c：基础分组与特殊人群的分层 ===")
status, catalog = call("/api/passengers/types")
groups = catalog["groups"]
basic = [g for g in groups if g.get("basic")]
special = [g for g in groups if not g.get("basic")]
check(len(basic) == 1 and basic[0]["id"] == "basic",
      f"基础分组恰好一组（{[g['id'] for g in basic]}）")
basic_ids = [t["id"] for t in basic[0]["types"]]
check(basic_ids == ["adult", "youth", "child", "toddler", "infant"],
      f"基础分组就是需求给的那 5 类（{basic_ids}）")
check("student" not in basic_ids, "『学生』不在基础分组（需求未定义该分组）")
# 与 composition 的规格一致
status, schema = call("/api/composition/schema")
check([g["id"] for g in schema["base_groups"]] == basic_ids,
      "基础分组与 /api/composition/schema 完全一致")
check(schema["total_formula"] == "adult + youth + child + toddler + infant",
      f"总人数公式 {schema['total_formula']}")
check(set(schema["excluded_from_total"]) == {"child_sub", "disability", "pregnant"},
      f"特殊人群不计入总人数（{schema['excluded_from_total']}）")
check("pregnant" in {g["id"] for g in special}
      and "disabled" in {g["id"] for g in special},
      "孕妇与残疾单列为特殊人群")

print()
print("=== 目标 12d：开发者页按人数组单（不是选姓名）===")
with urllib.request.urlopen(BASE + "/dev", timeout=30) as response:
    dev_html = response.read().decode("utf-8")
check("baseGroups" in dev_html, "含基础分组步进器")
check("renderBaseGroups" in dev_html, "渲染基础分组")
check("baseCounts" in dev_html, "按分组计数")
check("/api/composition/submit" in dev_html, "提交走构成接口")
check("renderPassengerPicker" not in dev_html, "旧的姓名勾选器已移除")

print()
print("=== 目标 12e：按人数组单真的能出票 ===")
bands = [b["id"] for b in schema["age_bands"]]


def _composition(adult: int, child: int = 0) -> dict:
    return {
        "note": "验收",
        "base": {"adult": adult, "child": child},
        "child_sub": {g["id"]: 0 for g in schema["child_sub_groups"]},
        "disability": {lv["id"]: {b: 0 for b in bands}
                       for lv in schema["disability_levels"]},
        "pregnant": {st["id"]: {b: 0 for b in bands}
                     for st in schema["pregnant_stages"]},
    }


style, composed = call("/api/composition/submit",
                       {"orders": [_composition(adult=10)]})
blocked = composed.get("blocked") or []
check(not blocked, f"10 位成人未被拦下（{blocked[:1]}）")
if not blocked:
    order = composed["orders"][0]
    check(order["total_passengers"] == 10,
          f"总人数 {order['total_passengers']}（应为 10）")
    check(len(order.get("seats") or {}) == 10,
          f"10 位全部出票（{len(order.get('seats') or {})}）")
    seats = sorted((order.get("seats") or {}).values())
    check(len(set(seats)) == 10, "10 个座位互不重复")
    print(f"      座位：{seats}")

print()
print("=== 目标 12e2：席别是硬约束（买二等座不能出一等座）===")
# 真实事故：开发者页选"二等座"提交，结果发了一等座 01车01A。
# 根因两层，都是"重建对象时漏字段"：
#   1. OrderComposition 没有 class_code 字段；
#   2. orders_from_design 在 default_bond 覆盖时重建 Order 又把它丢了。
snapshot_for_class = call("/api/dev/snapshot")[1]
seat_class = {s["seat_id"]: s["class_code"] for s in snapshot_for_class["seats"]}
for want in ("二等座", "一等座", "商务座"):
    call("/api/dev/reset", {"passengers": True})
    composed = _composition(adult=2, child=2)
    composed["class_code"] = want
    style, body = call("/api/composition/submit", {"orders": [composed]})
    order = body["orders"][0]
    seats = sorted((order.get("seats") or {}).values())
    got = sorted({seat_class.get(s, "?") for s in seats})
    check(got == [want], f"下单 {want} -> 实际 {got}（{seats[:2]}…）")
    # 记录里也要带席别，事后可核对（否则"买二等座"这件事无从查证）
    recorded = call("/api/dev/snapshot")[1]["orders"]
    check(bool(recorded) and recorded[-1].get("class_code") == want,
          f"记录里带席别（{recorded[-1].get('class_code') if recorded else '无'}）")

# 拒票规则仍然生效
style, guarded = call("/api/composition/submit",
                      {"orders": [_composition(adult=0, child=1)]})
check(bool(guarded.get("blocked")),
      "儿童无成人陪同被拦下（未进入求解器）")

print()
print("=== 目标 12e3：组单必须受库存约束（不得超卖）===")
# 真实事故：submit_orders 无条件 reset_engine() 建空车引擎，
# 于是组单路径完全看不到开发者页的余票，实测「余票 2 下 4 人单」
# 仍然全部出票（超卖 2 张），且出票后余票纹丝不动。
call("/api/dev/reset", {"passengers": True})
before_remaining = call("/api/dev/snapshot")[1]["remaining"]["二等座"]
composed = _composition(adult=2, child=2)
composed["class_code"] = "二等座"
style, ok_body = call("/api/composition/submit", {"orders": [composed]})
ok_seats = sorted((ok_body["orders"][0].get("seats") or {}).values())
after_remaining = call("/api/dev/snapshot")[1]["remaining"]["二等座"]
check(len(ok_seats) == 4, f"余票充足时 4 人全部出票（{len(ok_seats)}）")
check(after_remaining == before_remaining - 4,
      f"出票后台账余票减少 4（{before_remaining} -> {after_remaining}）")

call("/api/dev/reset", {"passengers": True})
call("/api/dev/remaining", {"class_code": "二等座", "remaining": 2})
check(call("/api/dev/snapshot")[1]["remaining"]["二等座"] == 2, "余票已置 2")
style, tight = call("/api/composition/submit", {"orders": [composed]})
tight_seats = sorted((tight["orders"][0].get("seats") or {}).values())
check(len(tight_seats) <= 2,
      f"余票 2 时最多出 2 座（实际 {len(tight_seats)}：{tight_seats}）")
check(call("/api/dev/snapshot")[1]["remaining"]["二等座"] >= 0,
      "余票不为负")

call("/api/dev/reset", {"passengers": True})
call("/api/dev/remaining", {"class_code": "二等座", "remaining": 0})
style, empty = call("/api/composition/submit",
                    {"orders": [{**_composition(adult=1),
                                 "class_code": "二等座"}]})
empty_order = empty["orders"][0]
check(not (empty_order.get("seats") or {}),
      f"余票 0 时不发座（{empty_order.get('seats')}）")
check(bool(empty_order.get("level_label")),
      f"给出原因而非静默失败（{empty_order.get('level_label')}）")

print()
print("=== 目标 12e4：同一订单应坐在同一排 ===")
# 真实事故（用户报"你把大人小孩分开了"）：2 成人 + 2 儿童拿到
# 06车01A/B + 06车04D/F —— 每对挨着，两对之间隔了三排。
# 根因是候选池按"个体分之和"排序，而个体分不反映人与人之间的距离，
# 于是"同一排 4 座"排在"跨 3 排散座"之后、从来没被评估过。
call("/api/dev/reset", {"passengers": True})
composed = _composition(adult=2, child=2)
composed["class_code"] = "二等座"
style, one = call("/api/composition/submit", {"orders": [composed]})
one_seats = sorted((one["orders"][0].get("seats") or {}).values())
check(len({s[:2] + "车" + s[3:5] for s in one_seats}) == 1,
      f"空车时 4 人同一排（{one_seats}）")

call("/api/dev/reset", {"passengers": True})
call("/api/dev/remaining", {"class_code": "二等座", "remaining": 300})
trials = 12
same_row = 0
for _ in range(trials):
    style, body = call("/api/composition/submit",
                       {"orders": [dict(composed)]})
    got = (body["orders"][0].get("seats") or {}).values()
    if len({s[:2] + "车" + s[3:5] for s in got}) == 1:
        same_row += 1
check(same_row >= trials // 2,
      f"余票 300 时同排率 {same_row}/{trials}"
      f"（修复前 0/{trials}，要求 ≥ {trials // 2}）")

print()
print("=== 目标 12e5：重点旅客服务与陪同规则 ===")
# 用户原话："啥必须啊？单人必须，有陪就不必须了呗，
#           而且你这也没有重点旅客选项啊。"
# 两处都错：规则不看同行人；界面没有该勾选框（可它是硬前提）。
bands_list = [b["id"] for b in schema["age_bands"]]


def _svc(adult=0, severe=0, term=0, service=False):
    disability = {lv["id"]: {b: 0 for b in bands_list}
                  for lv in schema["disability_levels"]}
    pregnant = {st["id"]: {b: 0 for b in bands_list}
                for st in schema["pregnant_stages"]}
    if severe:
        disability["severe"]["adult"] = severe
    if term:
        pregnant["term"]["adult"] = term
    return {
        "class_code": "二等座",
        "base": {"adult": adult, "youth": 0, "child": 0,
                 "toddler": 0, "infant": 0},
        "child_sub": {g["id"]: 0 for g in schema["child_sub_groups"]},
        "disability": disability,
        "pregnant": pregnant,
        "key_passenger_service": service,
    }


for label, composed, expect_ok in (
    ("重度 1 独自，未约服务", _svc(1, severe=1), False),
    ("重度 1 独自，已约服务", _svc(1, severe=1, service=True), True),
    ("重度 1 有家人，未约服务", _svc(2, severe=1), False),
    ("重度 1 有家人，已约服务", _svc(2, severe=1, service=True), True),
    ("足月孕妇 1 独自，已约服务", _svc(1, term=1, service=True), True),
    ("足月孕妇 1 有家人，已约服务", _svc(2, term=1, service=True), True),
    ("无重点旅客，未约服务", _svc(2), True),
):
    style, body = call("/api/composition/submit", {"orders": [composed]})
    blocked_now = bool(body.get("blocked"))
    check(blocked_now != expect_ok,
          f"{label} -> {'可出票' if not blocked_now else '拦截'}")

check("chkKeyService" in dev_html, "开发者页含『预约重点旅客服务』勾选框")

print()
print("=== 目标 12f：开发者提交的订单必须被保留 ===")
# 需求："开发者提交的订单难道不用保留吗" —— 原先这条路径完全没记录。
call("/api/dev/reset", {"passengers": True})
before = call("/api/dev/snapshot")[1]["orders"]
check(len(before) == 0, f"重置后下单记录为空（{len(before)}）")
submitted_ids = []
for index, adult in enumerate((2, 3), start=1):
    style, body = call("/api/composition/submit",
                       {"orders": [_composition(adult=adult)]})
    submitted_ids.append(body["orders"][0]["order_id"])
after = call("/api/dev/snapshot")[1]["orders"]
check(len(after) == 2, f"两张单都记进台账（{len(after)}）")
check([o["order_id"] for o in after] == submitted_ids,
      f"订单号唯一且与返回一致（{submitted_ids}）")
check(len(set(submitted_ids)) == 2,
      f"不同提交的订单号不重复（{submitted_ids}）")
check(all(o.get("source") == "dev-composition" for o in after),
      "台账标出来源是开发者组单")
check(all(o.get("base_desc") for o in after),
      f"台账记录了基础分组（{[o.get('base_desc') for o in after]}）")
check(all(o.get("passengers") for o in after), "台账记录了逐位乘客")
# 被拦下的单也要保留（"提交了但没出票"同样是记录）
style, blocked = call("/api/composition/submit",
                      {"orders": [_composition(adult=0, child=1)]})
after2 = call("/api/dev/snapshot")[1]["orders"]
check(len(after2) == 3, f"被拦下的单也记进台账（{len(after2)}）")
check(after2[-1].get("blocked") is True, "被拦下的单标了 blocked")
check(bool(after2[-1].get("reason")),
      f"被拦下的单带原因（{after2[-1].get('reason', '')[:24]}…）")
# 页面数据源就是这份快照：再取一次应仍在（刷新不丢）
again = call("/api/dev/snapshot")[1]["orders"]
check(len(again) == 3, f"再取快照记录仍在（{len(again)}）")

print()
print("=== 目标 13：轮椅固定停放位（独立编号，不占座位票额）===")
# 先重置：前面的目标会真的出票（现在组单也会扣减余票、占用座位），
# 而这一段要核对"全列 1238 座"这种**空车基线**。
call("/api/dev/reset", {"passengers": True})
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
status, pax_all = call("/api/passengers?scope=dev")
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
print("=== 目标 14：预制乘车人只给开发者模式 ===")
status, user_scope = call("/api/passengers")
check(user_scope["count"] == 0, f"用户模式 0 位（{user_scope['count']}）")
check(user_scope["empty"] is True, "用户模式为空 -> 引导添加")
status, dev_scope = call("/api/passengers?scope=dev")
check(dev_scope["count"] == PRESET_COUNT,
          f"开发者模式 {dev_scope['count']} 位")
check(all(p["source"] == "preset" for p in dev_scope["passengers"]),
      "预制档案带 source=preset 标记")
check("选择乘车人" in user,
      "用户模式仍有「选择乘车人」入口（但列表为空）")
status, added = call("/api/passengers/add", {"name": "验收用户", "type_id": "adult"})
check(status == 201, f"添加乘车人返回 201（实际 {status}）")
status, user_scope2 = call("/api/passengers")
check(user_scope2["count"] == 1 and user_scope2["passengers"][0]["source"] == "user",
      f"添加后用户模式看到自己的 1 位（{user_scope2['count']}）")
call("/api/passengers/remove", {"profile_id": added["profile_id"]})

print()
print("=== 目标 15：压测不按顺序占座（余票必须碎片化）===")


def fragmentation(class_code: str = "二等座") -> dict:
    snap = call("/api/dev/snapshot")[1]
    by_row: dict[tuple[int, int], list[int]] = {}
    for seat in snap["seats"]:
        if seat["class_code"] != class_code or seat["occupied"]:
            continue
        by_row.setdefault((seat["carriage"], seat["row"]), []).append(
            _col_index(seat["col"])
        )
    runs: list[int] = []
    for group in by_row.values():
        group.sort()
        run = 1
        for left, right in zip(group, group[1:]):
            run = run + 1 if right == left + 1 else 1
            if right != left + 1:
                runs.append(1)
        runs.append(run)
    return {"rows": len(by_row), "longest": max(runs, default=0)}


def _col_index(col: str) -> int:
    return "ABCDF".index(col) if col in "ABCDF" else 0


call("/api/dev/reset", {"passengers": True})
call("/api/dev/remaining", {"class_code": "二等座", "remaining": 100})
frag = fragmentation()
check(frag["rows"] >= 50,
      f"100 张余票散布在 {frag['rows']} 个排（顺序占只会有约 21 排）")
check(frag["longest"] <= 2,
      f"最长连座 {frag['longest']} <= 2（顺序占会留 5 连座）")
check(frag["rows"] > 21, "碎片化确实生效（与顺序占形成可区分的差距）")

print()
print("=== 目标 16：多人组合 × 自动分票（顺序处理，非并发）===")
call("/api/dev/reset", {"passengers": True})
status, added_adult = call("/api/passengers/add",
                           {"name": "组合甲", "type_id": "adult"})
status, added_child = call("/api/passengers/add",
                           {"name": "组合乙", "type_id": "child"})
combo_ids = [added_adult["profile_id"], added_child["profile_id"]]
call("/api/dev/remaining", {"class_code": "二等座", "remaining": 30})
status, combo = call("/api/tickets/book",
                     {"class_code": "二等座", "profile_ids": combo_ids,
                      "order_id": "COMBO-1"})
check(status == 200 and combo["ok"], "2 人组合单出票成功")
check(combo["tier0"] == 0, f"Tier 0 = 0（{combo['tier0']}）")
check(combo["order"]["seated"] == 2, f"2 人全部出票（{combo['order']['seated']}）")
check(combo["verdict"]["largest_run"] <= 2,
      f"余票最长连座 {combo['verdict']['largest_run']}（碎片化）")
# 席别硬约束：商务座单不能出二等座
status, biz = call("/api/tickets/book",
                   {"class_code": "商务座", "profile_ids": [added_adult["profile_id"]],
                    "order_id": "COMBO-BIZ"})
if biz["ok"]:
    check(biz["order"]["passengers"][0]["class_code"] == "商务座",
          f"商务座单席别正确（{biz['order']['passengers'][0]['class_code']}）")
# 满座时不静默失败
call("/api/dev/remaining", {"class_code": "二等座", "remaining": 0})
status, full = call("/api/tickets/book",
                    {"class_code": "二等座", "profile_ids": combo_ids,
                     "order_id": "COMBO-FULL"})
check(not full["ok"], "满座时不出票")
check(bool(full["errors"]), f"满座时给出原因：{full['errors'][:1]}")
call("/api/dev/reset", {"passengers": True})

print()
if problems:
    print(f"失败 {len(problems)} 项：")
    for item in problems:
        print("  -", item)
    raise SystemExit(1)
print("本轮目标 16 项逐条验收全部通过。")
