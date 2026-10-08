"""V3.0：把训练好的策略接入统一求解器接口。

两个用途
--------
1. **在线决策**：``engine.book(order, solver="v3_rl")`` 走后端 → 用策略网络为订单选座；
2. **离线评估**：仿真器把 :class:`RlPolicyBundle` 当作 :class:`~smartrail.v3.simulator.Policy`
   使用，与 V1/V2 在同一订单流上并排对比。

模型来源
--------
* 若指定 ``checkpoint``（``.pt`` 文件）存在，则加载权重；
* 否则使用**随机初始化**的权重 —— 这仍然是一个可运行的策略（用于冒烟测试与
  "未训练基线"对比），并在 notes 中明确标注，避免把未训练模型当成有效结果。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..clustering import BookingState
from ..config import EngineConfig
from ..credit import CreditLedger
from ..models import Order, Solution
from ..scoring import Scorer
from ..solver import _to_assignments, build_context, evaluate_placement

# 工程根目录（本文件位于 <root>/smartrail/v3/policy.py）
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CHECKPOINT = PROJECT_ROOT / "artifacts" / "v3" / "v3_ppo.pt"
"""默认权重路径**基于工程根目录解析**，而不是当前工作目录。

这一点很重要：早期实现用相对路径，导致"从别的目录启动服务时找不到权重"，
策略静默退化为随机初始化 —— 表现为座位图里出现明显不合理的分配。
"""


@dataclass
class _LightState:
    """最小占座状态（策略只关心"哪些座位已被占"）。"""

    formation: Any
    occupied: set[str]


def resolve_checkpoint(checkpoint: str | Path | None) -> Path | None:
    """把检查点路径解析为绝对路径（相对路径按工程根目录解析）。"""
    if checkpoint is None:
        return None
    path = Path(checkpoint)
    return path if path.is_absolute() else (PROJECT_ROOT / path)


def load_policy_state(model, path: Path, device: str = "cpu") -> dict:
    """加载策略权重，**同时兼容两种存档格式**。

    历史原因：PPO 训练脚本存的是裸 ``state_dict``，行为克隆存的是
    ``{"state_dict": ..., "source": ...}`` 包装。不兼容会导致
    ``KeyError: 'state_dict'``（在 PPO 权重上）或反过来加载失败。
    这里统一处理，避免"换个训练脚本就读不了权重"。
    """
    import torch

    payload = torch.load(path, map_location=device)
    state = payload.get("state_dict") if isinstance(payload, dict) and "state_dict" in payload else payload
    model.load_state_dict(state)
    return payload if isinstance(payload, dict) else {}


def _load_model(checkpoint: str | Path | None, device: str = "cpu"):
    from .gnn import BipartiteSeatEncoder

    model = BipartiteSeatEncoder()
    loaded = False
    path = resolve_checkpoint(checkpoint)
    if path and path.exists():
        load_policy_state(model, path, device)
        loaded = True
    model.eval()
    return model, loaded


def solve_order_with_model(
    order: Order,
    formation,
    occupied: set[str],
    config: EngineConfig,
    credit: CreditLedger,
    checkpoint: str | Path | None = DEFAULT_CHECKPOINT,
    max_seats: int = 48,
    enforce_care_bonds: bool = True,
) -> tuple[dict[str, str], list[str], bool]:
    """用策略网络为一个订单选座，**并施加 Tier 0 硬安全过滤器**。

    返回 ``(乘客ID -> 座位ID, 候补列表, 是否加载了训练权重)``。

    为什么需要过滤器
    ----------------
    实测发现：纯学习型策略在"带娃家庭"上会把儿童候补掉（它学到的偏好是尽量
    少担风险）。这与本项目的**底线设计**冲突 —— Tier 0 属于绝对约束，
    不应依赖模型是否学到。因此这里采用生产上常见的分层做法：

    * **学习型策略负责偏好**（在候选座位之间做亲和度排序）；
    * **硬过滤器负责底线**（需照护者必须与支持人同车厢、不得被单独候补）。

    过滤器是确定性的、可审计的，且不影响策略本身的学习结果。
    """
    import torch

    from .features import build_observation
    from .gnn import GraphTensors
    from .rl import WAITLIST_ACTION, _policy_step

    model, loaded = _load_model(checkpoint)
    available = [s for s in formation.seats if s.seat_id not in occupied]
    placed: dict[str, str] = {}
    waitlisted: list[str] = []
    remaining_occupied = set(occupied)

    passengers = list(order.passengers)
    care_dependents = [
        p for p in passengers if p.needs_caregiver or p.is_child
    ]
    # 硬过滤器：先把需照护乘客与他们已决定的同伴排进"优先队列"
    priority_ids = {p.passenger_id for p in care_dependents}
    order_ids = [p.passenger_id for p in passengers]
    if enforce_care_bonds and care_dependents:
        # 让需照护乘客优先决策，且其支持人紧随其后 —— 这样"孩子先占位、
        # 家长跟上"成为默认路径，而不是等家长坐好后再发现孩子没位置。
        helpers_first: list[str] = []
        for dependent in care_dependents:
            helpers_first.append(dependent.passenger_id)
            for helper in sorted(helpers_of(order, dependent.passenger_id)):
                if helper not in helpers_first:
                    helpers_first.append(helper)
        order_ids = helpers_first + [pid for pid in order_ids if pid not in helpers_first]

    with torch.no_grad():
        for _step in range(len(order_ids) + 2):
            pending = [pid for pid in order_ids if pid not in placed and pid not in waitlisted]
            if not pending:
                break
            observation = build_observation(
                order,
                [s for s in available if s.seat_id not in remaining_occupied],
                config,
                credit,
                occupied_seats=remaining_occupied,
                all_seats=formation.seats,
                max_seats=max_seats,
            )
            mask = _pending_mask_ids(observation, set(pending))
            if enforce_care_bonds:
                _restrict_to_companion_carriage(
                    order, observation, mask, pending, placed, formation
                )
            if mask.sum() <= 0:
                break
            tensors = GraphTensors.from_observation(observation).batch()
            passenger_index, seat_index, _lp, _value = _policy_step(
                model, observation, tensors, mask, deterministic=True
            )
            pid = observation.passenger_ids[passenger_index]
            if pid not in pending:  # 掩码理论上已排除，双保险
                waitlisted.append(pid)
                continue
            if seat_index == WAITLIST_ACTION:
                waitlisted.append(pid)
                continue
            seat_id = observation.candidate_seat_ids[seat_index]
            placed[pid] = seat_id
            remaining_occupied.add(seat_id)

    if enforce_care_bonds:
        _repair_care_bonds(order, formation, placed, waitlisted, remaining_occupied)
    _ = (priority_ids, care_dependents)
    return placed, waitlisted, loaded


def helpers_of(order: Order, pid: str) -> set[str]:
    """该乘客的支持人（MANDATORY 绑定 + 本单中的照护人）。"""
    people = set(order.caregivers_of(pid))
    people |= {other for other in order.caregivers() if other != pid}
    return people


def _pending_mask_ids(observation, pending_ids: set[str]):
    import numpy as np

    seat_count = len(observation.candidate_seat_ids)
    mask = np.zeros(
        (observation.passenger_mask.shape[0], observation.seat_mask.shape[0] + 1),
        dtype=np.float32,
    )
    for index, pid in enumerate(observation.passenger_ids):
        if pid not in pending_ids:
            continue
        mask[index, :seat_count] = observation.seat_mask[:seat_count]
        mask[index, -1] = 1.0
    return mask


def _restrict_to_companion_carriage(
    order: Order,
    observation,
    mask,
    pending: list[str],
    placed: dict[str, str],
    formation,
):
    """硬过滤器（决策前）：把需照护乘客的候选限制在支持人所在车厢。

    若某位需照护乘客尚未落座，而它的支持人已经坐好，那么该乘客**只能**选
    支持人所在车厢的座位 —— 把 Tier 0 从"事后惩罚"变成"事前不可能"。
    """
    seat_by_id = formation.by_id()
    for index, pid in enumerate(observation.passenger_ids):
        if pid not in pending:
            continue
        carriage: int | None = None
        for helper in helpers_of(order, pid):
            seat_id = placed.get(helper)
            if seat_id:
                carriage = seat_by_id[seat_id].carriage
                break
        if carriage is None:
            continue
        for seat_index, seat_id in enumerate(observation.candidate_seat_ids):
            if seat_by_id[seat_id].carriage != carriage:
                mask[index, seat_index] = 0.0
        # 若该车厢已无候选，保留候补动作（由 _repair_care_bonds 兜底）


def _repair_care_bonds(
    order: Order,
    formation,
    placed: dict[str, str],
    waitlisted: list[str],
    occupied: set[str],
) -> None:
    """硬过滤器（决策后）：修复"需照护者与支持人跨车厢"的违规。

    做法是把违规的需照护乘客**换到支持人所在车厢**的一个空位上（优先紧邻）；
    若该车厢已满，则把两人一起转入候补 —— 宁可都候补，也不接受 Tier 0。
    """
    seat_by_id = formation.by_id()
    for passenger in order.passengers:
        pid = passenger.passenger_id
        if not (passenger.needs_caregiver or passenger.is_child):
            continue
        seat_id = placed.get(pid)
        helpers = [h for h in helpers_of(order, pid) if h in placed]
        if not helpers:
            # 没有已就座的支持人：如果自己被单独安置，说明被孤立了 → 转入候补
            if seat_id:
                occupied.discard(seat_id)
                placed.pop(pid, None)
                waitlisted.append(pid)
            continue
        if seat_id:
            carriage = seat_by_id[seat_id].carriage
            if any(seat_by_id[placed[h]].carriage == carriage for h in helpers):
                continue
        # 需要修复：在支持人车厢找一个空位
        helper_carriages = {seat_by_id[placed[h]].carriage for h in helpers}
        candidates = [
            seat
            for seat in formation.seats
            if seat.carriage in helper_carriages and seat.seat_id not in occupied
        ]
        if seat_id:
            occupied.discard(seat_id)
            placed.pop(pid, None)
        if candidates:
            # 优先紧邻支持人的座位
            helper_seats = [seat_by_id[placed[h]] for h in helpers]
            candidates.sort(key=lambda seat: min(seat.manhattan_to(h) for h in helper_seats))
            chosen = candidates[0]
            placed[pid] = chosen.seat_id
            occupied.add(chosen.seat_id)
            if pid in waitlisted:
                waitlisted.remove(pid)
        else:
            waitlisted.append(pid)
            # 支持人也一起候补，避免"家长上车、孩子没上"
            for helper in helpers:
                helper_seat = placed.pop(helper, None)
                if helper_seat:
                    occupied.discard(helper_seat)
                if helper not in waitlisted:
                    waitlisted.append(helper)


class RlPolicyBackend:
    """V3.0：学习型策略后端（需要 torch）。"""

    name = "v3_rl"
    description = "V3.0 学习型策略：二分图编码器（GNN）+ PPO，按亲和度与 Tier 约束联合训练"
    requires: tuple[str, ...] = ("torch",)

    def __init__(self, checkpoint: str | Path | None = DEFAULT_CHECKPOINT) -> None:
        self.checkpoint = checkpoint

    def __call__(
        self,
        order: Order,
        state: BookingState,
        config: EngineConfig,
        mode: str = "smart",
        time_budget_ms: float | None = None,
        **kwargs: object,
    ) -> Solution:
        _ = time_budget_ms
        started = time.perf_counter()
        checkpoint = kwargs.get("checkpoint", self.checkpoint)
        credit = kwargs.get("credit")
        ledger = credit if isinstance(credit, CreditLedger) else CreditLedger()
        placed, waitlisted, loaded = solve_order_with_model(
            order,
            state.formation,
            set(state.occupied),
            config,
            ledger,
            checkpoint=checkpoint,  # type: ignore[arg-type]
        )
        ctx = build_context(order, config)
        seats = {pid: state.formation.seat(sid) for pid, sid in placed.items()}
        value, violations = evaluate_placement(seats, ctx, Scorer(config))
        # 真实推理耗时：验收页要把它和 V1/V2 的决策时延放在同一栏对比，
        # 因此这里必须实际计时（早期漏了这一步，页面显示 0ms）。
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        solution = Solution(
            assignments=_to_assignments(seats, ctx, violations),
            waitlisted=sorted(set(waitlisted)),
            total_affinity=value,
            violations=violations,
            solver="v3-rl(gnn+ppo)",
            elapsed_ms=elapsed_ms,
            mode=mode,
            notes=[
                (
                    f"已加载训练权重：{checkpoint}"
                    if loaded
                    else "未找到训练权重，使用随机初始化策略（仅用于冒烟测试/未训练基线对比）。"
                ),
                "Tier 0 由**硬安全过滤器**保证（学习型策略只负责偏好排序）。",
            ],
        )
        solution.breakdown = _breakdown(violations)
        return solution


def _breakdown(violations) -> dict[str, float]:
    out: dict[str, float] = {}
    for v in violations:
        key = f"tier{v.tier}:{v.code}"
        out[key] = out.get(key, 0.0) + v.affinity
    return dict(sorted(out.items(), key=lambda kv: kv[1]))


class RlPolicyAdapter:
    """把学习型策略接到仿真器的 :class:`Policy` 接口上。

    与 :class:`RlPolicyBackend` 的区别：仿真器会传入**共享的信用账本**，
    于是"动态信用体系"的闭环也在对比中生效（被投诉过的乘客下次会被屏蔽静音车厢）。
    """

    def __init__(
        self,
        checkpoint: str | Path | None = DEFAULT_CHECKPOINT,
        time_budget_ms: float | None = None,
    ) -> None:
        self.checkpoint = checkpoint
        self.time_budget_ms = time_budget_ms
        self.name = "v3_rl(gnn+ppo)"

    def decide(self, context) -> Solution:
        from .policy import RlPolicyBackend  # 自身模块，保持可读性

        backend = RlPolicyBackend(self.checkpoint)
        return backend(
            context.order,
            context.state,
            context.config,
            mode="smart",
            time_budget_ms=self.time_budget_ms,
            credit=context.credit,
        )


__all__ = [
    "DEFAULT_CHECKPOINT",
    "RlPolicyAdapter",
    "RlPolicyBackend",
    "solve_order_with_model",
]
