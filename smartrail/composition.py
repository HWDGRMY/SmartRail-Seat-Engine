"""订单构成模型：基础分组 + 儿童细分 + 残疾 / 孕妇两个独立维度 + 出票校验。

设计口径（来自业务规格）
------------------------
**基础分组管总人数**：``总人数 = 成人 + 青少年 + 儿童 + 幼儿 + 婴儿``，
只有这五档参与求和。

**儿童细分、残疾、孕妇都不参与总人数求和**，它们只做标签、服务与出票规则：

* 儿童细分：安静 / 吵闹（行为标签）；
* 残疾：**独立维度**，按"程度 × 年龄段"交叉计数。残疾人不一定是成人，
  所以不能挂在成人下面；
* 孕妇：**独立维度**，按"孕期 × 年龄段"交叉计数。孕妇也可能是青少年。

**陪同人已计入基础分组**（他就是那位成人），因此**不额外加总人数**。

为什么把校验做成纯函数
----------------------
规格要求"每个分组独立配置、独立组件、独立 testId、独立纯函数校验"。
纯函数没有 I/O、没有全局状态，因此：

1. 能在没有后端的环境里逐条验证（本项目 74 条断言的既有风格）；
2. 前端可以直接复用同一套语义（本模块同时导出为结构化数据）；
3. 校验失败只返回**原因字符串**，由调用方决定是禁用按钮还是提示。
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import Any, Mapping, Sequence

# ---------------------------------------------------------------------------
# 基础分组：**总人数的唯一来源**
# ---------------------------------------------------------------------------

BASE_GROUP_FIELDS: tuple[dict[str, Any], ...] = (
    {"id": "adult", "label": "成人", "desc": "满18周岁及以上", "min": 0, "max": 99},
    {"id": "youth", "label": "青少年", "desc": "满14周岁但未满18周岁", "min": 0, "max": 99},
    {"id": "child", "label": "儿童", "desc": "满4周岁但未满14周岁", "min": 0, "max": 99},
    {"id": "toddler", "label": "幼儿", "desc": "满1周岁但未满4周岁", "min": 0, "max": 99},
    {"id": "infant", "label": "婴儿", "desc": "未满1周岁", "min": 0, "max": 99},
)
"""基础分组字段定义（原样保留）。

年龄边界有一处容易错：**14 岁整属于青少年**。
儿童是"未满 14 周岁"，青少年是"满 14 周岁"，两者在 14 岁整这一点上不重叠。
"""

BASE_GROUP_IDS: tuple[str, ...] = tuple(item["id"] for item in BASE_GROUP_FIELDS)

CHILD_SUB_FIELDS: tuple[dict[str, Any], ...] = (
    {"id": "child_quiet", "label": "儿童（4-14）（安静）", "min": 0, "max": 99},
    {"id": "child_noisy", "label": "儿童（4-14）（吵闹）", "min": 0, "max": 99},
)
"""儿童行为细分：**只是标签**，不参与总人数。"""

CHILD_SUB_IDS: tuple[str, ...] = tuple(item["id"] for item in CHILD_SUB_FIELDS)

DISABILITY_LEVELS: tuple[dict[str, Any], ...] = (
    {"id": "mild", "label": "轻度", "requires_key_service": False, "requires_companion": False},
    {"id": "moderate", "label": "中度", "requires_key_service": False, "requires_companion": False},
    {"id": "severe", "label": "重度", "requires_key_service": True, "requires_companion": True},
    {"id": "critical", "label": "极重度", "requires_key_service": True, "requires_companion": True},
)
"""残疾程度档位。

轻度／中度：不强制重点旅客、不强制陪同（重点旅客可选）。
重度／极重度：**必须预约重点旅客服务，且每位至少 1 名成人陪同**。
"""

DISABILITY_LEVEL_IDS: tuple[str, ...] = tuple(item["id"] for item in DISABILITY_LEVELS)
DISABILITY_LEVEL_BY_ID: dict[str, dict[str, Any]] = {
    item["id"]: item for item in DISABILITY_LEVELS
}

SEVERE_LEVEL_IDS: tuple[str, ...] = tuple(
    item["id"] for item in DISABILITY_LEVELS if item["requires_key_service"]
)

PREGNANT_STAGES: tuple[dict[str, Any], ...] = (
    {"id": "early", "label": "1-3个月", "requires_key_service": False,
     "requires_companion": False, "configurable": False},
    {"id": "mid", "label": "4-6个月", "requires_key_service": False,
     "requires_companion": False, "configurable": False},
    {"id": "late", "label": "7-9个月", "requires_key_service": False,
     "requires_companion": False, "configurable": True},
    {"id": "term", "label": "10个月/37周+", "requires_key_service": True,
     "requires_companion": True, "configurable": False},
)
"""孕妇孕期档位。

1-6 个月：可独立购票，不强制重点旅客、不强制陪同。
7-9 个月：**平台可配置**是否要求重点旅客／陪同（本实现默认不要求，
         由 :class:`PlatformPolicy` 的 ``late_pregnancy_requires_*`` 打开）。
10 个月/37 周+：必须预约重点旅客，且至少 1 名成人陪同。
"""

PREGNANT_STAGE_IDS: tuple[str, ...] = tuple(item["id"] for item in PREGNANT_STAGES)
PREGNANT_STAGE_BY_ID: dict[str, dict[str, Any]] = {
    item["id"]: item for item in PREGNANT_STAGES
}
TERM_STAGE_IDS: tuple[str, ...] = tuple(
    item["id"] for item in PREGNANT_STAGES if item["requires_key_service"]
)

DISABILITY_AGE_BANDS: tuple[str, ...] = BASE_GROUP_IDS
"""残疾／孕妇的年龄段就是基础分组的五档（同一套年龄口径）。"""


# ---------------------------------------------------------------------------
# 平台可配置策略
# ---------------------------------------------------------------------------


@dataclass
class PlatformPolicy:
    """平台侧可配置的出票策略。

    规格里明确"平台可配置"的项集中在这里，避免把策略散落到校验逻辑里。
    """

    late_pregnancy_requires_key_service: bool = False
    """7-9 个月孕妇是否要求重点旅客服务（默认否）。"""

    late_pregnancy_requires_companion: bool = False
    """7-9 个月孕妇是否要求成人陪同（默认否）。"""

    moderate_cannot_companion: bool = False
    """中度残疾人是否也不能当陪同人（默认可以）。

    规格原文："如果平台认为中度也不能陪同，可以再减成人中中度残疾数。"
    默认按更宽松的口径：只有重度／极重度不能陪同。
    """

    def to_dict(self) -> dict[str, Any]:
        return {item.name: getattr(self, item.name) for item in fields(self)}


# ---------------------------------------------------------------------------
# 订单构成
# ---------------------------------------------------------------------------


def _empty_matrix(rows: Sequence[str], columns: Sequence[str]) -> dict[str, dict[str, int]]:
    return {row: {column: 0 for column in columns} for row in rows}


@dataclass
class OrderComposition:
    """一张订单的人员构成。

    三个维度各自独立：基础分组（管总人数）、残疾（程度 × 年龄段）、
    孕妇（孕期 × 年龄段）。后两者**不计入总人数**。
    """

    order_id: str = ""
    note: str = ""

    base: dict[str, int] = field(default_factory=lambda: {k: 0 for k in BASE_GROUP_IDS})
    child_sub: dict[str, int] = field(default_factory=lambda: {k: 0 for k in CHILD_SUB_IDS})
    disability: dict[str, dict[str, int]] = field(
        default_factory=lambda: _empty_matrix(DISABILITY_LEVEL_IDS, DISABILITY_AGE_BANDS)
    )
    pregnant: dict[str, dict[str, int]] = field(
        default_factory=lambda: _empty_matrix(PREGNANT_STAGE_IDS, DISABILITY_AGE_BANDS)
    )
    companion_count: int | None = None
    """**显式登记**的陪同人数；``None`` 表示未单独登记。

    注意与"可用健康成人"的区别：

    * **可用健康成人**由基础分组成人减去不能陪同的残疾成人**自动算出**，
      它决定"这张单能不能出票"（规格的最终条件是
      ``可用健康成人 >= 所需成人陪同``）；
    * **陪同人数**是用户在"陪同人数"组件里**主动登记**的额外信息。
      若它保持 ``None``（未登记），就不参与校验 —— 早期把它默认成 ``0``，
      导致"成人 1 带 1 个儿童"这种完全正常的订单被误判为
      "所需陪同至少 1 名，当前仅登记 0 名"而拒票。
    """
    key_passenger_service: bool = False
    """是否已预约重点旅客服务。"""
    services: list[str] = field(default_factory=list)
    """其它服务项：轮椅、担架、导盲犬、无障碍车厢等。"""
    class_code: str = ""
    """**已购席别**（如 ``二等座``）。空字符串表示不限席别。

    这是一个**硬约束**，必须一路带到 :class:`~smartrail.models.Order`；
    漏传的后果实测过：开发者页选了"二等座"，求解器却按"不限席别"处理，
    给 2 成人 2 儿童发了一等座 —— 用户报的"买二等座出一等座"。
    """
    wheelchair_count: int = 0
    """其中**使用轮椅**的人数。

    为什么要单列（真实反馈："你这么分那轮椅区有啥用啊"）：

    残疾的"程度"（轻/中/重/极重）**不等于**"类别"。原先按程度映射支持需求，
    极重度映射到的是 ``INTELLECTUAL_DISABILITY``（智力障碍）——
    于是选"极重度残疾"的人**永远拿不到轮椅固定停放位**，
    4 个停放位一次都不会被占用，整块无障碍车厢形同虚设。

    轮椅是**类别**维度：轻度和极重度都可能用轮椅，也都可能不用。
    所以这里单独计数，映射到 ``SupportNeed.WHEELCHAIR``，
    由求解器优先安排进轮椅固定停放位。

    与其它叠加维度一样，它**不计入总人数** —— 数值不得超过基础分组总人数。
    """

    @property
    def wheelchair_riders(self) -> int:
        """实际会分到轮椅的乘客数（受总人数上限约束）。"""
        return max(0, min(self.wheelchair_count, self.total_passengers))

    # -- 总人数 ----------------------------------------------------------
    @property
    def total_passengers(self) -> int:
        """**总人数只由基础分组求和。**"""
        return sum(self.base.values())

    @property
    def minors_under_14(self) -> int:
        """未满 14 周岁：婴儿 + 幼儿 + 儿童（**不含青少年**，14 岁整算青少年）。"""
        return self.base["infant"] + self.base["toddler"] + self.base["child"]

    # -- 残疾 / 孕妇 ------------------------------------------------------
    def disability_count(self, level: str | None = None,
                         age_band: str | None = None) -> int:
        """残疾人数：可按程度、年龄段或两者交叉过滤。"""
        levels = [level] if level else list(DISABILITY_LEVEL_IDS)
        bands = [age_band] if age_band else list(DISABILITY_AGE_BANDS)
        return sum(self.disability[lv][bd] for lv in levels for bd in bands)

    def pregnant_count(self, stage: str | None = None,
                       age_band: str | None = None) -> int:
        """孕妇人数：可按孕期、年龄段或两者交叉过滤。"""
        stages = [stage] if stage else list(PREGNANT_STAGE_IDS)
        bands = [age_band] if age_band else list(DISABILITY_AGE_BANDS)
        return sum(self.pregnant[st][bd] for st in stages for bd in bands)

    @property
    def severe_disability_count(self) -> int:
        """重度 + 极重度残疾人数（每位都需要 1 名成人陪同）。"""
        return sum(self.disability_count(level=lv) for lv in SEVERE_LEVEL_IDS)

    @property
    def term_pregnancy_count(self) -> int:
        """10 个月 / 37 周以上孕妇人数（每位都需要 1 名成人陪同）。"""
        return sum(self.pregnant_count(stage=st) for st in TERM_STAGE_IDS)

    # -- 可用健康成人 ------------------------------------------------------
    def unavailable_adults(self, policy: PlatformPolicy) -> int:
        """不能充当陪同人的成人数（重度／极重度，可选含中度）。"""
        levels = list(SEVERE_LEVEL_IDS)
        if policy.moderate_cannot_companion:
            levels.append("moderate")
        return sum(self.disability_count(level=lv, age_band="adult") for lv in levels)

    def healthy_adults(self, policy: PlatformPolicy | None = None) -> int:
        """可用健康成人 = 基础分组成人 − 其中不能陪同的残疾成人数。"""
        policy = policy or PlatformPolicy()
        return max(0, self.base["adult"] - self.unavailable_adults(policy))

    def required_companions(self, policy: PlatformPolicy | None = None) -> int:
        """所需成人陪同数。

        一位成人陪同人**可以同时照看多位需要陪同的人**（例如一位家长带两个孩子），
        因此这里取"需要的陪同人数下限"而不是简单累加 —— 规格里的校验也写成
        ``companionCount < severeCount``，即"重度残疾几位就需要几位陪同"。

        实际取值为三类需求的最大值：重度／极重度残疾人数、足月孕妇人数、
        有未成年人时的 1 名成人。
        """
        policy = policy or PlatformPolicy()
        needed = 0
        needed = max(needed, self.severe_disability_count)
        needed = max(needed, self.term_pregnancy_count)
        if policy.late_pregnancy_requires_companion:
            needed = max(needed, self.pregnant_count(stage="late"))
        if self.minors_under_14 > 0:
            needed = max(needed, 1)
        return needed

    # -- 序列化 ----------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id,
            "note": self.note,
            "class_code": self.class_code,
            "wheelchair_count": self.wheelchair_count,
            "base": dict(self.base),
            "child_sub": dict(self.child_sub),
            "disability": {k: dict(v) for k, v in self.disability.items()},
            "pregnant": {k: dict(v) for k, v in self.pregnant.items()},
            "companion_count": self.companion_count,
            "key_passenger_service": self.key_passenger_service,
            "services": list(self.services),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any], order_id: str = "") -> OrderComposition:
        """从请求体构造，**只接受已知字段**，未知键忽略（避免前端多加字段炸掉）。"""
        composition = cls(
            order_id=str(payload.get("order_id") or order_id),
            note=str(payload.get("note") or ""),
            class_code=str(payload.get("class_code") or ""),
            wheelchair_count=_as_count(payload.get("wheelchair_count", 0)),
        )
        raw_base = payload.get("base") or {}
        for key in BASE_GROUP_IDS:
            composition.base[key] = _as_count(raw_base.get(key, 0))
        raw_child = payload.get("child_sub") or {}
        for key in CHILD_SUB_IDS:
            composition.child_sub[key] = _as_count(raw_child.get(key, 0))
        raw_disability = payload.get("disability") or {}
        for level in DISABILITY_LEVEL_IDS:
            row = raw_disability.get(level) or {}
            for band in DISABILITY_AGE_BANDS:
                composition.disability[level][band] = _as_count(row.get(band, 0))
        raw_pregnant = payload.get("pregnant") or {}
        for stage in PREGNANT_STAGE_IDS:
            row = raw_pregnant.get(stage) or {}
            for band in DISABILITY_AGE_BANDS:
                composition.pregnant[stage][band] = _as_count(row.get(band, 0))
        composition.companion_count = (
            _as_count(payload["companion_count"])
            if payload.get("companion_count") is not None
            else None
        )
        composition.key_passenger_service = bool(payload.get("key_passenger_service", False))
        composition.services = [str(item) for item in (payload.get("services") or [])]
        return composition


def _as_count(value: Any) -> int:
    """把输入规整为非负整数（前端可能传来字符串、None 或浮点）。"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, number)


# ---------------------------------------------------------------------------
# 纯函数校验（每个分组一个，互不依赖）
# ---------------------------------------------------------------------------


def validate_minor_companion(
    composition: OrderComposition, policy: PlatformPolicy | None = None
) -> str | None:
    """未满 14 周岁单独购票必须至少 1 名成人陪同，否则拒票。

    青少年（14-18 周岁）**不拒票**，可独立购票。
    """
    _ = policy
    if composition.minors_under_14 > 0 and composition.base["adult"] == 0:
        return "未满14周岁的婴儿、幼儿、儿童必须至少由1名成人陪同，否则不予出票"
    return None


def validate_child_sub_consistency(
    composition: OrderComposition, policy: PlatformPolicy | None = None
) -> str | None:
    """儿童细分之和不得超过基础分组的儿童人数。

    细分只是**标签**，所以"安静 2 + 吵闹 1"最多只能对应 3 位儿童。
    这条校验防止前端出现"3 个儿童却有 5 个行为标签"这种数据不自洽。
    """
    _ = policy
    tagged = composition.child_sub["child_quiet"] + composition.child_sub["child_noisy"]
    if tagged > composition.base["child"]:
        return (f"儿童行为细分合计 {tagged} 人，超过基础分组的儿童人数 "
                f"{composition.base['child']} 人")
    return None


def validate_group_consistency(
    composition: OrderComposition, policy: PlatformPolicy | None = None
) -> list[str]:
    """各维度的人数不得超过对应年龄段的基础人数。

    规格的测试重点之一："每个年龄段各残疾程度之和 ≤ 该年龄段基础人数"。
    孕妇同理（虽然规格只写了残疾，但同一口径对孕妇成立）。
    """
    _ = policy
    problems: list[str] = []
    for band in DISABILITY_AGE_BANDS:
        disabled = composition.disability_count(age_band=band)
        if disabled > composition.base[band]:
            label = dict((item["id"], item["label"]) for item in BASE_GROUP_FIELDS)[band]
            problems.append(
                f"{label}的残疾人数 {disabled} 超过该年龄段基础人数 "
                f"{composition.base[band]}"
            )
    for band in DISABILITY_AGE_BANDS:
        pregnant = composition.pregnant_count(age_band=band)
        if pregnant > composition.base[band]:
            label = dict((item["id"], item["label"]) for item in BASE_GROUP_FIELDS)[band]
            problems.append(
                f"{label}的孕妇人数 {pregnant} 超过该年龄段基础人数 "
                f"{composition.base[band]}"
            )
    return problems


def validate_disability(
    composition: OrderComposition, policy: PlatformPolicy | None = None
) -> str | None:
    """重度／极重度残疾：**必须预约重点旅客服务**；**独立出行**时才必须有陪同。

    原先的写法是"每位重度残疾人都要配 1 名成人"（``usable < severe``），
    而且**完全不看有没有同行人**：

    * 1 位重度残疾人 + 3 位家人 -> 因为"可用健康成人 3 < 重度 1"？不会，
      但 "1 位重度残疾人 + 0 位家人 + 登记重点服务" 仍被拒，
      理由写成"必须至少 1 名成人陪同" —— 可需求里从没要求残疾人必须有陪同，
      只要求**独立出行**的重度残疾人由站车协助；
    * 更荒谬的是"2 位重度残疾人 + 2 位家人"也被拒，因为要 2 名成人而
      可用健康成人算出来是 2 —— 边界正好卡住，用户完全无法理解。

    用户的原话："啥必须啊？单人必须，有陪就不必须了呗。"
    所以改成：

    * 重点旅客服务：**始终必须**（重度/极重度由站车协助）；
    * 成人陪同：只在**没有任何其他成人同行**时才必须
      （即"独自出行"）；有同行人就不强制。
    """
    policy = policy or PlatformPolicy()
    severe = composition.severe_disability_count
    if severe <= 0:
        return None
    if not composition.key_passenger_service:
        return "重度/极重度残疾必须预约重点旅客服务"
    # 有同行成人就不强制陪同；只有"独自出行"才要求。
    if composition.base["adult"] <= 0:
        return (
            f"重度/极重度残疾旅客独自出行时必须有同行成人"
            f"（{severe} 位重点旅客，当前无同行成人）"
        )
    return None


def validate_pregnant(
    composition: OrderComposition, policy: PlatformPolicy | None = None
) -> str | None:
    """10 个月 / 37 周以上孕妇：**必须预约重点旅客**；**独自出行**时才必须有陪同。

    与 :func:`validate_disability` 同一口径 —— 原先写成
    "每位足月孕妇都要配 1 名成人"（``usable < term``），
    可孕妇本人就在基础分组的成人里，于是"足月孕妇 1 人 + 家人 0 人"
    与"足月孕妇 1 人 + 家人 1 人"算出来的可用健康成人数经常一样，
    用户看到的就是"明明有人陪还被拒"。
    """
    policy = policy or PlatformPolicy()
    term = composition.term_pregnancy_count
    if term > 0:
        if not composition.key_passenger_service:
            return "10个月/37周以上孕妇必须预约重点旅客服务"
        # 有同行成人就不强制陪同；只有独自出行才要求。
        if composition.base["adult"] <= 0:
            return (
                f"10个月/37周以上孕妇独自出行时必须有同行成人"
                f"（{term} 位重点旅客，当前无同行成人）"
            )
        return None
    if policy.late_pregnancy_requires_key_service:
        late = composition.pregnant_count(stage="late")
        if late > 0 and not composition.key_passenger_service:
            return "按当前平台策略，7-9个月孕妇必须预约重点旅客服务"
    return None


def validate_companion_count(
    composition: OrderComposition, policy: PlatformPolicy | None = None
) -> str | None:
    """检查"显式登记的陪同人数"是否自相矛盾。

    只在用户**确实登记了**陪同人数时才校验（``companion_count is not None``）：

    * 登记的陪同人数不能超过可用健康成人数 —— 重度/极重度残疾人不能当陪同人，
      所以"成人 1（重度残疾）+ 登记陪同 1"是错的；
    * 登记的陪同人数不能低于所需陪同数 —— 低于就说明照护安排不成立。

    未登记（``None``）时**不校验**：出票资格由
    ``可用健康成人 >= 所需成人陪同`` 决定，而不是由这个可选的登记值决定。
    """
    policy = policy or PlatformPolicy()
    if composition.companion_count is None:
        return None
    needed = composition.required_companions(policy)
    usable = composition.healthy_adults(policy)
    if composition.companion_count > usable:
        return (
            f"登记的陪同人数 {composition.companion_count} 超过可用健康成人 "
            f"{usable} 名（重度/极重度残疾人不能作为陪同人）"
        )
    if composition.companion_count < needed:
        return (
            f"所需成人陪同至少 {needed} 名，当前仅登记 {composition.companion_count} 名"
        )
    return None


def validate_empty(
    composition: OrderComposition, policy: PlatformPolicy | None = None
) -> str | None:
    """空单不能出票。"""
    _ = policy
    if composition.total_passengers == 0:
        return "订单中没有乘客（总人数为 0）"
    return None


VALIDATORS: tuple[tuple[str, Any], ...] = (
    ("empty", validate_empty),
    ("minor_companion", validate_minor_companion),
    ("child_sub", validate_child_sub_consistency),
    ("group_consistency", validate_group_consistency),
    ("disability", validate_disability),
    ("pregnant", validate_pregnant),
    ("companion_count", validate_companion_count),
)
"""校验器清单。**每一条都是独立纯函数**，可单独调用、单独测试。

统一签名 ``(composition, policy) -> str | list[str] | None``：
前两个校验器声明了 ``policy`` 参数却不使用，是为了让调用方能无差别地
遍历这张表 —— 早期用"按名字判断要不要传 policy"的写法，新增校验器时
很容易忘记登记，导致它**静默不被执行**。
"""


@dataclass
class CompositionCheck:
    """一张订单的校验结论。"""

    order_id: str
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    total_passengers: int = 0
    required_companions: int = 0
    healthy_adults: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id,
            "ok": self.ok,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "total_passengers": self.total_passengers,
            "required_companions": self.required_companions,
            "healthy_adults": self.healthy_adults,
        }


def check_composition(
    composition: OrderComposition, policy: PlatformPolicy | None = None
) -> CompositionCheck:
    """跑完全部纯函数校验，汇总成一份结论。

    无差别遍历 :data:`VALIDATORS`：不再按名字判断要不要传 ``policy``,
    避免新增校验器时漏登记而**静默不执行**。
    """
    policy = policy or PlatformPolicy()
    errors: list[str] = []
    for _name, validator in VALIDATORS:
        outcome = validator(composition, policy)
        if outcome is None:
            continue
        if isinstance(outcome, (list, tuple)):
            errors.extend(str(item) for item in outcome)
        else:
            errors.append(str(outcome))

    # 服务类提示（不算错误，但要让用户知道现场会怎么处理）
    warnings: list[str] = []
    if composition.severe_disability_count and composition.key_passenger_service:
        warnings.append(
            f"已预约重点旅客服务，{composition.severe_disability_count} 位重度/极重度"
            f"旅客将由站车协助"
        )
    if composition.term_pregnancy_count and composition.key_passenger_service:
        warnings.append(
            f"已预约重点旅客服务，{composition.term_pregnancy_count} 位足月孕妇"
            f"将由站车协助"
        )
    return CompositionCheck(
        order_id=composition.order_id,
        ok=not errors,
        errors=errors,
        warnings=warnings,
        total_passengers=composition.total_passengers,
        required_companions=composition.required_companions(policy),
        healthy_adults=composition.healthy_adults(policy),
    )


# ---------------------------------------------------------------------------
# 字段清单（供前端渲染，语义与后端同一份来源）
# ---------------------------------------------------------------------------


def composition_schema(policy: PlatformPolicy | None = None) -> dict[str, Any]:
    """前端渲染所需的全部字段定义与规则。"""
    policy = policy or PlatformPolicy()
    return {
        "base_groups": list(BASE_GROUP_FIELDS),
        "child_sub_groups": list(CHILD_SUB_FIELDS),
        "disability_levels": list(DISABILITY_LEVELS),
        "pregnant_stages": list(PREGNANT_STAGES),
        "age_bands": [
            {"id": item["id"], "label": item["label"]} for item in BASE_GROUP_FIELDS
        ],
        "total_formula": "adult + youth + child + toddler + infant",
        "excluded_from_total": ["child_sub", "disability", "pregnant",
                                "wheelchair_count"],
        # 轮椅是**类别**维度，不是程度维度：轻度与极重度都可能用轮椅。
        # 单列出来，否则 4 个轮椅固定停放位永远不会被占用。
        "wheelchair": {
            "id": "wheelchair_count",
            "label": "轮椅旅客",
            "desc": "其中使用轮椅的人数（独立编号的固定停放位，不占普通座位票额）",
            "bays_total": 4,
            "bays_note": "全列 4 个停放位：04 车 W1/W2、12 车 W1/W2",
        },
        "policy": policy.to_dict(),
        "predicates": {
            "minors_under_14": "infant + toddler + child",
            "severe_levels": list(SEVERE_LEVEL_IDS),
            "term_stages": list(TERM_STAGE_IDS),
        },
    }


__all__ = [
    "BASE_GROUP_FIELDS",
    "BASE_GROUP_IDS",
    "CHILD_SUB_FIELDS",
    "CHILD_SUB_IDS",
    "DISABILITY_AGE_BANDS",
    "DISABILITY_LEVELS",
    "DISABILITY_LEVEL_IDS",
    "PREGNANT_STAGES",
    "PREGNANT_STAGE_IDS",
    "SEVERE_LEVEL_IDS",
    "TERM_STAGE_IDS",
    "VALIDATORS",
    "CompositionCheck",
    "OrderComposition",
    "PlatformPolicy",
    "check_composition",
    "composition_schema",
    "validate_child_sub_consistency",
    "validate_companion_count",
    "validate_disability",
    "validate_empty",
    "validate_group_consistency",
    "validate_minor_companion",
    "validate_pregnant",
]
