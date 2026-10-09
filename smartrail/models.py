"""领域模型（Data Models）。

对应 README 第 3 节：乘客图谱（Passenger Graph）与座位图谱（Seat Vector）。
所有模型均为 frozen dataclass，保证可哈希、可比较、可缓存，便于在毫秒级求解器里
作为字典键使用。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

# ---------------------------------------------------------------------------
# 3.1 乘客图谱
# ---------------------------------------------------------------------------


class TicketType(str, Enum):
    """票种。"""

    ADULT = "adult"              # 成人
    CHILD = "child"              # 儿童
    STUDENT = "student"          # 学生
    DISABLED_VETERAN = "disabled_veteran"  # 残疾军人


class RelationType(str, Enum):
    """同订单下的关系拓扑。"""

    SOLO = "solo"                  # 独立出行
    COUPLE = "couple"              # 伴侣
    NUCLEAR_FAMILY = "nuclear_family"        # 核心家庭
    MULTI_GEN_FAMILY = "multi_gen_family"    # 多代家庭
    CARE = "care"                  # 看护 / 陪护
    GROUP = "group"                # 团体


class SupportNeed(str, Enum):
    """能力与医疗状态（政务 API 静默获取或前端申报）。"""

    WHEELCHAIR = "wheelchair"              # 轮椅：硬约束匹配无障碍专区
    INDEPENDENT_BLIND = "independent_blind"  # 独立视障：偏好过道/近车门，严禁强行匹配陪护
    PREGNANT_LATE = "pregnant_late"        # 孕晚期：硬约束绑定同行人 + 近卫生间/过道
    INFANT = "infant"                      # 婴儿（0-3 岁）：绝对硬约束绑定看护人
    TODDLER = "toddler"                    # 幼童（3-6 岁）：绝对硬约束绑定看护人
    ELDERLY = "elderly"                    # 老人：软偏好过道/近车门
    INTELLECTUAL_DISABILITY = "intellectual_disability"  # 智力障碍：需照护


class DeclaredBehavior(str, Enum):
    """家长申报的行为倾向。"""

    LIVELY = "lively"    # 活泼型
    QUIET = "quiet"      # 安静型
    UNKNOWN = "unknown"


class BondType(str, Enum):
    """绑定强度（用于乘客团元胞划分）。"""

    MANDATORY = "mandatory"  # 硬核单元：绝对不可拆（Tier 0）
    STRONG = "strong"        # 强绑定：不应跨车厢（Tier 1）
    SOFT = "soft"            # 软绑定：可拆，但拆开有代价（Tier 2/4）


class DataSource(str, Enum):
    """属性来源，用于审计与可解释性。"""

    TICKET = "ticket"          # 票种推断
    DECLARED = "declared"      # 前端申报
    GOV_API = "gov_api"        # 政务数据 API


@dataclass(frozen=True)
class Passenger:
    """单个乘客的多维向量。"""

    passenger_id: str
    ticket_type: TicketType = TicketType.ADULT
    age: int = 30
    name: str = ""
    support_needs: frozenset[SupportNeed] = field(default_factory=frozenset)
    declared_behavior: DeclaredBehavior = DeclaredBehavior.UNKNOWN
    quietness_score: float = 100.0
    preference_window: bool = False
    preference_aisle: bool = False
    preference_quiet: bool = False
    """用户**主动勾选**"请优先为我分配静音车厢席位"。

    与"合规奖励" ``t5_quiet_solo_adult`` 是两件事，别混：

    * 后者是**合规性奖励** —— 系统鼓励"本就不会吵的旅客"进静音车厢，
      与用户是否勾选无关，因此不勾选时也可能被分到静音车厢；
    * 本字段是**用户偏好** —— 勾了就应当在余票允许时优先满足，
      不勾也不该被惩罚（静音车厢并非"更高级"，只是不同）。

    早期实现只加了前者、没加这个字段，导致"勾选静音"与"不勾选"的下座
    **完全一样** —— 界面上的勾选框是个装饰。
    """
    needs_caregiver: bool = False   # 需照护人员（儿童/婴儿/智力障碍/孕晚期等）
    is_caregiver: bool = False      # 本次出行承担照护职责
    source: DataSource = DataSource.DECLARED
    metadata: Mapping[str, Any] = field(default_factory=dict)

    # -- 派生属性 ---------------------------------------------------------
    @property
    def is_child(self) -> bool:
        return self.ticket_type is TicketType.CHILD or self.age < 14

    @property
    def is_vulnerable(self) -> bool:
        """弱势群体：需要额外照护或设施保障。"""
        return bool(self.support_needs) or self.is_child or self.age >= 70

    @property
    def is_infant(self) -> bool:
        """婴儿（0-3 岁）：不可控，极高排斥静音车厢。"""
        return SupportNeed.INFANT in self.support_needs or self.age <= 3

    @property
    def is_mobility_impaired(self) -> bool:
        return SupportNeed.WHEELCHAIR in self.support_needs

    @property
    def quiet_carriage_blocked(self) -> bool:
        """静音信用分低于 60，强制屏蔽静音车厢选择权限。"""
        return self.quietness_score < 60.0

    @property
    def quiet_repulsion(self) -> float:
        """静音车厢排斥权重：[0, 1]，越大越不该进静音车厢。

        婴儿不可控 -> 1.0；活泼型儿童 -> 0.85；普通儿童 -> 0.5；
        成人 -> 0.0。静音信用分越低，权重越高。
        """
        base = 0.0
        if self.is_infant:
            base = 1.0
        elif self.is_child:
            base = 0.85 if self.declared_behavior is DeclaredBehavior.LIVELY else 0.5
        elif self.support_needs & {
            SupportNeed.WHEELCHAIR,
            SupportNeed.INDEPENDENT_BLIND,
            SupportNeed.INTELLECTUAL_DISABILITY,
        }:
            base = 0.6
        if self.declared_behavior is DeclaredBehavior.LIVELY:
            base = max(base, 0.85)
        # 信用分扣减放大排斥：100 分 -> 不放大；0 分 -> ×2
        penalty = max(0.0, (100.0 - self.quietness_score) / 100.0)
        return min(2.0, base * (1.0 + penalty))


@dataclass(frozen=True)
class PassengerUnit:
    """乘客团元胞：必须一起决策的最小单元（硬核单元 / 软性单元）。"""

    unit_id: str
    passengers: tuple[Passenger, ...]
    bond: BondType
    relation: RelationType = RelationType.SOLO

    @property
    def size(self) -> int:
        return len(self.passengers)

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(p.passenger_id for p in self.passengers)

    @property
    def has_vulnerable(self) -> bool:
        return any(p.is_vulnerable for p in self.passengers)

    @property
    def quiet_repulsion(self) -> float:
        """单元级别的静音车厢排斥权重（取最大值）。"""
        return max((p.quiet_repulsion for p in self.passengers), default=0.0)

    @property
    def weight(self) -> float:
        """优先级权重：硬核 > 强绑定 > 软绑定；弱势群体加权。"""
        base = {BondType.MANDATORY: 1000.0, BondType.STRONG: 100.0, BondType.SOFT: 10.0}[self.bond]
        return base + (500.0 if self.has_vulnerable else 0.0) + self.size


@dataclass(frozen=True)
class Order:
    """一次购票请求（同订单 ID 下的关系图）。"""

    order_id: str
    passengers: tuple[Passenger, ...]
    relation: RelationType = RelationType.SOLO
    units: tuple[PassengerUnit, ...] = ()
    # 乘客 ID 对 -> 绑定强度，用于跨元胞关系
    bonds: Mapping[frozenset[str], BondType] = field(default_factory=dict)
    default_bond: "BondType | None" = None
    """未显式声明关系时的默认绑定强度。

    ``None`` 表示"按 :class:`~smartrail.config.EngineConfig` 的配置决定"；
    构造订单的一侧（API / 仿真器）会把它设成 ``STRONG``，实现"同订单默认坐一起"。
    显式设为 ``BondType.SOFT`` 可表达"同一订单但不必坐一起"。
    """
    class_code: str = ""
    """**已购席别**（如 ``二等座``）。空字符串表示不限席别（压测 / 仿真场景）。

    这是一个**硬约束**：旅客不能坐到自己没买的席别上。
    早期实现完全没有这个字段，"下单二等座"只是个展示参数 —— 实测出现
    「下单商务座 -> 分到二等座 03车01A」，等于付了一等座的价钱坐二等座，
    票务系统里不可接受。因此席别必须参与**候选池构造**，
    而不是靠代价函数"倾向于"选对席别（罚分只能影响选哪个，
    候选池才决定能不能选 —— 本项目反复吃到的同一个教训）。
    """

    @property
    def size(self) -> int:
        return len(self.passengers)

    def bond_of(self, a: str, b: str) -> BondType:
        """查询两人之间的绑定强度。

        **未显式声明时默认按"同行人"处理**（见
        :attr:`smartrail.config.EngineConfig.same_order_default_bond`）。

        理由是最朴素的产品常识：没人会跟陌生人拼一个订单买票。同一订单里的人
        天然相互认识，"尽量坐到一起"应当是默认行为，而不是要让用户勾选的选项。
        早期实现返回 SOFT，等于把同一订单的人当成互不相识的散客，可能被拆到
        不同车厢 —— 既不符合直觉，也白白浪费了"同单必然同行"这条免费信息。

        需要"同一订单但各自出行"（公司代订、多人出差）时，请在订单里显式声明
        ``BondType.SOFT``（或由 API 传 ``same_order_bond="soft"``）。
        """
        explicit = self.bonds.get(frozenset((a, b)))
        if explicit is not None:
            return explicit
        if self.default_bond is not None:
            return self.default_bond
        return BondType.SOFT

    def caregivers_of(self, passenger_id: str) -> tuple[str, ...]:
        """返回与该乘客存在 MANDATORY 绑定的其他乘客。"""
        return tuple(
            pid
            for key, bond in self.bonds.items()
            if bond is BondType.MANDATORY and passenger_id in key
            for pid in key
            if pid != passenger_id
        )

    def caregivers(self) -> frozenset[str]:
        return frozenset(p.passenger_id for p in self.passengers if p.is_caregiver)


# ---------------------------------------------------------------------------
# 3.2 座位图谱
# ---------------------------------------------------------------------------


class SeatFeature(str, Enum):
    """座位级设施标签。"""

    AISLE = "aisle"                    # 过道座位
    WINDOW = "window"                  # 靠窗
    MIDDLE = "middle"                  # 中间座
    ACCESSIBLE = "accessible"          # 无障碍专区
    NEAR_TOILET = "near_toilet"        # 近卫生间
    NEAR_DOOR = "near_door"            # 靠近车门


@dataclass(frozen=True)
class Seat:
    """座位图谱中的一个座位（物理坐标 + 设施标签 + 车厢属性）。"""

    seat_id: str
    carriage: int
    row: int          # X 轴：排号
    col: str          # 列号，如 A/B/C/D/F
    features: frozenset[SeatFeature] = field(default_factory=frozenset)
    col_index: int = 0          # 车厢内列序号（含过道占位）
    aisle_crossings: Mapping[str, int] = field(default_factory=dict)
    """到本车厢其他列序号需要跨越的座位数（含过道计 1 个通道单位）。"""
    is_quiet_carriage: bool = False
    class_code: str = "二等座"
    accessible_zone: bool = False
    carriage_columns: tuple[str, ...] = ()
    """本座位**所在区段**的列布局。

    为什么座位要自己记住列布局，而不是去查 ``Carriage.columns``：
    真实车型里一节车厢可以含多种席别（01 车 = 一等座 2+2 + 商务座 1+2），
    而 ``Carriage.columns`` 只能表达**一种**布局（取座位最多的那个区段）。
    于是 01 车的商务座会被误认为在一节 4 列的车厢里 ——
    凡是用 ``len(carriage.columns)`` 判断"这排有几个座""哪里是过道"的代码都会算错。

    留空时回退到 ``Carriage.columns``（兼容旧构造路径）。
    """

    @property
    def columns(self) -> tuple[str, ...]:
        """本座位所在区段的列布局。"""
        return self.carriage_columns or (
            "A", "B", "C", "D", "F",
        )

    @property
    def position(self) -> tuple[int, str]:
        return (self.row, self.col)

    @property
    def coord(self) -> tuple[int, int, int]:
        """(车厢, 排号, 列序号) 三维物理坐标。"""
        return (self.carriage, self.row, self.col_index)

    @property
    def is_aisle(self) -> bool:
        return SeatFeature.AISLE in self.features

    def in_accessible_zone(self) -> bool:
        return self.accessible_zone or SeatFeature.ACCESSIBLE in self.features

    def manhattan_to(self, other: "Seat") -> int:
        """绝对曼哈顿距离模型。

        跨车厢赋予极高转移权重（README 3.2：代价 = 10000），同排跨过道额外
        计入过道通道（+2），保证"同排但被过道分隔"劣于"同排紧邻"、
        但远优于"跨车厢"。
        """
        if self.carriage != other.carriage:
            return 10_000 + abs(self.row - other.row)
        if self.row == other.row:
            return self.aisle_crossings.get(other.col, abs(self.col_index - other.col_index))
        # 同车厢不同排：先横向走到对方列，再纵向跨排
        return abs(self.row - other.row) + self.aisle_crossings.get(other.col, 1)


@dataclass(frozen=True)
class WheelchairBay:
    """轮椅固定停放位（**独立资源，不占普通座位票额**）。

    为什么必须与 Seat 分开
    ----------------------
    早期实现把轮椅位当成"无障碍专区里的普通座位"（04/12 车第 1-2 排共 20 座），
    这是错的，会同时算错两件事：

    1. **座位票额**：这 20 个座位被从普通票额里"扣掉"了，实际它们本来
       就该卖给别人 —— 04 车定员 78 座里就包含它们；
    2. **轮椅容量**：真实动车组每节无障碍车厢只有 **2 个**轮椅固定停放位
       （带安全带/绑带的独立空间），不是 10 个。按 10 算会让"轮椅位够不够"
       的判断过于乐观 5 倍。

    停放位与"编号"的关系（**完全独立编号**）
    ----------------------------------------
    轮椅旅客**没有座位号**，凭证上写的是停放位编号（如 ``04车W1``）。
    系统内部仍需要一个"落点"来驱动代价函数与座位图，因此有两个概念：

    * ``bay_id``：**对外的编号**，形如 ``04车W1``。这是出票凭证、
      用户界面与座位图上显示的标识；
    * ``slot_seat_id``：**仅内部使用的落点座位**，只为复用求解器而存在，
      **绝不作为座位号展示给旅客**，也不占用该座位的票额语义。

    早期让两者合一（直接把 ``04车01A`` 当作轮椅旅客的座位号），
    结果是"停放位与普通座位共用编号"——旅客看到的是个座位号，
    而不是"轮椅位"。这既不符合无障碍出行的实际凭证，也让人误以为
    停放位吃掉了座位票额。
    """

    bay_id: str
    carriage: int
    #: 停放位所在的大致位置（排号），用于就近安排的代价计算
    row: int = 1
    #: **仅内部使用**的落点座位（复用求解器）；不作为座位号展示
    slot_seat_id: str = ""
    #: 旁边的陪同座位（现实中与停放位同排，供同行人使用）
    companion_seats: tuple[str, ...] = ()

    @property
    def label(self) -> str:
        return f"{self.carriage:02d}车 轮椅位"

    def to_dict(self) -> dict[str, Any]:
        return {
            "bay_id": self.bay_id,
            "carriage": self.carriage,
            "row": self.row,
            "label": self.label,
            # 对外只暴露编号，不暴露内部落点座位
            "display": self.bay_id,
            "slot_seat_id": self.slot_seat_id,
            "companion_seats": list(self.companion_seats),
        }


@dataclass(frozen=True)
class Carriage:
    """车厢属性标签。"""

    number: int
    class_code: str
    columns: tuple[str, ...]
    aisle_after: str          # 该列之后是过道
    rows: int = 17
    is_quiet_carriage: bool = False
    has_accessible_zone: bool = False
    has_toilet: bool = False
    door_positions: tuple[int, ...] = (1, 17)
    #: 轮椅固定停放位数量（不占座位票额）
    wheelchair_bays: int = 0

    def col_options(self) -> tuple[str, ...]:
        return self.columns


@dataclass(frozen=True)
class TrainFormation:
    """编组：车厢列表 + 座位索引 + 轮椅停放位。"""

    train_code: str
    carriages: tuple[Carriage, ...]
    seats: tuple[Seat, ...]
    wheelchair_bays: tuple[WheelchairBay, ...] = ()

    def by_id(self) -> dict[str, Seat]:
        return {s.seat_id: s for s in self.seats}

    def seat(self, seat_id: str) -> Seat:
        for s in self.seats:
            if s.seat_id == seat_id:
                return s
        raise KeyError(seat_id)

    def bay(self, bay_id: str) -> WheelchairBay:
        for item in self.wheelchair_bays:
            if item.bay_id == bay_id:
                return item
        raise KeyError(bay_id)

    @property
    def total_wheelchair_bays(self) -> int:
        return len(self.wheelchair_bays)

    def bays_in(self, carriage: int) -> tuple[WheelchairBay, ...]:
        return tuple(b for b in self.wheelchair_bays if b.carriage == carriage)

    @property
    def seat_ids(self) -> tuple[str, ...]:
        return tuple(s.seat_id for s in self.seats)

    def of_carriage(self, number: int) -> tuple[Seat, ...]:
        return tuple(s for s in self.seats if s.carriage == number)


# ---------------------------------------------------------------------------
# 分配结果
# ---------------------------------------------------------------------------


def seat_key(seat_id: str) -> tuple[int, int, int]:
    """座位 ID（如 "07车12F" / "7-12-F"）的稳定排序键。"""
    digits: list[int] = []
    letters = ""
    buf = ""
    for ch in seat_id:
        if ch.isdigit():
            buf += ch
        else:
            if buf:
                digits.append(int(buf))
                buf = ""
            if ch.isalpha():
                letters += ch
    if buf:
        digits.append(int(buf))
    carriage, row = (digits + [0, 0])[:2]
    col_rank = ord(letters[0]) - 64 if letters else 99
    return (carriage, row, col_rank)


@dataclass
class Assignment:
    """单个乘客的分配结果。"""

    passenger_id: str
    seat_id: str
    carriage: int
    row: int
    col: str
    quiet_carriage: bool
    reason: str = ""
    unit_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "passenger_id": self.passenger_id,
            "seat_id": self.seat_id,
            "carriage": self.carriage,
            "row": self.row,
            "col": self.col,
            "quiet_carriage": self.quiet_carriage,
            "reason": self.reason,
            "unit_id": self.unit_id,
        }


@dataclass
class Notice:
    """面向旅客 / 乘务员的**待办提示**。

    设计动机（来自真实运营口径）：**出票优先于座位理想度**。
    当"相邻座位"这类偏好无法满足时，系统应当**照常出票**，同时把需要现场处理的
    事项明确交出去 —— 而不是拒票让旅客买不到票。拒票是最后手段，不是首选手段。

    因此 Notice 与 Violation 的分工是：
    * :class:`Violation` 描述"代价函数扣了多少分"（给算法与审计看）；
    * :class:`Notice` 描述"谁需要被谁怎么处理"（给站车服务看）。
    """

    passenger_id: str
    kind: str
    """提示类型，取值见 :data:`NOTICE_KINDS`。"""
    level: str
    """严重程度：``info``（仅告知）/ ``attention``（建议现场关注）/ ``action``（需现场处理）。"""
    message: str
    seat_id: str | None = None
    carriage: int | None = None
    companions: list[str] = field(default_factory=list)
    """需要被安排到一起的同行人（未满足时给出，便于乘务员现场调剂）。"""

    def to_dict(self) -> dict[str, Any]:
        return {
            "passenger_id": self.passenger_id,
            "kind": self.kind,
            "level": self.level,
            "message": self.message,
            "seat_id": self.seat_id,
            "carriage": self.carriage,
            "companions": list(self.companions),
        }


NOTICE_KINDS: dict[str, str] = {
    "no_adjacent_seat": "无相邻座位，已出票，建议现场调剂",
    "accessible_zone_full": "无障碍专区售罄，已出票（普通座位），需站车协助",
    "caregiver_split": "照护人与被照护人未相邻，已出票，需现场确认",
    "pregnant_no_companion": "孕妇未与同行人相邻，已出票，建议现场关注",
    "reunion_available": "车内有可调剂座位，可现场安排坐到一起",
    "scattered": "同行人被分散在多排，已出票，建议现场尽量合并",
}


@dataclass
class AdjacencyStatus:
    """订单级的"相邻就座"结论。

    求解器**不因相邻不满足而拒票**，但必须把结论明确报出来：
    ``satisfied`` 全部相邻；``compromised`` 有人未相邻但已出票；``impossible``
    车厢内已无相邻空位（此时仍出票，并给出"上车找列车员"的提示）。
    """

    level: str = "satisfied"
    affected: list[str] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)

    @property
    def needs_crew(self) -> bool:
        return self.level in {"compromised", "impossible"}

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "affected": list(self.affected),
            "messages": list(self.messages),
        }


@dataclass
class Solution:
    """求解器输出：分配方案 + 代价明细 + 可解释性报告。

    内部使用 **affinity（亲和度）** 表示"越大越优"（惩罚为负、奖励为正），
    对外则输出 **cost（代价分，越大越严重）** = -affinity，便于客服与论文使用。
    """

    assignments: dict[str, Assignment] = field(default_factory=dict)
    waitlisted: list[str] = field(default_factory=list)
    total_affinity: float = 0.0
    breakdown: dict[str, float] = field(default_factory=dict)
    violations: list["Violation"] = field(default_factory=list)
    solver: str = ""
    elapsed_ms: float = 0.0
    notes: list[str] = field(default_factory=list)
    mode: str = ""
    notices: list[Notice] = field(default_factory=list)
    """面向旅客/乘务员的待办提示（"出票优先"策略的产物）。"""
    adjacency: AdjacencyStatus = field(default_factory=AdjacencyStatus)
    """相邻就座的订单级结论。"""

    @property
    def total_cost(self) -> float:
        """全局代价分（README 4.1 的口径：Tier 0 = 100000 分）。"""
        return -self.total_affinity

    @property
    def seated(self) -> list[str]:
        return list(self.assignments)

    @property
    def hard_violations(self) -> list["Violation"]:
        return [v for v in self.violations if v.tier == 0]

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "solver": self.solver,
            "total_cost": round(self.total_cost, 3),
            "affinity": round(self.total_affinity, 3),
            "elapsed_ms": round(self.elapsed_ms, 4),
            "breakdown": {k: round(-v, 3) for k, v in self.breakdown.items()},
            "assignments": [
                a.to_dict() for _, a in sorted(self.assignments.items(), key=lambda kv: seat_key(kv[1].seat_id))
            ],
            "waitlisted": list(self.waitlisted),
            "violations": [v.to_dict() for v in self.violations],
            "notes": list(self.notes),
            "notices": [n.to_dict() for n in self.notices],
            "adjacency": self.adjacency.to_dict(),
        }


@dataclass(frozen=True)
class Violation:
    """一条代价条目。

    ``affinity`` 为内部口径（惩罚为负、奖励为正）；``penalty`` 为对外展示口径
    （恒为非负的"分"），``is_reward`` 区分这是奖励还是惩罚。
    """

    code: str
    tier: int
    affinity: float
    passengers: tuple[str, ...] = ()
    detail: str = ""

    @property
    def penalty(self) -> float:
        """展示用的分值（正的量级）。"""
        return abs(self.affinity)

    @property
    def is_reward(self) -> bool:
        return self.affinity > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "tier": self.tier,
            "penalty": round(self.penalty, 3),
            "is_reward": self.is_reward,
            "passengers": list(self.passengers),
            "detail": self.detail,
        }
