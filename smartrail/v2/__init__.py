"""求解器插件层（V1.0 启发式 / V2.0 运筹学 / V3.0 学习型策略）。

设计动机
--------
V1.0 的求解器写在 :mod:`smartrail.solver` 里，直接实现了启发式 + 分支限界。
后续版本要引入 OR-Tools CP-SAT（V2.0）与神经网络策略（V3.0），但**不能**让核心
引擎因此离不开这些重依赖：内网、离线、或只想跑轻量启发式的场景必须照常工作。

因此这里引入一层薄薄的"求解器协议 + 注册表"：

* :class:`SolverBackend` —— 统一入口签名 ``solve(order, state, config, mode, time_budget_ms)``；
* :func:`register` / :func:`get_backend` / :func:`available_backends` —— 注册与发现；
* :func:`solve_with` —— 按名字调用；依赖缺失时明确抛 :class:`BackendUnavailable`
  （而不是静默降级，让调用方自己决定要不要回退）。

引擎侧通过 ``engine.book(..., solver="v2_cpsat")`` 选择后端；可视化验收页会并排
调用多个后端做对比。

内置后端
--------
======================  ==================  ============================================
名称                    依赖                说明
======================  ==================  ============================================
``v1_heuristic``        无                  V1.0 贪心 + 分支限界（降级/默认）
``v2_cpsat``            ortools             CP-SAT（MILP）精确建模，Tier 0/1 为硬约束
``v3_rl``               torch               GNN + PPO 学习型策略（可选，缺失则回退 V2）
======================  ==================  ============================================
"""

from __future__ import annotations

from .registry import (
    BackendUnavailable,
    SolverBackend,
    available_backends,
    get_backend,
    register,
    solve_with,
)

__all__ = [
    "BackendUnavailable",
    "SolverBackend",
    "available_backends",
    "get_backend",
    "register",
    "solve_with",
]
