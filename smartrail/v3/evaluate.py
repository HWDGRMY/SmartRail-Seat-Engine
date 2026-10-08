"""统一评估：V1 / V2 / V3 在同一订单流上的并排对比。

这是验收页面的数据源，也是"三个版本谁更好"的唯一权威口径：

* **同一订单流**：三个策略吃完全相同的随机种子与客流画像（可复现）；
* **同一指标定义**：亲和度、Tier 0 违规、孤立计数、静音车厢误用、就座率、时延分位；
* **依赖缺失显式可见**：v2_cpsat 需要 ortools、v3_rl 需要 torch，
  缺失时返回 ``skipped`` + 原因，而不是静默换一个引擎冒充。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Sequence

from ..v2 import available_backends
from ..v2.registry import missing_modules
from .simulator import Policy, RulePolicy, TrafficProfile, run_policy

DEFAULT_POLICIES = ("v1_heuristic", "v2_cpsat", "v2_exact", "v3_rl")


@dataclass
class ComparisonRequest:
    """对比请求。"""

    policies: Sequence[str] = DEFAULT_POLICIES
    steps: int = 60
    seed: int = 7
    fill: float = 0.0
    complaint_rate: float = 0.0
    profile: TrafficProfile | None = None
    time_budget_ms: float | None = None


@dataclass
class ComparisonResult:
    """对比结果（可直接序列化给前端）。"""

    request: ComparisonRequest
    rows: list[dict[str, Any]] = field(default_factory=list)
    elapsed_s: float = 0.0
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "request": {
                "policies": list(self.request.policies),
                "steps": self.request.steps,
                "seed": self.request.seed,
                "fill": self.request.fill,
                "complaint_rate": self.request.complaint_rate,
            },
            "rows": self.rows,
            "elapsed_s": round(self.elapsed_s, 3),
            "notes": self.notes,
            "backends": available_backends(),
        }


def _policy_for(name: str, time_budget_ms: float | None) -> Policy:
    """构造策略实例（v3_rl 走学习型策略适配器）。"""
    if name == "v3_rl":
        from .policy import RlPolicyAdapter

        return RlPolicyAdapter(time_budget_ms=time_budget_ms)
    return RulePolicy(name)


def run_comparison(request: ComparisonRequest | None = None) -> ComparisonResult:
    """跑一次三版本并排对比。"""
    request = request or ComparisonRequest()
    backends = available_backends()
    started = time.perf_counter()
    result = ComparisonResult(request=request)

    for name in request.policies:
        info = backends.get(name)
        if info is None:
            result.rows.append(
                {"policy": name, "skipped": True, "reason": "未注册的求解器后端"}
            )
            continue
        missing = missing_modules(tuple(info["requires"]))  # type: ignore[arg-type]
        if missing:
            result.rows.append(
                {
                    "policy": name,
                    "skipped": True,
                    "reason": f"缺少依赖：{', '.join(missing)}（pip install {' '.join(missing)}）",
                }
            )
            continue
        try:
            summary = run_policy(
                _policy_for(name, request.time_budget_ms),
                steps=request.steps,
                seed=request.seed,
                fill=request.fill,
                profile=request.profile,
                complaint_rate=request.complaint_rate,
            )
            summary["skipped"] = False
            result.rows.append(summary)
        except Exception as error:  # pragma: no cover - 单策略失败不应中断对比
            result.rows.append(
                {"policy": name, "skipped": True, "reason": f"运行失败：{type(error).__name__}: {error}"}
            )

    result.elapsed_s = time.perf_counter() - started
    # 相对 V1 的增益（验收页最关心的一栏）
    baseline = next(
        (row for row in result.rows if row.get("policy") == "v1_heuristic" and not row.get("skipped")),
        None,
    )
    if baseline:
        for row in result.rows:
            if row.get("skipped"):
                continue
            base = float(baseline["total_affinity"]) or 1.0
            row["affinity_vs_v1"] = round(
                (float(row["total_affinity"]) - float(baseline["total_affinity"]))
                / abs(base),
                4,
            )
            row["p99_vs_v1"] = round(
                float(row["latency_ms"]["p99"]) - float(baseline["latency_ms"]["p99"]), 3
            )
    result.notes.append(
        "所有策略使用同一订单流（同 seed / 同客流画像 / 同初始占座比例），指标定义完全一致。"
    )
    return result


def leaderboard(result: ComparisonResult) -> list[dict[str, Any]]:
    """按"亲和度优先、Tier 0 其次"排序的简易榜单（供前端高亮）。"""
    rows = [row for row in result.rows if not row.get("skipped")]
    return sorted(
        rows,
        key=lambda row: (
            int(row.get("tier0_violations", 0)),
            -float(row.get("total_affinity", 0.0)),
        ),
    )


__all__ = [
    "ComparisonRequest",
    "ComparisonResult",
    "DEFAULT_POLICIES",
    "leaderboard",
    "run_comparison",
]
