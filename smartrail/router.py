"""流量网关与决策路由（Hierarchical Decision State Machine）。

对应 README 第 2 节：根据实时余票量 / 并发压力动态切换三种模式，避免过度设计、
杜绝算力浪费；并在雪崩边缘自动降级（绝对底线）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class AllocationMode(str, Enum):
    """三种工作模式。"""

    FREE = "free"          # 模式一：余票充足 -> 自由选座 + 可视化座位图
    SMART = "smart"        # 模式二：余票紧张 -> 智能协同分配（代价函数 + 图匹配）
    DEGRADED = "degraded"  # 模式三：极端碎片化 -> 强制降级，内存级贪心发牌


@dataclass(frozen=True)
class RoutingSignals:
    """决策路由的输入信号。"""

    availability_ratio: float          # 余票率 0..1
    concurrency: int = 0               # 当前并发请求数
    p99_latency_ms: float = 0.0        # 近期 P99 决策延迟
    tickets_per_second: float = 0.0    # 售票吞吐


@dataclass
class Thresholds:
    """模式切换阈值。"""

    free_min_ratio: float = 0.35        # 余票率 >= 35% 视为充足
    degraded_max_ratio: float = 0.05    # 余票率 <= 5% 强制降级
    degraded_max_concurrency: int = 5_000
    degraded_max_latency_ms: float = 15.0
    smart_max_concurrency: int = 2_000


@dataclass
class DecisionRouter:
    """分层决策状态机。"""

    thresholds: Thresholds = field(default_factory=Thresholds)

    def route(self, signals: RoutingSignals) -> tuple[AllocationMode, str]:
        t = self.thresholds
        if signals.concurrency > t.degraded_max_concurrency:
            return (
                AllocationMode.DEGRADED,
                f"并发 {signals.concurrency} 超过阈值 {t.degraded_max_concurrency}，触发雪崩保护降级。",
            )
        if signals.availability_ratio <= t.degraded_max_ratio:
            return (
                AllocationMode.DEGRADED,
                f"余票率 {signals.availability_ratio:.1%} 已跌破 {t.degraded_max_ratio:.0%}，"
                "进入极端碎片化模式。",
            )
        if signals.p99_latency_ms > t.degraded_max_latency_ms:
            return (
                AllocationMode.DEGRADED,
                f"P99 决策延迟 {signals.p99_latency_ms:.1f}ms 超过 {t.degraded_max_latency_ms}ms，"
                "为保吞吐主动降级。",
            )
        if signals.availability_ratio >= t.free_min_ratio and signals.concurrency <= t.smart_max_concurrency:
            return (
                AllocationMode.FREE,
                f"余票率 {signals.availability_ratio:.1%} 充足，开放自由选座。",
            )
        return (
            AllocationMode.SMART,
            f"余票率 {signals.availability_ratio:.1%} 偏紧，启用智能协同分配。",
        )

    # ------------------------------------------------------------------
    def released_constraints(self, mode: AllocationMode) -> tuple[str, ...]:
        """当前模式下**被抛弃**的约束（可解释性 & 降级细则）。

        README 6.3：降级模式最先抛弃 Tier 3（静音车厢排斥），
        其次是连座等软约束；Tier 0 永不抛弃。
        """
        if mode is AllocationMode.DEGRADED:
            return ("tier3_quiet_repulsion", "tier4_aisle_separated", "tier5_rewards", "contiguous_block")
        if mode is AllocationMode.SMART:
            return ()
        return ()

    def effective_weights(self, mode: AllocationMode) -> dict[str, float]:
        """返回该模式下生效的 Tier 权重（降级时软约束权重归零）。"""
        if mode is AllocationMode.DEGRADED:
            return {"tier0": 1.0, "tier1": 1.0, "tier2": 0.5, "tier3": 0.0, "tier4": 0.0, "tier5": 0.0}
        return {"tier0": 1.0, "tier1": 1.0, "tier2": 1.0, "tier3": 1.0, "tier4": 1.0, "tier5": 1.0}


DEFAULT_ROUTER = DecisionRouter()
