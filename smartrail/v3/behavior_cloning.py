"""V3.0 行为克隆：用 V1 的解当老师，先教会策略"该怎么选"，再交给 PPO 微调。

为什么需要这一步（前几轮训练失败的根本原因）
------------------------------------------
纯 PPO 在这个任务上信用分配极难：一位乘客的座位选择要到**整单结束**才体现好坏，
终局奖励要反传穿过整个 episode；而动作空间是"乘客 × 座位"的展平空间，
早期随机策略几乎不可能偶然选中"把孩子放在家长旁边"这种正确组合。

V1 已经有 100% 就座率的高质量解 —— 这等于**免费的老师**。
行为克隆把"从零探索"变成"模仿已知的好答案"，是本项目里性价比最高的一步。

实现要点
--------
V1（``assign_exact``）一次性给出整单分配，因此**不需要让环境跑一遍再采样**：

1. 对一个订单跑一次 V1，得到 ``{乘客: 座位}``；
2. 按"先安排需照护者"的顺序回放这个分配，每步构建一次观测（已落座座位被剔除，
   与 RL 环境的观测口径完全一致），把该乘客的**专家座位**记成标签；
3. 用掩码交叉熵训练策略去拟合这些标签。

这样数据生成只花一次求解的钱，几千个订单几十秒就能采完。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from ..config import EngineConfig
from ..models import BondType, Order
from ..solver import assign_exact, build_context
from .features import build_observation
from .gnn import BipartiteSeatEncoder, GraphTensors
from .rl import WAITLIST_ACTION, SeatAllocationEnv
from .simulator import OrderStream, TrafficProfile


@dataclass
class ExpertSample:
    """一条专家样本：一次观测 + 老师在该观测下选择的动作。"""

    passenger_ids: list[str]
    candidate_seat_ids: list[str]
    passenger_index: int
    seat_index: int
    """专家选的座位在候选列表中的下标；``-1`` 表示候补。"""


def solve_with_teacher(
    order: Order,
    formation,
    occupied: set[str],
    config: EngineConfig,
    seat_pool: Sequence[str] | None = None,
) -> tuple[dict[str, str], list[str]]:
    """用 V1 分支限界求解一个订单（这就是"老师"）。

    ``seat_pool``：**限制老师的可选座位**到网络的候选池。

    为什么必须限制（这是本项目行为克隆最关键的一处修正）
    --------------------------------------------------
    不限制时，V1 在全部 1156 个座位里搜索，而策略只看到 48 个候选 ——
    实测 **V1 的选择几乎从不在候选池内**（池内命中 0-2 个/单）。
    也就是说老师的"正确答案"对策略而言是**不可见的动作**，行为克隆根本无从学起
    （早期版本因此把 78% 的标签退化成"候补"）。

    限制之后，老师与策略面对**同一个决策空间**，标签才有意义。
    代价是老师在这个受限空间里的解不再全局最优，但它仍是该空间内的最优，
    这正是我们想让学生模仿的对象。
    """
    from ..clustering import BookingState
    from ..models import TrainFormation

    if seat_pool:
        allowed = set(seat_pool)
        scoped = TrainFormation(
            train_code=formation.train_code,
            carriages=formation.carriages,
            seats=tuple(s for s in formation.seats if s.seat_id in allowed),
        )
    else:
        scoped = formation

    state = BookingState(formation=scoped)
    state.occupied = {sid for sid in occupied if sid in {s.seat_id for s in scoped.seats}}
    ctx = build_context(order, config)
    solution = assign_exact(ctx, state, config, mode="smart")
    placed = {pid: a.seat_id for pid, a in solution.assignments.items()}
    return placed, list(solution.waitlisted)


class BehaviorCloningDataset:
    """把专家轨迹存成可直接喂给网络的张量。

    实现选择：这里存**原始观测**，在 ``tensors()`` 里统一调
    :meth:`GraphTensors.from_observation` 编码，而不是在采集时逐条编码。
    原因是采集阶段手工索引张量维度极易出错（本项目真实踩过：
    在 ``add()`` 里多切了一维，导致 global_features 变成 28 维而模型期望 12 维，
    报错信息只显示 "shapes cannot be multiplied"，很难定位）。
    批量编码同时也与 PPO 的批量前向路径保持一致，避免两条路径漂移。
    """

    def __init__(self) -> None:
        self.observations: list[Any] = []
        self.masks: list[Any] = []
        self.actions: list[int] = []
        self.order_ids: list[str] = []
        self.expert_seat_rates: list[float] = []

    def __len__(self) -> int:
        return len(self.actions)

    def add(self, observation, mask, action: int, order_id: str) -> None:
        import numpy as np

        self.observations.append(observation)
        self.masks.append(np.asarray(mask, dtype=np.float32))
        self.actions.append(int(action))
        self.order_ids.append(order_id)

    def tensors(self, device: str = "cpu"):
        """批量编码成 GraphTensors，**每条都保留 batch 维**（形状 (1, ...)）。

        为什么保留 batch 维而不是挤掉：这样 ``x[rows]`` 这类按样本索引天然正确。
        早期版本把特征维也拼在一起（(B*P,F) 等），索引 ``x[[0,1]]`` 会落到
        特征维上，报错是 "shape '[28, 96, 96, 96]' is invalid" —— 完全看不出
        是索引方式的问题。代价只是后续统一用 ``cat`` 拼批。
        """
        import numpy as np
        import torch

        encoded = [
            GraphTensors.from_observation(obs, device=device).batch()
            for obs in self.observations
        ]
        return (
            GraphTensors(
                passenger_features=torch.cat([t.passenger_features for t in encoded], dim=0),
                seat_features=torch.cat([t.seat_features for t in encoded], dim=0),
                edge_features=torch.cat([t.edge_features for t in encoded], dim=0),
                seat_mask=torch.cat([t.seat_mask for t in encoded], dim=0),
                passenger_mask=torch.cat([t.passenger_mask for t in encoded], dim=0),
                global_features=torch.cat([t.global_features for t in encoded], dim=0),
            ),
            torch.as_tensor(np.stack(self.masks), dtype=torch.float32, device=device),
            torch.as_tensor(self.actions, dtype=torch.long, device=device),
        )


class TeacherAgent:
    """同候选池内的贪婪老师，用**预计算表**做增量打分。

    效率说明（这是本项目最慢的一段代码，值得记录）
    ------------------------------------------------
    三代实现的复杂度对比（每单）：

    1. 对每个候选座位跑一遍完整 ``evaluate_placement`` —— O(候选数 × 整单评估)，
       60 单要十几分钟，废弃；
    2. 逐候选调 ``FastScorer.row()`` / ``pair()`` —— 仍是 Python 循环，
       实测某些订单会卡住（一次 ``choose()`` 里要算
       乘客数 × 候选数 × 已落座数个成对值）；
    3. **当前实现**：两位乘客 × 任意两个候选座位的成对分值只有
       ``P² × S²`` 个（P ≤ 10、S = 48，最多二十来万），一次性预计算成字典，
       之后每次查表是 O(已落座人数)。单位置个体分同样每乘客只建一行。

    这样既保住了"与环境同源的口径"，又把热循环压到查表级别。
    """

    def __init__(self, order: Order, formation, config: EngineConfig) -> None:
        from ..fastscore import FastScorer
        from ..solver import build_context

        self.order = order
        self.formation = formation
        self.config = config
        self.ctx = build_context(order, config)
        self.seats = list(formation.seats)
        self.fs = FastScorer(
            order, self.ctx.passengers, self.seats, config, self.ctx.unit_of
        )
        self._rows: dict[str, list[float]] = {}
        self._pairs: dict[tuple[str, str, int, int], float] = {}

    def row(self, pid: str) -> list[float]:
        cached = self._rows.get(pid)
        if cached is None:
            cached = self.fs.row(pid)
            self._rows[pid] = cached
        return cached

    def prime(self, candidate_seat_ids: Sequence[str], passenger_ids: Sequence[str]) -> None:
        """为"本步的候选池 × 待定乘客"预计算个体分与成对分。

        每一步的候选池是共享的，因此预计算的键空间很小；预计算后
        热循环里只剩查表，避免了每次都能触发 ``pair()`` 的完整计算。
        """
        slots = [self.fs.seat_slot[sid] for sid in candidate_seat_ids]
        for pid in passenger_ids:
            self.row(pid)  # 建好整行，后续按 slot 直接索引
        for index, a in enumerate(passenger_ids):
            for b in passenger_ids[index + 1 :]:
                for sa in slots:
                    for sb in slots:
                        if sa == sb:
                            continue
                        self._pairs[(a, b, sa, sb)] = self.fs.pair(a, b, sa, sb)

    def marginal_gain(self, pid: str, seat_id: str, placed: dict[str, str]) -> float:
        """把 pid 放到 seat_id 带来的亲和度增量（近似权威口径）。

        包含三部分：

        1. **个体项**：``row(pid)[slot]``；
        2. **与已落座同伴的成对项**：查预计算表；
        3. **单元级安全项**：需照护者与支持人的紧邻加成 / 孤立惩罚。

        第 3 项是**必须**的，而且是最容易漏的一项。最初版本只算前两项，
        老师在带娃家庭上"只要坐得下就坐"，结果实测孤立照护 16-19 例：
        策略忠实模仿了老师，也就忠实学会了"把孩子和父母分开"。
        诊断方法是把老师和策略放在同一环境跑：两者指标几乎相同
        （-3420.5/16 vs -3425.5/16），说明问题在老师的目标而非策略的学习。

        第 3 项可以在 O(支持人数) 内算完：只需比较候选座位与已落座支持人的
        曼哈顿距离，取最优情形即可。
        """
        slot = self.fs.seat_slot[seat_id]
        gain = self.row(pid)[slot]
        for other_id, other_seat in placed.items():
            if other_id == pid:
                continue
            other_slot = self.fs.seat_slot[other_seat]
            key = (pid, other_id, slot, other_slot)
            value = self._pairs.get(key)
            if value is None:
                value = self.fs.pair(pid, other_id, slot, other_slot)
                self._pairs[key] = value
            gain += value
        gain += self._unit_term(pid, seat_id, placed)
        return gain

    def _unit_term(self, pid: str, seat_id: str, placed: dict[str, str]) -> float:
        """单元级项：单独放置 pid 时的"紧邻加成"或"完全孤立"惩罚。

        口径与 :func:`smartrail.solver.evaluate_placement` 的单元级判定保持一致：
        * 存在已落座支持人且其中至少一位与自己相邻 -> 加 ``b5_child_adjacent``；
        * 支持人一个都没落座 -> ``t1_isolated_care_member``；
        * 支持人已落座但都不相邻 -> 也给孤立惩罚。

        注意这里**不修改** ``placed``，因此不影响环境的真实状态。
        """
        passenger = self.ctx.passengers.get(pid)
        if passenger is None or not self.ctx.care_dependent.get(pid, False):
            return 0.0
        helpers = self.ctx.helpers.get(pid, frozenset())
        if not helpers:
            return 0.0
        seat = self.formation.seat(seat_id)
        decided = False
        adjacent = False
        for helper in helpers:
            helper_seat_id = placed.get(helper)
            if helper_seat_id is None:
                continue
            decided = True
            helper_seat = self.formation.seat(helper_seat_id)
            if (
                helper_seat.carriage == seat.carriage
                and helper_seat.manhattan_to(seat) <= 1
            ):
                adjacent = True
        if not decided:
            return 0.0  # 支持人都还没放，结局未定，暂不计分（与搜索期口径一致）
        return (
            self.config.b5_child_adjacent if adjacent else self.config.t1_isolated_care_member
        )

    def choose(
        self, observation, mask, placed: dict[str, str], occupied: set[str]
    ) -> tuple[int, int]:
        """返回 (乘客行号, 座位列号)；选不出更优的就返回候补。"""
        pending = [
            pid
            for row, pid in enumerate(observation.passenger_ids)
            if mask[row].sum() > 0.5
        ]
        if not pending:
            return 0, WAITLIST_ACTION
        self.prime(observation.candidate_seat_ids, pending)
        best_gain: float | None = None
        best: tuple[int, int] = (
            observation.passenger_ids.index(pending[0]),
            WAITLIST_ACTION,
        )
        for pid in pending:
            row = observation.passenger_ids.index(pid)
            for seat_index, seat_id in enumerate(observation.candidate_seat_ids):
                if mask[row, seat_index] < 0.5 or seat_id in occupied:
                    continue
                gain = self.marginal_gain(pid, seat_id, placed)
                if best_gain is None or gain > best_gain:
                    best_gain = gain
                    best = (row, seat_index)
        # 只有在**完全没有空位可选**时才候补。老师不主动候补的理由：
        # 线上口径是"有余票就出票"（OperatingPolicy 的出票优先原则），
        # 老师若因为某位置亲和度为负就候补，会把"拒票"当成正常行为教给策略 ——
        # 而拒票只应发生在确实无座时。
        return best


def teacher_next_action(
    env: SeatAllocationEnv,
    observation,
    mask,
) -> tuple[int, int]:
    """兼容包装：用环境缓存好的 :class:`TeacherAgent` 决策。"""
    agent = getattr(env, "_teacher_agent", None)
    if agent is None or agent.order is not env.order:
        agent = TeacherAgent(env.order, env.formation, env.config)
        env._teacher_agent = agent  # type: ignore[attr-defined]
    return agent.choose(observation, mask, env.placed, env.occupied)


def collect_expert_samples(
    episodes: int,
    seed: int = 0,
    config: EngineConfig | None = None,
    profile: TrafficProfile | None = None,
    fill: float = 0.0,
    max_seats: int = 48,
    verbose: bool = False,
    allow_projection: bool = True,
) -> BehaviorCloningDataset:
    """采集专家样本：**老师 = V1 整单求解**，标签投影到网络的候选池。

    为什么老师必须是"整单求解"而不是"逐步贪婪"
    ------------------------------------------
    这一版踩过一个很有代表性的坑：我先用"每步选边际增益最大的座位"当老师，
    单步看无懈可击，但因为它看不到后续，会把一家三口放成
    ``01车01A / 01车07D / 01车08A`` —— 第二位家长占了远处的座位，
    等到孩子落座时已经没有相邻位置，于是既有孤立惩罚（-3500）又白占了两个座。
    实测孤立照护 16 例、亲和度 -3420。

    而 V1（``assign_exact``）是**整单分支限界**，天然不会犯这种错。
    这正是"行为克隆用整单最优当老师"的价值：老师要比学生站得高。

    候选池投影
    ----------
    V1 可能在网络的 48 个候选之外选座（它看的是全部 1156 座）。这些座位策略
    **看不见也选不了**，直接丢掉会退化成"候补"标签（更早一版就因此把 78% 的
    标签变成了候补）。因此这里做投影：把 V1 的座位映射到候选池中**同一车厢、
    距离最近**的空位，并在 ``verbose`` 下报告投影比例。
    """
    config = config or EngineConfig()
    env = SeatAllocationEnv(
        seed=seed, config=config, profile=profile, fill=fill, max_seats=max_seats
    )
    dataset = BehaviorCloningDataset()
    projected = 0
    total_labels = 0

    for index in range(episodes):
        observation = env.reset_with(seed_offset=env.stream.counter)
        env.current_observation = observation
        order = env.order
        # 关键：把老师的搜索空间限制在**与策略相同的候选池**内
        placed_expert, _waitlisted = solve_with_teacher(
            order,
            env.formation,
            set(env.occupied),
            config,
            seat_pool=observation.candidate_seat_ids,
        )
        seat_count = 0
        while True:
            mask = env.action_mask(observation)
            if mask.sum() <= 0:
                break
            pending = [
                pid
                for row, pid in enumerate(observation.passenger_ids)
                if mask[row].sum() > 0.5
            ]
            if not pending:
                break
            pid = pending[0]
            row = observation.passenger_ids.index(pid)
            seat_id = placed_expert.get(pid)
            seat_index = WAITLIST_ACTION
            if seat_id is not None:
                total_labels += 1
                if seat_id in observation.candidate_seat_ids:
                    seat_index = observation.candidate_seat_ids.index(seat_id)
                elif allow_projection:
                    seat_index = _project_to_pool(
                        seat_id, observation, mask[row], env, config
                    )
                    projected += 1
            if seat_index == WAITLIST_ACTION:
                # 投影也找不到合适位置：这一单放弃，避免教出"拒票"的坏习惯
                break
            action = _flatten(row, seat_index, len(observation.candidate_seat_ids))
            dataset.add(observation, mask, action, order.order_id)
            result = env.step(row, seat_index)
            observation = result.observation
            if result.done:
                break
        else:
            pass
        dataset.expert_seat_rates.append(
            len(env.placed) / max(1, len(order.passengers))
        )
        if verbose and (index + 1) % 25 == 0:
            average = sum(dataset.expert_seat_rates) / len(dataset.expert_seat_rates)
            ratio = projected / max(1, total_labels)
            print(
                f"  采集 {index + 1}/{episodes} 单，样本 {len(dataset)} 条，"
                f"老师就座率 {average:.3f}，投影比例 {ratio:.1%}"
            )

    if verbose:
        print(
            f"[BC] 候选池投影：{projected}/{total_labels} "
            f"（{projected / max(1, total_labels):.1%}）"
        )
    return dataset


def _project_to_pool(
    seat_id: str,
    observation,
    row_mask,
    env: SeatAllocationEnv,
    config: EngineConfig,
) -> int:
    """把池外座位投影到候选池内**同车厢距离最近**的空位。"""
    expert_seat = env.formation.seat(seat_id)
    best_index = WAITLIST_ACTION
    best_distance = None
    for seat_index, candidate_id in enumerate(observation.candidate_seat_ids):
        if row_mask[seat_index] < 0.5:
            continue
        candidate = env.formation.seat(candidate_id)
        if candidate.carriage != expert_seat.carriage:
            continue
        distance = candidate.manhattan_to(expert_seat)
        if best_distance is None or distance < best_distance:
            best_distance = distance
            best_index = seat_index
    return best_index


def _sample_occupancy(env: SeatAllocationEnv, order: Order, fill: float) -> set[str]:
    """为这一单采样一个占座背景（复用环境的 RNG，保证可复现）。"""
    if fill <= 0:
        return set()
    seats = list(env.all_seat_ids)
    env.rng.shuffle(seats)
    return set(seats[: int(len(seats) * fill)])


def _is_care_dependent(pid: str, order: Order) -> bool:
    from ..scoring import is_care_dependent as _check

    passenger = next((p for p in order.passengers if p.passenger_id == pid), None)
    return bool(passenger and _check(passenger, order))


def _pending_mask(observation, pending: set[str]):
    import numpy as np

    seat_count = len(observation.candidate_seat_ids)
    mask = np.zeros(
        (observation.passenger_mask.shape[0], observation.seat_mask.shape[0] + 1),
        dtype=np.float32,
    )
    for index, pid in enumerate(observation.passenger_ids):
        if pid not in pending:
            continue
        mask[index, :seat_count] = observation.seat_mask[:seat_count]
        mask[index, -1] = 1.0
    return mask


def _flatten(passenger_index: int, seat_index: int, seat_count: int) -> int:
    """把 (乘客, 座位/候补) 展平成一个动作下标，与 ``_policy_step`` 的口径一致。"""
    column = seat_index if seat_index >= 0 else seat_count
    return passenger_index * (seat_count + 1) + column


def train_behavior_cloning(
    dataset: BehaviorCloningDataset,
    model: BipartiteSeatEncoder | None = None,
    epochs: int = 30,
    batch_size: int = 64,
    learning_rate: float = 3e-3,
    device: str = "cpu",
    verbose: bool = True,
    seed: int = 0,
) -> tuple[BipartiteSeatEncoder, list[dict[str, float]]]:
    """掩码交叉熵训练：让策略在每一步都选老师的动作。"""
    import numpy as np
    import torch
    import torch.nn.functional as F

    torch.manual_seed(seed)
    model = (model or BipartiteSeatEncoder()).to(device)
    tensors, masks, actions = dataset.tensors(device)
    optimizer = torch.optim.Adam(model.parameters(), lr=learning_rate)
    history: list[dict[str, float]] = []
    total = len(dataset)
    generator = np.random.default_rng(seed)

    for epoch in range(epochs):
        order = generator.permutation(total)
        epoch_loss = 0.0
        correct = 0
        batches = 0
        for start in range(0, total, batch_size):
            rows = list(range(start, min(start + batch_size, total)))
            batch = GraphTensors(
                passenger_features=tensors.passenger_features[rows],
                seat_features=tensors.seat_features[rows],
                edge_features=tensors.edge_features[rows],
                seat_mask=tensors.seat_mask[rows],
                passenger_mask=tensors.passenger_mask[rows],
                global_features=tensors.global_features[rows],
            )
            logits, _value = model(batch)
            batch_size_actual, n_pass, n_seat = logits.shape
            extra = torch.zeros(
                (batch_size_actual, n_pass, 1), dtype=logits.dtype, device=device
            )
            full = torch.cat([logits, extra], dim=2)
            mask = masks[rows]
            full = full.masked_fill(mask < 0.5, -1e9)
            flat = full.reshape(batch_size_actual, -1)
            target = actions[rows]
            loss = F.cross_entropy(flat, target)
            optimizer.zero_grad()
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            optimizer.step()
            epoch_loss += float(loss.item())
            with torch.no_grad():
                correct += int((flat.argmax(dim=1) == target).sum().item())
            batches += 1
        record = {
            "epoch": epoch + 1,
            "loss": epoch_loss / max(1, batches),
            "action_accuracy": correct / max(1, total),
        }
        history.append(record)
        if verbose and (epoch + 1) % 5 == 0:
            print(
                f"  [BC {epoch + 1:>3}/{epochs}] loss={record['loss']:.4f} "
                f"动作准确率={record['action_accuracy']:.3f}"
            )
    return model, history


def run_behavior_cloning(
    episodes: int = 300,
    seed: int = 11,
    fill: float = 0.0,
    epochs: int = 30,
    batch_size: int = 64,
    learning_rate: float = 3e-3,
    device: str = "cpu",
    out_dir: str | Path = "artifacts/v3",
    profile: TrafficProfile | None = None,
    verbose: bool = True,
) -> dict[str, Any]:
    """完整的行为克隆流程：采数据 → 训练 → 保存权重与曲线。"""
    from .rl import evaluate_policy

    started = time.perf_counter()
    config = EngineConfig()
    if verbose:
        print(f"[BC] 采集专家样本：{episodes} 单（fill={fill}）")
    dataset = collect_expert_samples(
        episodes=episodes, seed=seed, config=config, profile=profile, fill=fill,
        verbose=verbose,
    )
    if verbose:
        avg_rate = sum(dataset.expert_seat_rates) / max(1, len(dataset.expert_seat_rates))
        print(f"[BC] 样本 {len(dataset)} 条，老师平均就座率 {avg_rate:.3f}")

    model, history = train_behavior_cloning(
        dataset, epochs=epochs, batch_size=batch_size,
        learning_rate=learning_rate, device=device, verbose=verbose, seed=seed,
    )

    target = Path(out_dir)
    target.mkdir(parents=True, exist_ok=True)
    # 小规模运行（例如冒烟测试）不覆盖正式权重。
    # 真实教训：给 CLI 加上 --behavior-cloning 后，我用 20 单跑了一次参数校验，
    # 结果那次产出的 48 样本模型**直接覆盖了训练 1500 单的正式权重**
    # （两者写入同一个 v3_bc.pt）。现在按规模分流，避免一次冒烟测试毁掉产物。
    checkpoint_name = "v3_bc.pt" if episodes >= 300 else "v3_bc_smoke.pt"
    checkpoint = target / checkpoint_name
    import torch as _torch

    _torch.save(
        {"state_dict": model.state_dict(), "source": "behavior_cloning", "seed": seed},
        checkpoint,
    )

    # 注意 evaluate_policy 的第二个参数是 EngineConfig（不是 env）——
    # 传错会得到 "SeatAllocationEnv object has no attribute 'aisle_crossing_weight'"。
    evaluation = evaluate_policy(model, config, device=device, episodes=20, seed=seed + 1)
    elapsed = time.perf_counter() - started
    curve = {
        "source": "behavior_cloning",
        "episodes": episodes,
        "samples": len(dataset),
        "expert_seat_rate": sum(dataset.expert_seat_rates)
        / max(1, len(dataset.expert_seat_rates)),
        "epochs": epochs,
        "history": history,
        "final_eval": evaluation,
        "elapsed_s": round(elapsed, 2),
        "checkpoint": str(checkpoint),
    }
    (target / ("v3_bc_curve.json" if episodes >= 300 else "v3_bc_smoke_curve.json")).write_text(
        json.dumps(curve, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if verbose:
        print(
            f"[BC] 完成：{elapsed:.1f}s，动作准确率 "
            f"{history[-1]['action_accuracy']:.3f}，评估 {json.dumps(evaluation, ensure_ascii=False)}"
        )
    return curve


__all__ = [
    "BehaviorCloningDataset",
    "ExpertSample",
    "collect_expert_samples",
    "run_behavior_cloning",
    "solve_with_teacher",
    "train_behavior_cloning",
]
