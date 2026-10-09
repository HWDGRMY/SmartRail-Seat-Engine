"""人员构成（composition）→ 订单（Order）的映射，接入选座引擎。

分工
----
* :mod:`smartrail.composition` 负责**组单与出票校验**（纯函数，不碰座位）；
* :mod:`smartrail.solver` 负责**给这些人找座位**（不知道"订单是怎么填的"）。

本模块是两者之间的唯一桥梁：把"成人 2 / 儿童 1 / 重度残疾成人 1"这类
计数，展开成引擎能理解的 :class:`~smartrail.models.Passenger` 列表。

两条必须守住的语义
------------------
1. **总人数只由基础分组求和**：细分、残疾、孕妇都是**已有乘客的标签**，
   展开时不能再生成新的人；
2. **陪同人已计入基础分组成人**：把"需要 1 名成人陪同"翻译成绑定关系，
   而不是新增一位乘客 —— 否则总人数就与基础分组对不上了。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .composition import (
    BASE_GROUP_IDS,
    OrderComposition,
    PlatformPolicy,
    check_composition,
)
from .models import (
    BondType,
    DeclaredBehavior,
    Order,
    Passenger,
    RelationType,
    SupportNeed,
    TicketType,
)

# 基础分组 → 引擎侧票种／年龄／标签
_BASE_SPEC: dict[str, dict[str, Any]] = {
    "adult": {"label": "成人", "age": 35, "ticket": TicketType.ADULT,
              "needs_caregiver": False, "is_caregiver": True},
    "youth": {"label": "青少年", "age": 16, "ticket": TicketType.CHILD,
              "needs_caregiver": False, "is_caregiver": False},
    "child": {"label": "儿童", "age": 9, "ticket": TicketType.CHILD,
              "needs_caregiver": True, "is_caregiver": False},
    "toddler": {"label": "幼儿", "age": 2, "ticket": TicketType.CHILD,
                "needs_caregiver": True, "is_caregiver": False},
    "infant": {"label": "婴儿", "age": 0, "ticket": TicketType.CHILD,
               "needs_caregiver": True, "is_caregiver": False},
}

# 基础分组 → 引擎侧支持需求（婴儿/幼儿有专门枚举）
_BASE_SUPPORT: dict[str, tuple[SupportNeed, ...]] = {
    "infant": (SupportNeed.INFANT,),
    "toddler": (SupportNeed.TODDLER,),
}

# 残疾程度 → 支持需求。重度/极重度按"需照护"处理（引擎侧有对应枚举）
_DISABILITY_SUPPORT: dict[str, tuple[SupportNeed, ...]] = {
    "mild": (),
    "moderate": (),
    "severe": (SupportNeed.INTELLECTUAL_DISABILITY,),
    "critical": (SupportNeed.INTELLECTUAL_DISABILITY,),
}

# 孕期 → 支持需求。足月（37 周+）映射到引擎的"孕晚期"硬约束
_PREGNANT_SUPPORT: dict[str, tuple[SupportNeed, ...]] = {
    "early": (),
    "mid": (),
    "late": (SupportNeed.PREGNANT_LATE,),
    "term": (SupportNeed.PREGNANT_LATE,),
}


@dataclass
class CompositionMapping:
    """映射结果：订单 + 校验结论 + 结构化标签（供前端展示）。"""

    order: Order
    check: Any
    total_passengers: int
    info: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "order_id": self.order.order_id,
            "check": self.check.to_dict(),
            "total_passengers": self.total_passengers,
            "info": dict(self.info),
            "passengers": [
                {
                    "passenger_id": p.passenger_id,
                    "name": p.name,
                    "ticket_type": p.ticket_type.value,
                    "age": p.age,
                    "support_needs": sorted(n.value for n in p.support_needs),
                    "needs_caregiver": p.needs_caregiver,
                    "is_caregiver": p.is_caregiver,
                }
                for p in self.order.passengers
            ],
        }


def _expand_attributes(
    composition: OrderComposition, band: str
) -> tuple[list[str | None], list[str | None]]:
    """把某一年龄段的"残疾程度"与"孕期"展开成**逐人的属性**。

    返回两个等长列表：``(残疾程度 or None, 孕期 or None)``，长度等于该年龄段
    在这个维度上的最大人数。

    关键点：**残疾与孕妇是不同的人**。早期实现把两个列表按同一个下标 zip
    到同一位乘客身上，于是出现过"成人1 = 重度残疾 + 孕妇37周+"这种
    物理上不可能的组合。现在改为顺序填充：先放残疾人，再放孕妇，
    各自占据不同的位置；两者总数超过该年龄段人数时由
    :func:`~smartrail.composition.validate_group_consistency` 报错。
    """
    levels: list[str | None] = []
    for level, row in composition.disability.items():
        levels.extend([level] * row.get(band, 0))
    stages: list[str | None] = []
    for stage, row in composition.pregnant.items():
        stages.extend([stage] * row.get(band, 0))

    size = max(len(levels), len(stages))
    disabilities: list[str | None] = list(levels) + [None] * (size - len(levels))
    pregnancies: list[str | None] = [None] * len(levels) + list(stages)
    pregnancies += [None] * (size - len(pregnancies))
    return disabilities, pregnancies


def _child_behavior(
    composition: OrderComposition, child_index: int
) -> DeclaredBehavior:
    """把儿童细分（安静/吵闹）映射到引擎的申报行为。

    细分是**标签**，按顺序贴给儿童：先贴安静，再贴吵闹，剩下的保持未知。
    """
    quiet = composition.child_sub.get("child_quiet", 0)
    noisy = composition.child_sub.get("child_noisy", 0)
    if child_index < quiet:
        return DeclaredBehavior.QUIET
    if child_index < quiet + noisy:
        return DeclaredBehavior.LIVELY
    return DeclaredBehavior.UNKNOWN


def build_order(
    composition: OrderComposition,
    policy: PlatformPolicy | None = None,
    order_id: str | None = None,
) -> CompositionMapping:
    """把人员构成展开成引擎订单。

    展开规则（严格守住"总人数只由基础分组求和"）：

    * 基础分组每个人都生成一位乘客 —— 这是**唯一**新增乘客的地方；
    * 残疾 / 孕妇 / 儿童细分都是**给已有乘客贴标签**，不新增人；
    * 陪同关系翻译成硬绑定（成人 ↔ 需要照护的人），而不是新增人。
    """
    policy = policy or PlatformPolicy()
    check = check_composition(composition, policy)
    oid = order_id or composition.order_id or "COMPOSE"
    identity = f"{oid}"
    passengers: list[Passenger] = []
    info: dict[str, Any] = {
        "base": dict(composition.base),
        "disability": {k: dict(v) for k, v in composition.disability.items()},
        "pregnant": {k: dict(v) for k, v in composition.pregnant.items()},
        "child_sub": dict(composition.child_sub),
        "services": list(composition.services),
    }

    seq = 0
    # 记录"需要照护的人"，稍后与可用健康成人做硬绑定
    care_ids: list[str] = []
    adult_ids: list[str] = []
    healthy_adult_ids: list[str] = []
    summaries: list[str] = []

    for band in BASE_GROUP_IDS:
        count = composition.base.get(band, 0)
        if count <= 0:
            continue
        spec = _BASE_SPEC[band]
        dis_levels, preg_stages = _expand_attributes(composition, band)
        unusable_adults = (
            composition.unavailable_adults(policy) if band == "adult" else 0
        )
        unusable_left = unusable_adults

        for index in range(count):
            seq += 1
            pid = f"{identity}-{band}-{index + 1}"
            needs: set[SupportNeed] = set(_BASE_SUPPORT.get(band, ()))
            behavior = DeclaredBehavior.UNKNOWN
            if band == "child":
                behavior = _child_behavior(composition, index)

            level = dis_levels[index] if index < len(dis_levels) else None
            stage = preg_stages[index] if index < len(preg_stages) else None
            if level:
                needs.update(_DISABILITY_SUPPORT.get(level, ()))
            if stage:
                needs.update(_PREGNANT_SUPPORT.get(stage, ()))

            # 需照护：基础分组本身要求（儿童/幼儿/婴儿），或命中重度以上残疾
            needs_care = bool(spec["needs_caregiver"])
            if stage == "term":
                needs_care = True
            if level in ("severe", "critical"):
                needs_care = True

            # 成人是否可充当陪同人：重/极重度残疾的成人不可
            is_caregiver = bool(spec["is_caregiver"])
            unavailable = False
            if band == "adult" and unusable_left > 0 and level in (
                "severe", "critical",
            ):
                is_caregiver = False
                unavailable = True
                unusable_left -= 1
            elif band == "adult" and unusable_left > 0 and policy.moderate_cannot_companion and level == "moderate":
                is_caregiver = False
                unavailable = True
                unusable_left -= 1

            tags = [spec["label"]]
            if level:
                from .composition import DISABILITY_LEVEL_BY_ID

                tags.append(f"{DISABILITY_LEVEL_BY_ID[level]['label']}残疾")
            if stage:
                from .composition import PREGNANT_STAGE_BY_ID

                tags.append(f"孕妇{PREGNANT_STAGE_BY_ID[stage]['label']}")
            name = "·".join(tags)
            summaries.append(name)

            passengers.append(
                Passenger(
                    passenger_id=pid,
                    name=name if len(tags) > 1 else f"{spec['label']}{index + 1}",
                    age=spec["age"],
                    ticket_type=spec["ticket"],
                    support_needs=frozenset(needs),
                    declared_behavior=behavior,
                    needs_caregiver=needs_care,
                    is_caregiver=is_caregiver and not needs_care,
                )
            )
            if needs_care:
                care_ids.append(pid)
            if band == "adult":
                adult_ids.append(pid)
                if not unavailable:
                    healthy_adult_ids.append(pid)

    # 陪同关系 -> 硬绑定（不新增人！）
    bonds: dict[frozenset[str], BondType] = {}
    for position, care_id in enumerate(care_ids):
        if not healthy_adult_ids:
            break
        adult_id = healthy_adult_ids[position % len(healthy_adult_ids)]
        bonds[frozenset((care_id, adult_id))] = BondType.MANDATORY

    # 关系拓扑：有照护需求就是照护单，否则按人数判断
    if care_ids:
        relation = RelationType.CARE
    elif len(passengers) > 1:
        relation = RelationType.GROUP
    else:
        relation = RelationType.SOLO

    order = Order(
        order_id=oid,
        passengers=tuple(passengers),
        relation=relation,
        bonds=bonds,
        # 同一订单内的人默认强绑定（"没人跟陌生人拼一个订单买票"）
        default_bond=BondType.STRONG,
    )
    info["care_passengers"] = list(care_ids)
    info["healthy_adults"] = list(healthy_adult_ids)
    info["bond_count"] = len(bonds)
    info["names"] = summaries
    info["key_passenger_service"] = composition.key_passenger_service
    info["companion_count"] = composition.companion_count
    return CompositionMapping(
        order=order,
        check=check,
        total_passengers=composition.total_passengers,
        info=info,
    )


__all__ = ["CompositionMapping", "build_order"]
