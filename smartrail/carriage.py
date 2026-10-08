"""车厢编组与座位图谱生成（Seat Vector 的空间坐标构建）。

物理约定
--------
* ``row``   : 排号（X 轴），1..rows
* ``col``   : 列号（Y 轴），如 "A/B/C/D/F"；缺 E 沿用国铁惯例
* 过道位于 ``aisle_after`` 列之后；过道本身也算一个"通道单位"参与曼哈顿距离

静音车厢、无障碍专区、卫生间、车门均建模为车厢/座位标签，供代价函数与
降级策略读取。
"""

from __future__ import annotations

from dataclasses import dataclass

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


@dataclass(frozen=True)
class CarriageSpec:
    """车厢规格（编组配置的输入）。"""

    number: int
    class_code: str = "二等座"
    rows: int = 17
    is_quiet_carriage: bool = False
    accessible_rows: tuple[int, ...] = ()   # 无障碍专区排号
    has_toilet: bool = False
    door_rows: tuple[int, ...] = (1, 17)


def _build_carriage(spec: CarriageSpec) -> Carriage:
    columns, aisle_after, _ = _LAYOUTS[spec.class_code]
    return Carriage(
        number=spec.number,
        class_code=spec.class_code,
        columns=columns,
        aisle_after=aisle_after,
        rows=spec.rows,
        is_quiet_carriage=spec.is_quiet_carriage,
        has_accessible_zone=bool(spec.accessible_rows),
        has_toilet=spec.has_toilet,
        door_positions=tuple(spec.door_rows),
    )


def _aisle_crossings(columns: tuple[str, ...], aisle_after: str, weight: float = 2.0) -> dict[str, int]:
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
    """
    carriages: list[Carriage] = []
    seats: list[Seat] = []
    seen_ids: set[str] = set()
    for spec in specs:
        carriage = _build_carriage(spec)
        if carriage.number in {c.number for c in carriages}:
            raise ValueError(f"车厢编号重复：{carriage.number}（每节车厢必须有唯一编号）")
        carriages.append(carriage)
        columns = carriage.columns
        kinds = _COL_KIND["".join(columns)]
        crossings = _aisle_crossings(columns, carriage.aisle_after, aisle_weight)
        for row in range(1, carriage.rows + 1):
            near_door = min(abs(row - d) for d in carriage.door_positions) < near_door_rows
            near_toilet = carriage.has_toilet and (
                min(abs(row - d) for d in carriage.door_positions) < near_toilet_rows
                or row >= carriage.rows - near_toilet_rows + 1
            )
            accessible = row in spec.accessible_rows
            for idx, col in enumerate(columns):
                features = {kinds[col]}
                if kinds[col] is SeatFeature.AISLE:
                    features.add(SeatFeature.AISLE)
                if near_door:
                    features.add(SeatFeature.NEAR_DOOR)
                if near_toilet:
                    features.add(SeatFeature.NEAR_TOILET)
                if accessible:
                    features.add(SeatFeature.ACCESSIBLE)
                seat_id = f"{carriage.number:02d}车{row:02d}{col}"
                if seat_id in seen_ids:
                    raise ValueError(f"座位 ID 重复：{seat_id}（编组配置有误）")
                seen_ids.add(seat_id)
                seats.append(
                    Seat(
                        seat_id=seat_id,
                        carriage=carriage.number,
                        row=row,
                        col=col,
                        features=frozenset(features),
                        col_index=idx,
                        aisle_crossings=crossings[col],
                        is_quiet_carriage=carriage.is_quiet_carriage,
                        class_code=carriage.class_code,
                        accessible_zone=accessible,
                    )
                )
    return TrainFormation(train_code=train_code, carriages=tuple(carriages), seats=tuple(seats))


def crh_16_car_formation(
    train_code: str = "G1234",
    quiet_carriages: tuple[int, ...] = (5,),
    rows: int = 17,
    aisle_weight: float = 2.0,
    near_door_rows: int = 3,
    near_toilet_rows: int = 3,
) -> TrainFormation:
    """16 节编组：1-8 二等座、9-12 一等座、13-16 商务座。

    默认 5 车为静音车厢，1 车含无障碍专区（第 1-2 排）与卫生间。

    **各车厢排数不同**（这是一个被用户抓出来的真实缺陷）
    ----------------------------------------------------
    早期实现让 16 节车厢**共用同一个排数**（默认 17），只靠"减少列数"来区分坐席，
    于是商务座被算成 ``3 列 × 17 排 = 51 座`` —— 而现实中商务座是 1+2 布局，
    整个车厢只有 10 多个座位；一等座也只有约 48 座，不是 68 座。

    为什么这不只是"数字不真实"：座位数是容量上限，直接决定
    "满座时会不会拒票""无障碍专区能服务几位轮椅旅客"这类运营判断。
    一个把商务座容量夸大 4 倍的模型，会让压力测试得出过于乐观的结论。

    修正后的编组（对照 CRH380A/CR400 系列公开资料）：

    | 车厢 | 坐席 | 布局 | 排数 | 座位数 |
    | :--- | :--- | :--- | ---: | ---: |
    | 1 车 | 二等座 + 无障碍专区 + 卫生间 | 3+2 | 16 | 80 |
    | 2-7 车 | 二等座 | 3+2 | 17 | 85 |
    | 8 车 | 二等座 + 卫生间 | 3+2 | 14 | 70 |
    | 9-12 车 | 一等座 | 2+2 | 12 | 48 |
    | 13 车 | 商务座 + 卫生间 | 1+2 | 4 | 12 |
    | 14-15 车 | 商务座 | 1+2 | 5 | 15 |
    | 16 车 | 商务座 + 卫生间 | 1+2 | 5 | 15 |
    """
    # 各车厢的排数：餐车/卫生间车厢与高等级坐席的车厢排数都更少
    row_plan: dict[int, int] = {
        1: 16,                       # 含无障碍专区与卫生间，排数略少
        **{number: rows for number in range(2, 8)},
        8: 14,                       # 含卫生间
        **{number: 12 for number in range(9, 13)},   # 一等座：2+2 且排数少
        13: 4,                       # 商务座：1+2，仅 4 排
        **{number: 5 for number in range(14, 17)},
    }
    specs: list[CarriageSpec] = []
    for number in range(1, 17):
        if number <= 8:
            class_code = "二等座"
        elif number <= 12:
            class_code = "一等座"
        else:
            class_code = "商务座"
        accessible_rows: tuple[int, ...] = ()
        if number == 1:
            accessible_rows = (1, 2)
        car_rows = row_plan.get(number, rows)
        specs.append(
            CarriageSpec(
                number=number,
                class_code=class_code,
                rows=car_rows,
                is_quiet_carriage=number in quiet_carriages,
                accessible_rows=accessible_rows,
                has_toilet=number in (1, 4, 8, 12, 16),
                door_rows=(1, car_rows),
            )
        )
    return build_formation(
        specs,
        train_code=train_code,
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
