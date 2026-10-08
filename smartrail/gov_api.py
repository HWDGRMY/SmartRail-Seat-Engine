"""政务数据 API 抽象层（残联 / 政务平台）。

README 5.2 中轮椅、视障等状态来自"政务 API 静默获取"。本模块只定义**接口契约**
与一个**本地模拟实现**，用于学术演练：

* 真实落地必须走授权、脱敏、最小必要原则，并留痕审计；
* 本项目不含任何真实公民隐私数据（见 README 免责声明）。

契约由 :class:`SupportDataProvider` 定义，线上可替换为 HTTP 客户端实现，
分配引擎只依赖该协议，不关心数据来源。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, Sequence, runtime_checkable

from .models import DataSource, Passenger, SupportNeed, TicketType


@dataclass(frozen=True)
class SupportProfile:
    """政务平台返回的资格档案（最小必要字段）。"""

    passenger_id: str
    support_needs: frozenset[SupportNeed]
    certificate_no: str = ""
    verified: bool = True
    source: DataSource = DataSource.GOV_API
    notes: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "passenger_id": self.passenger_id,
            "support_needs": sorted(n.value for n in self.support_needs),
            "certificate_no": self.certificate_no,
            "verified": self.verified,
            "source": self.source.value,
            "notes": self.notes,
        }


@runtime_checkable
class SupportDataProvider(Protocol):
    """资格数据提供方协议。"""

    def lookup(self, passenger_id: str) -> SupportProfile | None:
        """按乘客 ID 查询（真实实现需带授权令牌与审计日志）。"""

    def lookup_many(self, passenger_ids: Sequence[str]) -> dict[str, SupportProfile]:
        """批量查询，减少 RPC 往返。"""


class InMemorySupportProvider:
    """本地模拟实现：仅用于测试与仿真。"""

    def __init__(self, profiles: dict[str, SupportProfile] | None = None) -> None:
        self._profiles: dict[str, SupportProfile] = dict(profiles or {})

    def register(self, profile: SupportProfile) -> None:
        self._profiles[profile.passenger_id] = profile

    def register_needs(self, passenger_id: str, needs: Sequence[SupportNeed], certificate_no: str = "") -> None:
        self._profiles[passenger_id] = SupportProfile(
            passenger_id=passenger_id,
            support_needs=frozenset(needs),
            certificate_no=certificate_no,
        )

    def lookup(self, passenger_id: str) -> SupportProfile | None:
        return self._profiles.get(passenger_id)

    def lookup_many(self, passenger_ids: Sequence[str]) -> dict[str, SupportProfile]:
        return {pid: self._profiles[pid] for pid in passenger_ids if pid in self._profiles}


@dataclass
class GovernmentApiClient:
    """带审计日志的装饰器：任何一次静默获取都会留下可追溯记录。"""

    provider: SupportDataProvider
    audit_log: list[dict[str, str]] = field(default_factory=list)

    def lookup(self, passenger_id: str, purpose: str = "seat_allocation") -> SupportProfile | None:
        self.audit_log.append({"passenger_id": passenger_id, "purpose": purpose, "action": "lookup"})
        return self.provider.lookup(passenger_id)

    def enrich(self, passengers: Sequence[Passenger]) -> list[Passenger]:
        """把政务数据合并进乘客图谱（补充 support_needs，不覆盖用户申报）。"""
        profiles = self.provider.lookup_many([p.passenger_id for p in passengers])
        out: list[Passenger] = []
        for passenger in passengers:
            profile = profiles.get(passenger.passenger_id)
            if not profile:
                out.append(passenger)
                continue
            self.audit_log.append(
                {"passenger_id": passenger.passenger_id, "purpose": "enrich", "action": "merge"}
            )
            from dataclasses import replace

            out.append(
                replace(
                    passenger,
                    support_needs=passenger.support_needs | profile.support_needs,
                    source=DataSource.GOV_API if profile.verified else passenger.source,
                )
            )
        return out


def demo_provider() -> InMemorySupportProvider:
    """构造一个包含轮椅 / 视障 / 智力障碍资格的最小演示数据集。"""
    provider = InMemorySupportProvider()
    provider.register_needs("P-WHEEL-1", [SupportNeed.WHEELCHAIR], certificate_no="CJ-2024-0001")
    provider.register_needs("P-BLIND-1", [SupportNeed.INDEPENDENT_BLIND], certificate_no="CJ-2024-0002")
    provider.register_needs("P-ID-1", [SupportNeed.INTELLECTUAL_DISABILITY], certificate_no="CJ-2024-0003")
    return provider


def infer_ticket_type(age: int, declared: TicketType | None = None) -> TicketType:
    """票种推断（仅在没有明确票种时使用）。"""
    if declared is not None:
        return declared
    return TicketType.CHILD if age < 14 else TicketType.ADULT
