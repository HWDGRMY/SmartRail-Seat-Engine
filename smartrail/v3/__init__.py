"""V3.0：仿真器、图神经网络与强化学习。

对外只暴露一个插件后端 :class:`RlPolicyBackend`（在 :mod:`.policy` 中），
它要求 ``torch``；缺少 torch 时注册表会把它标记为不可用，核心功能不受影响。
"""

from __future__ import annotations

__all__ = ["simulator", "features", "gnn", "rl", "policy"]
