"""12306 风格购票流程的验收：乘车人、余票、选座判定、自动分票、台账联动。

对应需求逐条守护：

* 预制各类人群各一位，作为默认购票人；
* 添加乘车人时选择人群类型；**必须先添加乘车人才能提交**；
* 用户端**不可见**整列座位图，选座服务**只显示一排 A/B/C/D/F**；
* 提供"静音车厢"勾选项；
* **仅当多人订单且余票无法满足选座要求时**，自动分票逻辑才介入；
* 开发者改余票**实时影响**用户模式；全局座位图展示已售与分票映射。

双模式：``python tests/test_ticketing.py`` 或 ``pytest``。
"""

from __future__ import annotations

import os
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from smartrail.ticketing import (  # noqa: E402
    SeatPreference,
    available_trains,
    book_ticket_order,
    build_order_from_profiles,
    build_profile_order,
    get_passenger_store,
    passenger_type_catalog,
    reset_dev_store,
    reset_passenger_store,
    seat_row_options,
)
from smartrail.ticketing.booking import evaluate_preference  # noqa: E402
from smartrail.ticketing.devstore import SOURCE_SOLD  # noqa: E402
from smartrail.ticketing.passenger_store import (  # noqa: E402
    PASSENGER_TYPE_GROUPS,
)

# 静音/偏好相关的验收需要直接对分数，这里导入一次供多个用例复用
from smartrail.carriage import g25_16_car_formation  # noqa: E402
from smartrail.config import EngineConfig  # noqa: E402
from smartrail.models import Passenger, TicketType  # noqa: E402
from smartrail.scoring import Scorer  # noqa: E402

UNDER_PYTEST = "PYTEST_CURRENT_TEST" in os.environ or "pytest" in sys.modules
_FAILURES: list[str] = []


def check(condition: bool, message: str) -> None:
    if condition:
        print(f"  PASS  {message}")
        return
    print(f"  FAIL  {message}")
    if UNDER_PYTEST:
        raise AssertionError(message)
    _FAILURES.append(message)


def test_default_profiles_cover_every_type() -> None:
    """预制乘车人：各类人群各一位。"""
    print("[购票] 预制乘车人")
    store = reset_passenger_store()
    profiles = store.all()
    catalog = passenger_type_catalog()
    check(len(profiles) == len(catalog["types"]),
          f"预制 {len(profiles)} 位 = 类型目录 {len(catalog['types'])} 种")
    kinds = Counter(profile.type_id for profile in profiles)
    duplicates = {k: v for k, v in kinds.items() if v > 1}
    check(not duplicates, f"每种人群恰好一位（重复：{duplicates or '无'}）")
    groups = catalog["groups"]
    check(len(groups) == len(PASSENGER_TYPE_GROUPS),
          f"分组数与定义一致（{len(groups)}）")
    check(sum(1 for g in groups if g.get("basic")) == 1,
          "恰好一个基础分组")
    # 每个分组都要有 hint 与 types，否则界面会渲染出空壳
    for group in groups:
        check(bool(group.get("hint")), f"『{group['label']}』有说明文案")
        check(bool(group["types"]), f"『{group['label']}』非空")


def test_add_passenger_gets_fresh_id() -> None:
    """新增乘车人的编号必须接在预制档案之后，不能撞号。

    编号**从预制档案数量推导**，不写死 —— 早期写死 ``C017``，
    预制档案增删之后就失效了（现在预制 18 位，下一位是 C019）。
    """
    print("[购票] 添加乘车人")
    store = reset_passenger_store()
    preset_count = len(store.presets())
    added = store.add({"name": "测试旅客", "type_id": "child"})
    expect = f"C{preset_count + 1:03d}"
    check(added.profile_id == expect,
          f"新编号接在 {preset_count} 位预制之后（{added.profile_id}，期望 {expect}）")
    check(store.get(added.profile_id) is not None, "新乘车人可查回")
    check(store.get(added.profile_id).source == "user",
          "新增的标记为 user 来源（用户模式可见）")
    check(len(store.user_added()) == 1, "用户添加列表里有 1 位")
    check(len(store.all()) == preset_count + 1, "全部档案多了 1 位")
    check(added.resolves_needs_caregiver(), "儿童标记为需照护")
    try:
        store.add({"name": "", "type_id": "adult"})
        check(False, "空姓名应报错")
    except ValueError as error:
        check("姓名" in str(error), f"空姓名被拒（{error}）")
    store.reset()
    check(store.all()[0].profile_id == "C001", "重置后回到预制档案")


def test_train_list_has_no_seat_map() -> None:
    """车次列表只有余票数字，**不含**座位分布图。"""
    print("[购票] 车次列表")
    dev = reset_dev_store()
    trains = available_trains(dev)
    check(len(trains) == 3, f"{len(trains)} 个车次")
    first = trains[0]
    check(first["train_code"] == "G25", f"首个车次 {first['train_code']}")
    check(first["total_seats"] == 1238, f"总定员 {first['total_seats']}")
    check("seats" not in first, "车次列表不含座位分布图")
    check(first["quiet_carriages"] == [3, 11],
          f"静音车厢 {first['quiet_carriages']}")
    check(all("row" not in c for c in first["classes"]), "席别条目不含排号")


def test_seat_row_options_is_one_row_only() -> None:
    """选座服务只显示一排 A/B/C/D/F。"""
    print("[购票] 选座服务")
    dev = reset_dev_store()
    row = seat_row_options(dev, "二等座")
    check([c["col"] for c in row] == ["A", "B", "C", "D", "F"],
          f"列顺序 {[c['col'] for c in row]}")
    check(all("row" not in c and "carriage" not in c and "seat_id" not in c
              for c in row),
          "选项不含排号/车厢号/座位号")
    features = {c["col"]: c["feature"] for c in row}
    check(features["A"] == "window" and features["F"] == "window", "A/F 靠窗")
    check(features["C"] == "aisle" and features["D"] == "aisle", "C/D 过道")
    check(features["B"] == "middle", "B 中间")


def test_no_split_when_adjacent_available() -> None:
    """空车时多人订单能同排，**不应**触发自动分票。"""
    print("[购票] 余票充足时不自动分票")
    dev = reset_dev_store()
    store = reset_passenger_store()
    verdict = evaluate_preference(dev, "二等座", 1, SeatPreference())
    check(not verdict.split_needed, f"单人：{verdict.reason}")
    family = store.by_ids(["C001", "C005", "C003"])   # 成人 + 儿童 + 老人
    verdict = evaluate_preference(dev, "二等座", len(family), SeatPreference())
    check(not verdict.split_needed,
          f"{len(family)} 人空车同排可行（最长连续 {verdict.largest_run}）")


def test_split_only_when_short() -> None:
    """余票不足时才自动分票，并给出建议。"""
    print("[购票] 余票不足才自动分票")
    dev = reset_dev_store()
    dev.set_remaining("二等座", 2)
    verdict = evaluate_preference(dev, "二等座", 6, SeatPreference())
    check(verdict.split_needed, f"6 人只剩 2 张 -> 分票（{verdict.reason}）")
    check(bool(verdict.suggestion), f"给出建议：{verdict.suggestion}")
    check("改选" in verdict.suggestion or "候补" in verdict.suggestion,
          "建议内容可操作")


def test_quiet_preference_changes_scoring() -> None:
    """勾选静音必须**真的改变打分** —— 否则勾选框就是装饰。

    这里的验证方式很关键。最初我写的是"勾选静音后，分配结果落在 03/11 车"，
    但那是**弱验证**：静音车厢恰好是遍历顺序最靠前的，不勾选也会分到静音座，
    所以断言恒真、什么也没证明。

    真正能区分的做法是**对同一个座位、只改偏好，比分数**：
    座位的物理属性完全一样，差异只可能来自偏好项本身。
    """
    print("[购票] 静音偏好改变打分")
    formation = g25_16_car_formation()
    config = EngineConfig()
    quiet_seat = formation.seat("03车01A")     # 静音车厢

    def total(quiet_pref: bool) -> float:
        passenger = Passenger(
            passenger_id="P1", name="测试", age=35, ticket_type=TicketType.ADULT,
            support_needs=frozenset(), preference_quiet=quiet_pref,
        )
        cost, _ = Scorer(config).individual_cost(passenger, quiet_seat)
        return cost

    without = total(False)
    with_pref = total(True)
    check(with_pref > without,
          f"勾选后静音座位的奖励更高（{without} -> {with_pref}）")
    check(with_pref - without > 0, "偏好项确实被计入（差值 > 0）")

    # 非静音座位不应因该偏好得到任何加分
    plain_seat = formation.seat("02车01A")
    passenger = Passenger(
        passenger_id="P1", name="测试", age=35, ticket_type=TicketType.ADULT,
        support_needs=frozenset(), preference_quiet=True,
    )
    plain_cost, plain_terms = Scorer(config).individual_cost(passenger, plain_seat)
    check(plain_cost <= 0 or not any(
        "静音" in term.detail for term in plain_terms),
        f"非静音座位不因该偏好加分（cost={plain_cost}）")

    # 偏好通过 build_order_from_profiles 传进乘客对象
    store = reset_passenger_store()
    order = build_order_from_profiles(
        store.by_ids(["C001"]), order_id="TQ",
        preference=SeatPreference(columns=["A", "F"], quiet=True),
    )
    passenger = order.passengers[0]
    check(passenger.preference_quiet is True, "偏好写入乘客对象")
    check(passenger.preference_window is True, "A/F 映射为靠窗偏好")
    plain = build_order_from_profiles(store.by_ids(["C001"]), order_id="TQ2")
    check(plain.passengers[0].preference_quiet is False,
          "未勾选时不带静音偏好")


def test_quiet_preference_is_bounded() -> None:
    """偏好加成不得突破"约束优先于偏好"的奖励上限。

    ``individual_cost`` 返回的是**亲和度**（越大越好，奖励为正、惩罚为负），
    奖励总额被 ``max_reward_per_passenger`` 钳制，
    保证任何偏好都无法抵消最低一级惩罚（Tier 4 = 10 分）。
    这是设计原则的机器可验证不变量。

    注意这里刻意加了"反证"断言：**至少有一个座位真的拿到了奖励**。
    否则"奖励 <= 上限"在奖励恒为 0 时也成立，整条测试就是空转 ——
    而空转的断言比没有断言更糟，它会让人以为验证过了。
    """
    print("[购票] 偏好加成有上限")

    def affinity(seat_id: str) -> tuple[float, list]:
        passenger = Passenger(
            passenger_id="P1", name="测试", age=35, ticket_type=TicketType.ADULT,
            support_needs=frozenset(),
            preference_quiet=True, preference_window=True, preference_aisle=True,
        )
        return Scorer(config).individual_cost(passenger, formation.seat(seat_id))

    config = EngineConfig()
    formation = g25_16_car_formation()
    values: list[float] = []
    for seat_id in ("03车01A", "02车01A", "01车01A", "08车01A"):
        cost, _ = affinity(seat_id)
        values.append(cost)
        check(cost <= config.max_reward_per_passenger,
              f"{seat_id} 亲和度 {cost} <= 上限 {config.max_reward_per_passenger}")
    check(any(value > 0 for value in values),
          f"确实有座位拿到奖励（{values}）—— 否则上限断言是空转")
    check(max(values) == config.max_reward_per_passenger,
          f"静音+靠窗+过道叠加到顶时恰被钳制在 {config.max_reward_per_passenger}"
          f"（实际 {max(values)}）")


def test_booking_writes_ledger() -> None:
    """下单出票后，座位与订单都要进台账。"""
    print("[购票] 下单写入台账")
    dev = reset_dev_store()
    store = reset_passenger_store()
    check(len(dev.occupied) == 0, "初始无占用")
    result = book_ticket_order(
        dev, store.by_ids(["C001", "C005", "C003"]),
        class_code="二等座", preference=SeatPreference(), order_id="T1",
    )
    check(result["ok"], f"出票成功（{result['order']['seated']} 人）")
    check(result["order"]["seated"] == 3, "3 人全部出票")
    check(len(dev.occupied) == 3, f"台账 {len(dev.occupied)} 个占用")
    check(all(o.source == SOURCE_SOLD for o in dev.occupied.values()),
          "占用来源为 sold")
    check(all(o.order_id == "T1" for o in dev.occupied.values()), "归属订单 T1")
    check(len(dev.orders) == 1, f"订单记录 {len(dev.orders)} 条")
    seats = [p["seat_id"] for p in result["order"]["passengers"]]
    check(all(seats), f"每人都有座位：{seats}")
    check(all(p["carriage"] and p["col"] for p in result["order"]["passengers"]),
          "座位带车厢与列号（供开发者座位图映射）")


def test_remaining_updates_live() -> None:
    """余票随售出实时下降；开发者改余票实时影响用户模式。"""
    print("[购票] 余票实时联动")
    dev = reset_dev_store()
    store = reset_passenger_store()
    before = dev.remaining("二等座")["二等座"]
    book_ticket_order(dev, store.by_ids(["C002"]), class_code="二等座",
                      order_id="T2")
    after = dev.remaining("二等座")["二等座"]
    check(after == before - 1, f"售出 1 张：{before} -> {after}")

    for target, status in ((93, "有票"), (10, "10张"), (0, "无")):
        dev.set_remaining("二等座", target)
        live = dev.remaining("二等座")["二等座"]
        trains = available_trains(dev)
        second = [c for c in trains[0]["classes"] if c["class_code"] == "二等座"][0]
        check(live == target and second["status"] == status
              and second["bookable"] == (target > 0),
              f"设定 {target} -> 余票 {live}，显示「{second['status']}」，"
              f"可订={second['bookable']}")


def test_rejects_incomplete_selection() -> None:
    """未添加乘车人被拒；未满 14 岁单独购票被拒。"""
    print("[购票] 提交校验")
    dev = reset_dev_store()
    store = reset_passenger_store()
    empty = book_ticket_order(dev, [], class_code="二等座", order_id="T3")
    check(not empty["ok"], "空乘车人列表 -> 拒票")
    check("请先添加乘车人" in empty["errors"][0], f"原因：{empty['errors']}")

    child_only = book_ticket_order(
        dev, store.by_ids(["C005"]), class_code="二等座", order_id="T4",
    )
    check(not child_only["ok"], "儿童单独 -> 拒票")
    check(any("未满 14 周岁" in e for e in child_only["errors"]),
          f"原因：{child_only['errors']}")

    youth_only = book_ticket_order(
        dev, store.by_ids(["C004"]), class_code="二等座", order_id="T5",
    )
    check(youth_only["ok"], "青少年单独 -> 放行（14 岁整不拒票）")


def test_dev_snapshot_covers_whole_train() -> None:
    """开发者全局座位图：全列座位 + 静音/无障碍标记 + 分票映射。"""
    print("[开发者] 全局座位图")
    dev = reset_dev_store()
    dev.fill_to_ratio(0.35)
    snapshot = dev.snapshot()
    check(len(snapshot["seats"]) == 1238, f"座位图 {len(snapshot['seats'])} 个座位")
    check(snapshot["occupied_count"] > 300,
          f"已售 {snapshot['occupied_count']} 个")
    check(len(snapshot["carriages"]) == 16, f"车厢 {len(snapshot['carriages'])} 节")
    check(snapshot["quiet_carriages"] == [3, 11],
          f"静音车厢 {snapshot['quiet_carriages']}")
    occupied = [s for s in snapshot["seats"] if s["occupied"]]
    check(len(occupied) == snapshot["occupied_count"],
          "座位图占用数与计数一致")
    quiet_seats = [s for s in snapshot["seats"] if s["quiet"]]
    check(len(quiet_seats) == 186, f"静音座位 {len(quiet_seats)} 个")
    accessible = [s for s in snapshot["seats"] if s["accessible"]]
    check(len(accessible) >= 6, f"无障碍座位 {len(accessible)} 个")

    # 逐节车厢的定员与**构成**必须与车型图对得上。
    # 混合车厢（01车=一等32+商务5、08车=二等43+商务6）只显示"主席别 + 总定员"
    # 是看不出构成的，而需求给的车型图正是用 "32/5" 这种写法标注的。
    by_carriage = {c["number"]: c for c in snapshot["carriages"]}
    check(by_carriage[2]["total"] == 93, f"02 车定员 {by_carriage[2]['total']}")
    check(by_carriage[4]["total"] == 78, f"04 车定员 {by_carriage[4]['total']}")
    check(by_carriage[8]["total"] == 49, f"08 车定员 {by_carriage[8]['total']}")
    check(by_carriage[4]["accessible"] and by_carriage[12]["accessible"],
          "04/12 车标为无障碍车厢")
    expected_summary = {
        1: "一等座32+商务座5", 2: "二等座93", 3: "二等座93", 4: "二等座78",
        5: "二等座83", 6: "二等座93", 7: "二等座93", 8: "二等座43+商务座6",
        9: "一等座32+商务座5", 10: "二等座93", 11: "二等座93", 12: "二等座78",
        13: "二等座83", 14: "二等座93", 15: "二等座93", 16: "二等座43+商务座6",
    }
    mismatch = {
        number: (want, by_carriage[number].get("class_summary"))
        for number, want in expected_summary.items()
        if by_carriage[number].get("class_summary") != want
    }
    check(not mismatch, f"16 节车厢构成与车型图一致（不符：{mismatch or '无'}）")
    check(by_carriage[1].get("class_totals") == {"一等座": 32, "商务座": 5},
          f"01 车逐席别计数 {by_carriage[1].get('class_totals')}")


def test_reset_clears_everything() -> None:
    """重置系统：占用与订单都要清空。"""
    print("[开发者] 重置系统")
    dev = reset_dev_store()
    store = reset_passenger_store()
    book_ticket_order(dev, store.by_ids(["C001"]), class_code="二等座",
                      order_id="T6")
    check(len(dev.occupied) > 0 and len(dev.orders) > 0, "重置前有数据")
    dev = reset_dev_store()
    check(len(dev.occupied) == 0, "重置后无占用")
    check(len(dev.orders) == 0, "重置后无订单")
    check(dev.remaining("二等座")["二等座"] == 1152,
          f"二等座余票回到 {dev.remaining('二等座')['二等座']}")


def test_fill_ratio_and_manual_locks_are_separate() -> None:
    """``fill_to_ratio`` 只回收"预设"占用，不动开发者手动锁定的座位。"""
    print("[开发者] 预设占用与手动锁定分离")
    dev = reset_dev_store()
    dev.set_remaining("二等座", 50)          # 手动锁定
    manual = sum(1 for o in dev.occupied.values() if o.source == "manual")
    check(manual > 0, f"手动锁定 {manual} 个")
    dev.fill_to_ratio(0.0)                   # 只该回收 preset
    still = sum(1 for o in dev.occupied.values() if o.source == "manual")
    check(still == manual, f"手动锁定不受 fill_to_ratio 影响（{still}）")
    dev.reset()
    check(len(dev.occupied) == 0, "reset 才清空全部")


def test_occupancy_rejects_bare_string() -> None:
    """占用接口必须拦住"传了单个字符串"——否则会被逐字符迭代。"""
    print("[开发者] 字符串入参防护")
    from smartrail.clustering import _as_seat_ids

    try:
        _as_seat_ids("02车01A")
        check(False, "单个字符串应被拒绝")
    except TypeError as error:
        check("逐字符" in str(error), f"被拒（{error}）")
    check(_as_seat_ids(["02车01A"]) == ("02车01A",), "列表入参正常")
    check(_as_seat_ids(("02车01A", "02车01B")) == ("02车01A", "02车01B"),
          "元组入参正常")


def test_same_order_passengers_sit_together() -> None:
    """同一订单的人默认坐在一起（硬/强绑定）。"""
    print("[购票] 同单同座")
    store = reset_passenger_store()
    order = build_profile_order(store.by_ids(["C001", "C005", "C003"]),
                                order_id="T7")
    check(order.default_bond.value == "strong",
          f"默认绑定 {order.default_bond.value}")
    check(len(order.bonds) >= 1, f"生成 {len(order.bonds)} 条硬绑定")
    check(all(b.value == "mandatory" for b in order.bonds.values()),
          "需照护者与成人之间是硬绑定")


def test_class_code_is_a_hard_constraint() -> None:
    """已购席别是**硬约束**：不能坐到自己没买的席别上。

    真实事故：``book_ticket_order(class_code="商务座")`` 被分到二等座
    ``03车01A`` —— 旅客付了商务座的价钱坐二等座。
    根因是两层都漏了：``Order`` 没有 ``class_code`` 字段，
    而 ``_prepare_order`` 重建 Order 时也不透传它。

    这里同时守住"席别过滤发生在候选池层"这一点 —— 若只是靠代价函数
    倾向于选对席别，压力大时仍会漏。
    """
    print("[购票] 席别硬约束")
    pax = reset_passenger_store()
    for want in ("二等座", "一等座", "商务座"):
        store = reset_dev_store()
        result = book_ticket_order(store, pax.by_ids(["C001"]),
                                   class_code=want, order_id=f"C-{want}")
        seat_id = result["order"]["passengers"][0]["seat_id"]
        seat = next((s for s in store.formation.seats if s.seat_id == seat_id), None)
        check(seat is not None, f"下单 {want}：分到真实座位 {seat_id}")
        check(seat is not None and seat.class_code == want,
              f"下单 {want} -> 分到 {seat.class_code if seat else '?'}"
              f"（{seat_id}）")
    # 多人单同样成立
    store = reset_dev_store()
    result = book_ticket_order(store, pax.by_ids(["C001", "C002", "C003", "C004"]),
                               class_code="一等座", order_id="C-MULTI")
    got = set()
    for item in result["order"]["passengers"]:
        seat = next((s for s in store.formation.seats
                     if s.seat_id == item["seat_id"]), None)
        got.add(seat.class_code if seat else "?")
    check(got == {"一等座"}, f"多人单席别一致（{got}）")


def test_engine_rebuild_keeps_order_fields() -> None:
    """``_prepare_order`` 重建订单时不得丢字段（结构不变量）。

    这个坑踩了三次（``default_bond``、``class_code``），所以加了机器检查：
    重建后的 ``Order`` 除了 ``passengers``（有意重算）之外，
    其余字段必须与原订单一致。
    """
    print("[购票] 重建订单不丢字段")
    from smartrail.engine import SeatEngine
    from smartrail.models import BondType, Order, Passenger, RelationType

    engine = SeatEngine()
    original = Order(
        order_id="REBUILD",
        passengers=(Passenger("A1", age=35),),
        relation=RelationType.GROUP,
        bonds={},
        default_bond=BondType.STRONG,
        class_code="一等座",
    )
    rebuilt = engine._prepare_order(original)
    check(rebuilt.class_code == "一等座",
          f"class_code 透传（{rebuilt.class_code!r}）")
    check(rebuilt.default_bond is BondType.STRONG, "default_bond 透传")
    check(rebuilt.relation is RelationType.GROUP, "relation 透传")
    check(rebuilt.order_id == "REBUILD", "order_id 透传")
    # 反证：若漏传，守卫必须报错（而不是静默重置）
    import dataclasses as _dc

    broken = _dc.replace(rebuilt, class_code="")
    try:
        from smartrail.engine import _assert_rebuild_keeps_fields

        _assert_rebuild_keeps_fields(rebuilt, broken)
        check(False, "漏传字段时应触发守卫")
    except AssertionError as error:
        check("class_code" in str(error), f"守卫拦下漏传（{str(error)[:50]}…）")


def test_base_groups_match_composition_spec() -> None:
    """基础分组必须与 ``composition.BASE_GROUP_FIELDS`` **完全一致**。

    需求原文给的就是这 5 类：

        id: 'adult'   成人   满18周岁及以上
        id: 'youth'   青少年  满14周岁但未满18周岁
        id: 'child'   儿童   满4周岁但未满14周岁
        id: 'toddler' 幼儿   满1周岁但未满4周岁
        id: 'infant'  婴儿   未满1周岁

    真实事故：``passenger_store.PASSENGER_TYPES`` 里另立了一套 16 类，
    还**自己加了"学生"**当成基础分组，并把"孕妇（4-6个月）"
    "视障（需导盲）"这类特殊人群平铺在同一层。
    需求方从未定义过"学生"这个分组 —— 学生是**票价属性**，不是人群分类。

    这里守住三件事：① 基础分组恰好是那 5 类；② 没有"学生"基础分组；
    ③ 每个类型都归属于某个基础分组（``base_group`` 合法）。
    """
    print("[购票] 基础分组与规格一致")
    from smartrail.composition import BASE_GROUP_FIELDS

    spec = [(item["id"], item["label"], item["desc"]) for item in BASE_GROUP_FIELDS]
    catalog = passenger_type_catalog()
    basic_groups = [g for g in catalog["groups"] if g.get("basic")]
    check(len(basic_groups) == 1, f"基础分组恰好 1 组（{len(basic_groups)}）")
    basic = basic_groups[0]
    check(basic["id"] == "basic", f"基础分组 id = {basic['id']}")
    got = [(t["id"], t["label"], t["desc"]) for t in basic["types"]]
    check([g[0] for g in got] == [s[0] for s in spec],
          f"基础分组顺序与 id 一致（{[g[0] for g in got]}）")
    check([g[1] for g in got] == [s[1] for s in spec],
          f"标签一致（{[g[1] for g in got]}）")
    check(len(got) == 5, f"恰好 5 类（{len(got)}）")

    # "学生"不能是基础分组
    base_ids = [t["id"] for t in basic["types"]]
    check("student" not in base_ids, "『学生』不在基础分组里（需求未定义该分组）")
    student = next(t for t in catalog["types"] if t["id"] == "student")
    check("票价" in student["desc"] or "优惠" in student["desc"],
          f"『学生』被标为票价属性（{student['desc']}）")
    # 注意 g["types"] 是**字典列表**（含 label/desc），不是 id 列表
    student_group = next(g for g in catalog["groups"]
                         if any(t["id"] == "student" for t in g["types"]))
    check("票种" in student_group["label"] or "票价" in student_group["label"],
          f"『学生』归在票种分组（{student_group['label']}）")

    # 每个类型都必须有合法的 base_group
    every = {t["id"] for t in catalog["types"]}
    covered = {t["id"] for g in catalog["groups"] for t in g["types"]}
    check(covered == every,
          f"分组覆盖全部 {len(every)} 种类型（缺失 {sorted(every - covered)}）")
    spec_ids = {s[0] for s in spec}
    bad = [t["id"] for t in catalog["types"]
           if t.get("base_group") not in spec_ids]
    check(not bad, f"所有类型都归属到基础分组（非法：{bad or '无'}）")
    # 特殊人群必须声明自己是叠加维度
    for t in catalog["types"]:
        if t["id"] not in spec_ids:
            check(bool(t.get("special")),
                  f"『{t['label']}』声明为叠加维度（special={t.get('special')}）")


def test_special_dimensions_do_not_change_total() -> None:
    """特殊人群是**叠加维度**，不改变总人数（与 composition 的公式一致）。"""
    print("[购票] 特殊人群不计入总人数")
    from smartrail.composition import OrderComposition, PlatformPolicy

    bands = ("adult", "youth", "child", "toddler", "infant")
    composition = OrderComposition()
    composition.base["adult"] = 3
    composition.base["child"] = 2
    composition.disability["severe"]["adult"] = 1
    composition.pregnant["term"]["adult"] = 1
    composition.child_sub["child_quiet"] = 1
    check(composition.total_passengers == 5,
          f"总人数只数基础分组（{composition.total_passengers}，应为 5）")
    # 健康成人要扣掉不能陪同的重度残疾
    policy = PlatformPolicy()
    check(composition.healthy_adults(policy) == 2,
          f"健康成人扣掉重度残疾（{composition.healthy_adults(policy)}）")
    check(composition.minors_under_14 == 2,
          f"未满 14 岁只数 child+toddler+infant（{composition.minors_under_14}）")


def test_user_page_has_collapsible_special_section() -> None:
    """用户模式页面必须有可展开的『需要特别服务』折叠区。"""
    print("[购票] 用户模式折叠区")
    page = (ROOT / "smartrail" / "web" / "ticketing.html").read_text(encoding="utf-8")
    check('id="specialBody"' in page, "含折叠区容器")
    check('id="btnSpecial"' in page, "含展开按钮")
    check("需要特别服务" in page, "按钮文案说明是特别服务")
    check('id="typeList"' in page, "基础分组平铺容器仍在")
    check("typeGroups" in page, "按接口返回的分组渲染（不写死）")
    check("group.basic" in page, "按 basic 标记决定平铺或收起")


def test_dev_page_composes_by_headcount() -> None:
    """开发者页必须支持**添加任意数量**的基础分组，而不是勾选姓名。

    需求："这里人名一点都不重要，而且我需要添加任意数量的成人什么的"。
    """
    print("[购票] 开发者页按人数分组")
    page = (ROOT / "smartrail" / "web" / "developer.html").read_text(encoding="utf-8")
    check("baseGroups" in page, "含基础分组步进器容器")
    check("renderBaseGroups" in page, "渲染基础分组步进器")
    check("baseCounts" in page, "按分组计数（不是选姓名）")
    check("compositionSchema" in page, "从 /api/composition/schema 取口径（不写死）")
    check("/api/composition/submit" in page, "提交走构成接口")
    check("stepper" in page, "含加减步进器")
    check("btnSpecialToggle" in page, "特殊人群可折叠")
    check("specialBox" in page, "特殊人群容器")
    check("data-testid" in page and "base-" in page,
          "步进器有 testid（可被自动化点击）")
    # 不该再有"勾选姓名"的旧控件
    check("renderPassengerPicker" not in page, "旧的姓名勾选器已移除")
    check("pickedProfiles" not in page, "旧的已选姓名状态已移除")


def test_booking_page_is_gone() -> None:
    """批量提交页面已按需求删除，且没有残留引用。

    需求："批量提交订单删了不要，我要在开发者页面见到提交订单"。
    删页面容易、删干净难 —— 残留的路由/链接会让页面 404 而不是消失。
    """
    print("[购票] /booking 已下线")
    check(not (ROOT / "smartrail" / "web" / "booking.html").exists(),
          "booking.html 已删除")
    server = (ROOT / "smartrail" / "api" / "stdlib_server.py").read_text(
        encoding="utf-8")
    check('"/booking"' not in server, "stdlib_server 不再注册 /booking")
    app = (ROOT / "smartrail" / "api" / "app.py").read_text(encoding="utf-8")
    check('"/booking"' not in app, "FastAPI 版本不再注册 /booking")
    # 开发者页必须有提交订单入口
    dev = (ROOT / "smartrail" / "web" / "developer.html").read_text(encoding="utf-8")
    check("提交订单" in dev, "开发者页含『提交订单』")
    check("btnSubmitOrder" in dev, "含提交按钮")


def main() -> int:
    tests = [
        test_default_profiles_cover_every_type,
        test_add_passenger_gets_fresh_id,
        test_train_list_has_no_seat_map,
        test_seat_row_options_is_one_row_only,
        test_no_split_when_adjacent_available,
        test_split_only_when_short,
        test_quiet_preference_changes_scoring,
        test_quiet_preference_is_bounded,
        test_booking_writes_ledger,
        test_remaining_updates_live,
        test_rejects_incomplete_selection,
        test_dev_snapshot_covers_whole_train,
        test_reset_clears_everything,
        test_fill_ratio_and_manual_locks_are_separate,
        test_occupancy_rejects_bare_string,
        test_same_order_passengers_sit_together,
        test_class_code_is_a_hard_constraint,
        test_engine_rebuild_keeps_order_fields,
        test_base_groups_match_composition_spec,
        test_special_dimensions_do_not_change_total,
        test_user_page_has_collapsible_special_section,
        test_dev_page_composes_by_headcount,
        test_booking_page_is_gone,
    ]
    if UNDER_PYTEST:
        for test in tests:
            test()
        return 0
    passed = 0
    for test in tests:
        before = len(_FAILURES)
        try:
            test()
        except AssertionError:
            continue
        if len(_FAILURES) == before:
            passed += 1
    print("-" * 72)
    if _FAILURES:
        print(f"失败 {len(_FAILURES)} 项：")
        for item in _FAILURES:
            print("  -", item)
        return 1
    print(f"全部 {passed} 组断言通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
