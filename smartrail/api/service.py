"""与 Web 框架无关的服务核心（business core）。

把"请求 -> 领域对象 -> 引擎 -> 响应字典"的逻辑集中在这里，好处有两个：

1. FastAPI 路由层只剩薄薄一层装饰，业务逻辑可**脱离 Web 框架**单测；
2. 在没有 FastAPI 的环境（例如内网离线机器）可以用
   :mod:`smartrail.api.stdlib_server` 起一个同构的备用服务。

本模块不 import 任何第三方库。
"""

from __future__ import annotations

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
        )
        if default_bond is not None:
            override_value = (
                BondType.SOFT
                if str(default_bond).lower() in {"none", "off", "false", "soft"}
                else _coerce_enum(BondType, default_bond)
            )
            order = Order(
                order_id=order.order_id,
                passengers=order.passengers,
                relation=order.relation,
                bonds=order.bonds,
                default_bond=override_value,
            )
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


def submit_orders(payload: Mapping[str, Any]) -> dict[str, Any]:
    """逐单求解"用户手工构造的订单"，并明确告出哪张单不能满足、为什么。

    与 :func:`concurrent_simulation` 的区别：那个是"按人群类型批量生成订单"，
    这个是"用户自己组单，自己决定每单几个人、都是什么人"。
    """
    from ..feasibility import analyse_order, feasibility_catalog, outcome_summary

    orders = orders_from_design(payload)
    formation = str(payload.get("formation", "crh16"))
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
        # 求解前先做结构性可行性判定（不预测具体方案，只判硬约束）
        feasibility = analyse_order(
            order, engine.formation.seats, engine.state.occupied
        )
        if not feasibility.feasible:
            # **结构性不可行就不要去求解**。两个理由：
            # 1. 结果一样（都是出不了票），但求解大单元会触发组合爆炸 ——
            #    实测"4 位轮椅 + 4 位家属"的 8 人订单让求解卡住数分钟；
            # 2. 提前给出的是**可操作的原因**（专区座位不够），
            #    而不是求解器超时后的笼统"无可行座位"。
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
            "partial_orders": counts.get("partial", 0),
            "action_orders": counts.get("action_required", 0),
            "impossible_orders": counts.get("impossible", 0),
            "tier0_violations": sum(item["tier0"] for item in results),
            "wall_ms": round(wall_ms, 2),
        },
        "unmet_orders": [
            {
                "order_id": item["order_id"],
                "level": item["level"],
                "level_label": item["level_label"],
                "reasons": item["reasons"],
            }
            for item in results
            if item["level"] != "fulfilled"
        ],
        "feasibility_codes": feasibility_catalog(),
        "train": snapshot,
        "seat_owner": owner_by_seat,
        "catalog": concurrent_simulation({})["catalog"],
    }


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
