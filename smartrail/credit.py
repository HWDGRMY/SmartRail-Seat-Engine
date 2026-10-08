"""动态信用体系（Quietness Score）。

对应 README 5.1：
* 初始 100 分；
* 在静音车厢被投诉 -50；表现良好 +10；
* 低于 60 分时，未来购票**强制屏蔽**静音车厢权限（由
  :attr:`smartrail.models.Passenger.quiet_carriage_blocked` 与 Scorer 共同保证）；
* 闭环反馈：向乘务员终端推送"活泼型儿童分配预警"。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

INITIAL_SCORE = 100.0
COMPLAINT_PENALTY = -50.0
COMMENDATION_BONUS = 10.0
BLOCK_THRESHOLD = 60.0


@dataclass
class QuietnessRecord:
    """单个乘客的信用档案。"""

    passenger_id: str
    score: float = INITIAL_SCORE
    complaints: int = 0
    commendations: int = 0
    events: list[dict[str, Any]] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return self.score < BLOCK_THRESHOLD

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["blocked"] = self.blocked
        return data


class CreditLedger:
    """内存信用账本（可持久化到 JSON，便于仿真与离线分析）。"""

    def __init__(self, path: str | Path | None = None) -> None:
        self._records: dict[str, QuietnessRecord] = {}
        self._path = Path(path) if path else None
        if self._path and self._path.exists():
            self.load()

    # -- 读 ---------------------------------------------------------------
    def get(self, passenger_id: str) -> QuietnessRecord:
        return self._records.setdefault(passenger_id, QuietnessRecord(passenger_id))

    def score(self, passenger_id: str) -> float:
        return self.get(passenger_id).score

    def is_blocked(self, passenger_id: str) -> bool:
        return self.get(passenger_id).blocked

    def all(self) -> dict[str, QuietnessRecord]:
        return dict(self._records)

    # -- 写 ---------------------------------------------------------------
    def apply(self, passenger_id: str, delta: float, reason: str = "") -> QuietnessRecord:
        record = self.get(passenger_id)
        record.score = max(0.0, min(150.0, record.score + delta))
        record.events.append({"delta": delta, "reason": reason, "score": record.score})
        if delta < 0:
            record.complaints += 1
        elif delta > 0:
            record.commendations += 1
        self.save()
        return record

    def report_complaint(self, passenger_id: str, reason: str = "静音车厢被投诉") -> QuietnessRecord:
        return self.apply(passenger_id, COMPLAINT_PENALTY, reason)

    def report_good_behavior(self, passenger_id: str, reason: str = "静音车厢表现良好") -> QuietnessRecord:
        return self.apply(passenger_id, COMMENDATION_BONUS, reason)

    def set_score(self, passenger_id: str, score: float, reason: str = "人工调整") -> QuietnessRecord:
        record = self.get(passenger_id)
        delta = score - record.score
        return self.apply(passenger_id, delta, reason)

    # -- 持久化 -----------------------------------------------------------
    def save(self) -> None:
        if not self._path:
            return
        payload = {pid: rec.to_dict() for pid, rec in self._records.items()}
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def load(self) -> None:
        if not self._path or not self._path.exists():
            return
        payload = json.loads(self._path.read_text(encoding="utf-8"))
        self._records = {}
        for pid, data in payload.items():
            self._records[pid] = QuietnessRecord(
                passenger_id=pid,
                score=float(data.get("score", INITIAL_SCORE)),
                complaints=int(data.get("complaints", 0)),
                commendations=int(data.get("commendations", 0)),
                events=list(data.get("events", [])),
            )


def attrition_warnings(assignments: list[Any], carriage_lookup: dict[str, Any]) -> list[dict[str, Any]]:
    """闭环反馈：生成乘务员终端的"活泼型儿童分配预警"。

    ``assignments`` 需为 :class:`smartrail.models.Assignment` 序列。
    """
    warnings: list[dict[str, Any]] = []
    for assignment in assignments:
        seat = carriage_lookup.get(assignment.seat_id)
        if seat is None:
            continue
        warnings.append(
            {
                "seat_id": assignment.seat_id,
                "carriage": assignment.carriage,
                "row": assignment.row,
                "quiet_carriage": assignment.quiet_carriage,
                "passenger_id": assignment.passenger_id,
            }
        )
    return warnings
