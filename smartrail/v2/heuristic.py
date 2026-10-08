"""V1.0 启发式求解器作为插件后端（无第三方依赖）。

把这个后端放进插件层有两个作用：

1. 让引擎与可视化页面能用**同一个入口**调用所有版本，便于并排对比；
2. 为 V2/V3 提供"质量基线"与"依赖缺失时的安全回退"。
"""

from __future__ import annotations

from ..clustering import BookingState
from ..config import EngineConfig
from ..models import Order, Solution
from ..solver import assign_exact, assign_greedy, build_context


class HeuristicBackend:
    """V1.0：单元拆解 + 余票分块 + 分支限界。"""

    name = "v1_heuristic"
    description = "V1.0 启发式：单元拆解 + 余票分块 + 分支限界（无第三方依赖）"
    requires: tuple[str, ...] = ()

    def __call__(
        self,
        order: Order,
        state: BookingState,
        config: EngineConfig,
        mode: str = "smart",
        time_budget_ms: float | None = None,
        **kwargs: object,
    ) -> Solution:
        _ = kwargs
        ctx = build_context(order, config)
        if mode == "degraded":
            return assign_greedy(ctx, state, config, mode=mode)
        return assign_exact(ctx, state, config, mode=mode, time_budget_ms=time_budget_ms)


__all__ = ["HeuristicBackend"]
