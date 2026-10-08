"""V3.0：状态 → 张量的特征工程（numpy 实现，无需 PyTorch 即可单测）。

为什么单独一层
--------------
"图神经网络能不能学到东西"高度依赖输入特征。把特征抽取从网络里拆出来：

* 可以用纯 numpy 写、纯 numpy 测（不依赖 torch，CI 上跑得快）；
* 特征含义逐项可解释（验收页里能直接展示"模型看到了什么"）；
* 换网络结构时特征层不用动。

特征设计（二分图：乘客节点 ↔ 座位节点，边 = 候选座位）
------------------------------------------------------
乘客节点（``PASSENGER_FEATURES`` 维）：票种 / 年龄 / 每类 support_need 的 one-hot /
是否需照护 / 是否照护人 / 静音信用分 / 是否被屏蔽静音车厢 / 静音排斥权重 /
申报行为 one-hot / 硬核邻居数 / 软绑定邻居数 / 偏好过道 / 偏好靠窗。

座位节点（``SEAT_FEATURES`` 维）：车厢 / 排 / 列序号 / 过道 / 靠窗 / 中间 /
无障碍专区 / 静音车厢 / 近车门 / 近卫生间 / 是否空闲 / 该车厢占用率。

候选边（``EDGE_FEATURES`` 维）：个体亲和度 / 无障碍匹配 / 与已就位同伴的紧邻程度 /
两侧空位（连座潜力）/ 该车厢占用率。

全局节点（``GLOBAL_FEATURES`` 维）：余票率 / 静音占用率 / 无障碍占用率 /
订单规模 / 需照护人数 / 硬核对数 / 模式 one-hot / 步骤进度。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

import numpy as np

from ..config import EngineConfig
from ..credit import CreditLedger
from ..fastscore import FastScorer
from ..models import (
    BondType,
    DeclaredBehavior,
    Order,
    Passenger,
    Seat,
    SupportNeed,
    TicketType,
)
from ..scoring import is_care_dependent, support_people_of
from ..solver import OrderContext, build_context

MAX_PASSENGERS = 10
MAX_SEATS = 48
MAX_GLOBAL = 12

_TICKET_KINDS = list(TicketType)
_NEEDS = list(SupportNeed)
_BEHAVIORS = list(DeclaredBehavior)

PASSENGER_FEATURES = (
    2 + 1 + len(_NEEDS) + 4 + 1 + len(_BEHAVIORS) + 2 + 2
)  # = 2+1+7+4+1+3+2+2 = 22
SEAT_FEATURES = 12
EDGE_FEATURES = 5
GLOBAL_FEATURES = MAX_GLOBAL


@dataclass
class GraphObservation:
    """一个订单的图观测（全部为定长数组，便于 batch 与序列化）。"""

    passenger_features: np.ndarray   # (MAX_PASSENGERS, PASSENGER_FEATURES)
    seat_features: np.ndarray        # (MAX_SEATS, SEAT_FEATURES)
    edge_index: np.ndarray           # (2, MAX_PASSENGERS*MAX_SEATS)
    edge_features: np.ndarray        # (MAX_PASSENGERS*MAX_SEATS, EDGE_FEATURES)
    seat_mask: np.ndarray            # (MAX_SEATS,)
    passenger_mask: np.ndarray       # (MAX_PASSENGERS,)
    global_features: np.ndarray      # (GLOBAL_FEATURES,)
    candidate_seat_ids: list[str]
    passenger_ids: list[str]
    passenger_seat_scores: list[list[float]]
    """(乘客序号, 候选座位序号) -> 归一化个体亲和度，供策略打分使用。"""

    def to_dict(self) -> dict[str, Any]:
        return {
            "passenger_features": self.passenger_features.round(4).tolist(),
            "seat_features": self.seat_features.round(4).tolist(),
            "edge_features": self.edge_features.round(4).tolist(),
            "seat_mask": self.seat_mask.astype(int).tolist(),
            "passenger_mask": self.passenger_mask.astype(int).tolist(),
            "global_features": self.global_features.round(4).tolist(),
            "candidate_seat_ids": list(self.candidate_seat_ids),
            "passenger_ids": list(self.passenger_ids),
            "dims": {
                "passenger_features": PASSENGER_FEATURES,
                "seat_features": SEAT_FEATURES,
                "edge_features": EDGE_FEATURES,
                "global_features": GLOBAL_FEATURES,
            },
        }


def _norm(value: float, low: float, high: float) -> float:
    if high <= low:
        return 0.0
    return max(0.0, min(1.0, (value - low) / (high - low)))


def passenger_vector(
    passenger: Passenger, ctx: OrderContext, credit: CreditLedger
) -> np.ndarray:
    """单个乘客节点的特征向量。"""
    score = credit.score(passenger.passenger_id)
    helpers = support_people_of(passenger, ctx.order)
    mandatory = [
        h for h in helpers if ctx.order.bond_of(passenger.passenger_id, h) is BondType.MANDATORY
    ]
    soft = [h for h in helpers if h not in mandatory]
    vector: list[float] = [
        1.0 if passenger.ticket_type is TicketType.ADULT else 0.0,
        1.0 if passenger.is_child else 0.0,
        _norm(passenger.age, 0, 90),
    ]
    vector.extend(1.0 if need in passenger.support_needs else 0.0 for need in _NEEDS)
    vector.extend(
        [
            1.0 if is_care_dependent(passenger, ctx.order) else 0.0,
            1.0 if passenger.is_caregiver else 0.0,
            _norm(score, 0, 150),
            1.0 if score < 60.0 else 0.0,
            _norm(passenger.quiet_repulsion, 0, 2),
        ]
    )
    vector.extend(
        1.0 if passenger.declared_behavior is behavior else 0.0 for behavior in _BEHAVIORS
    )
    vector.extend(
        [
            _norm(len(mandatory), 0, 3),
            _norm(len(soft), 0, 5),
            1.0 if passenger.preference_aisle else 0.0,
            1.0 if passenger.preference_window else 0.0,
        ]
    )
    return np.asarray(vector, dtype=np.float32)


def seat_vector(seat: Seat, carriage_occupancy: Mapping[int, float]) -> np.ndarray:
    """单个座位节点的特征向量。"""
    features = {f.value for f in seat.features}
    return np.asarray(
        [
            _norm(seat.carriage, 1, 16),
            _norm(seat.row, 1, 20),
            _norm(seat.col_index, 0, 4),
            1.0 if seat.is_aisle else 0.0,
            1.0 if "window" in features else 0.0,
            1.0 if "middle" in features else 0.0,
            1.0 if seat.in_accessible_zone() else 0.0,
            1.0 if seat.is_quiet_carriage else 0.0,
            1.0 if "near_door" in features else 0.0,
            1.0 if "near_toilet" in features else 0.0,
            1.0,
            float(carriage_occupancy.get(seat.carriage, 0.0)),
        ],
        dtype=np.float32,
    )


def _carriage_occupancy(
    all_seats: Sequence[Seat], occupied: set[str]
) -> dict[int, float]:
    """各车厢占用率（以**整列车**为分母，而不是以余票为分母）。"""
    totals: dict[int, int] = {}
    taken: dict[int, int] = {}
    for seat in all_seats:
        totals[seat.carriage] = totals.get(seat.carriage, 0) + 1
        if seat.seat_id in occupied:
            taken[seat.carriage] = taken.get(seat.carriage, 0) + 1
    return {
        carriage: (taken.get(carriage, 0) / total if total else 0.0)
        for carriage, total in totals.items()
    }


def build_observation(
    order: Order,
    available_seats: Sequence[Seat],
    config: EngineConfig,
    credit: CreditLedger,
    occupied_seats: set[str] | None = None,
    all_seats: Sequence[Seat] | None = None,
    mode: str = "smart",
    step: int = 0,
    max_steps: int = 1,
    solved_violations: int = 0,
    max_seats: int = MAX_SEATS,
) -> GraphObservation:
    """把一个订单 + 当前余票编码成图观测。

    候选座位池按"全体乘客的个体亲和度之和"排序取前 N —— 与 V2.0 的 CP-SAT
    候选压缩思路一致，因此 V3 学到的打分与 V2 的精确解可直接比较。
    """
    ctx = build_context(order, config)
    seats = list(available_seats)
    occupied = set(occupied_seats or set())
    reference = list(all_seats) if all_seats is not None else seats
    occupancy = _carriage_occupancy(reference, occupied)
    fs = FastScorer(order, ctx.passengers, seats, config, ctx.unit_of)

    passengers = list(order.passengers)[:MAX_PASSENGERS]
    passenger_features = np.zeros((MAX_PASSENGERS, PASSENGER_FEATURES), dtype=np.float32)
    passenger_mask = np.zeros(MAX_PASSENGERS, dtype=np.float32)
    for index, passenger in enumerate(passengers):
        passenger_features[index] = passenger_vector(passenger, ctx, credit)
        passenger_mask[index] = 1.0

    # -- 候选座位池 -------------------------------------------------------
    if seats:
        aggregate = np.zeros(len(seats), dtype=np.float32)
        for passenger in passengers:
            aggregate += np.asarray(fs.row(passenger.passenger_id), dtype=np.float32)
        picked = sorted(int(i) for i in np.argsort(-aggregate)[:max_seats])
    else:
        picked = []
    candidate_seats = [seats[i] for i in picked]
    seat_count = len(candidate_seats)

    seat_features = np.zeros((max_seats, SEAT_FEATURES), dtype=np.float32)
    seat_mask = np.zeros(max_seats, dtype=np.float32)
    for local, seat in enumerate(candidate_seats):
        seat_features[local] = seat_vector(seat, occupancy)
        seat_mask[local] = 1.0

    # 用于"连座潜力"的快速索引
    by_carriage_row: dict[tuple[int, int], set[int]] = {}
    for index, seat in enumerate(seats):
        by_carriage_row.setdefault((seat.carriage, seat.row), set()).add(seat.col_index)

    # 已占座位的坐标索引：用于判断"某乘客的绑定同伴是否已坐在同一排"
    occupied_positions = {
        seat.seat_id: (seat.carriage, seat.row) for seat in reference if seat.seat_id in occupied
    }

    edge_count = MAX_PASSENGERS * max_seats
    edge_index = np.zeros((2, edge_count), dtype=np.int64)
    edge_features = np.zeros((edge_count, EDGE_FEATURES), dtype=np.float32)
    seat_scores: list[list[float]] = [[0.0] * seat_count for _ in range(MAX_PASSENGERS)]
    cursor = 0
    for i in range(MAX_PASSENGERS):
        for j in range(max_seats):
            edge_index[0, cursor] = i
            edge_index[1, cursor] = j
            if i < len(passengers) and j < seat_count:
                passenger = passengers[i]
                seat = candidate_seats[j]
                slot = picked[j]
                affinity = fs.row(passenger.passenger_id)[slot]
                helpers = support_people_of(passenger, ctx.order)
                # companion_same_row：绑定同伴是否已坐在同一排（1=是）
                companion_same_row = 0.0
                for helper in helpers:
                    position = occupied_positions.get(helper)
                    if position and position == (seat.carriage, seat.row):
                        companion_same_row = 1.0
                        break
                # side_space：同排左右两侧是否还有空位（连座潜力）
                row_cols = by_carriage_row.get((seat.carriage, seat.row), set())
                side_space = sum(
                    1 for delta in (-1, 1) if (seat.col_index + delta) in row_cols
                )
                edge_features[cursor] = np.asarray(
                    [
                        _norm(affinity, -100_000.0, 200.0),
                        1.0 if seat.in_accessible_zone() else 0.0,
                        companion_same_row,
                        _norm(side_space, 0, 2),
                        float(occupancy.get(seat.carriage, 0.0)),
                    ],
                    dtype=np.float32,
                )
                seat_scores[i][j] = float(edge_features[cursor][0])
            else:
                edge_features[cursor] = 0.0
            cursor += 1

    # -- 全局节点 ---------------------------------------------------------
    total_seats = max(1, len(reference))
    quiet_total = sum(1 for s in reference if s.is_quiet_carriage)
    quiet_taken = sum(1 for s in reference if s.is_quiet_carriage and s.seat_id in occupied)
    acc_total = sum(1 for s in reference if s.in_accessible_zone())
    acc_taken = sum(1 for s in reference if s.in_accessible_zone() and s.seat_id in occupied)
    care_count = sum(1 for p in passengers if is_care_dependent(p, ctx.order))
    hard_pairs = sum(
        1
        for a in order.passengers
        for b in order.passengers
        if a.passenger_id < b.passenger_id
        and order.bond_of(a.passenger_id, b.passenger_id) is BondType.MANDATORY
    )
    mode_index = {"free": 0, "smart": 1, "degraded": 2}.get(mode, 1)
    global_features = np.zeros(MAX_GLOBAL, dtype=np.float32)
    global_features[:8] = np.asarray(
        [
            _norm(len(seats) / total_seats, 0, 1),
            _norm(quiet_taken / quiet_total if quiet_total else 0.0, 0, 1),
            _norm(acc_taken / acc_total if acc_total else 0.0, 0, 1),
            _norm(len(passengers), 0, MAX_PASSENGERS),
            _norm(care_count, 0, 4),
            _norm(hard_pairs, 0, 6),
            _norm(solved_violations, 0, 5),
            _norm(step / max(1, max_steps), 0, 1),
        ],
        dtype=np.float32,
    )
    global_features[8 + mode_index] = 1.0

    return GraphObservation(
        passenger_features=passenger_features,
        seat_features=seat_features,
        edge_index=edge_index,
        edge_features=edge_features,
        seat_mask=seat_mask,
        passenger_mask=passenger_mask,
        global_features=global_features,
        candidate_seat_ids=[s.seat_id for s in candidate_seats],
        passenger_ids=[p.passenger_id for p in passengers],
        passenger_seat_scores=seat_scores,
    )


__all__ = [
    "EDGE_FEATURES",
    "GLOBAL_FEATURES",
    "GraphObservation",
    "MAX_GLOBAL",
    "MAX_PASSENGERS",
    "MAX_SEATS",
    "PASSENGER_FEATURES",
    "SEAT_FEATURES",
    "build_observation",
    "passenger_vector",
    "seat_vector",
]
