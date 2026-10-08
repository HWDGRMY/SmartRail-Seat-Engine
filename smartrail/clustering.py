"""余票分块与多中心聚类（README 4.2 步骤 1）。

把连续空位划分为 Block：既要在"排内连续"，也要在"物理上可达"。
过道被视作一个可跨越的通道；跨车厢的座位**永远不会**进入同一个 Block，
这正是"跨车厢代价 = 10000"在数据层的第一道体现。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import Seat, TrainFormation


@dataclass(frozen=True)
class SeatBlock:
    """一个连续空位块。"""

    block_id: str
    carriage: int
    row: int
    seats: tuple[Seat, ...]
    accessible: bool = False
    quiet: bool = False

    @property
    def size(self) -> int:
        return len(self.seats)

    @property
    def seat_ids(self) -> tuple[str, ...]:
        return tuple(s.seat_id for s in self.seats)

    def combinations_of_size(self, k: int) -> list[tuple[Seat, ...]]:
        """块内所有大小为 k 的**连续**子段（滑动窗口）。"""
        if k <= 0 or k > self.size:
            return []
        return [tuple(self.seats[i : i + k]) for i in range(self.size - k + 1)]


@dataclass
class BookingState:
    """可变的余票与占座状态。"""

    formation: TrainFormation
    occupied: set[str] = field(default_factory=set)
    holds: dict[str, str] = field(default_factory=dict)  # seat_id -> order_id (已确认分配)

    @property
    def total_seats(self) -> int:
        return len(self.formation.seats)

    @property
    def available_seats(self) -> tuple[Seat, ...]:
        return tuple(s for s in self.formation.seats if s.seat_id not in self.occupied)

    @property
    def availability_ratio(self) -> float:
        if not self.total_seats:
            return 0.0
        return len(self.available_seats) / self.total_seats

    def occupy(self, seat_ids: tuple[str, ...] | list[str], order_id: str = "") -> None:
        for seat_id in seat_ids:
            self.occupied.add(seat_id)
            if order_id:
                self.holds[seat_id] = order_id

    def release(self, seat_ids: tuple[str, ...] | list[str]) -> None:
        for seat_id in seat_ids:
            self.occupied.discard(seat_id)
            self.holds.pop(seat_id, None)

    def mark_occupied(self, seat_ids: tuple[str, ...] | list[str]) -> None:
        """外部（其他订单 / 售票系统）占座。"""
        self.occupied.update(seat_ids)


def cluster_blocks(seats: tuple[Seat, ...] | list[Seat], carriage_columns: dict[int, tuple[str, ...]]) -> list[SeatBlock]:
    """把空位聚类成 Block。

    同一排内按列顺序扫描，遇到"物理不相邻"（缺一座位）就断开；跨排/跨车厢必然断开。
    """
    buckets: dict[tuple[int, int], list[Seat]] = {}
    for seat in seats:
        buckets.setdefault((seat.carriage, seat.row), []).append(seat)
    blocks: list[SeatBlock] = []
    for (carriage, row), row_seats in sorted(buckets.items()):
        columns = carriage_columns[carriage]
        order = {col: i for i, col in enumerate(columns)}
        row_seats.sort(key=lambda s: order.get(s.col, 99))
        run: list[Seat] = []
        for seat in row_seats:
            if run and order.get(seat.col, 99) != order.get(run[-1].col, 99) + 1:
                blocks.append(_make_block(carriage, row, run))
                run = []
            run.append(seat)
        if run:
            blocks.append(_make_block(carriage, row, run))
    return blocks


def _make_block(carriage: int, row: int, seats: list[Seat]) -> SeatBlock:
    return SeatBlock(
        block_id=f"{carriage:02d}-{row:02d}",
        carriage=carriage,
        row=row,
        seats=tuple(seats),
        accessible=any(s.in_accessible_zone() for s in seats),
        quiet=any(s.is_quiet_carriage for s in seats),
    )


def blocks_of(formation: TrainFormation, seats: tuple[Seat, ...] | list[Seat]) -> list[SeatBlock]:
    columns = {c.number: c.columns for c in formation.carriages}
    return cluster_blocks(list(seats), columns)


def best_window(block: SeatBlock, size: int) -> list[tuple[Seat, ...]]:
    """块内可用窗口；若块比需求小则返回空。"""
    return block.combinations_of_size(size)
