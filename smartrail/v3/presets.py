"""人群预置库：验收模拟时可任意组合的"人群类型"。

设计目标（来自验收需求）
------------------------
用户测试时应当能"**直接添加任意数量的各种人群组合**"，因此这里提供一份
**覆盖全部支持需求与关系形态**的类型清单：

* 单身 / 情侣 / 带娃家庭 / 多代同堂 / 团体 / 重点旅客（轮椅、视障、孕晚期、
  婴儿、幼童、老人、智力障碍）/ 商务 / 学生 / 混合；

每个类型都给出**确定性的乘客与关系定义**（同一 ``seed`` 结果可复现），
并且刻意把"硬绑定""强绑定""无声明关系"三种情形都覆盖到，
以便验证"同订单默认同座"这条策略。

覆盖度校验
----------
:func:`coverage_report` 会报告"本次模拟实际用到了哪些支持需求 / 关系类型"，
前端据此显示"人群覆盖是否完整"，避免出现"看起来测了很多、其实只测了单身"的情况。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

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


@dataclass(frozen=True)
class PassengerSpec:
    """一位乘客的声明（不含订单归属）。"""

    key: str
    name: str
    age: int
    ticket_type: TicketType = TicketType.ADULT
    support_needs: tuple[SupportNeed, ...] = ()
    behavior: DeclaredBehavior = DeclaredBehavior.UNKNOWN
    needs_caregiver: bool = False
    is_caregiver: bool = False
    quietness_score: float = 100.0
    preference_aisle: bool = False
    preference_window: bool = False

    def build(self, passenger_id: str) -> Passenger:
        return Passenger(
            passenger_id=passenger_id,
            name=self.name,
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


@dataclass(frozen=True)
class PresetType:
    """一种"人群类型"的完整定义。"""

    key: str
    label: str
    description: str
    relation: RelationType
    members: tuple[PassengerSpec, ...]
    bonds: tuple[tuple[int, int, BondType], ...] = ()
    """成员下标之间的显式绑定；未列出的组合走"同订单默认同座"。"""
    tags: tuple[str, ...] = field(default_factory=tuple)

    @property
    def size(self) -> int:
        return len(self.members)


def _p(key: str, name: str, age: int, **kwargs: Any) -> PassengerSpec:
    return PassengerSpec(key=key, name=name, age=age, **kwargs)


# ---------------------------------------------------------------------------
# 人群类型清单（覆盖全部支持需求与关系形态）
# ---------------------------------------------------------------------------

PRESETS: tuple[PresetType, ...] = (
    PresetType(
        key="solo",
        label="单身出行",
        description="1 位普通成人，靠窗偏好",
        relation=RelationType.SOLO,
        members=(_p("S1", "独行旅客", 29, preference_window=True),),
        tags=("基础",),
    ),
    PresetType(
        key="couple",
        label="情侣 / 夫妻",
        description="2 位成人，强绑定同行",
        relation=RelationType.COUPLE,
        members=(_p("C1", "先生", 33), _p("C2", "女士", 31, is_caregiver=True)),
        bonds=((0, 1, BondType.STRONG),),
        tags=("基础", "同行"),
    ),
    PresetType(
        key="family_child",
        label="带娃家庭",
        description="两位家长 + 1 名活泼型儿童（硬绑定，Tier 0 守护对象）",
        relation=RelationType.NUCLEAR_FAMILY,
        members=(
            _p("F1", "父亲", 36, is_caregiver=True),
            _p("F2", "母亲", 34, is_caregiver=True),
            _p("F3", "孩子", 5, ticket_type=TicketType.CHILD,
               behavior=DeclaredBehavior.LIVELY, needs_caregiver=True),
        ),
        bonds=((0, 2, BondType.MANDATORY), (1, 2, BondType.MANDATORY), (0, 1, BondType.STRONG)),
        tags=("重点", "儿童", "硬绑定"),
    ),
    PresetType(
        key="family_infant",
        label="带婴儿家庭",
        description="两位家长 + 1 名婴儿（婴儿不可控，严禁静音车厢）",
        relation=RelationType.NUCLEAR_FAMILY,
        members=(
            _p("I1", "家长甲", 31, is_caregiver=True),
            _p("I2", "家长乙", 30, is_caregiver=True),
            _p("I3", "婴儿", 1, ticket_type=TicketType.CHILD,
               support_needs=(SupportNeed.INFANT,), needs_caregiver=True),
        ),
        bonds=((0, 2, BondType.MANDATORY), (1, 2, BondType.MANDATORY)),
        tags=("重点", "婴儿", "硬绑定"),
    ),
    PresetType(
        key="multi_gen",
        label="多代同堂",
        description="两位老人 + 两位成人 + 1 名幼童（5 人，跨代照护）",
        relation=RelationType.MULTI_GEN_FAMILY,
        members=(
            _p("M1", "爷爷", 74, support_needs=(SupportNeed.ELDERLY,), preference_aisle=True),
            _p("M2", "奶奶", 71, support_needs=(SupportNeed.ELDERLY,)),
            _p("M3", "爸爸", 40, is_caregiver=True),
            _p("M4", "妈妈", 38, is_caregiver=True),
            _p("M5", "幼童", 4, ticket_type=TicketType.CHILD,
               support_needs=(SupportNeed.TODDLER,), needs_caregiver=True,
               behavior=DeclaredBehavior.QUIET),
        ),
        bonds=((0, 4, BondType.STRONG), (2, 4, BondType.MANDATORY), (3, 4, BondType.MANDATORY)),
        tags=("重点", "老人", "幼童", "多代"),
    ),
    PresetType(
        key="wheelchair",
        label="轮椅旅客 + 陪护",
        description="轮椅旅客（硬约束：只能坐无障碍专区）+ 1 位陪护",
        relation=RelationType.COUPLE,
        members=(
            _p("W1", "轮椅旅客", 66, support_needs=(SupportNeed.WHEELCHAIR,),
               needs_caregiver=True),
            _p("W2", "陪护", 42, is_caregiver=True),
        ),
        bonds=((0, 1, BondType.MANDATORY),),
        tags=("重点", "无障碍", "硬绑定"),
    ),
    PresetType(
        key="blind",
        label="独立视障旅客",
        description="独立视障：只偏好过道 / 近车门，**不强行配陌生人**",
        relation=RelationType.SOLO,
        members=(
            _p("B1", "视障旅客", 45, support_needs=(SupportNeed.INDEPENDENT_BLIND,),
               preference_aisle=True),
        ),
        tags=("重点", "视障"),
    ),
    PresetType(
        key="blind_group",
        label="视障旅客 + 导盲同伴",
        description="视障旅客与同伴同行（硬绑定）",
        relation=RelationType.COUPLE,
        members=(
            _p("G1", "视障旅客", 52, support_needs=(SupportNeed.INDEPENDENT_BLIND,),
               needs_caregiver=True),
            _p("G2", "同伴", 50, is_caregiver=True),
        ),
        bonds=((0, 1, BondType.MANDATORY),),
        tags=("重点", "视障", "硬绑定"),
    ),
    PresetType(
        key="pregnant",
        label="孕晚期 + 同伴",
        description="孕晚期旅客（偏好近卫生间 / 过道）+ 1 位同伴",
        relation=RelationType.COUPLE,
        members=(
            _p("P1", "孕晚期旅客", 30, support_needs=(SupportNeed.PREGNANT_LATE,),
               needs_caregiver=True),
            _p("P2", "同伴", 32, is_caregiver=True),
        ),
        bonds=((0, 1, BondType.MANDATORY),),
        tags=("重点", "孕晚期", "硬绑定"),
    ),
    PresetType(
        key="intellectual",
        label="智力障碍旅客 + 照护",
        description="智力障碍旅客（需照护）+ 1 位照护人",
        relation=RelationType.COUPLE,
        members=(
            _p("D1", "旅客", 26, support_needs=(SupportNeed.INTELLECTUAL_DISABILITY,),
               needs_caregiver=True),
            _p("D2", "照护人", 48, is_caregiver=True),
        ),
        bonds=((0, 1, BondType.MANDATORY),),
        tags=("重点", "需照护", "硬绑定"),
    ),
    PresetType(
        key="elderly_couple",
        label="老年夫妻",
        description="两位老人（偏好过道，方便活动）",
        relation=RelationType.COUPLE,
        members=(
            _p("E1", "老先生", 76, support_needs=(SupportNeed.ELDERLY,),
               preference_aisle=True),
            _p("E2", "老太太", 73, support_needs=(SupportNeed.ELDERLY,),
               preference_aisle=True),
        ),
        bonds=((0, 1, BondType.STRONG),),
        tags=("重点", "老人"),
    ),
    PresetType(
        key="group4",
        label="4 人团体",
        description="4 位同事同行（无显式声明，验证『同订单默认同座』）",
        relation=RelationType.GROUP,
        members=(
            _p("T1", "同事一", 30), _p("T2", "同事二", 31),
            _p("T3", "同事三", 33), _p("T4", "同事四", 29),
        ),
        tags=("团体", "默认同座"),
    ),
    PresetType(
        key="group6",
        label="6 人团体",
        description="6 位同行（无显式声明，考验『同订单默认同座』的极限）",
        relation=RelationType.GROUP,
        members=tuple(_p(f"K{i}", f"同行{i}", 26 + i) for i in range(1, 7)),
        tags=("团体", "默认同座", "长尾"),
    ),
    PresetType(
        key="students",
        label="学生团体",
        description="3 位学生票旅客（安静型）",
        relation=RelationType.GROUP,
        members=(
            _p("U1", "学生一", 20, ticket_type=TicketType.STUDENT,
               behavior=DeclaredBehavior.QUIET),
            _p("U2", "学生二", 21, ticket_type=TicketType.STUDENT,
               behavior=DeclaredBehavior.QUIET),
            _p("U3", "学生三", 19, ticket_type=TicketType.STUDENT,
               behavior=DeclaredBehavior.QUIET),
        ),
        tags=("学生",),
    ),
    PresetType(
        key="business",
        label="商务旅客",
        description="2 位成人，明确要求靠过道 / 靠窗（验证设施匹配奖励）",
        relation=RelationType.COUPLE,
        members=(
            _p("Z1", "商务甲", 41, preference_aisle=True),
            _p("Z2", "商务乙", 39, preference_window=True),
        ),
        tags=("基础", "设施偏好"),
    ),
    PresetType(
        key="low_credit",
        label="低信用旅客",
        description="静音信用分 45（低于 60，**禁止选静音车厢**，验证信用闭环）",
        relation=RelationType.SOLO,
        members=(_p("L1", "被投诉旅客", 37, quietness_score=45.0),),
        tags=("信用",),
    ),
    PresetType(
        key="mixed_big",
        label="混合大家庭",
        description="两位老人 + 两位家长 + 幼童 + 婴儿（6 人，最复杂组合）",
        relation=RelationType.MULTI_GEN_FAMILY,
        members=(
            _p("H1", "爷爷", 78, support_needs=(SupportNeed.ELDERLY,),
               needs_caregiver=True, preference_aisle=True),
            _p("H2", "爸爸", 45, is_caregiver=True),
            _p("H3", "妈妈", 43, is_caregiver=True),
            _p("H4", "幼童", 5, ticket_type=TicketType.CHILD,
               support_needs=(SupportNeed.TODDLER,), needs_caregiver=True,
               behavior=DeclaredBehavior.LIVELY),
            _p("H5", "婴儿", 2, ticket_type=TicketType.CHILD,
               support_needs=(SupportNeed.INFANT,), needs_caregiver=True),
            _p("H6", "奶奶", 75, support_needs=(SupportNeed.ELDERLY,)),
        ),
        bonds=(
            (0, 1, BondType.MANDATORY), (1, 3, BondType.MANDATORY),
            (2, 4, BondType.MANDATORY), (2, 3, BondType.MANDATORY),
            (5, 1, BondType.STRONG),
        ),
        tags=("重点", "复杂", "婴儿", "幼童", "老人"),
    ),
    PresetType(
        key="wheelchair_family",
        label="轮椅旅客 + 家属",
        description="轮椅旅客 + 2 位家属（验证据专区容量『轮椅进专区、家属陪同』）",
        relation=RelationType.MULTI_GEN_FAMILY,
        members=(
            _p("R1", "轮椅旅客", 70, support_needs=(SupportNeed.WHEELCHAIR,),
               needs_caregiver=True),
            _p("R2", "长子", 46, is_caregiver=True),
            _p("R3", "孙子", 9, ticket_type=TicketType.CHILD),
        ),
        bonds=((0, 1, BondType.MANDATORY), (1, 2, BondType.STRONG)),
        tags=("重点", "无障碍", "家属"),
    ),
)

PRESET_BY_KEY: dict[str, PresetType] = {preset.key: preset for preset in PRESETS}


def build_order(
    preset: PresetType,
    order_id: str,
    index: int = 0,
    default_bond: BondType | None = None,
) -> Order:
    """把人群类型实例化成一张订单。

    ``default_bond`` 决定"未显式声明关系的人"怎么处理，默认取
    :attr:`EngineConfig.same_order_bond_level`（即"同订单默认坐一起"）。
    """
    if default_bond is None:
        default_bond = (
            BondType.STRONG
            if DEFAULT_CONFIG.same_order_default_bond
            else BondType.SOFT
        )
    if DEFAULT_CONFIG.same_order_bond_level == "mandatory" and default_bond is BondType.STRONG:
        default_bond = BondType.MANDATORY

    passengers = tuple(
        member.build(f"{member.key}-{index}") for member in preset.members
    )
    bonds: dict[frozenset[str], BondType] = {}
    for a_index, b_index, bond in preset.bonds:
        bonds[frozenset((passengers[a_index].passenger_id, passengers[b_index].passenger_id))] = bond
    return Order(
        order_id=order_id,
        passengers=passengers,
        relation=preset.relation,
        bonds=bonds,
        default_bond=default_bond,
    )


def coverage_report(orders: list[Order]) -> dict[str, Any]:
    """报告本次模拟覆盖了哪些人群特征 —— 用于证明"全覆盖"而不是"看着多"。"""
    needs: set[str] = set()
    tickets: set[str] = set()
    behaviors: set[str] = set()
    bonds: dict[str, int] = {level.value: 0 for level in BondType}
    passenger_count = 0
    for order in orders:
        passenger_count += len(order.passengers)
        for passenger in order.passengers:
            needs.update(need.value for need in passenger.support_needs)
            tickets.add(passenger.ticket_type.value)
            behaviors.add(passenger.declared_behavior.value)
        for index, a in enumerate(order.passengers):
            for b in order.passengers[index + 1 :]:
                bonds[order.bond_of(a.passenger_id, b.passenger_id).value] += 1
    return {
        "passengers": passenger_count,
        "orders": len(orders),
        "support_needs": sorted(needs),
        "all_support_needs": sorted(need.value for need in SupportNeed),
        "missing_support_needs": sorted(
            {need.value for need in SupportNeed} - needs
        ),
        "ticket_types": sorted(tickets),
        "behaviors": sorted(behaviors),
        "bond_levels": bonds,
    }


__all__ = [
    "PRESETS",
    "PRESET_BY_KEY",
    "PassengerSpec",
    "PresetType",
    "build_order",
    "coverage_report",
]
