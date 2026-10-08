"""V3.0：RL 环境与 PPO 训练（自研实现，不依赖 Stable-Baselines3）。

为什么自己写环境与 PPO
----------------------
README 的 V3.0 设想是 ``PyTorch + Stable-Baselines3``。但在受限/离线环境里
SB3 与 gymnasium 往往装不上，而 V3.0 真正要证明的是"**让 AI 在模拟订单中
自我博弈，学到动态信用与降级策略**"这件事本身。因此本模块：

* 环境是**自研的、零第三方依赖**的 :class:`SeatAllocationEnv`（不需要 gym 接口）；
* PPO 是**纯 PyTorch 手写实现**（约 150 行，含 GAE、裁剪代理目标、熵奖励、价值裁剪）；
* 训练入口 :func:`train` 输出学习曲线 JSON，供验收页画图。

如果以后要换成 SB3，只要把 :class:`SeatAllocationEnv` 包一层 gym 适配器即可，
网络与损失函数无需改动。

任务设定（把"选座"变成序列决策）
--------------------------------
一个 episode = 一个订单。环境逐步让策略为**一位乘客**选择座位（或选择候补）：

* **动作空间**：该乘客的候选座位之一 + 一个"候补"选项；
* **即时奖励**：本题的亲和度是**全局可加**的（个体项 + 成对项），因此可以把
  增量亲和度直接作为奖励 —— 这是本问题能干净地做 RL 的关键性质；
* **终止奖励**：Tier 0 违规惩罚、候补惩罚、整单亲和度奖励（与线上口径一致）；
* **状态**：见 :mod:`smartrail.v3.features` 的图观测。
"""

from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

import numpy as np

try:  # pragma: no cover
    import torch
    import torch.nn.functional as F
except ImportError as error:  # pragma: no cover
    raise ImportError("V3.0 的训练/推理需要 PyTorch：pip install torch") from error

from ..config import EngineConfig
from ..credit import CreditLedger
from ..models import Seat, Solution, TicketType
from ..scoring import is_care_dependent
from ..solver import OrderContext, build_context
from .features import GraphObservation, build_observation
from .gnn import BipartiteSeatEncoder, GraphTensors
from .simulator import OrderStream, TrafficProfile

# ---------------------------------------------------------------------------
# 环境
# ---------------------------------------------------------------------------

WAITLIST_ACTION = -1


@dataclass
class StepResult:
    observation: GraphObservation
    reward: float
    done: bool
    info: dict[str, Any] = field(default_factory=dict)


class SeatAllocationEnv:
    """单个订单的序列决策环境（零第三方依赖，除 numpy）。"""

    def __init__(
        self,
        seed: int = 0,
        config: EngineConfig | None = None,
        profile: TrafficProfile | None = None,
        fill: float = 0.0,
        max_seats: int = 48,
        occupancy_drift: bool = True,
    ) -> None:
        self.config = config or EngineConfig()
        self.rng = random.Random(seed)
        self.stream = OrderStream(seed=seed, profile=profile)
        self.credit = CreditLedger()
        self.max_seats = max_seats
        self.occupancy_drift = occupancy_drift
        self.fill = fill
        self.formation = _formation_for(self.config)
        self.all_seat_ids = [seat.seat_id for seat in self.formation.seats]
        self.reset()

    # -- 生命周期 ---------------------------------------------------------
    def reset(self, order=None) -> GraphObservation:
        self.occupied: set[str] = set()
        if self.fill > 0:
            seats = list(self.all_seat_ids)
            self.rng.shuffle(seats)
            self.occupied = set(seats[: int(len(seats) * self.fill)])
        self.order = order if order is not None else self.stream.next_order()
        self.ctx: OrderContext = build_context(self.order, self.config)
        self.placed: dict[str, str] = {}
        self.waitlisted: list[str] = []
        self.pending = [p.passenger_id for p in self.order.passengers]
        self.rewards: list[float] = []
        self.tier0 = 0
        self.steps = 0
        self.current_observation = self._observe()
        return self.current_observation

    @property
    def passenger_count(self) -> int:
        return len(self.order.passengers)

    def available_seats(self) -> list[Seat]:
        return [s for s in self.formation.seats if s.seat_id not in self.occupied]

    # -- 观测 -------------------------------------------------------------
    def _observe(self) -> GraphObservation:
        return build_observation(
            self.order,
            self.available_seats(),
            self.config,
            self.credit,
            occupied_seats=self.occupied,
            all_seats=self.formation.seats,
            step=self.steps,
            max_steps=max(1, self.passenger_count),
        )

    def action_mask(self, observation: GraphObservation) -> np.ndarray:
        """可行动作：候选座位（未被占、且该乘客**尚未落座**） + 候补。

        返回形状 ``(passengers, seats+1)`` 的 0/1 掩码，最后一列为"候补"。

        注意：已落座/已候补的乘客必须整行置零，否则策略会在同一位乘客上反复
        选择"候补"（每次都改变平局），导致 episode 提前结束 —— 这是本模块
        早期只推进了 1 步的原因。
        """
        pending = set(self.pending)
        rows = observation.passenger_mask.shape[0]
        seats = observation.seat_mask.shape[0]
        mask = np.zeros((rows, seats + 1), dtype=np.float32)
        for index, pid in enumerate(observation.passenger_ids):
            if pid not in pending:
                continue
            mask[index, :seats] = observation.seat_mask
            mask[index, -1] = 1.0
        return mask

    # -- 交互 -------------------------------------------------------------
    def step(self, passenger_index: int, seat_index: int) -> StepResult:
        """让 passenger_index 坐 seat_index（``-1`` 表示候补）。

        动作所用的乘客/座位索引都必须来自 **最近一次观测**（``current_observation``），
        因为每次落座都会重新生成候选池（已占座位被剔除），索引空间随之变化。
        """
        observation = self.current_observation
        pid = observation.passenger_ids[passenger_index]
        passenger = next(p for p in self.order.passengers if p.passenger_id == pid)
        reward = 0.0

        if seat_index == WAITLIST_ACTION:
            self.waitlisted.append(pid)
            reward += self.config.rl_waitlist_penalty
            if is_care_dependent(passenger, self.ctx.order):
                reward += self.config.rl_waitlist_penalty  # 需照护者候补代价更高
        else:
            seat_id = observation.candidate_seat_ids[seat_index]
            seat = self.formation.seat(seat_id)
            before = self._affinity_total()
            self.placed[pid] = seat_id
            self.occupied.add(seat_id)
            after = self._affinity_total()
            reward += self._affinity_scale() * (after - before)
            # 就座本身给固定正奖励，且**显著高于**亲和度波动：
            # 否则策略会退回"全部候补"的退化解（既不违规也不扣分）。
            reward += self.config.rl_seat_bonus
        # 无论落座还是候补，都必须把该乘客移出 pending。
        # 这里曾经只在"落座"分支移除，导致"候补"动作对状态毫无影响：
        # 外部（如行为克隆的老师）如果连续选择候补，episode 就**永不结束** ——
        # 实测表现为采集脚本静默卡死，且不抛任何异常，极难定位。
        if pid in self.pending:
            self.pending.remove(pid)
        self.steps += 1

        done = not self.pending
        if done:
            termination, info = self._terminal()
            reward += termination
            self.tier0 = int(info.get("tier0", 0))
        self.rewards.append(reward)
        self.current_observation = self._observe()
        return StepResult(self.current_observation, reward, done, {"passenger": pid})

    def _affinity_total(self) -> float:
        """当前已放置部分的权威亲和度（与线上同源，保证口径一致）。"""
        from ..scoring import Scorer
        from ..solver import evaluate_placement

        if not self.placed:
            return 0.0
        seats = {pid: self.formation.seat(sid) for pid, sid in self.placed.items()}
        value, _ = evaluate_placement(seats, self.ctx, Scorer(self.config))
        return value

    def _affinity_scale(self) -> float:
        """把亲和度差归一化到"每位乘客约 ±5 分"的量级。

        为什么必须归一化：亲和度是**可加**的，三人家庭与十人团体的量级相差数倍；
        固定系数会让大订单的梯度淹没小订单。按每位乘客的参考量级
        （``rl_affinity_reference``，默认 200）折算后，奖励尺度在不同订单之间保持一致。
        """
        reference = max(1.0, self.config.rl_affinity_reference)
        return self.config.rl_affinity_scale / reference

    def _terminal(self) -> tuple[float, dict[str, Any]]:
        from ..scoring import Scorer
        from ..solver import evaluate_placement

        seats = {pid: self.formation.seat(sid) for pid, sid in self.placed.items()}
        value, violations = evaluate_placement(seats, self.ctx, Scorer(self.config))
        tier0 = sum(1 for v in violations if v.tier == 0)
        isolation = sum(1 for v in violations if v.code == "T1_ISOLATED_CARE_MEMBER")
        penalty = self.config.rl_tier0_penalty * tier0 + self.config.rl_isolation_penalty * isolation
        return penalty, {"tier0": tier0, "isolation": isolation, "affinity": value}

    # -- 供训练循环使用 ---------------------------------------------------
    def reset_with(self, seed_offset: int = 0) -> GraphObservation:
        self.rng = random.Random((self.stream.counter + 1) * 7919 + seed_offset)
        return self.reset()

    def solution(self) -> Solution:
        """把环境的最终状态转成标准 :class:`Solution`（供仿真器记账）。"""
        from ..scoring import Scorer
        from ..solver import _to_assignments, evaluate_placement

        seats = {pid: self.formation.seat(sid) for pid, sid in self.placed.items()}
        value, violations = evaluate_placement(seats, self.ctx, Scorer(self.config))
        solution = Solution(
            assignments=_to_assignments(seats, self.ctx, violations),
            waitlisted=sorted(self.waitlisted),
            total_affinity=value,
            violations=violations,
            solver="v3-rl",
            elapsed_ms=0.0,
            mode="smart",
        )
        solution.breakdown = {}
        return solution


def _formation_for(config: EngineConfig):
    from ..carriage import crh_16_car_formation

    return crh_16_car_formation(
        aisle_weight=config.aisle_crossing_weight,
        near_door_rows=config.near_door_rows,
        near_toilet_rows=config.near_toilet_rows,
    )


# ---------------------------------------------------------------------------
# PPO
# ---------------------------------------------------------------------------


@dataclass
class PpoConfig:
    """PPO 超参数（保守默认值，小规模下稳定优先）。"""

    episodes_per_batch: int = 24
    updates: int = 60
    gamma: float = 0.97
    lam: float = 0.95
    clip: float = 0.2
    entropy_coef: float = 0.01
    value_coef: float = 0.5
    lr: float = 3e-4
    epochs: int = 4
    hidden: int = 96
    rounds: int = 2
    max_grad_norm: float = 1.0
    seed: int = 7
    fill: float = 0.0
    eval_every: int = 5
    eval_episodes: int = 12


@dataclass
class Transition:
    observation: GraphObservation
    passenger_index: int
    seat_index: int
    log_prob: float
    value: float
    reward: float
    done: bool
    action_mask: np.ndarray
    """该步的**完整**动作掩码，形状 ``(MAX_PASSENGERS, MAX_SEATS + 1)``。

    必须保存完整矩阵而不是单行：策略分布是在"乘客 × 动作"的展平空间上定义的，
    重算时需要整张掩码才能复现同一分布（早期只存单行，导致张量形状不匹配）。
    """


def _encode(model: BipartiteSeatEncoder, observation: GraphObservation, device: str):
    tensors = GraphTensors.from_observation(observation, device=device).batch()
    return tensors


def _policy_step(
    model: BipartiteSeatEncoder,
    observation: GraphObservation,
    tensors: GraphTensors,
    mask: np.ndarray,
    deterministic: bool,
) -> tuple[int, int, float, float]:
    """选一个 (乘客, 座位/候补) 动作，返回 (乘客索引, 座位索引, log_prob, value)。"""
    logits, value = model(tensors)
    logits = logits[0]  # (P, S)
    seat_count = logits.shape[1]
    pending_mask = mask[:, :seat_count]
    waitlist_mask = mask[:, -1:]

    # 在"座位 logits"后附加一列候补分数（固定 0），构成完整动作分布
    extra = torch.zeros((logits.shape[0], 1), dtype=logits.dtype, device=logits.device)
    full = torch.cat([logits, extra], dim=1)
    availability = torch.as_tensor(mask, dtype=logits.dtype, device=logits.device)
    full = full.masked_fill(availability < 0.5, -1e9)
    _ = (pending_mask, waitlist_mask)

    flat = full.reshape(-1)
    distribution = torch.distributions.Categorical(logits=flat)
    if deterministic:
        action = int(torch.argmax(flat).item())
    else:
        action = int(distribution.sample().item())
    passenger_index = action // full.shape[1]
    seat_index = action % full.shape[1]
    if seat_index >= seat_count:
        seat_index = WAITLIST_ACTION
    # 注意设备：张量可能在 CUDA 上，动作索引必须显式放到同一设备
    action_tensor = torch.as_tensor(action, device=flat.device)
    return (
        passenger_index,
        seat_index,
        float(distribution.log_prob(action_tensor).item()),
        float(value.reshape(-1)[0].item()),
    )


def collect_episode(
    env: SeatAllocationEnv,
    model: BipartiteSeatEncoder,
    device: str,
    deterministic: bool = False,
) -> list[Transition]:
    """跑一个 episode，返回轨迹。"""
    observation = env.reset_with(seed_offset=env.stream.counter)
    env.current_observation = observation
    transitions: list[Transition] = []
    while True:
        mask = env.action_mask(observation)
        if mask.sum() <= 0:
            break
        tensors = _encode(model, observation, device)
        passenger_index, seat_index, log_prob, value = _policy_step(
            model, observation, tensors, mask, deterministic
        )
        result = env.step(passenger_index, seat_index)
        transitions.append(
            Transition(
                observation=observation,
                passenger_index=passenger_index,
                seat_index=seat_index,
                log_prob=log_prob,
                value=value,
                reward=result.reward,
                done=result.done,
                action_mask=mask,
            )
        )
        observation = result.observation
        if result.done:
            break
    return transitions


def compute_gae(
    transitions: Sequence[Transition], gamma: float, lam: float, last_value: float = 0.0
) -> tuple[np.ndarray, np.ndarray]:
    """广义优势估计（GAE-λ）。"""
    rewards = np.asarray([t.reward for t in transitions], dtype=np.float32)
    values = np.asarray([t.value for t in transitions], dtype=np.float32)
    dones = np.asarray([t.done for t in transitions], dtype=np.float32)
    advantages = np.zeros_like(rewards)
    last_advantage = 0.0
    next_value = last_value
    for index in reversed(range(len(rewards))):
        mask = 1.0 - dones[index]
        delta = rewards[index] + gamma * next_value * mask - values[index]
        last_advantage = delta + gamma * lam * mask * last_advantage
        advantages[index] = last_advantage
        next_value = values[index]
    returns = advantages + values
    if len(advantages) > 1:
        advantages = (advantages - advantages.mean()) / (advantages.std() + 1e-8)
    return advantages, returns


def evaluate_policy(
    model: BipartiteSeatEncoder,
    config: EngineConfig,
    device: str,
    episodes: int = 12,
    seed: int = 1234,
    fill: float = 0.0,
) -> dict[str, float]:
    """用确定性策略跑若干 episode，给出可比指标（不训练）。"""
    env = SeatAllocationEnv(seed=seed, config=config, fill=fill)
    model.eval()
    affinity_total = 0.0
    tier0_total = 0
    isolation_total = 0
    seated = 0
    waitlisted = 0
    with torch.no_grad():
        for _ in range(episodes):
            observation = env.reset()
            env.current_observation = observation
            while True:
                mask = env.action_mask(observation)
                if mask.sum() <= 0:
                    break
                tensors = _encode(model, observation, device)
                passenger_index, seat_index, _lp, _v = _policy_step(
                    model, observation, tensors, mask, deterministic=True
                )
                result = env.step(passenger_index, seat_index)
                observation = result.observation
                if result.done:
                    break
            info = env._terminal()[1]
            affinity_total += float(info.get("affinity", 0.0))
            tier0_total += int(info.get("tier0", 0))
            isolation_total += int(info.get("isolation", 0))
            seated += len(env.placed)
            waitlisted += len(env.waitlisted)
    model.train()
    return {
        "episodes": episodes,
        "avg_affinity": affinity_total / max(1, episodes),
        "tier0_violations": tier0_total,
        "isolated_care": isolation_total,
        "seated": seated,
        "waitlisted": waitlisted,
        "seat_rate": seated / max(1, seated + waitlisted),
    }


def train(
    config: EngineConfig | None = None,
    ppo: PpoConfig | None = None,
    out_dir: str | Path | None = None,
    device: str | None = None,
    verbose: bool = True,
) -> dict[str, Any]:
    """PPO 训练主循环。返回学习曲线与最终模型路径。"""
    config = config or EngineConfig()
    ppo = ppo or PpoConfig()
    device = device or ("cuda" if torch.cuda.is_available() else "cpu")
    torch.manual_seed(ppo.seed)
    np.random.seed(ppo.seed)

    model = BipartiteSeatEncoder(hidden=ppo.hidden, rounds=ppo.rounds).to(device)
    optimizer = torch.optim.Adam(model.parameters(), lr=ppo.lr)
    env = SeatAllocationEnv(seed=ppo.seed, config=config, fill=ppo.fill)

    curve: list[dict[str, Any]] = []
    started = time.perf_counter()
    for update in range(1, ppo.updates + 1):
        batch: list[Transition] = []
        episode_returns: list[float] = []
        episode_affinity: list[float] = []
        for _ in range(ppo.episodes_per_batch):
            transitions = collect_episode(env, model, device)
            if transitions:
                batch.extend(transitions)
                episode_returns.append(float(sum(t.reward for t in transitions)))
                episode_affinity.append(float(env._terminal()[1].get("affinity", 0.0)))
        if not batch:
            continue

        advantages, returns = compute_gae(batch, ppo.gamma, ppo.lam)
        advantages_t = torch.as_tensor(advantages, dtype=torch.float32, device=device)
        returns_t = torch.as_tensor(returns, dtype=torch.float32, device=device)
        old_log_probs = torch.as_tensor(
            [t.log_prob for t in batch], dtype=torch.float32, device=device
        )
        # 预先把观测转成张量，避免每个 epoch 重复转换
        encoded = [
            _encode(model, t.observation, device) for t in batch
        ]

        losses: list[float] = []
        # 批量化：把整批转移一次性喂给网络，而不是逐条前向（快一个数量级）。
        # 注意 encoded 里每个元素已经是 batch=1 的张量，因此这里用 cat 而不是 stack，
        # 否则会多出一维导致 reshape 失败。
        batch_tensors = GraphTensors(
            passenger_features=torch.cat([t.passenger_features for t in encoded], dim=0),
            seat_features=torch.cat([t.seat_features for t in encoded], dim=0),
            edge_features=torch.cat([t.edge_features for t in encoded], dim=0),
            seat_mask=torch.cat([t.seat_mask for t in encoded], dim=0),
            passenger_mask=torch.cat([t.passenger_mask for t in encoded], dim=0),
            global_features=torch.cat([t.global_features for t in encoded], dim=0),
        )
        masks = torch.as_tensor(
            np.stack([t.action_mask for t in batch]), dtype=torch.float32, device=device
        )
        actions = torch.as_tensor(
            [
                t.passenger_index * masks.shape[2]
                + (t.seat_index if t.seat_index >= 0 else masks.shape[2] - 1)
                for t in batch
            ],
            dtype=torch.long,
            device=device,
        )
        for _epoch in range(ppo.epochs):
            logits, values = model(batch_tensors)
            batch_size, n_pass, n_seat = logits.shape
            extra = torch.zeros((batch_size, n_pass, 1), dtype=logits.dtype, device=device)
            full = torch.cat([logits, extra], dim=2)
            full = full.masked_fill(masks < 0.5, -1e9)
            flat = full.reshape(batch_size, -1)
            distribution = torch.distributions.Categorical(logits=flat)
            new_log_probs = distribution.log_prob(actions)
            entropy = distribution.entropy()
            ratio = torch.exp(new_log_probs - old_log_probs)
            surrogate = torch.min(
                ratio * advantages_t,
                torch.clamp(ratio, 1 - ppo.clip, 1 + ppo.clip) * advantages_t,
            )
            value_loss = F.mse_loss(values.reshape(-1), returns_t)
            loss = (
                -surrogate.mean()
                + ppo.value_coef * value_loss
                - ppo.entropy_coef * entropy.mean()
            )
            optimizer.zero_grad()
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), ppo.max_grad_norm)
            optimizer.step()
            losses.append(float(loss.item()))

        record = {
            "update": update,
            "avg_return": float(np.mean(episode_returns)),
            "avg_affinity": float(np.mean(episode_affinity)),
            "loss": float(np.mean(losses)),
            "elapsed_s": round(time.perf_counter() - started, 2),
        }
        if update % ppo.eval_every == 0 or update == ppo.updates:
            record["eval"] = evaluate_policy(
                model, config, device, episodes=ppo.eval_episodes, seed=ppo.seed + 1000, fill=ppo.fill
            )
        curve.append(record)
        if verbose:
            eval_part = record.get("eval", {})
            print(
                f"[PPO {update:>3}/{ppo.updates}] return={record['avg_return']:>8.2f} "
                f"affinity={record['avg_affinity']:>9.1f} loss={record['loss']:>7.3f} "
                f"tier0={eval_part.get('tier0_violations', '-')}"
            )

    result: dict[str, Any] = {
        "device": device,
        "updates": ppo.updates,
        "curve": curve,
        "elapsed_s": round(time.perf_counter() - started, 2),
        "final_eval": evaluate_policy(
            model, config, device, episodes=max(20, ppo.eval_episodes), seed=ppo.seed + 777, fill=ppo.fill
        ),
    }
    if out_dir is not None:
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        torch.save(model.state_dict(), out / "v3_ppo.pt")
        (out / "v3_training_curve.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        result["model_path"] = str(out / "v3_ppo.pt")
        result["curve_path"] = str(out / "v3_training_curve.json")
    return result


__all__ = [
    "PpoConfig",
    "SeatAllocationEnv",
    "StepResult",
    "Transition",
    "WAITLIST_ACTION",
    "collect_episode",
    "compute_gae",
    "evaluate_policy",
    "train",
]
