"""车厢编组与座位图谱生成（Seat Vector 的空间坐标构建）。

物理约定
--------
* ``row``   : 排号（X 轴），1..rows
* ``col``   : 列号（Y 轴），如 "A/B/C/D/F"；缺 E 沿用国铁惯例
* 过道位于 ``aisle_after`` 列之后；过道本身也算一个"通道单位"参与曼哈顿距离

**一节车厢可以含多种席别**
--------------------------
真实动车组里 01 车是"一等座 32 + 商务座 5"，08 车是"二等座 43 + 商务座 6"。
早期实现让整节车厢只有一个 ``class_code``，于是没法表达这种混合车厢，
只能把 01 车整体算成商务座或整体算成二等座 —— 两种都是错的。
现在用 :class:`Block` 描述"一节车厢内的一个坐席区段"，按顺序拼接。

静音车厢、无障碍专区、卫生间、车门均建模为车厢/座位标签，
供代价函数与降级策略读取。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import Carriage, Seat, SeatFeature, TrainFormation

# 国铁常见的座位布局：列号 + 列型
_LAYOUTS: dict[str, tuple[str, str, int]] = {
    # class_code: (columns, aisle_after, aisle_width_units)
    "二等座": ("ABCDF", "C", 1),
    "一等座": ("ACDF", "C", 1),
    "商务座": ("ACF", "C", 1),
}

# 每种坐席的列型（窗/中/过道）
_COL_KIND: dict[str, dict[str, SeatFeature]] = {
    "ABCDF": {
        "A": SeatFeature.WINDOW,
        "B": SeatFeature.MIDDLE,
        "C": SeatFeature.AISLE,
        "D": SeatFeature.AISLE,
        "F": SeatFeature.WINDOW,
    },
    "ACDF": {
        "A": SeatFeature.WINDOW,
        "C": SeatFeature.AISLE,
        "D": SeatFeature.AISLE,
        "F": SeatFeature.WINDOW,
    },
    "ACF": {
        "A": SeatFeature.WINDOW,
        "C": SeatFeature.AISLE,
        "F": SeatFeature.WINDOW,
    },
}

# 席别 → 默认列布局
CLASS_LAYOUT: dict[str, str] = {
    "二等座": "ABCDF",
    "一等座": "ACDF",
    "商务座": "ACF",
}


@dataclass(frozen=True)
class Block:
    """车厢内的一个坐席区段（一种席别、固定布局、若干排）。

    ``seats_per_row`` 为 ``None`` 时按布局列数铺满；给出数字时，
    最后一行只铺前 N 列 —— 用来精确对齐车型图的定员
    （例如 08 车二等座定员 43 = 8×5 + 3）。
    """

    class_code: str
    rows: int
    columns: str | None = None
    seats_per_row: int | None = None
    accessible_rows: tuple[int, ...] = ()   # 区段内相对排号

    @property
    def layout(self) -> str:
        return self.columns or CLASS_LAYOUT[self.class_code]

    def seat_total(self) -> int:
        full_columns = len(self.layout)
        if self.seats_per_row is None:
            return self.rows * full_columns
        if self.rows <= 0:
            return 0
        # 前 rows-1 排铺满，最后一行铺 seats_per_row 列
        return (self.rows - 1) * full_columns + min(self.seats_per_row, full_columns)


@dataclass(frozen=True)
class CarriageSpec:
    """车厢规格（编组配置的输入）。

    ``blocks`` 非空时按区段拼装；为空时退化为"整节一种席别"
    （用 ``class_code`` + ``rows``，兼容旧调用与迷你编组）。
    """

    number: int
    class_code: str = "二等座"
    rows: int = 17
    is_quiet_carriage: bool = False
    accessible_rows: tuple[int, ...] = ()   # 无障碍专区排号（绝对排号）
    has_toilet: bool = False
    door_rows: tuple[int, ...] = (1, 17)
    blocks: tuple[Block, ...] = field(default_factory=tuple)
    note: str = ""

    def effective_blocks(self) -> tuple[Block, ...]:
        if self.blocks:
            return self.blocks
        return (
            Block(
                class_code=self.class_code,
                rows=self.rows,
                accessible_rows=self.accessible_rows,
            ),
        )

    def seat_total(self) -> int:
        return sum(block.seat_total() for block in self.effective_blocks())

    def class_summary(self) -> str:
        """如 ``"一等座32+商务座5"``，用于座位图标注。"""
        parts: list[str] = []
        for block in self.effective_blocks():
            parts.append(f"{block.class_code}{block.seat_total()}")
        return "+".join(parts)


def _build_carriage(spec: CarriageSpec) -> Carriage:
    blocks = spec.effective_blocks()
    # 车厢的"主席别"取座位数最多的那个区段（用于粗粒度筛选）
    main = max(blocks, key=lambda block: block.seat_total())
    layout = main.layout
    aisle_after = _LAYOUTS[main.class_code][1]
    total_rows = sum(block.rows for block in blocks)
    return Carriage(
        number=spec.number,
        class_code=main.class_code,
        columns=tuple(layout),
        aisle_after=aisle_after,
        rows=total_rows,
        is_quiet_carriage=spec.is_quiet_carriage,
        has_accessible_zone=bool(spec.accessible_rows) or any(
            block.accessible_rows for block in blocks
        ),
        has_toilet=spec.has_toilet,
        door_positions=tuple(spec.door_rows),
    )


def _aisle_crossings(columns: tuple[str, ...], aisle_after: str,
                     weight: float = 2.0) -> dict[str, int]:
    """预计算同排两列之间的跨越代价，用于座位级曼哈顿距离。

    规则：座位之间的每个间隔计 1；跨越过道的那个间隔计 ``weight``（默认 2，
    即"过道通道比一个座位更难跨越"，但远低于跨车厢的 10000）。
    因此 "B" -> "C" 为 1，而 "C" -> "D"（跨过道）为 2。
    """
    n = len(columns)
    aisle_idx = columns.index(aisle_after)
    # 第 i 个座位与其右侧邻居之间的间隔代价
    gap = [weight if i == aisle_idx else 1.0 for i in range(n - 1)]
    prefix = [0.0]
    for value in gap:
        prefix.append(prefix[-1] + value)
    crossings: dict[str, dict[str, int]] = {}
    for i, a in enumerate(columns):
        row_costs: dict[str, int] = {}
        for j, b in enumerate(columns):
            if i == j:
                continue
            row_costs[b] = max(1, int(round(abs(prefix[j] - prefix[i]))))
        crossings[a] = row_costs
    return crossings


def build_formation(
    specs: list[CarriageSpec] | tuple[CarriageSpec, ...],
    train_code: str = "G1234",
    aisle_weight: float = 2.0,
    near_door_rows: int = 3,
    near_toilet_rows: int = 3,
) -> TrainFormation:
    """根据车厢规格生成完整的座位图谱。

    * ``aisle_weight`` / ``near_door_rows`` / ``near_toilet_rows`` 均可调，
      与 :class:`smartrail.config.EngineConfig` 中同名参数保持一致的语义。
    * 座位 ID 唯一性在此**强制校验**：ID 重复会让"座位图谱"退化成多对一，
      进而产生"两个人拿到同一个座位号"这类灾难性错误，必须在构造阶段就拦住。
    * 混合车厢里，排号在区段之间**连续累加**，但座位 ID 的排号用**全局排号**，
      因此 01 车的商务座是 ``01车09A`` 而不是 ``01车01A``（避免与一等座撞号）。
    """
    carriages: list[Carriage] = []
    seats: list[Seat] = []
    seen_ids: set[str] = set()
    for spec in specs:
        carriage = _build_carriage(spec)
        if carriage.number in {c.number for c in carriages}:
            raise ValueError(f"车厢编号重复：{carriage.number}（每节车厢必须有唯一编号）")
        carriages.append(carriage)
        blocks = spec.effective_blocks()
        global_row = 0
        for block in blocks:
            columns = tuple(block.layout)
            kinds = _COL_KIND["".join(columns)]
            crossings = _aisle_crossings(columns, _LAYOUTS[block.class_code][1], aisle_weight)
            for local_row in range(1, block.rows + 1):
                global_row += 1
                # 最后一行可能只铺部分列（用于精确对齐定员）
                limit = len(columns)
                if block.seats_per_row is not None and local_row == block.rows:
                    limit = min(block.seats_per_row, len(columns))
                near_door = min(
                    (abs(global_row - d) for d in carriage.door_positions), default=999
                ) < near_door_rows
                near_toilet = carriage.has_toilet and (
                    min((abs(global_row - d) for d in carriage.door_positions), default=999)
                    < near_toilet_rows
                    or global_row >= carriage.rows - near_toilet_rows + 1
                )
                accessible = (
                    global_row in spec.accessible_rows
                    or local_row in block.accessible_rows
                )
                for idx, col in enumerate(columns[:limit]):
                    features = {kinds[col]}
                    if kinds[col] is SeatFeature.AISLE:
                        features.add(SeatFeature.AISLE)
                    if near_door:
                        features.add(SeatFeature.NEAR_DOOR)
                    if near_toilet:
                        features.add(SeatFeature.NEAR_TOILET)
                    if accessible:
                        features.add(SeatFeature.ACCESSIBLE)
                    seat_id = f"{carriage.number:02d}车{global_row:02d}{col}"
                    if seat_id in seen_ids:
                        raise ValueError(f"座位 ID 重复：{seat_id}（编组配置有误）")
                    seen_ids.add(seat_id)
                    seats.append(
                        Seat(
                            seat_id=seat_id,
                            carriage=carriage.number,
                            row=global_row,
                            col=col,
                            features=frozenset(features),
                            col_index=idx,
                            aisle_crossings=crossings[col],
                            is_quiet_carriage=carriage.is_quiet_carriage,
                            class_code=block.class_code,
                            accessible_zone=accessible,
                            carriage_columns=columns,
                        )
                    )
    return TrainFormation(train_code=train_code, carriages=tuple(carriages), seats=tuple(seats))


# ---------------------------------------------------------------------------
# 京沪 G25 真实 16 节编组（对照用户提供的车型图）
# ---------------------------------------------------------------------------

#: 车型图上的定员（用于校验，改编组时若对不上会直接报错）
G25_EXPECTED_SEATS: dict[int, int] = {
    1: 32 + 5, 2: 93, 3: 93, 4: 78, 5: 83, 6: 93, 7: 93, 8: 43 + 6,
    9: 32 + 5, 10: 93, 11: 93, 12: 78, 13: 83, 14: 93, 15: 93, 16: 43 + 6,
}

#: 车型图上的车型代号
G25_CAR_TYPES: dict[int, str] = {
    1: "TC01 一等/商务座车", 2: "M02 二等座车", 3: "TP03 二等座车",
    4: "MH04 带残疾人卫生间二等座车", 5: "MB05 二等座车/餐车",
    6: "TP06 二等座车", 7: "M07 二等座车", 8: "TC08 二等/商务座车",
    9: "TC01 一等/商务座车", 10: "M02 二等座车", 11: "TP03 二等座车",
    12: "MH04 带残疾人卫生间二等座车", 13: "MB05 二等座车/餐车",
    14: "TP06 二等座车", 15: "M07 二等座车", 16: "TC08 二等/商务座车",
}


def _second_class_block(seats: int, *, accessible_rows: tuple[int, ...] = ()) -> Block:
    """构造二等座区段，精确命中定员（5 列，最后一行可不满）。

    93 = 18×5 + 3，83 = 16×5 + 3，78 = 15×5 + 3。
    末排只铺 3 列（ABC）—— 这一排现实中靠车门/卫生间，正好放得下 3 个座。
    """
    full, remainder = divmod(seats, 5)
    if remainder == 0:
        return Block("二等座", full, accessible_rows=accessible_rows)
    return Block("二等座", full + 1, seats_per_row=remainder,
                 accessible_rows=accessible_rows)


def g25_16_car_formation(
    train_code: str = "G25",
    quiet_carriages: tuple[int, ...] = (3, 11),
    aisle_weight: float = 2.0,
    near_door_rows: int = 3,
    near_toilet_rows: int = 3,
) -> TrainFormation:
    """京沪大标杆 G25 的 16 节编组（对照用户提供的车型图）。

    | 车厢 | 车型 | 定员 | 构成 |
    | ---: | :--- | ---: | :--- |
    | 01 | TC01 一等/商务座车 | 32+5 | 一等座 2+2 × 8 排 + 商务座 1+2 × 2 排 |
    | 02 | M02 二等座车 | 93 | 二等座 3+2，18 排 + 3 座 |
    | 03 | TP03 二等座车 | 93 | 同上，**静音车厢** |
    | 04 | MH04 带残疾人卫生间 | 78 | 二等座 3+2，15 排 + 3 座 |
    | 05 | MB05 二等/餐车 | 83 | 二等座 3+2，16 排 + 3 座 |
    | 06 | TP06 二等座车 | 93 | 同 02 |
    | 07 | M07 二等座车 | 93 | 同 02 |
    | 08 | TC08 二等/商务座车 | 43+6 | 二等座 + 商务座 1+2 × 2 排 |
    | 09-16 | 与 01-08 同型 | 同 | **11 车为静音车厢** |

    合计 **1417 座**。无障碍专区设在 04 车（带残疾人卫生间），
    与车型图上的 MH04 对应。
    """
    specs: list[CarriageSpec] = []
    for number in range(1, 17):
        is_quiet = number in quiet_carriages
        base = number if number <= 8 else number - 8
        if base == 1:
            # TC01 一等/商务座车：图上定员「32/5」
            # 一等座 2+2 × 8 排 = 32
            # 商务座 1+2：第 1 排 3 座 + 第 2 排 2 座 = 5（末排不满，靠车门）
            blocks = (Block("一等座", 8), Block("商务座", 2, seats_per_row=2))
            accessible: tuple[int, ...] = ()
            toilet = False
        elif base in (2, 3, 6, 7):
            blocks = (_second_class_block(93),)
            accessible = ()
            toilet = False
        elif base == 4:
            # 带残疾人卫生间：二等座 78，无障碍专区设在前两排
            blocks = (_second_class_block(78, accessible_rows=(1, 2)),)
            accessible = ()
            toilet = True
        elif base == 5:
            blocks = (_second_class_block(83),)
            accessible = ()
            toilet = False
        else:  # base == 8
            blocks = (_second_class_block(43), Block("商务座", 2))
            accessible = ()
            toilet = False
        specs.append(
            CarriageSpec(
                number=number,
                class_code=blocks[0].class_code,
                rows=sum(block.rows for block in blocks),
                is_quiet_carriage=is_quiet,
                accessible_rows=accessible,
                has_toilet=toilet,
                door_rows=(1, sum(block.rows for block in blocks)),
                blocks=blocks,
                note=G25_CAR_TYPES[number],
            )
        )
    formation = build_formation(
        specs,
        train_code=train_code,
        aisle_weight=aisle_weight,
        near_door_rows=near_door_rows,
        near_toilet_rows=near_toilet_rows,
    )
    # 定员必须与车型图一致 —— 否则说明区段拼装算错了
    actual = {
        carriage.number: sum(
            1 for seat in formation.seats if seat.carriage == carriage.number
        )
        for carriage in formation.carriages
    }
    mismatch = {
        number: (expected, actual.get(number))
        for number, expected in G25_EXPECTED_SEATS.items()
        if actual.get(number) != expected
    }
    if mismatch:
        raise ValueError(f"编组定员与车型图不符：{mismatch}")
    return formation


def crh_16_car_formation(
    train_code: str = "G1234",
    quiet_carriages: tuple[int, ...] = (3, 11),
    rows: int = 17,
    aisle_weight: float = 2.0,
    near_door_rows: int = 3,
    near_toilet_rows: int = 3,
) -> TrainFormation:
    """兼容入口：现在转发到 :func:`g25_16_car_formation`。

    保留这个名字是因为既有代码、测试与文档都在用它。``rows`` 参数已不再使用
    （真实编组由车型图决定，不是所有车厢一个排数）。

    历史背景（被用户抓出来的真实缺陷）：早期实现让 16 节车厢**共用同一个排数**，
    只靠"减少列数"区分坐席，于是商务座被算成 ``3 列 × 17 排 = 51 座`` ——
    现实中商务座整个车厢只有 5 座。座位数是容量上限，直接决定
    "满座时会不会拒票""无障碍专区能服务几位轮椅旅客"这类运营判断。
    """
    _ = rows  # 保留签名兼容；排数由车型图决定
    return g25_16_car_formation(
        train_code=train_code,
        quiet_carriages=quiet_carriages,
        aisle_weight=aisle_weight,
        near_door_rows=near_door_rows,
        near_toilet_rows=near_toilet_rows,
    )


def mini_formation(
    rows: int = 6,
    quiet_carriages: tuple[int, ...] = (2,),
    aisle_weight: float = 2.0,
    near_door_rows: int = 3,
    near_toilet_rows: int = 3,
) -> TrainFormation:
    """小规模编组，便于单元测试与快速仿真（3 节二等座）。

    注意：每节车厢必须有**唯一**编号，否则座位 ID 会重复（早期版本正是把
    ``number`` 写成了常量 1，导致 30 个座位里只有 10 个唯一 ID）。
    """
    specs = [
        CarriageSpec(
            number=number,
            class_code="二等座",
            rows=rows,
            accessible_rows=(1,) if number == 1 else (),
            has_toilet=number == 1,
            door_rows=(1, rows),
            is_quiet_carriage=number in quiet_carriages,
        )
        for number in (1, 2, 3)
    ]
    return build_formation(
        specs,
        train_code="T-MINI",
        aisle_weight=aisle_weight,
        near_door_rows=near_door_rows,
        near_toilet_rows=near_toilet_rows,
    )
