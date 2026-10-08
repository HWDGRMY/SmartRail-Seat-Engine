"""求解器后端的注册表与协议（不依赖任何第三方库）。"""

from __future__ import annotations

import importlib.util
from typing import Protocol, runtime_checkable

from ..clustering import BookingState
from ..config import EngineConfig
from ..models import Order, Solution


class BackendUnavailable(RuntimeError):
    """后端所需的第三方依赖未安装（例如未安装 OR-Tools 时的 v2_cpsat）。"""


@runtime_checkable
class SolverBackend(Protocol):
    """求解器后端协议。"""

    name: str
    description: str
    requires: tuple[str, ...]

    def __call__(
        self,
        order: Order,
        state: BookingState,
        config: EngineConfig,
        mode: str = "smart",
        time_budget_ms: float | None = None,
        **kwargs: object,
    ) -> Solution: ...


_REGISTRY: dict[str, SolverBackend] = {}


def register(backend: SolverBackend) -> SolverBackend:
    """注册一个后端（同名覆盖，便于测试打桩）。"""
    _REGISTRY[backend.name] = backend
    return backend


def get_backend(name: str) -> SolverBackend:
    if name not in _REGISTRY:
        known = ", ".join(sorted(_REGISTRY)) or "（空）"
        raise KeyError(f"未知求解器后端 {name!r}，已注册：{known}")
    return _REGISTRY[name]


def missing_modules(modules: tuple[str, ...]) -> list[str]:
    """返回其中**未安装**的模块名。"""
    return [m for m in modules if importlib.util.find_spec(m) is None]


def available_backends() -> dict[str, dict[str, object]]:
    """列出全部后端及其依赖可用性（供前端与 /api/config 展示）。"""
    out: dict[str, dict[str, object]] = {}
    for name, backend in sorted(_REGISTRY.items()):
        missing = missing_modules(backend.requires)
        out[name] = {
            "description": backend.description,
            "requires": list(backend.requires),
            "available": not missing,
            "missing": missing,
        }
    return out


def solve_with(
    name: str,
    order: Order,
    state: BookingState,
    config: EngineConfig,
    mode: str = "smart",
    time_budget_ms: float | None = None,
    **kwargs: object,
) -> Solution:
    """按名字调用后端；依赖缺失时抛 :class:`BackendUnavailable`。"""
    backend = get_backend(name)
    missing = missing_modules(backend.requires)
    if missing:
        raise BackendUnavailable(
            f"求解器 {name!r} 需要 {', '.join(missing)}（未安装）。"
            f"安装示例：pip install {' '.join(missing)}"
        )
    return backend(order, state, config, mode=mode, time_budget_ms=time_budget_ms, **kwargs)


def _register_builtin() -> None:
    """注册内置后端（延迟导入，避免循环依赖）。"""
    from .cpsat import CpSatBackend
    from .exact import ExactPureBackend
    from .heuristic import HeuristicBackend

    for backend in (HeuristicBackend(), CpSatBackend(), ExactPureBackend()):
        register(backend)
    # V3.0 的后端在 smartrail.v3.policy 里；导入失败（例如无 torch）不应影响
    # 其余后端的可用性，因此这里单独兜底。
    try:
        from ..v3.policy import RlPolicyBackend

        register(RlPolicyBackend())
    except Exception:  # pragma: no cover - 取决于运行环境
        pass


_register_builtin()

__all__ = [
    "BackendUnavailable",
    "SolverBackend",
    "available_backends",
    "get_backend",
    "missing_modules",
    "register",
    "solve_with",
]
