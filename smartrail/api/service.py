"""与 Web 框架无关的服务核心（business core）。

把"请求 -> 领域对象 -> 引擎 -> 响应字典"的逻辑集中在这里，好处有两个：

1. FastAPI 路由层只剩薄薄一层装饰，业务逻辑可**脱离 Web 框架**单测；
2. 在没有 FastAPI 的环境（例如内网离线机器）可以用
   :mod:`smartrail.api.stdlib_server` 起一个同构的备用服务。

本模块不 import 任何第三方库。
"""

from __future__ import annotations

import dataclasses
import json
import time
from typing import Any, Iterable, Mapping, Sequence

from ..carriage import crh_16_car_formation, mini_formation
from ..config import DEFAULT_CONFIG
from ..credit import BLOCK_THRESHOLD, CreditLedger
from ..engine import SeatEngine
from ..fixtures import SCENARIOS
from ..gov_api import demo_provider
from ..models import (
    BondType,
    DeclaredBehavior,
    Order,
    Passenger,
    RelationType,
    Solution,
    SupportNeed,
    TicketType,
)
from ..router import AllocationMode, RoutingSignals, Thresholds
from ..scoring import Scorer


def create_engine(formation: str = "crh16", credit: CreditLedger | None = None) -> SeatEngine:
    """构造演示引擎（16 节编组 + 政务资格模拟 + 内存信用账本）。"""
    train = mini_formation() if formation == "mini" else crh_16_car_formation()
    return SeatEngine(
        formation=train,
        credit_ledger=credit or CreditLedger(),
        support_provider=demo_provider(),
        thresholds=Thresholds(),
    )


# ---------------------------------------------------------------------------
# 请求解析
# ---------------------------------------------------------------------------


def order_from_payload(payload: Mapping[str, Any]) -> Order:
    """把 HTTP 请求体（dict）转成领域对象 :class:`Order`。

    只依赖标准库，便于在任何框架下复用（FastAPI / 备用服务器 / 测试）。
    """
    raw_passengers = list(payload.get("passengers") or [])
    if not raw_passengers:
        raise ValueError("passengers 不能为空")
    passengers: list[Passenger] = []
    for item in raw_passengers:
        needs = frozenset(_coerce_enum(SupportNeed, value) for value in item.get("support_needs", []))
        ticket = _coerce_enum(TicketType, item.get("ticket_type", TicketType.ADULT.value))
        behavior = _coerce_enum(
            DeclaredBehavior, item.get("declared_behavior", DeclaredBehavior.UNKNOWN.value)
        )
        passengers.append(
            Passenger(
                passenger_id=str(item["passenger_id"]),
                ticket_type=ticket,
                age=int(item.get("age", 35)),
                name=str(item.get("name") or item["passenger_id"]),
                support_needs=needs,
                declared_behavior=behavior,
                quietness_score=float(item.get("quietness_score", 100.0)),
                preference_aisle=bool(item.get("preference_aisle", False)),
                preference_window=bool(item.get("preference_window", False)),
                is_caregiver=bool(item.get("is_caregiver", False)),
                needs_caregiver=(
                    bool(item.get("needs_caregiver", False))
                    or SupportNeed.INFANT in needs
                    or ticket is TicketType.CHILD
                    or SupportNeed.PREGNANT_LATE in needs
                ),
            )
        )
    bonds: dict[frozenset[str], BondType] = {}
    for item in payload.get("bonds") or []:
        bonds[frozenset((str(item["a"]), str(item["b"])))] = _coerce_enum(
            BondType, item.get("bond", BondType.STRONG.value)
        )
    # 同订单默认同座：没显式声明关系的人，默认按"同行人"（STRONG）处理。
    # 调用方可用 same_order_bond="soft" / "none" 覆盖（公司代订、多人各自出差）。
    override = payload.get("same_order_bond")
    if override is None:
        default_bond = (
            BondType.STRONG if DEFAULT_CONFIG.same_order_default_bond else BondType.SOFT
        )
    elif str(override).lower() in {"none", "off", "false", "soft"}:
        default_bond = BondType.SOFT
    else:
        default_bond = _coerce_enum(BondType, override)
    return Order(
        order_id=str(payload.get("order_id") or "O-HTTP"),
        passengers=tuple(passengers),
        relation=_coerce_enum(RelationType, payload.get("relation", RelationType.SOLO.value)),
        bonds=bonds,
        default_bond=default_bond,
        # **已购席别必须透传**。漏了它的后果实测过：
        # 批量提交接口（含开发者页组单）选的"二等座"完全不生效，
        # 求解器按空席别处理 -> 过滤关闭 -> 给 2 成人 2 儿童发了
        # 01车的**一等座**。用户报的就是"买二等座出一等座"。
        #
        # 空字符串表示"不限席别"（压测/仿真场景），保持向后兼容。
        class_code=str(payload.get("class_code") or ""),
    )


def orders_from_design(payload: Mapping[str, Any]) -> list[Order]:
    """把**用户手工构造**的订单列表转成领域对象。

    这是验收台的核心入口：用户可以任意张数、每张任意人数与任意乘客类型地
    提交订单，系统逐单求解并**明确告出哪张单不能满足、为什么**。

    请求体形状::

        {
          "orders": [
            {
              "order_id": "O1",                  # 可省略，自动编号
              "relation": "nuclear_family",      # 可选，影响代价函数的关系项
              "passengers": [
                {"key": "adult"},                # 用类型库（自动带年龄/需求/偏好）
                {"key": "wheelchair"}
              ],
              "bond_mode": "auto",               # auto | mandatory | strong | soft
              "note": "轮椅旅客 + 家属"           # 仅用于结果展示
            }
          ],
          "same_order_bond": "strong"            # 可选，全局覆盖"同订单默认同座"
        }

    ``key`` 走 :mod:`smartrail.v3.archetypes` 的类型库；也可以用 ``passenger``
    直接给完整字段（与 ``/api/book`` 的乘客结构一致），两者可混用。
    """
    from ..v3.archetypes import ARCHETYPE_BY_KEY

    raw_orders = list(payload.get("orders") or [])
    if not raw_orders:
        raise ValueError("orders 不能为空")

    default_bond = payload.get("same_order_bond")
    orders: list[Order] = []
    for order_index, raw in enumerate(raw_orders):
        items = list(raw.get("passengers") or [])
        if not items:
            raise ValueError(f"第 {order_index + 1} 张订单没有任何乘客")
        order_id = str(raw.get("order_id") or f"DESIGN-{order_index + 1}")

        passengers: list[Passenger] = []
        for item_index, item in enumerate(items):
            key = item.get("key")
            if key:
                archetype = ARCHETYPE_BY_KEY.get(str(key))
                if archetype is None:
                    raise ValueError(f"未知乘客类型：{key}")
                passenger_id = str(item.get("passenger_id") or f"{key}-{order_index + 1}-{item_index + 1}")
                name = str(item.get("name") or f"{archetype.label}{item_index + 1}")
                passengers.append(archetype.build(passenger_id, name))
            else:
                # 允许直接给完整字段（与 /api/book 的乘客结构一致）
                passengers.append(
                    order_from_payload(
                        {
                            "order_id": order_id,
                            "passengers": [{**item, "passenger_id": item.get("passenger_id") or f"P-{order_index + 1}-{item_index + 1}"}],
                        }
                    ).passengers[0]
                )

        # 关系：默认按订单里有没有儿童/重点旅客自动判，也可显式指定
        relation = raw.get("relation")
        if relation is None:
            relation = _guess_relation(passengers)
        else:
            relation = _coerce_enum(RelationType, relation)

        # 绑定强度：auto 时按"订单构成"推断 —— 有照护需求就硬绑定，
        # 有儿童就强绑定，纯成人团体用软绑定（没人会跟陌生人硬绑在一起）
        bond_mode = str(raw.get("bond_mode") or "auto").lower()
        if bond_mode == "auto":
            inferred = _guess_default_bond(passengers)
            order_default = inferred
        elif bond_mode in {"none", "off", "false", "soft"}:
            order_default = BondType.SOFT
        else:
            order_default = _coerce_enum(BondType, bond_mode)

        # 显式声明的关系优先
        bonds: dict[frozenset[str], BondType] = {}
        for item in raw.get("bonds") or []:
            bonds[frozenset((str(item["a"]), str(item["b"])))] = _coerce_enum(
                BondType, item.get("bond", BondType.MANDATORY.value)
            )

        order = Order(
            order_id=order_id,
            passengers=tuple(passengers),
            relation=relation,
            bonds=bonds,
            default_bond=order_default,
            # **席别必须透传**（可写在订单上，也可写在请求顶层作为默认值）。
            # 漏了它，批量提交/开发者组单选的"二等座"完全不生效 ——
            # 求解器按空席别处理、过滤关闭，实测给 2 成人 2 儿童发了一等座，
            # 就是用户报的"买二等座出一等座"。
            class_code=str(raw.get("class_code")
                           or payload.get("class_code") or ""),
        )
        if default_bond is not None:
            override_value = (
                BondType.SOFT
                if str(default_bond).lower() in {"none", "off", "false", "soft"}
                else _coerce_enum(BondType, default_bond)
            )
            # **重建 Order 不能丢字段**。这里曾经漏掉 class_code，
            # 于是"请求顶层给了 same_order_bond"的路径下席别被静默重置为空 ——
            # 与 engine._prepare_order 漏 default_bond / class_code 是同一类错误。
            order = dataclasses.replace(order, default_bond=override_value)
        orders.append(order)
    return orders


def _guess_relation(passengers: Sequence[Passenger]) -> RelationType:
    """按订单构成猜一个合适的关系类型（影响代价函数的关系项）。"""
    if len(passengers) <= 1:
        return RelationType.SOLO
    if any(passenger.needs_caregiver for passenger in passengers):
        return RelationType.CARE
    if any(passenger.is_child for passenger in passengers):
        return RelationType.NUCLEAR_FAMILY
    return RelationType.GROUP


def _guess_default_bond(passengers: Sequence[Passenger]) -> BondType:
    """按订单构成推断默认绑定强度。

    * 有照护需求（婴儿/幼童/轮椅/孕晚期…）-> ``MANDATORY``：这些关系不能拆；
    * 有儿童 -> ``STRONG``：必须尽量坐一起，但不至于拒票；
    * 纯成人 -> ``SOFT``：同行人尽量靠近即可。
    """
    if any(passenger.needs_caregiver for passenger in passengers):
        return BondType.MANDATORY
    if any(passenger.is_child for passenger in passengers):
        return BondType.STRONG
    return BondType.STRONG if DEFAULT_CONFIG.same_order_default_bond else BondType.SOFT


def _coerce_enum(enum_cls: Any, value: Any) -> Any:
    if isinstance(value, enum_cls):
        return value
    try:
        return enum_cls(str(value))
    except ValueError as error:  # pragma: no cover - 输入校验分支
        allowed = ", ".join(member.value for member in enum_cls)
        raise ValueError(f"非法取值 {value!r}，允许：{allowed}") from error


def signals_from_payload(payload: Mapping[str, Any], engine: SeatEngine) -> RoutingSignals | None:
    ratio = payload.get("availability_ratio")
    concurrency = int(payload.get("concurrency") or 0)
    if ratio is None and not concurrency:
        return None
    return RoutingSignals(
        availability_ratio=float(ratio) if ratio is not None else engine.state.availability_ratio,
        concurrency=concurrency,
    )


# ---------------------------------------------------------------------------
# 动作
# ---------------------------------------------------------------------------


def book_order(engine: SeatEngine, payload: Mapping[str, Any]) -> dict[str, Any]:
    """处理一次购票请求，返回可 JSON 序列化的响应体。"""
    order = order_from_payload(payload)
    mode = payload.get("mode")
    chosen = payload.get("chosen_seats")
    if mode == AllocationMode.FREE.value and chosen:
        solution = engine.validate_free_seats(order, dict(chosen))
        payload_out = {
            "order_id": order.order_id,
            "mode": AllocationMode.FREE.value,
            "routing_reason": "调用方显式指定自由选座并提交了自选座位。",
            "explanation": Scorer.explain(solution.violations),
            "crew_warnings": [],
            **solution.to_dict(),
        }
        return {"ok": bool(solution.assignments), "status": 200 if solution.assignments else 409, "body": payload_out}
    result = engine.book(
        order,
        signals=signals_from_payload(payload, engine),
        mode=AllocationMode(mode) if mode else None,
    )
    return {"ok": True, "status": 200, "body": result.to_dict()}


def run_scenario(engine: SeatEngine, name: str, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """执行一个预置场景。"""
    if name not in SCENARIOS:
        raise KeyError(f"未知场景：{name}（可选：{', '.join(sorted(SCENARIOS))}）")
    payload = payload or {}
    mode = payload.get("mode")
    signals = None
    if payload.get("availability_ratio") is not None:
        signals = RoutingSignals(availability_ratio=float(payload["availability_ratio"]))
    elif payload.get("concurrency"):
        signals = RoutingSignals(
            availability_ratio=engine.state.availability_ratio,
            concurrency=int(payload["concurrency"]),
        )
    result = engine.book(
        SCENARIOS[name](),
        signals=signals,
        mode=AllocationMode(mode) if mode else None,
    )
    return result.to_dict()


def update_credit(engine: SeatEngine, passenger_id: str, action: str) -> dict[str, Any]:
    """乘务员端信用分闭环：投诉 / 表扬。"""
    if action == "complaint":
        record = engine.credit.report_complaint(passenger_id)
    elif action == "commendation":
        record = engine.credit.report_good_behavior(passenger_id)
    else:
        raise ValueError("action 必须为 complaint 或 commendation")
    return {
        "passenger_id": passenger_id,
        "score": record.score,
        "blocked": record.blocked,
        "quiet_carriage_allowed": not record.blocked,
        "threshold": BLOCK_THRESHOLD,
        "message": (
            f"信用分 {record.score:.0f} 分"
            + ("，已屏蔽静音车厢选座权限。" if record.blocked else "，静音车厢权限正常。")
        ),
    }


def compare_solvers(payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """V1 / V2 / V3 并排对比（验收页的核心接口）。

    所有策略吃同一条订单流（同 seed / 同客流画像 / 同初始占座比例），
    指标定义统一；缺依赖的后端会以 ``skipped`` + 原因出现在结果里。
    """
    from ..v3.evaluate import ComparisonRequest, leaderboard, run_comparison

    payload = payload or {}
    policies = payload.get("policies") or None
    request = ComparisonRequest(
        policies=tuple(policies) if policies else ComparisonRequest.__dataclass_fields__[
            "policies"
        ].default,
        steps=int(payload.get("steps", 60)),
        seed=int(payload.get("seed", 7)),
        fill=float(payload.get("fill", 0.0)),
        complaint_rate=float(payload.get("complaint_rate", 0.0)),
        time_budget_ms=(
            float(payload["time_budget_ms"]) if payload.get("time_budget_ms") is not None else None
        ),
    )
    result = run_comparison(request)
    body = result.to_dict()
    body["leaderboard"] = [
        {"policy": row["policy"], "rank": index + 1}
        for index, row in enumerate(leaderboard(result))
    ]
    return body


def training_curve(path: str | Path | None = None) -> dict[str, Any]:
    """读取 V3 的学习曲线（训练产物），供验收页画图。"""
    from pathlib import Path as _Path

    target = _Path(path) if path else _Path("artifacts/v3/v3_training_curve.json")
    if not target.exists():
        return {"available": False, "reason": f"未找到学习曲线：{target}"}
    return {"available": True, "path": str(target), **json.loads(target.read_text(encoding="utf-8"))}


def explain_solution(solution) -> str:
    return Scorer.explain(solution.violations)


def scenario_passengers(name: str) -> dict[str, Any]:
    """把预置场景导出成**可直接填进前端表单**的乘客与绑定关系。

    为什么要有这个接口：前端如果自己再抄一份乘客数据，两边就会漂移
    （改了 fixtures 忘了改前端）。这里直接把场景对象序列化，前端只负责渲染，
    保证"预置场景"与"后端场景"永远是同一份定义。
    """
    if name not in SCENARIOS:
        raise KeyError(f"未知场景：{name}（可选：{', '.join(sorted(SCENARIOS))}）")
    order = SCENARIOS[name]()
    return {
        "scenario": name,
        "order_id": order.order_id,
        "relation": order.relation.value,
        "passengers": [
            {
                "passenger_id": p.passenger_id,
                "name": p.name or p.passenger_id,
                "age": p.age,
                "ticket_type": p.ticket_type.value,
                "declared_behavior": p.declared_behavior.value,
                "support_needs": sorted(need.value for need in p.support_needs),
                "preference_aisle": p.preference_aisle,
                "preference_window": p.preference_window,
            }
            for p in order.passengers
        ],
        "bonds": [
            {"a": a, "b": b, "bond": order.bond_of(a, b).value}
            for index, a in enumerate(p.passenger_id for p in order.passengers)
            for b in [q.passenger_id for q in order.passengers[index + 1 :]]
            if order.bond_of(a, b) is not BondType.SOFT
        ],
    }


def _empty_solution(order_id: str) -> Solution:
    """构造一个"什么都没安排"的解，用于结构性不可行的订单（不跑求解器）。"""
    _ = order_id
    return Solution(
        assignments={},
        waitlisted=[],
        total_affinity=0.0,
        solver="precheck(结构性不可行，未求解)",
    )


def submit_orders(
    payload: Mapping[str, Any],
    engine: "SeatEngine | None" = None,
) -> dict[str, Any]:
    """逐单求解"用户手工构造的订单"，并明确告出哪张单不能满足、为什么。

    与 :func:`concurrent_simulation` 的区别：那个是"按人群类型批量生成订单"，
    这个是"用户自己组单，自己决定每单几个人、都是什么人"。

    ``engine``：**传入时用它，不再新建**。这一步是必需的 ——
    早先这里无条件 ``reset_engine()`` 建**空车**，于是这条路径完全看不到
    开发者页设定的余票与已售座位，实测「二等座余票 2，下 4 人单」
    依然"全部出票"发了 4 张票（超卖 2 张），而且出票后余票纹丝不动
    （1152 -> 1152）。用户模式在同样状态下会正确分票并扣减余票。
    """
    from ..feasibility import analyse_order, feasibility_catalog, outcome_summary

    orders = orders_from_design(payload)
    formation = str(payload.get("formation", "crh16"))
    if engine is None:
        ledger = CreditLedger()
        engine = reset_engine(
            create_engine(formation, ledger),
            formation=formation,
            fill=float(payload.get("fill", 0.0)),
            seed=int(payload.get("seed", 7)),
        )
    seat_lookup = engine.formation.by_id()
    started = time.perf_counter()

    results: list[dict[str, Any]] = []
    seat_owner: dict[str, int] = {}
    for index, order in enumerate(orders):
        # 求解前先做情况判定（不预测具体方案，只看结构性事实）
        feasibility = analyse_order(
            order, engine.formation.seats, engine.state.occupied,
            formation=engine.formation,
        )
        if feasibility.severity == "blocking":
            # **只有"全车真的没空座"才跳过求解**。理由：
            # 1. 结果一样（出不了票），但求解大单元会触发组合爆炸 ——
            #    实测"8 人订单"曾让求解卡住数分钟；
            # 2. 提前给出的是**事实**（全车余票不足），而不是笼统的"无可行座位"。
            #
            # 注意：特殊情况**不再**走这条分支。无障碍专区不足、同车厢装不下，
            # 都属于"提示后照常出票"，必须真的去求解。
            summary = outcome_summary(order, _empty_solution(order.order_id), feasibility)
            summary["index"] = index
            summary["note"] = str(
                (payload.get("orders") or [{}])[index].get("note") or ""
            )
            summary["passengers_detail"] = [
                {
                    "passenger_id": passenger.passenger_id,
                    "label": passenger.name,
                    "support_needs": sorted(need.value for need in passenger.support_needs),
                    "seated": False,
                    "seat_id": None,
                }
                for passenger in order.passengers
            ]
            summary["seats"] = {}
            summary["carriages"] = []
            summary["adjacency_level"] = "impossible"
            summary["total_cost"] = 0.0
            summary["elapsed_ms"] = 0.0
            summary["color_index"] = index % 12
            results.append(summary)
            continue

        outcome = engine.book(order, mode=str(payload.get("mode", "smart")))
        solution = outcome.solution
        summary = outcome_summary(order, solution, feasibility)
        summary["index"] = index
        summary["note"] = str(
            (payload.get("orders") or [{}])[index].get("note") or ""
        )
        summary["passengers_detail"] = [
            {
                "passenger_id": passenger.passenger_id,
                "label": passenger.name,
                "support_needs": sorted(need.value for need in passenger.support_needs),
                "seated": passenger.passenger_id in solution.assignments,
                "seat_id": (
                    solution.assignments[passenger.passenger_id].seat_id
                    if passenger.passenger_id in solution.assignments
                    else None
                ),
            }
            for passenger in order.passengers
        ]
        summary["seats"] = {
            pid: assignment.seat_id
            for pid, assignment in solution.assignments.items()
        }
        summary["carriages"] = sorted(
            {
                seat_lookup[assignment.seat_id].carriage
                for assignment in solution.assignments.values()
                if assignment.seat_id in seat_lookup
            }
        )
        summary["adjacency_level"] = solution.adjacency.level
        summary["total_cost"] = round(solution.total_cost, 1)
        summary["elapsed_ms"] = round(solution.elapsed_ms, 3)
        summary["color_index"] = index % 12
        for passenger_id in solution.assignments:
            seat_owner[passenger_id] = index
        results.append(summary)

    wall_ms = (time.perf_counter() - started) * 1000.0
    requested = sum(item["requested"] for item in results)
    seated = sum(item["seated"] for item in results)
    counts: dict[str, int] = {}
    for item in results:
        counts[item["level"]] = counts.get(item["level"], 0) + 1

    snapshot = engine.snapshot()
    # 座位 -> 订单序号，供座位图按订单着色
    owner_by_seat: dict[str, int] = {}
    for item in results:
        for _pid, seat_id in item["seats"].items():
            owner_by_seat[seat_id] = item["color_index"]

    return {
        "orders": results,
        "summary": {
            "orders": len(results),
            "requested_passengers": requested,
            "seated_passengers": seated,
            "waitlisted_passengers": requested - seated,
            "seat_rate": round(seated / requested, 4) if requested else 1.0,
            "fulfilled_orders": counts.get("fulfilled", 0),
            "confirmed_orders": counts.get("confirmed", 0),
            "partial_orders": counts.get("partial", 0),
            "action_orders": counts.get("action_required", 0),
            "impossible_orders": counts.get("impossible", 0),
            "tier0_violations": sum(item["tier0"] for item in results),
            "wall_ms": round(wall_ms, 2),
        },
        # 需要用户拍板的问题（前端应弹确认框，而不是替用户拒票）
        "confirmations": [
            {
                "order_id": item["order_id"],
                "question": item["question"],
                "code": (item.get("feasibility") or {}).get("code", ""),
                "message": item["reasons"][0] if item["reasons"] else "",
                "seated": item["seated"],
                "requested": item["requested"],
            }
            for item in results
            if item.get("question")
        ],
        # "需要用户知道或处理"的订单：不含直接出票的那两档。
        # 注意不能只排除 ``fulfilled`` —— 那样会把 ``confirmed`` 也塞进来，
        # 而 confirmed 是"票已出、用户确认即可"，不属于"需要处理"。
        "unmet_orders": [
            {
                "order_id": item["order_id"],
                "level": item["level"],
                "level_label": item["level_label"],
                "reasons": item["reasons"],
            }
            for item in results
            if item["level"] in ("partial", "action_required", "impossible")
        ],
        "feasibility_codes": feasibility_catalog(),
        "train": snapshot,
        "seat_owner": owner_by_seat,
        "catalog": concurrent_simulation({})["catalog"],
    }


def submit_compositions(payload: Mapping[str, Any]) -> dict[str, Any]:
    """按"人员构成"提交多张订单：**先校验，通过才求解**。

    与 :func:`submit_orders` 的区别：那个接受"手工挑乘客"，这个接受
    "基础分组 + 儿童细分 + 残疾 + 孕妇"的分组计数（见 :mod:`smartrail.composition`）。

    校验不通过的订单**不会被提交给求解器** —— 规格明确要求
    "重度/极重度残疾 + 未约重点旅客 -> 拒票""婴幼儿 + 成人0 -> 拒票"，
    这类问题必须在组单阶段就拦住，而不是事后提示。

    每张订单的构成**完全独立**（多订单数据隔离）：校验与展开都只看自己那份。
    """
    from ..composition import PlatformPolicy, OrderComposition, composition_schema
    from ..mapping import build_order

    raw_orders = list(payload.get("orders") or [])
    if not raw_orders:
        raise ValueError("orders 不能为空")
    policy_payload = payload.get("policy") or {}
    policy = PlatformPolicy(
        late_pregnancy_requires_key_service=bool(
            policy_payload.get("late_pregnancy_requires_key_service", False)
        ),
        late_pregnancy_requires_companion=bool(
            policy_payload.get("late_pregnancy_requires_companion", False)
        ),
        moderate_cannot_companion=bool(
            policy_payload.get("moderate_cannot_companion", False)
        ),
    )

    # 订单号：调用方给了就用，否则按**台账已有的单数**续号。
    #
    # 早期无条件用 ``COMP-{index+1}``，于是每次提交都是 COMP-1 ——
    # 台账里三张不同的订单全叫 COMP-1，记录无法区分（实测抓到的）。
    from ..ticketing import get_dev_store as _dev_store

    existing = len(_dev_store().orders)
    compositions = [
        OrderComposition.from_dict(
            item,
            order_id=str(item.get("order_id") or "")
            or f"COMP-{existing + index + 1}",
        )
        for index, item in enumerate(raw_orders)
    ]
    # 先跑全部校验，把不通过的挑出来
    mappings = [build_order(item, policy) for item in compositions]
    blocked = [
        {
            "order_id": mapping.order.order_id,
            "note": composition.note,
            "errors": mapping.check.errors,
            "total_passengers": mapping.total_passengers,
        }
        for composition, mapping in zip(compositions, mappings)
        if not mapping.check.ok
    ]

    # 只把校验通过的订单交给求解器（**每张订单独立求解、独立占座**）
    runnable = [
        item for item, mapping in zip(raw_orders, mappings) if mapping.check.ok
    ]
    solved: dict[str, Any] = {}
    seat_map: dict[str, Any] = {}
    # 用**开发者台账的真实库存**建引擎：余票滑块与已售座位都在台账里，
    # 必须让求解器看到同一份事实 —— 否则会出现"余票 2 却发出 4 张票"。
    live_engine = ledger_engine()
    if runnable:
        submitted = submit_orders(
            {
                "orders": [
                    {
                        "order_id": mapping.order.order_id,
                        "note": composition.note,
                        "passengers": [
                            {
                                "passenger_id": p.passenger_id,
                                "name": p.name,
                                "age": p.age,
                                "ticket_type": p.ticket_type.value,
                                "support_needs": sorted(n.value for n in p.support_needs),
                                "declared_behavior": p.declared_behavior.value,
                                "needs_caregiver": p.needs_caregiver,
                                "is_caregiver": p.is_caregiver,
                            }
                            for p in mapping.order.passengers
                        ],
                        "bonds": [
                            # 键是 frozenset（无序对），先解包再组装
                            {"a": members[0], "b": members[1], "bond": bond.value}
                            for members, bond in (
                                (tuple(pair), bond)
                                for pair, bond in mapping.order.bonds.items()
                            )
                            if len(members) == 2
                        ],
                        "bond_mode": "strong",
                        # 席别必须一路带到 Order —— 否则界面选的"二等座"
                        # 不生效，会出现"买二等座出一等座"（实测过）。
                        "class_code": composition.class_code,
                    }
                    for composition, mapping in zip(compositions, mappings)
                    if mapping.check.ok
                ],
                "fill": payload.get("fill", 0.0),
                "seed": payload.get("seed", 7),
                # 顶层席别作为默认值透传（订单自己给了就以订单为准）。
                # 漏了这一步，"写在请求顶层"的写法会被静默忽略。
                "class_code": payload.get("class_code", ""),
                "same_order_bond": payload.get("same_order_bond"),
            },
            # 用台账引擎：余票与已售座位都取自同一份事实
            engine=live_engine,
        )
        solved = {
            item["order_id"]: item for item in submitted.get("orders", [])
        }
        seat_map = submitted
        # 出票后把座位**写回台账** —— 否则开发者页的余票纹丝不动，
        # 明明出了票却还显示满车余票（实测 1152 -> 1152）。
        #
        # 轮椅停放位的落实在下面（orders_payload 拼好之后）：
        # 那一刻才拿得到逐位乘客的明细。停放位是独立资源、不占座位票额，
        # 所以这里的座位记账不受它影响。
        _persist_sold_seats(submitted)

    orders_payload: list[dict[str, Any]] = []
    for composition, mapping in zip(compositions, mappings):
        entry = mapping.to_dict()
        entry["note"] = composition.note
        entry["blocked"] = not mapping.check.ok
        result = solved.get(mapping.order.order_id)
        entry["result"] = result or None
        if result:
            entry["seats"] = result.get("seats", {})
            entry["carriages"] = result.get("carriages", [])
            entry["level"] = result.get("level")
            entry["level_label"] = result.get("level_label")
            entry["color_index"] = result.get("color_index", 0)
            # 轮椅停放位不够时的询问文案（"询问后出正常坐票"）——
            # 它是在 solved 上算出来的，必须搬到订单层级，
            # 否则页面只看到"已出票"却不知道有人坐的是普通座。
            if result.get("question"):
                entry["question"] = result["question"]
            if result.get("wheelchair_bays"):
                entry["wheelchair_bays"] = result["wheelchair_bays"]
            if result.get("notices"):
                entry["notices"] = result["notices"]
        else:
            entry["seats"] = {}
            entry["carriages"] = []
            entry["level"] = "blocked"
            entry["level_label"] = "不予出票（组单校验未通过）"
            entry["color_index"] = 0
        orders_payload.append(entry)

    total_requested = sum(item["total_passengers"] for item in orders_payload)
    seated = sum((item["result"] or {}).get("seated", 0) for item in orders_payload)
    runnable_total = sum(
        item["total_passengers"] for item in orders_payload if not item["blocked"]
    )
    # **轮椅固定停放位**（需求："无障碍要求完全独立编号"）。
    #
    # 放在这里而不是求解刚结束时：这一步要按 passenger_id 改写
    # ``entry["result"]["passengers_detail"]``（页面「下单记录」读的就是它），
    # 而那份 detail 是订单摘要拼进 result 的，求解刚结束时还读不到 ——
    # 早先放在求解后，结果只有 ``entry["seats"]`` 被改写、detail 保持旧值，
    # 页面上同一个人的座位号两处不一致（`seats` 写 04车W1、detail 写 04车01B）。
    bay_report = _apply_bay_identity_for(orders_payload, mappings)
    # 停放位不够时的询问文案要落到订单层级（页面直接读 entry["question"]）
    for entry in orders_payload:
        info = bay_report.get(str(entry.get("order_id") or ""))
        if not info:
            continue
        entry["wheelchair_bays"] = {
            "needed": info["needed"],
            "free_before": info["free_before"],
            "assigned": info["assigned"],
        }
        if info.get("question"):
            entry["question"] = info["question"]
    # 把已求解的订单**记进开发者台账**。
    # 需求："开发者提交的订单难道不用保留吗"。原先这条路径完全没记录 ——
    # /api/composition/submit 直接把订单交给主引擎，而"用户下单记录"
    # 读的是开发者台账（devstore.orders），所以开发者在 /dev 提交的订单
    # 在记录里一条都看不到，刷新还会被覆盖。
    #
    # 记的形状与用户模式（booking.py 的 order_record）保持一致，
    # 这样前端 renderOrders 不用为两种来源写两套渲染。
    _record_composition_orders(orders_payload, seat_map)

    return {
        "orders": orders_payload,
        "blocked": blocked,
        "blocked_count": len(blocked),
        "summary": {
            "orders": len(orders_payload),
            "blocked_orders": len(blocked),
            "submitted_orders": len(orders_payload) - len(blocked),
            "requested_passengers": total_requested,
            "submitted_passengers": runnable_total,
            "seated_passengers": seated,
            "waitlisted_passengers": max(0, runnable_total - seated),
            "tier0_violations": sum(
                (item["result"] or {}).get("tier0", 0) for item in orders_payload
            ),
            "wall_ms": float((seat_map or {}).get("summary", {}).get("wall_ms", 0.0)),
        },
        "unmet_orders": [
            {
                "order_id": item["order_id"],
                "level": item["level"],
                "level_label": item["level_label"],
                "reasons": (item["result"] or {}).get("reasons", [])
                or item["check"]["errors"],
            }
            for item in orders_payload
            if item["level"] != "fulfilled"
        ],
        "confirmations": (seat_map or {}).get("confirmations", []),
        "train": (seat_map or {}).get("train") or {},
        "seat_owner": (seat_map or {}).get("seat_owner", {}),
        "schema": composition_schema(policy),
    }


def ledger_engine() -> SeatEngine:
    """按**开发者台账的真实状态**建一个引擎。

    余票滑块与"用户已售"都存在台账里，而求解器只认引擎的 ``state.occupied``。
    两者必须是同一份事实 —— 早先组单路径新建空车引擎，于是
    "二等座余票 2"的状态下 4 人单照样全部出票（超卖 2 张），
    出票后余票还纹丝不动。
    """
    from ..ticketing import get_dev_store

    store = get_dev_store()
    engine = SeatEngine(formation=store.formation)
    occupied = set(store.engine_occupied)
    if occupied:
        engine.state.mark_occupied(occupied)
    return engine


def _apply_bay_identity_for(
    entries: Sequence[dict[str, Any]],
    mappings: Sequence[Any],
) -> dict[str, dict[str, Any]]:
    """给组单结果落实轮椅固定停放位（独立编号）。

    返回 ``{order_id: {needed, free_before, assigned, question}}``。

    复用用户模式那一份实现（``booking._apply_bay_identity``），不另写一套 ——
    两套实现迟早会漂移，而"停放位到底占不占座位票额"这种规则一旦漂移
    就会变成超卖或虚占。

    **停放位不够时按"询问后出普通坐票"处理**（需求原文："如果 4 个轮椅位
    都卖完了，可在询问后出正常坐票"）：照常出票，但必须给出询问文案，
    否则用户根本不知道第 5 位轮椅旅客坐的是普通座位。
    """
    from ..ticketing import get_dev_store
    from ..ticketing.booking import _apply_bay_identity

    store = get_dev_store()
    by_id = {mapping.order.order_id: mapping.order for mapping in mappings}
    report: dict[str, dict[str, Any]] = {}
    for entry in entries:
        order_id = str(entry.get("order_id") or "")
        order = by_id.get(order_id)
        if order is None or entry.get("blocked"):
            continue
        # 位置对齐，**不能按 passenger_id 匹配**：
        # mapping.order 里的人叫 COMPOSE-adult-1（组单阶段的临时编号），
        # 而提交给求解器的人叫 COMP-1-adult-1（订单号定下来之后重编的），
        # 两边 ID 天然不同。submit_orders 是按顺序构造乘客的，
        # 所以按下标配对才是正确的。
        payloads = [
            {"passenger_id": passenger.passenger_id,
             "seat_id": entry.get("seats", {}).get(passenger.passenger_id)}
            for passenger in order.passengers
        ]
        if any(not item["seat_id"] for item in payloads):
            continue
        needed = sum(
            1 for passenger in order.passengers if passenger.is_mobility_impaired
        )
        free_before = len(store.free_bays())
        assigned = _apply_bay_identity(
            store, order, payloads,
            color_index=int(entry.get("color_index") or 0),
            order_id=order_id,
        )
        entry["seats"] = {
            item["passenger_id"]: item["seat_id"] for item in payloads
        }
        entry["carriages"] = sorted({
            item.get("carriage") for item in payloads if item.get("carriage")
        })
        # 逐位乘客的座位号也同步（页面「下单记录」直接读这一份）。
        #
        # 优先按 passenger_id 配；两边对不上时（组单阶段的临时编号
        # 与订单号定下来之后重编的编号可能不同）退回按下标配 ——
        # 求解结果与 order.passengers 是同一顺序构造的。
        detail = (entry.get("result") or {}).get("passengers_detail") or []
        by_pid = {item["passenger_id"]: item for item in payloads}
        for index, row in enumerate(detail):
            source = by_pid.get(row.get("passenger_id"))
            if source is None and index < len(payloads):
                source = payloads[index]
            if source is None:
                continue
            row["seat_id"] = source["seat_id"]
            row["wheelchair_bay"] = source.get("wheelchair_bay", "")
        question = ""
        if needed > free_before:
            short = needed - free_before
            total_bays = len(store.formation.wheelchair_bays)
            question = (
                f"本单有 {needed} 位轮椅旅客，但轮椅固定停放位仅剩 {free_before} 个"
                f"（全列共 {total_bays} 个，分布在 04 车与 12 车）。"
                f"其中 {short} 位已改出**普通坐票**"
                f"（轮椅停放位独立于座位票额，普通坐票仍可正常出票，"
                f"站车将协助上下车）。"
            )
        report[order_id] = {
            "needed": needed,
            "free_before": free_before,
            "assigned": len(assigned),
            "question": question,
        }
    return report


def _persist_sold_seats(submitted: Mapping[str, Any]) -> int:
    """把求解结果里"已售出"的座位写回开发者台账。返回写入数量。

    必须写回，否则开发者页的余票不会减少 —— 明明出了票却仍显示满车余票，
    余票滑块与座位图也会与实际出票脱节。

    **轮椅停放位不写座位**：它是独立资源、有独立编号，已由
    :func:`_apply_bay_identity_for` 按停放位记账（不占普通座位票额）。
    """
    from ..ticketing import get_dev_store
    from ..ticketing.devstore import SOURCE_SOLD

    store = get_dev_store()
    written = 0
    for order in submitted.get("orders", []):
        order_id = str(order.get("order_id") or "")
        # 逐个乘客写，才能按 ``wheelchair_bay`` 把停放位排除掉。
        #
        # 注意取的是 ``passengers_detail``，不是 ``passengers`` ——
        # submit_orders 的结果里只有前者（``passengers`` 是我一开始想当然
        # 写下的键名，结果这个函数静默地什么都没写：
        # 出票后 free_seats 仍然是 1238）。
        for row in (order.get("passengers_detail") or []):
            if row.get("wheelchair_bay"):
                continue          # 停放位另账，见 _apply_bay_identity_for
            seat_id = row.get("seat_id") or ""
            if not seat_id or store.is_occupied(seat_id):
                continue
            store.occupy([seat_id], source=SOURCE_SOLD, order_id=order_id,
                         passenger_name=str(row.get("label") or ""),
                         color_index=int(order.get("color_index") or 0))
            written += 1
    return written


def _record_composition_orders(
    orders_payload: Sequence[Mapping[str, Any]],
    seat_map: Mapping[str, Any],
) -> None:
    """把构成组单的结果写进开发者台账，供"用户下单记录"显示。

    形状刻意与 :func:`smartrail.ticketing.booking.book_ticket_order` 产出的
    ``order_record`` 一致（order_id / class_code / passengers / rows / split …），
    这样前端只需要一套渲染逻辑。

    被拦下的订单也记，但标 ``blocked=True`` —— "提交了但没出票"
    同样是需要保留的记录，否则用户会以为提交丢了。
    """
    from ..ticketing import get_dev_store

    store = get_dev_store()
    owner = seat_map.get("seat_owner") or {}
    color_of = {
        seat_id: index for seat_id, index in owner.items()
    } if all(isinstance(v, int) for v in owner.values()) else {}
    # 座位号 -> 席别（用于给记录补上"这一单买的是什么席别"）
    seat_class: dict[str, str] = {}
    try:
        for seat in create_engine().formation.seats:
            seat_class[seat.seat_id] = seat.class_code
    except Exception:  # noqa: BLE001 - 取不到就留空，不影响记录本身
        seat_class = {}

    for entry in orders_payload:
        seats = entry.get("seats") or {}
        info = entry.get("info") or {}
        base = info.get("base") or {}
        passengers = []
        for item in entry.get("passengers") or []:
            seat_id = seats.get(item["passenger_id"], "")
            passengers.append({
                "passenger_id": item["passenger_id"],
                "name": item.get("name", ""),
                "ticket_type": item.get("ticket_type", ""),
                "seat_id": seat_id,
                "carriage": 0,
                "row": 0,
                "col": "",
                "class_code": "",
                "quiet": False,
                "wheelchair_bay": "",
            })
        # 车厢/排号从座位号解析（形如「01车01A」），与用户模式保持同构；
        # 顺带补上席别 —— 记录里没有席别的话，"买二等座"这件事
        # 在事后无法核对（实测记录里的 class_code 一直是空的）。
        for payload in passengers:
            seat_id = payload["seat_id"]
            if len(seat_id) >= 5 and "车" in seat_id:
                head, _, tail = seat_id.partition("车")
                payload["carriage"] = int(head) if head.isdigit() else 0
                digits = "".join(ch for ch in tail[:2] if ch.isdigit())
                payload["row"] = int(digits) if digits else 0
                payload["col"] = tail[-1] if tail else ""
            payload["class_code"] = seat_class.get(seat_id, "")
        base_desc = " + ".join(
            f"{key}×{value}" for key, value in base.items() if value
        ) or "无"
        store.record_order({
            "order_id": entry.get("order_id", ""),
            "class_code": entry.get("class_code")
                          or (entry.get("result") or {}).get("class_code", "")
                          or (next((p["class_code"] for p in passengers
                                    if p["class_code"]), "")),
            "source": "dev-composition",
            "note": entry.get("note", ""),
            "blocked": bool(entry.get("blocked")),
            "level": entry.get("level", ""),
            "level_label": entry.get("level_label", ""),
            "base_desc": base_desc,
            "total_passengers": entry.get("total_passengers", 0),
            "healthy_adults": (entry.get("check") or {}).get("healthy_adults"),
            "required_companions": (entry.get("check") or {}).get(
                "required_companions"),
            "reason": "；".join((entry.get("check") or {}).get("errors") or []),
            "passengers": passengers,
            "rows": sorted({
                f"{p['carriage']:02d}车{p['row']}排"
                for p in passengers if p["carriage"]
            }),
            "seated": len(seats),
            "split": False,
            "color_index": entry.get("color_index", 0),
        })


def concurrent_simulation(payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """并发订单模拟：把"任意数量 × 任意人群类型"的订单一次性压给引擎。

    **两种下单方式**（推荐第一种，见 :mod:`smartrail.v3.archetypes`）：

    * ``archetypes={"adult": 5, "wheelchair": 2, "infant": 1}`` ——
      **模块化**：按"人"自由组合，系统自动把有照护关系的人组织成订单
      （轮椅旅客会自动配一位陪护、婴儿会配看护人），零散旅客各自成单；
    * ``presets={"family_child": 2}`` —— 旧的"固定套餐"方式，保留兼容。
    """
    from ..v3.concurrent import preset_catalog, run_concurrent

    payload = payload or {}

    def _expand(raw: Any) -> dict[str, int]:
        if isinstance(raw, Mapping):
            return {str(key): max(0, int(count)) for key, count in raw.items()}
        if isinstance(raw, (list, tuple)):
            return {str(item): 1 for item in raw}
        return {}

    archetypes = _expand(payload.get("archetypes"))
    preset_counts = _expand(payload.get("presets"))

    preset_keys: list[str] = []
    for key, count in preset_counts.items():
        preset_keys.extend([key] * count)

    result = run_concurrent(
        preset_keys=preset_keys or None,
        fill=float(payload.get("fill", 0.0)),
        seed=int(payload.get("seed", 7)),
        solver=str(payload.get("solver", "v1_heuristic")),
        formation=str(payload.get("formation", "crh16")),
        archetype_counts=archetypes or None,
        group_friends=bool(payload.get("group_friends", False)),
        auto_caregiver=bool(payload.get("auto_caregiver", True)),
    )
    result["catalog"] = preset_catalog()
    result["input"] = {
        "archetypes": archetypes,
        "presets": preset_counts,
        "group_friends": bool(payload.get("group_friends", False)),
    }
    return result


def reset_engine(
    engine: SeatEngine,
    formation: str = "crh16",
    fill: float = 0.0,
    seed: int = 7,
) -> SeatEngine:
    """重建引擎，并可预占 ``fill`` 比例的车厢座位。

    为什么要支持预占：前端要能测"半车""只剩零散座位""满车"这些真实场景。
    如果只能从空车开始，用户永远看不到"没有相邻座位"时系统怎么办，
    而这恰恰是本项目最想让人验证的行为。

    预占方式与仿真器一致（同一 seed 可复现）：在全部座位里**随机**抽取，
    因此不会人为制造"特别整齐"或"特别零散"的假象。
    """
    import random

    fresh = SeatEngine(
        formation=mini_formation() if formation == "mini" else crh_16_car_formation(),
        # 信用账本跨重置保留：投诉记录属于旅客，不该因为换了车次就清零。
        credit_ledger=engine.credit,
        support_provider=demo_provider(),
        thresholds=Thresholds(),
    )
    ratio = max(0.0, min(0.95, float(fill)))
    if ratio > 0:
        seats = list(fresh.formation.seats)
        rng = random.Random(seed)
        count = int(round(len(seats) * ratio))
        chosen = rng.sample(range(len(seats)), min(count, len(seats)))
        fresh.state.mark_occupied([seats[index].seat_id for index in chosen])
    return fresh


def config_payload(engine: SeatEngine) -> dict[str, Any]:
    """可解释性 & 论文复现所需的全部参数。"""
    return {
        "engine_config": DEFAULT_CONFIG.to_dict(),
        "quiet_credit_block_threshold": BLOCK_THRESHOLD,
        "thresholds": dict(engine.router.thresholds.__dict__),
        "scenarios": sorted(SCENARIOS),
        "modes": [mode.value for mode in AllocationMode],
        "train_code": engine.formation.train_code,
    }


def scenario_names() -> Sequence[str]:
    return tuple(sorted(SCENARIOS))


def crew_warnings(engine: SeatEngine, order: Order, solution) -> list[dict[str, Any]]:
    return engine.crew_warnings(order, solution)


def available_fixtures() -> Iterable[str]:  # pragma: no cover - 便于调试
    return SCENARIOS.keys()
