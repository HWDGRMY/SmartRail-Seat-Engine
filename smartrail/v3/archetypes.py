"""模块化乘客类型库：验收时按"人"自由组合，系统自动分组为订单。

设计动机（来自验收反馈）
------------------------
第一版做成了"固定套餐"：每个可选项是一个**整单**（带娃家庭 = 3 人一起），
用户只能选"来几单带娃家庭"。但真实需求是：

> 勾选**任意数量的各种人群**（成人、孕妇、盲人、婴儿……），
> 然后让系统自己去组织成订单。

因此本模块把两件事**拆开**：

* :class:`Archetype` —— **单个人**的类型（成人、学生、轮椅旅客、孕妇、婴儿……）；
* :func:`build_orders_from_archetypes` —— **自动组单**：把这些人组织成合理的订单。

自动组单规则
------------
1. **有 ``group_key`` 的类型会归到同一张订单**（婴儿与看护人、轮椅旅客与陪护、
   视障与导盲同伴）。
2. **零散成人/学生默认不拼陌生人**，每人一张单独订单 —— 这正符合项目里
   "没人会跟陌生人拼一个订单买票"的产品判断。
3. 用户可以显式开启 :func:`group_friends`，把零散同行者按 :data:`FRIEND_GROUP_SIZE`
   人一组拼成"朋友结伴"订单，用来验证团体场景。

这样"任意数量 × 任意人群"就能覆盖到全部支持需求，同时订单内部始终是**真实关系**。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Sequence

from ..config import DEFAULT_CONFIG
from ..models import (
    BondType,
    DeclaredBehavior,
    Order,
    Passenger,
    RelationType,
    SupportNeed,
    TicketType,
)

FRIEND_GROUP_SIZE = 4
"""显式开启"朋友结伴"时，每张团体订单的人数。"""


@dataclass(frozen=True)
class Archetype:
    """**单个人**的类型定义。"""

    key: str
    label: str
    age: int
    ticket_type: TicketType = TicketType.ADULT
    support_needs: tuple[SupportNeed, ...] = ()
    behavior: DeclaredBehavior = DeclaredBehavior.UNKNOWN
    needs_caregiver: bool = False
    is_caregiver: bool = False
    quietness_score: float = 100.0
    preference_aisle: bool = False
    preference_window: bool = False
    group_key: str | None = None
    """非空表示"必须与同一 key 的其他人同属一张订单"（照护关系）。"""
    group_relation: RelationType = RelationType.COUPLE
    role: str = "adult"
    """粗分类，供前端分组展示：adult / child / vulnerable / caregiver / group。"""
    note: str = ""

    def build(self, passenger_id: str, name: str) -> Passenger:
        return Passenger(
            passenger_id=passenger_id,
            name=name,
            age=self.age,
            ticket_type=self.ticket_type,
            support_needs=frozenset(self.support_needs),
            declared_behavior=self.behavior,
            needs_caregiver=self.needs_caregiver,
            is_caregiver=self.is_caregiver,
            quietness_score=self.quietness_score,
            preference_aisle=self.preference_aisle,
            preference_window=self.preference_window,
        )


def _a(key: str, label: str, age: int, **kwargs: Any) -> Archetype:
    return Archetype(key=key, label=label, age=age, **kwargs)


# ---------------------------------------------------------------------------
# 乘客类型清单（"人"的粒度）
# ---------------------------------------------------------------------------

ARCHETYPES: tuple[Archetype, ...] = (
    # ---- 普通旅客 ----
    _a("adult", "成人", 34, role="adult", note="普通成人旅客"),
    _a("adult_window", "成人（靠窗）", 36, preference_window=True, role="adult",
       note="明确要求靠窗，验证设施匹配奖励"),
    _a("adult_aisle", "成人（靠过道）", 38, preference_aisle=True, role="adult",
       note="明确要求靠过道"),
    _a("student", "学生", 20, ticket_type=TicketType.STUDENT,
       behavior=DeclaredBehavior.QUIET, role="adult"),
    _a("senior", "老年旅客", 72, support_needs=(SupportNeed.ELDERLY,),
       preference_aisle=True, role="vulnerable"),
    _a("low_credit", "低信用旅客", 37, quietness_score=45.0, role="vulnerable",
       note="静音信用分 45 < 60，禁止选静音车厢"),
    # ---- 儿童 ----
    _a("child", "儿童（6 岁）", 6, ticket_type=TicketType.CHILD,
       needs_caregiver=True, behavior=DeclaredBehavior.LIVELY, role="child",
       group_key="family", group_relation=RelationType.NUCLEAR_FAMILY,
       note="与看护人硬绑定"),
    _a("child_quiet", "儿童（安静型）", 9, ticket_type=TicketType.CHILD,
       needs_caregiver=True, behavior=DeclaredBehavior.QUIET, role="child",
       group_key="family", group_relation=RelationType.NUCLEAR_FAMILY),
    _a("toddler", "幼童（3 岁）", 3, ticket_type=TicketType.CHILD,
       support_needs=(SupportNeed.TODDLER,), needs_caregiver=True, role="child",
       group_key="toddler_family", group_relation=RelationType.NUCLEAR_FAMILY),
    _a("infant", "婴儿（1 岁）", 1, ticket_type=TicketType.CHILD,
       support_needs=(SupportNeed.INFANT,), needs_caregiver=True, role="child",
       group_key="infant_family", group_relation=RelationType.NUCLEAR_FAMILY,
       note="不可控，严禁静音车厢"),
    # ---- 重点旅客 ----
    _a("wheelchair", "轮椅旅客", 68, support_needs=(SupportNeed.WHEELCHAIR,),
       needs_caregiver=True, role="vulnerable", group_key="wheelchair_pair",
       note="硬约束：只能坐无障碍专区，系统会自动配一位陪护"),
    _a("blind", "视障旅客（独立）", 45,
       support_needs=(SupportNeed.INDEPENDENT_BLIND,), preference_aisle=True,
       role="vulnerable", note="只偏好过道/近车门，**不强行配陌生人**"),
    _a("blind_with_guide", "视障旅客（需导盲）", 52,
       support_needs=(SupportNeed.INDEPENDENT_BLIND,), needs_caregiver=True,
       role="vulnerable", group_key="blind_pair",
       note="与导盲同伴硬绑定"),
    _a("pregnant", "孕晚期旅客", 31,
       support_needs=(SupportNeed.PREGNANT_LATE,), needs_caregiver=True,
       role="vulnerable", group_key="pregnant_pair",
       note="偏好近卫生间/过道"),
    _a("intellectual", "智力障碍旅客", 26,
       support_needs=(SupportNeed.INTELLECTUAL_DISABILITY,), needs_caregiver=True,
       role="vulnerable", group_key="intellectual_pair"),
    # ---- 照护人 / 同伴（通常随重点旅客自动加入，也可单独添加） ----
    _a("caregiver", "照护人 / 同伴", 44, is_caregiver=True, role="caregiver",
       note="与同订单的重点旅客硬绑定"),
    _a("caregiver_alt", "第二照护人", 41, is_caregiver=True, role="caregiver"),
    _a("companion_child", "随行儿童", 8, ticket_type=TicketType.CHILD,
       role="child", group_key="family"),
)

ARCHETYPE_BY_KEY: dict[str, Archetype] = {item.key: item for item in ARCHETYPES}

# 需要"自动配一位照护人"的类型：现实里这些旅客不可能独自出行
AUTO_CAREGIVER_FOR: dict[str, str] = {
    "wheelchair": "caregiver",
    "blind_with_guide": "caregiver",
    "pregnant": "caregiver",
    "intellectual": "caregiver",
    "infant": "caregiver",
    "toddler": "caregiver",
}

ADULTS_ARE_CAREGIVERS = True
"""成人是否可充当同一批订单里儿童的家长。

**开启**（默认）：勾"成人 + 儿童"会生成"家长带孩子"的订单，而不是给儿童
另配一位照护人 —— 后者会凭空多出一位乘客，与用户的输入不符。
需要专门的陪护人员时，直接勾"照护人 / 同伴"即可。
"""


def default_bond() -> BondType:
    """按配置决定"同订单未声明关系"的默认绑定强度（同订单默认同座）。"""
    if not DEFAULT_CONFIG.same_order_default_bond:
        return BondType.SOFT
    return (
        BondType.MANDATORY
        if DEFAULT_CONFIG.same_order_bond_level == "mandatory"
        else BondType.STRONG
    )


@dataclass
class _Plan:
    """待安排的一位乘客 + 它的配对状态（内部使用）。

    用**稳定序号**表达配对，而不是对象引用或 ``id()`` ——
    早期用 ``id()`` 判断"这个照护人是否已被占用"，遇到 ``dataclasses.replace``
    产生的新对象就失效，儿童因此全部被生成孤身订单。
    """

    seq: int
    archetype: Archetype
    partner: int | None = None
    auto: bool = False


@dataclass
class BuiltOrder:
    """一张自动组好的订单 + 它是怎么来的（便于前端解释）。"""

    order: Order
    archetype_keys: list[str] = field(default_factory=list)
    origin: str = "solo"
    """``care``（照护关系自动成单）/ ``friends``（显式拼团）/ ``solo``（单人）。"""
    sequence: int = 0
    """订单内最小的展开序号，仅用于让输出顺序与输入顺序一致（可复现）。"""

    @property
    def size(self) -> int:
        return len(self.order.passengers)


def build_orders_from_archetypes(
    counts: dict[str, int],
    group_friends: bool = False,
    order_prefix: str = "SIM",
    auto_caregiver: bool = True,
) -> list[BuiltOrder]:
    """把"每种人各来几个"组织成合理的订单列表。

    ``counts`` 形如 ``{"adult": 5, "wheelchair": 2, "infant": 1}``。
    ``auto_caregiver=True`` 时，需要照护的类型会自动补一位照护人
    （否则那些旅客会孤身出行，不符合现实，也测不出"照护绑定"这条核心约束）。
    """
    # ---------- 第 1 步：展开成"待安排的人"清单 ----------
    # 每个条目带一个**稳定序号**，后续配对只按序号操作，不依赖对象 identity
    # （早期用 ``id()`` 去重，遇到 ``dataclasses.replace`` 产生的新对象就失效）。
    plans: list[_Plan] = []
    for key, count in counts.items():
        archetype = ARCHETYPE_BY_KEY.get(key)
        if archetype is None:
            raise KeyError(f"未知乘客类型：{key}")
        for _ in range(max(0, int(count))):
            plans.append(_Plan(seq=len(plans), archetype=archetype))

    if auto_caregiver:
        # ---------- 第 2 步：补齐照护人 ----------
        # 规则（顺序即优先级）：
        #   1. 需要照护且**必须专属陪同**的类型（轮椅/需导盲/孕晚期/智力障碍/
        #      婴儿/幼童）—— 每位配一位照护人，且与成人数量无关，
        #      因为这些旅客不能由"顺手的家长"照顾；
        #   2. 儿童类 —— 优先用**已勾选的成人**当家长（"家长带孩子"），
        #      成人不够时才补照护人。否则勾"成人 6 + 儿童 2"会凭空多出 2 位乘客。
        #
        # 历史缺陷：早期按"类型"各自补照护人，先处理的类型把照护人用光，
        # 后面的婴儿/儿童只能生成孤身订单（实测 "儿童（6 岁）1 人订单"）；
        # 后来改成只数"带 group_key 的重点旅客"，又漏掉 family 组里的儿童。
        dedicated = [
            plan for plan in plans
            if plan.archetype.key in AUTO_CAREGIVER_FOR
            and plan.archetype.needs_caregiver
        ]
        # 「必须有陪护」= 显式列入 AUTO_CAREGIVER_FOR 的类型，或**任何儿童**。
        # 这里刻意用 role 判据而不是 needs_caregiver 标志：``companion_child``
        # 的 role 是 child 但 needs_caregiver 为 False，早期实现因此把它漏掉，
        # 生成了孤身儿童订单。
        children = [
            plan for plan in plans
            if plan.archetype.key not in AUTO_CAREGIVER_FOR
            and (
                plan.archetype.role == "child"
                or plan.archetype.needs_caregiver
            )
        ]
        helpers_available = sum(1 for plan in plans if plan.archetype.is_caregiver)
        # "潜在家长" = 所有**不是儿童、也不是照护人**的同行者。
        # 口径必须与下面 ``adults_pool`` 一致，否则补人数量会算错：
        # 早期口径不一致，导致勾了老年/视障等成人旅客时系统仍去补照护人，
        # 而"随行儿童"因为成人池里的都被别处用掉而落单。
        adults_available = sum(
            1
            for plan in plans
            if ADULTS_ARE_CAREGIVERS
            and plan.archetype.role != "child"
            and not plan.archetype.is_caregiver
            and not plan.archetype.needs_caregiver
        )
        helpers_needed = len(dedicated) + max(
            0, len(children) - (adults_available if ADULTS_ARE_CAREGIVERS else 0)
        )
        helper_archetype = ARCHETYPE_BY_KEY["caregiver"]
        for _ in range(max(0, helpers_needed - helpers_available)):
            plans.append(_Plan(seq=len(plans), archetype=helper_archetype))
        # 自动补进来的照护人需要标记，方便后面按需分配
        auto_from = len(plans) - max(0, helpers_needed - helpers_available)
        for index in range(auto_from, len(plans)):
            plans[index].auto = True

        # ---------- 第 3 步：配对 ----------
        # 专属陪同：为每位 dedicated 依次取一位还没配出去的照护人
        caretakers: list[_Plan] = [p for p in plans if p.archetype.is_caregiver]
        caretaker_cursor = 0
        for dependent in dedicated:
            while caretaker_cursor < len(caretakers):
                candidate = caretakers[caretaker_cursor]
                caretaker_cursor += 1
                if candidate.partner is None:
                    candidate.partner = dependent.seq
                    dependent.partner = candidate.seq
                    break
        # 儿童：优先找没配出去的成人当家长，其次找剩余照护人。
        # 口径与上面 ``adults_available`` 保持一致：非儿童、非照护人即家长候选
        # （成人、学生、老年旅客、视障旅客都可以照看孩子；但**儿童不能当家长**）。
        adults_pool = [
            p for p in plans
            if ADULTS_ARE_CAREGIVERS
            and p.archetype.role != "child"
            and not p.archetype.is_caregiver
            and not p.archetype.needs_caregiver
        ]
        adult_cursor = 0
        for child in children:
            while adult_cursor < len(adults_pool):
                candidate = adults_pool[adult_cursor]
                adult_cursor += 1
                if candidate.partner is None:
                    candidate.partner = child.seq
                    child.partner = candidate.seq
                    break
            else:
                while caretaker_cursor < len(caretakers):
                    candidate = caretakers[caretaker_cursor]
                    caretaker_cursor += 1
                    if candidate.partner is None:
                        candidate.partner = child.seq
                        child.partner = candidate.seq
                        break

    # ---------- 第 4 步：把配对关系折叠成订单 ----------
    built: list[BuiltOrder] = []
    emitted: set[int] = set()
    index = 0

    def make(members: list[_Plan], origin: str) -> BuiltOrder:
        nonlocal index
        index += 1
        passengers: list[Passenger] = []
        for offset, plan in enumerate(members):
            archetype = plan.archetype
            pid = f"{archetype.key}-{index}-{offset}"
            name = (
                f"{archetype.label}{offset + 1}" if len(members) > 1 else archetype.label
            )
            passengers.append(archetype.build(pid, name))
        bonds: dict[frozenset[str], BondType] = {}
        for i, a in enumerate(members):
            for j in range(i + 1, len(members)):
                b = members[j]
                if (
                    a.archetype.needs_caregiver
                    or b.archetype.needs_caregiver
                    or a.archetype.is_caregiver
                    or b.archetype.is_caregiver
                ):
                    bonds[
                        frozenset((passengers[i].passenger_id, passengers[j].passenger_id))
                    ] = BondType.MANDATORY
        relation = (
            members[0].archetype.group_relation
            if members and members[0].archetype.group_key
            else (RelationType.GROUP if len(members) > 1 else RelationType.SOLO)
        )
        return BuiltOrder(
            order=Order(
                order_id=f"{order_prefix}-{origin.upper()}-{index}",
                passengers=tuple(passengers),
                relation=relation,
                bonds=bonds,
                default_bond=default_bond(),
            ),
            archetype_keys=[m.archetype.key for m in members],
            origin=origin,
            sequence=min(m.seq for m in members),
        )

    # 4a) 配对关系 -> 成单（被照护者在前，照护人在后，语义直观）
    for plan in plans:
        if plan.seq in emitted:
            continue
        if plan.partner is None:
            continue
        mate = plans[plan.partner]
        if (
            plan.archetype.is_caregiver
            and not plan.archetype.needs_caregiver
            and not mate.archetype.is_caregiver
        ):
            # 照护人先跳过，等被照护者那条处理时一起成单
            continue
        emitted.add(plan.seq)
        emitted.add(mate.seq)
        built.append(make([plan, mate], "care"))

    # 4b) 没配出去的照护人：结伴成单（多人时按团体人数切分）
    leftover_helpers = [
        p for p in plans if p.archetype.is_caregiver and p.seq not in emitted
    ]
    for start in range(0, len(leftover_helpers), FRIEND_GROUP_SIZE):
        chunk = leftover_helpers[start : start + FRIEND_GROUP_SIZE]
        if chunk:
            emitted.update(p.seq for p in chunk)
            built.append(make(chunk, "solo"))

    # 4c) 其余人：默认各成一张订单（不拼陌生人），可显式拼团
    remaining = [p for p in plans if p.seq not in emitted]
    if group_friends and len(remaining) > 1:
        for start in range(0, len(remaining), FRIEND_GROUP_SIZE):
            chunk = remaining[start : start + FRIEND_GROUP_SIZE]
            if len(chunk) > 1:
                built.append(make(chunk, "friends"))
            elif chunk:
                built.append(make(chunk, "solo"))
    else:
        for plan in remaining:
            built.append(make([plan], "solo"))

    # 保序：按订单内首位乘客的展开序号排序，保证同一输入得到同一结果
    built.sort(key=lambda item: item.sequence)
    return built


def coverage_of(orders: Iterable[Order]) -> dict[str, Any]:
    """报告这批订单覆盖了哪些支持需求 / 票种 / 关系，用于证明"全覆盖"。"""
    needs: set[str] = set()
    tickets: set[str] = set()
    behaviors: set[str] = set()
    bonds: dict[str, int] = {level.value: 0 for level in BondType}
    sizes: list[int] = []
    passenger_count = 0
    orders = list(orders)
    for order in orders:
        passenger_count += len(order.passengers)
        sizes.append(len(order.passengers))
        for passenger in order.passengers:
            needs.update(need.value for need in passenger.support_needs)
            tickets.add(passenger.ticket_type.value)
            behaviors.add(passenger.declared_behavior.value)
        for position, a in enumerate(order.passengers):
            for b in order.passengers[position + 1 :]:
                bonds[order.bond_of(a.passenger_id, b.passenger_id).value] += 1
    all_needs = {need.value for need in SupportNeed}
    return {
        "orders": len(orders),
        "passengers": passenger_count,
        "order_sizes": sizes,
        "support_needs": sorted(needs),
        "all_support_needs": sorted(all_needs),
        "missing_support_needs": sorted(all_needs - needs),
        "ticket_types": sorted(tickets),
        "behaviors": sorted(behaviors),
        "bond_levels": bonds,
    }


def archetype_catalog() -> list[dict[str, Any]]:
    """类型清单（供前端渲染可勾选面板）。"""
    return [
        {
            "key": item.key,
            "label": item.label,
            "role": item.role,
            "age": item.age,
            "ticket_type": item.ticket_type.value,
            "support_needs": [need.value for need in item.support_needs],
            "needs_caregiver": item.needs_caregiver,
            "is_caregiver": item.is_caregiver,
            "group_key": item.group_key,
            "note": item.note,
        }
        for item in ARCHETYPES
    ]


__all__ = [
    "ARCHETYPES",
    "ARCHETYPE_BY_KEY",
    "AUTO_CAREGIVER_FOR",
    "FRIEND_GROUP_SIZE",
    "Archetype",
    "BuiltOrder",
    "archetype_catalog",
    "build_orders_from_archetypes",
    "coverage_of",
    "default_bond",
]
