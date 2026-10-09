"""乘车人档案库（12306 的"乘车人"管理）。

需求要点
--------
* **预设各类人群各一位**，作为默认购票人（开发者模式要求）；
* 添加乘车人时**选择人群类型**（成人、儿童、学生…）；
* 乘车人列表为空时引导添加；已有乘车人时也能继续添加；
* **必须先添加乘车人才能提交订单**。

为什么把"人群类型"做成显式目录而不是散落的枚举
----------------------------------------------
界面上要下拉选择，后端要据此推导支持需求（轮椅→无障碍专区、婴儿→绑定照护人…）。
两边必须**同一份定义**，否则会出现"界面能选、后端不认识"的漂移。
所以类型目录只有这一份 :data:`PASSENGER_TYPES`。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Mapping, Sequence

from ..models import (
    BondType,
    DeclaredBehavior,
    Order,
    Passenger,
    RelationType,
    SupportNeed,
    TicketType,
)

# ---------------------------------------------------------------------------
# 人群类型目录（界面下拉 + 后端推导，唯一来源）
# ---------------------------------------------------------------------------

PASSENGER_TYPES: tuple[dict[str, Any], ...] = (
    {
        "id": "adult", "label": "成人", "age": 35, "ticket": TicketType.ADULT,
        "needs_caregiver": False, "is_caregiver": True, "support": (),
        "price_ratio": 1.0, "desc": "满 18 周岁及以上",
    },
    {
        "id": "student", "label": "学生", "age": 20, "ticket": TicketType.STUDENT,
        "needs_caregiver": False, "is_caregiver": True, "support": (),
        "price_ratio": 0.75, "desc": "持学生证，寒暑假可购学生票",
    },
    {
        "id": "youth", "label": "青少年", "age": 16, "ticket": TicketType.CHILD,
        "needs_caregiver": False, "is_caregiver": False, "support": (),
        "price_ratio": 0.5, "desc": "满 14 周岁但未满 18 周岁，可独立购票",
    },
    {
        "id": "child", "label": "儿童", "age": 9, "ticket": TicketType.CHILD,
        "needs_caregiver": True, "is_caregiver": False, "support": (),
        "price_ratio": 0.5, "desc": "满 4 周岁但未满 14 周岁，须成人陪同",
    },
    {
        "id": "toddler", "label": "幼儿", "age": 2, "ticket": TicketType.CHILD,
        "needs_caregiver": True, "is_caregiver": False,
        "support": (SupportNeed.TODDLER,),
        "price_ratio": 0.0, "desc": "满 1 周岁但未满 4 周岁，须成人陪同",
    },
    {
        "id": "infant", "label": "婴儿", "age": 0, "ticket": TicketType.CHILD,
        "needs_caregiver": True, "is_caregiver": False,
        "support": (SupportNeed.INFANT,),
        "price_ratio": 0.0, "desc": "未满 1 周岁，须成人陪同",
    },
    {
        "id": "elderly", "label": "老人", "age": 72, "ticket": TicketType.ADULT,
        "needs_caregiver": False, "is_caregiver": True,
        "support": (SupportNeed.ELDERLY,),
        "price_ratio": 1.0, "desc": "60 周岁以上，优先下铺/近车门",
    },
    {
        "id": "pregnant_early", "label": "孕妇（1-3个月）", "age": 30,
        "ticket": TicketType.ADULT, "needs_caregiver": False,
        "is_caregiver": True, "support": (), "price_ratio": 1.0,
        "desc": "可独立购票，不强制陪同",
    },
    {
        "id": "pregnant_mid", "label": "孕妇（4-6个月）", "age": 30,
        "ticket": TicketType.ADULT, "needs_caregiver": False,
        "is_caregiver": True, "support": (), "price_ratio": 1.0,
        "desc": "可独立购票，不强制陪同",
    },
    {
        "id": "pregnant_late", "label": "孕妇（7-9个月）", "age": 30,
        "ticket": TicketType.ADULT, "needs_caregiver": True,
        "is_caregiver": False, "support": (SupportNeed.PREGNANT_LATE,),
        "price_ratio": 1.0, "desc": "近卫生间/过道；平台可配置是否强制陪同",
    },
    {
        "id": "pregnant_term", "label": "孕妇（10个月/37周+）", "age": 30,
        "ticket": TicketType.ADULT, "needs_caregiver": True,
        "is_caregiver": False, "support": (SupportNeed.PREGNANT_LATE,),
        "price_ratio": 1.0, "desc": "必须重点旅客服务 + 至少 1 名成人陪同",
    },
    {
        "id": "wheelchair", "label": "轮椅旅客", "age": 45,
        "ticket": TicketType.DISABLED_VETERAN, "needs_caregiver": False,
        "is_caregiver": False, "support": (SupportNeed.WHEELCHAIR,),
        "price_ratio": 1.0, "desc": "硬约束匹配无障碍专区",
    },
    {
        "id": "blind", "label": "视障旅客", "age": 40,
        "ticket": TicketType.DISABLED_VETERAN, "needs_caregiver": False,
        "is_caregiver": False, "support": (SupportNeed.INDEPENDENT_BLIND,),
        "price_ratio": 1.0, "desc": "独立视障：偏好过道/近车门，严禁强行匹配陪护",
    },
    {
        "id": "blind_with_guide", "label": "视障（需导盲）", "age": 40,
        "ticket": TicketType.DISABLED_VETERAN, "needs_caregiver": True,
        "is_caregiver": False, "support": (SupportNeed.INDEPENDENT_BLIND,),
        "price_ratio": 1.0, "desc": "需导盲犬或引导服务，须陪同",
    },
    {
        "id": "intellectual", "label": "智力障碍", "age": 25,
        "ticket": TicketType.DISABLED_VETERAN, "needs_caregiver": True,
        "is_caregiver": False,
        "support": (SupportNeed.INTELLECTUAL_DISABILITY,),
        "price_ratio": 1.0, "desc": "需照护，须陪同",
    },
    {
        "id": "caregiver", "label": "照护人/陪同", "age": 40,
        "ticket": TicketType.ADULT, "needs_caregiver": False,
        "is_caregiver": True, "support": (), "price_ratio": 1.0,
        "desc": "健康成人，可与需照护者硬绑定",
    },
)

#: 乘客档案来源
SOURCE_PRESET = "preset"
"""内置预制档案：**只给开发者模式**做默认购票人，用户模式不可见。"""

SOURCE_USER = "user"
"""用户在购票页自己添加的乘车人。"""

PASSENGER_TYPE_BY_ID: dict[str, dict[str, Any]] = {
    item["id"]: item for item in PASSENGER_TYPES
}

#: 界面选择的分组。
#:
#: **为什么这样分组**：早期把 16 种类型平铺给旅客看，里面并列出现了
#: "成人 / 幼儿 / 孕妇（1-3个月）/ 孕妇（4-6个月）/ 孕妇（7-9个月）/
#: 孕妇（10个月/37周+）/ 视障（需导盲）……" —— 旅客第一眼看到的是
#: 一堆需要医学与无障碍知识才能选的选项，普通人只会困惑。
#:
#: 真实的购票界面只有"成人 / 学生 / 儿童"这几档；孕妇按孕周、
#: 残疾按类别确实需要区分（因为待遇不同），但它们属于**特殊服务**，
#: 应当是"需要时才展开"的第二层，而不是和"成人"并列的第一层。
#:
#: 因此分成：
#:   * ``basic``      —— 常用（成人 / 学生 / 儿童 / 老人）
#:   * ``pregnant``   —— 孕妇（按孕周细分）
#:   * ``disabled``   —— 残疾旅客（按类别细分）
#:   * ``companion``  —— 陪同人
#:
#: ``basic=True`` 的分组在界面上直接平铺；其余分组收在"需要特别服务"里。
PASSENGER_TYPE_GROUPS: tuple[dict[str, Any], ...] = (
    {"id": "basic", "label": "常用", "basic": True,
     "hint": "绝大多数旅客选这几种",
     "types": ("adult", "student", "child", "elderly")},
    {"id": "pregnant", "label": "孕妇", "basic": False,
     "hint": "按孕周区分，晚期与足月需重点旅客服务",
     "types": ("pregnant_early", "pregnant_mid", "pregnant_late", "pregnant_term")},
    {"id": "disabled", "label": "残疾旅客", "basic": False,
     "hint": "按类别区分，轮椅旅客硬约束匹配轮椅停放位",
     "types": ("wheelchair", "blind", "blind_with_guide", "intellectual")},
    {"id": "companion", "label": "陪同人", "basic": False,
     "hint": "健康成人，用于与需照护者绑定同车厢",
     "types": ("caregiver",)},
    {"id": "minor", "label": "未成年细分", "basic": False,
     "hint": "青少年可独立购票；幼儿与婴儿须成人陪同",
     "types": ("youth", "toddler", "infant")},
)


# ---------------------------------------------------------------------------
# 乘车人档案
# ---------------------------------------------------------------------------


@dataclass
class PassengerProfile:
    """一位乘车人（含实名信息与人群类型）。"""

    profile_id: str
    name: str
    type_id: str
    id_card: str = ""
    phone: str = ""
    # 覆盖类型默认值（个别旅客有特殊需求时可以单独改）
    needs_caregiver: bool | None = None
    is_caregiver: bool | None = None
    declared_behavior: str = "unknown"
    source: str = SOURCE_USER
    """来源：``preset``（内置预制）或 ``user``（用户在购票页添加）。

    这个区分是**需求要求的**：预制数据是给**开发者模式**做默认购票人的，
    不是给用户看的。早期实现让用户模式的"选择乘车人"直接列出全部预制档案，
    等于把调试数据塞给了旅客 —— 既不真实（旅客不该看到别人的身份证号），
    也让"列表为空时引导添加"这条交互永远触发不到。
    """

    @property
    def spec(self) -> dict[str, Any]:
        return PASSENGER_TYPE_BY_ID.get(self.type_id, PASSENGER_TYPE_BY_ID["adult"])

    @property
    def label(self) -> str:
        return self.spec["label"]

    @property
    def age(self) -> int:
        return int(self.spec["age"])

    @property
    def ticket_type(self) -> TicketType:
        return self.spec["ticket"]

    @property
    def price_ratio(self) -> float:
        return float(self.spec["price_ratio"])

    def support_needs(self) -> frozenset[SupportNeed]:
        return frozenset(self.spec["support"])

    def resolves_caregiver(self) -> bool:
        if self.is_caregiver is not None:
            return bool(self.is_caregiver)
        return bool(self.spec["is_caregiver"])

    def resolves_needs_caregiver(self) -> bool:
        if self.needs_caregiver is not None:
            return bool(self.needs_caregiver)
        return bool(self.spec["needs_caregiver"])

    def to_passenger(self, order_id: str, index: int) -> Passenger:
        return Passenger(
            passenger_id=f"{order_id}-P{index + 1}",
            name=self.name,
            age=self.age,
            ticket_type=self.ticket_type,
            support_needs=self.support_needs(),
            declared_behavior=DeclaredBehavior(self.declared_behavior)
            if self.declared_behavior in {item.value for item in DeclaredBehavior}
            else DeclaredBehavior.UNKNOWN,
            needs_caregiver=self.resolves_needs_caregiver(),
            is_caregiver=self.resolves_caregiver() and not self.resolves_needs_caregiver(),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "name": self.name,
            "type_id": self.type_id,
            "type_label": self.label,
            "type_desc": self.spec["desc"],
            "age": self.age,
            "id_card": self.id_card,
            "phone": self.phone,
            "needs_caregiver": self.resolves_needs_caregiver(),
            "is_caregiver": self.resolves_caregiver(),
            "support_needs": sorted(n.value for n in self.support_needs()),
            "price_ratio": self.price_ratio,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> PassengerProfile:
        type_id = str(payload.get("type_id") or "adult")
        if type_id not in PASSENGER_TYPE_BY_ID:
            type_id = "adult"
        return cls(
            profile_id=str(payload.get("profile_id") or ""),
            name=str(payload.get("name") or "").strip(),
            type_id=type_id,
            id_card=str(payload.get("id_card") or ""),
            phone=str(payload.get("phone") or ""),
            needs_caregiver=payload.get("needs_caregiver"),
            is_caregiver=payload.get("is_caregiver"),
            declared_behavior=str(payload.get("declared_behavior") or "unknown"),
            source=str(payload.get("source") or SOURCE_USER),
        )


#: 各类人群各一位，作为默认购票人（开发者模式要求"预制数据"）。
#: 注意：``type_id`` **不得重复** —— 需求是"各类人群各一位"。
#: 早期这里有两位 ``adult``（周昊、周淑秀），test 会抓出来"只覆盖 15 种类型"。
DEFAULT_PROFILES: tuple[dict[str, str], ...] = (
    {"name": "周昊", "type_id": "adult", "id_card": "6204**********036",
     "phone": "138****0036"},
    {"name": "肖世泽", "type_id": "student", "id_card": "6204**********137",
     "phone": "139****0137"},
    {"name": "周海全", "type_id": "elderly", "id_card": "6204**********334",
     "phone": "137****0334"},
    {"name": "李思远", "type_id": "youth", "id_card": "6204**********518",
     "phone": "135****0518"},
    {"name": "王一诺", "type_id": "child", "id_card": "6204**********602",
     "phone": "138****0036"},
    {"name": "王二宝", "type_id": "toddler", "id_card": "6204**********711",
     "phone": "138****0036"},
    {"name": "王小满", "type_id": "infant", "id_card": "6204**********820",
     "phone": "138****0036"},
    {"name": "陈静怡", "type_id": "pregnant_early", "id_card": "6204**********903",
     "phone": "133****0903"},
    {"name": "赵敏", "type_id": "pregnant_mid", "id_card": "6204**********968",
     "phone": "132****0968"},
    {"name": "孙丽华", "type_id": "pregnant_late", "id_card": "6204**********014",
     "phone": "132****0014"},
    {"name": "孙丽萍", "type_id": "pregnant_term", "id_card": "6204**********125",
     "phone": "131****0125"},
    {"name": "刘建国", "type_id": "wheelchair", "id_card": "6204**********236",
     "phone": "130****0236"},
    {"name": "郑海燕", "type_id": "blind", "id_card": "6204**********347",
     "phone": "189****0347"},
    {"name": "冯光明", "type_id": "blind_with_guide", "id_card": "6204**********458",
     "phone": "188****0458"},
    {"name": "许文强", "type_id": "intellectual", "id_card": "6204**********569",
     "phone": "187****0569"},
    {"name": "何秀兰", "type_id": "caregiver", "id_card": "6204**********670",
     "phone": "186****0670"},
)


class PassengerStore:
    """乘车人档案库（内存态，进程内共享）。"""

    def __init__(self, profiles: Iterable[PassengerProfile] | None = None) -> None:
        self._profiles: list[PassengerProfile] = list(profiles or [])
        # 序号**从现有档案往后接**，而不是从 0 开始重新数。
        # 早期写成 ``self._seq = 0``，于是往预制档案库里 add 第一位乘车人时
        # 生成的编号 C001 与预制数据撞车 —— 直接抛"编号已存在"，
        # 界面表现为"添加乘车人按钮报错"。
        self._seq = max(
            (self._id_number(p.profile_id) for p in self._profiles), default=0
        )

    @staticmethod
    def _id_number(profile_id: str) -> int:
        """从 ``C007`` 这样的编号里取序号；不符合格式的返回 0。"""
        digits = "".join(ch for ch in profile_id if ch.isdigit())
        return int(digits) if digits else 0

    # -- 查询 ----------------------------------------------------------
    def all(self) -> list[PassengerProfile]:
        """全部档案（含预制）。**开发者模式**用。"""
        return list(self._profiles)

    def presets(self) -> list[PassengerProfile]:
        """仅内置预制档案（各类人群各一位）。"""
        return [p for p in self._profiles if p.source == SOURCE_PRESET]

    def user_added(self) -> list[PassengerProfile]:
        """仅用户自己添加的乘车人。**用户模式**只应看到这些。"""
        return [p for p in self._profiles if p.source != SOURCE_PRESET]

    def get(self, profile_id: str) -> PassengerProfile | None:
        for profile in self._profiles:
            if profile.profile_id == profile_id:
                return profile
        return None

    def by_ids(self, ids: Sequence[str]) -> list[PassengerProfile]:
        """按给定顺序取乘车人；**顺序即选座优先顺序**。"""
        found: list[PassengerProfile] = []
        index = {p.profile_id: p for p in self._profiles}
        for profile_id in ids:
            profile = index.get(profile_id)
            if profile is not None:
                found.append(profile)
        return found

    # -- 增删改 --------------------------------------------------------
    def add(self, payload: Mapping[str, Any]) -> PassengerProfile:
        profile = PassengerProfile.from_dict(payload)
        if not profile.name:
            raise ValueError("乘车人姓名不能为空")
        self._seq += 1
        if not profile.profile_id:
            profile = replace(profile, profile_id=f"C{self._seq:03d}")
        if self.get(profile.profile_id) is not None:
            raise ValueError(f"乘车人编号已存在：{profile.profile_id}")
        # 通过接口新增的一律算"用户添加"，即使调用方没显式给 source
        if profile.source == SOURCE_PRESET:
            profile = replace(profile, source=SOURCE_USER)
        self._profiles.append(profile)
        return profile

    def update(self, profile_id: str, payload: Mapping[str, Any]) -> PassengerProfile:
        current = self.get(profile_id)
        if current is None:
            raise ValueError(f"乘车人不存在：{profile_id}")
        merged = {**current.to_dict(), **dict(payload), "profile_id": profile_id}
        updated = PassengerProfile.from_dict(merged)
        self._profiles[self._profiles.index(current)] = updated
        return updated

    def remove(self, profile_id: str) -> bool:
        current = self.get(profile_id)
        if current is None:
            return False
        self._profiles.remove(current)
        return True

    def reset(self) -> None:
        self._profiles = build_default_profiles()
        self._seq = max(
            (self._id_number(p.profile_id) for p in self._profiles), default=0
        )


def build_default_profiles() -> list[PassengerProfile]:
    """预制各类人群各一位（**仅供开发者模式**）。"""
    return [
        PassengerProfile(profile_id=f"C{index + 1:03d}", source=SOURCE_PRESET, **item)
        for index, item in enumerate(DEFAULT_PROFILES)
    ]


_STORE: PassengerStore | None = None


def get_passenger_store() -> PassengerStore:
    global _STORE
    if _STORE is None:
        _STORE = PassengerStore(build_default_profiles())
    return _STORE


def reset_passenger_store() -> PassengerStore:
    global _STORE
    _STORE = PassengerStore(build_default_profiles())
    return _STORE


def passenger_type_catalog() -> dict[str, Any]:
    """界面"选择人群类型"用的完整目录。"""
    return {
        "types": [
            {
                "id": item["id"],
                "label": item["label"],
                "desc": item["desc"],
                "age": item["age"],
                "ticket": item["ticket"].value,
                "price_ratio": item["price_ratio"],
                "needs_caregiver": item["needs_caregiver"],
                "is_caregiver": item["is_caregiver"],
                "support_needs": sorted(n.value for n in item["support"]),
            }
            for item in PASSENGER_TYPES
        ],
        "groups": [
            {
                "id": group["id"],
                "label": group["label"],
                # 界面上"直接平铺"还是"收进需要特别服务"
                "basic": bool(group.get("basic")),
                "hint": group.get("hint", ""),
                "types": [
                    {
                        "id": tid,
                        "label": PASSENGER_TYPE_BY_ID[tid]["label"],
                        "desc": PASSENGER_TYPE_BY_ID[tid]["desc"],
                    }
                    for tid in group["types"]
                ],
            }
            for group in PASSENGER_TYPE_GROUPS
        ],
    }


# ---------------------------------------------------------------------------
# 乘车人 → 订单
# ---------------------------------------------------------------------------


def build_profile_order(
    profiles: Sequence[PassengerProfile],
    order_id: str = "TICKET",
) -> Order:
    """把选中的乘车人变成一张订单。

    默认策略与 12306 一致：**同一订单的人坐在一起**（谁跟不认识的人拼一张单呢），
    因此 ``default_bond`` 为强绑定；需要照护的人与可用成人之间额外加**硬绑定**。
    """
    passengers = [
        profile.to_passenger(order_id, index) for index, profile in enumerate(profiles)
    ]
    care = [p for p in passengers if p.needs_caregiver]
    adults = [p for p in passengers if p.is_caregiver]
    bonds: dict[frozenset[str], BondType] = {}
    for position, child in enumerate(care):
        if not adults:
            break
        adult = adults[position % len(adults)]
        bonds[frozenset((child.passenger_id, adult.passenger_id))] = BondType.MANDATORY

    if care:
        relation = RelationType.CARE
    elif len(passengers) > 1:
        relation = RelationType.GROUP
    else:
        relation = RelationType.SOLO

    return Order(
        order_id=order_id,
        passengers=tuple(passengers),
        relation=relation,
        bonds=bonds,
        default_bond=BondType.STRONG,
    )


def validate_selection(profiles: Sequence[PassengerProfile]) -> list[str]:
    """提交前的乘车人校验（返回错误列表，空表示通过）。

    对应需求："必须添加乘车人后，才能提交购票"，
    以及出票规则里"未满 14 周岁须成人陪同"。
    """
    errors: list[str] = []
    if not profiles:
        errors.append("请先添加乘车人")
        return errors
    minors = [p for p in profiles if p.resolves_needs_caregiver() and p.age < 14]
    adults = [p for p in profiles if p.resolves_caregiver()]
    if minors and not adults:
        errors.append(
            "未满 14 周岁的婴儿、幼儿、儿童必须至少由 1 名成人陪同，否则不予出票"
        )
    names: set[str] = set()
    for profile in profiles:
        if profile.name in names:
            errors.append(f"乘车人重复：{profile.name}")
        names.add(profile.name)
    return errors


__all__ = [
    "DEFAULT_PROFILES",
    "PASSENGER_TYPES",
    "PASSENGER_TYPE_BY_ID",
    "PASSENGER_TYPE_GROUPS",
    "PassengerProfile",
    "PassengerStore",
    "build_default_profiles",
    "build_profile_order",
    "get_passenger_store",
    "passenger_type_catalog",
    "reset_passenger_store",
    "validate_selection",
]
