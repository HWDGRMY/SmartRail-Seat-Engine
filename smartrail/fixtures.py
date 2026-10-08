"""建模辅助工厂：让测试、仿真与演示用尽可能少的代码构造乘客图谱与订单关系图。"""

from __future__ import annotations

from typing import Iterable, Mapping, Sequence

from .models import (
    BondType,
    DeclaredBehavior,
    Order,
    Passenger,
    RelationType,
    SupportNeed,
    TicketType,
)

# ---------------------------------------------------------------------------
# 乘客工厂
# ---------------------------------------------------------------------------


def adult(
    pid: str,
    age: int = 35,
    name: str = "",
    aisle: bool = False,
    window: bool = False,
    quietness: float = 100.0,
    needs: Iterable[SupportNeed] = (),
    caregiver: bool = False,
) -> Passenger:
    return Passenger(
        passenger_id=pid,
        ticket_type=TicketType.ADULT,
        age=age,
        name=name or pid,
        support_needs=frozenset(needs),
        quietness_score=quietness,
        preference_aisle=aisle,
        preference_window=window,
        is_caregiver=caregiver,
    )


def child(
    pid: str,
    age: int = 6,
    name: str = "",
    behavior: DeclaredBehavior = DeclaredBehavior.UNKNOWN,
    quietness: float = 100.0,
) -> Passenger:
    needs = {SupportNeed.INFANT} if age <= 3 else ({SupportNeed.TODDLER} if age <= 6 else set())
    return Passenger(
        passenger_id=pid,
        ticket_type=TicketType.CHILD,
        age=age,
        name=name or pid,
        support_needs=frozenset(needs),
        declared_behavior=behavior,
        quietness_score=quietness,
        needs_caregiver=True,
    )


def wheelchair(pid: str, age: int = 40, name: str = "") -> Passenger:
    return Passenger(
        passenger_id=pid,
        ticket_type=TicketType.ADULT,
        age=age,
        name=name or pid,
        support_needs=frozenset({SupportNeed.WHEELCHAIR}),
        needs_caregiver=False,
    )


def blind(pid: str, age: int = 45, name: str = "") -> Passenger:
    return Passenger(
        passenger_id=pid,
        ticket_type=TicketType.ADULT,
        age=age,
        name=name or pid,
        support_needs=frozenset({SupportNeed.INDEPENDENT_BLIND}),
        preference_aisle=True,
        is_caregiver=False,
    )


def pregnant(pid: str, age: int = 32, name: str = "") -> Passenger:
    return Passenger(
        passenger_id=pid,
        ticket_type=TicketType.ADULT,
        age=age,
        name=name or pid,
        support_needs=frozenset({SupportNeed.PREGNANT_LATE}),
        preference_aisle=True,
        needs_caregiver=True,
    )


def elderly(pid: str, age: int = 76, name: str = "") -> Passenger:
    return Passenger(
        passenger_id=pid,
        ticket_type=TicketType.ADULT,
        age=age,
        name=name or pid,
        support_needs=frozenset({SupportNeed.ELDERLY}),
    )


# ---------------------------------------------------------------------------
# 订单关系图工厂
# ---------------------------------------------------------------------------


def _bonds(entries: Mapping[tuple[str, str], BondType]) -> dict[frozenset[str], BondType]:
    return {frozenset(pair): bond for pair, bond in entries.items()}


def solo_order(pid: str, **kwargs) -> Order:
    return Order(
        order_id=f"O-{pid}",
        passengers=(adult(pid, **kwargs),),
        relation=RelationType.SOLO,
        bonds={},
    )


def couple_order(a_id: str = "A1", b_id: str = "A2", order_id: str = "O-COUPLE") -> Order:
    return Order(
        order_id=order_id,
        passengers=(adult(a_id, name="伴侣A"), adult(b_id, name="伴侣B")),
        relation=RelationType.COUPLE,
        bonds=_bonds({(a_id, b_id): BondType.STRONG}),
    )


def family_with_child_order(
    order_id: str = "O-FAMILY",
    child_age: int = 6,
    behavior: DeclaredBehavior = DeclaredBehavior.LIVELY,
) -> Order:
    """核心家庭：两名成人 + 一名儿童（儿童与成人 A 走 MANDATORY 绑定）。"""
    a = adult("A1", name="家长A", caregiver=True)
    b = adult("A2", name="家长B")
    c = child("C1", age=child_age, name="小朋友", behavior=behavior)
    return Order(
        order_id=order_id,
        passengers=(a, b, c),
        relation=RelationType.NUCLEAR_FAMILY,
        bonds=_bonds({("A1", "C1"): BondType.MANDATORY, ("A1", "A2"): BondType.STRONG,
                      ("A2", "C1"): BondType.STRONG}),
    )


def multi_gen_order(order_id: str = "O-MULTIGEN") -> Order:
    """多代家庭：两位老人 + 两名成人 + 一名婴儿。"""
    g1 = elderly("G1", name="爷爷")
    g2 = elderly("G2", 72, name="奶奶")
    a1 = adult("A1", name="儿子", caregiver=True)
    a2 = adult("A2", name="儿媳")
    baby = child("B1", age=1, name="宝宝", behavior=DeclaredBehavior.UNKNOWN)
    return Order(
        order_id=order_id,
        passengers=(g1, g2, a1, a2, baby),
        relation=RelationType.MULTI_GEN_FAMILY,
        bonds=_bonds(
            {
                ("A1", "B1"): BondType.MANDATORY,
                ("A2", "B1"): BondType.MANDATORY,
                ("A1", "A2"): BondType.STRONG,
                ("G1", "G2"): BondType.STRONG,
                ("A1", "G1"): BondType.STRONG,
                ("A2", "G2"): BondType.STRONG,
            }
        ),
    )


def wheelchair_order(order_id: str = "O-WHEEL", with_companion: bool = True) -> Order:
    w = wheelchair("W1", name="轮椅旅客")
    if not with_companion:
        return Order(order_id=order_id, passengers=(w,), relation=RelationType.SOLO, bonds={})
    c = adult("A1", name="陪同家属", caregiver=True)
    return Order(
        order_id=order_id,
        passengers=(w, c),
        relation=RelationType.CARE,
        bonds=_bonds({("W1", "A1"): BondType.STRONG}),
    )


def blind_order(order_id: str = "O-BLIND") -> Order:
    """独立视障：**不配陌生人**，只给过道/近车门。"""
    return Order(
        order_id=order_id,
        passengers=(blind("B1", name="视障旅客"),),
        relation=RelationType.SOLO,
        bonds={},
    )


def pregnant_order(order_id: str = "O-PREG") -> Order:
    w = pregnant("P1", name="孕晚期旅客")
    h = adult("A1", name="丈夫", caregiver=True)
    return Order(
        order_id=order_id,
        passengers=(w, h),
        relation=RelationType.CARE,
        bonds=_bonds({("P1", "A1"): BondType.MANDATORY}),
    )


def group_order(size: int = 6, order_id: str = "O-GROUP") -> Order:
    people = tuple(adult(f"T{i}", age=20 + i, name=f"团员{i}") for i in range(1, size + 1))
    bonds = {
        frozenset((people[i].passenger_id, people[i + 1].passenger_id)): BondType.STRONG
        for i in range(len(people) - 1)
    }
    return Order(order_id=order_id, passengers=people, relation=RelationType.GROUP, bonds=bonds)


SCENARIOS: dict[str, callable] = {
    "solo": lambda: solo_order("S1"),
    "couple": couple_order,
    "family_with_child": family_with_child_order,
    "multi_gen": multi_gen_order,
    "wheelchair": wheelchair_order,
    "blind": blind_order,
    "pregnant": pregnant_order,
    "group6": lambda: group_order(6),
}


def scenario_names() -> Sequence[str]:
    return tuple(SCENARIOS)
