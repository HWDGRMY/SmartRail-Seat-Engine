"""V3.0：二分图编码器（PyTorch）。

为什么是"二分图"而不是普通 GNN
-------------------------------
本问题是**乘客 × 座位**的二部图匹配：乘客节点与座位节点天然分属两类，
信息只在"候选边"上流动。因此网络结构采用标准的二分消息传递：

    round 1:  座位 → 乘客（乘客聚合自己所有候选座位的特征）
              乘客 → 座位（座位聚合所有可能坐它的乘客特征）
    round 2:  再来一轮，让"竞争同一座位"的乘客信息也能传导到位

这种结构比把乘客与座位混成同构图更有表达力，也更省参数。

网络输出
--------
每个 (乘客, 候选座位) 边输出一个**打分 logits**，策略在**列（座位）维度**做
softmax，从而天然满足"一个座位只能给一个人"的硬约束；解码时再叠加
"一位乘客只坐一个座位"的贪心顺序约束。

网络在缺少 PyTorch 的环境下不会被导入（见 :mod:`smartrail.v3.policy` 的可用性判断）。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

try:  # pragma: no cover - 取决于运行环境
    import torch
    import torch.nn as nn
    import torch.nn.functional as F
except ImportError as error:  # pragma: no cover
    raise ImportError("V3.0 的图编码器需要 PyTorch：pip install torch") from error

from .features import (
    EDGE_FEATURES,
    GLOBAL_FEATURES,
    MAX_PASSENGERS,
    MAX_SEATS,
    PASSENGER_FEATURES,
    SEAT_FEATURES,
)


@dataclass
class GraphTensors:
    """把 :class:`smartrail.v3.features.GraphObservation` 转成张量。"""

    passenger_features: "torch.Tensor"
    seat_features: "torch.Tensor"
    edge_features: "torch.Tensor"
    seat_mask: "torch.Tensor"
    passenger_mask: "torch.Tensor"
    global_features: "torch.Tensor"

    @staticmethod
    def from_observation(observation, device: str = "cpu") -> "GraphTensors":
        def tensor(array: np.ndarray) -> "torch.Tensor":
            return torch.as_tensor(array, dtype=torch.float32, device=device)

        return GraphTensors(
            passenger_features=tensor(observation.passenger_features),
            seat_features=tensor(observation.seat_features),
            edge_features=tensor(observation.edge_features),
            seat_mask=tensor(observation.seat_mask),
            passenger_mask=tensor(observation.passenger_mask),
            global_features=tensor(observation.global_features),
        )

    def batch(self) -> "GraphTensors":
        """增加 batch 维度（batch=1）。"""
        return GraphTensors(
            passenger_features=self.passenger_features.unsqueeze(0),
            seat_features=self.seat_features.unsqueeze(0),
            edge_features=self.edge_features.unsqueeze(0),
            seat_mask=self.seat_mask.unsqueeze(0),
            passenger_mask=self.passenger_mask.unsqueeze(0),
            global_features=self.global_features.unsqueeze(0),
        )


class BipartiteSeatEncoder(nn.Module):
    """乘客 ↔ 座位 二分图编码器 + 边打分头。"""

    def __init__(
        self,
        passenger_dim: int = PASSENGER_FEATURES,
        seat_dim: int = SEAT_FEATURES,
        edge_dim: int = EDGE_FEATURES,
        global_dim: int = GLOBAL_FEATURES,
        hidden: int = 96,
        rounds: int = 2,
        dropout: float = 0.0,
    ) -> None:
        super().__init__()
        self.rounds = rounds
        self.passenger_encoder = nn.Sequential(
            nn.Linear(passenger_dim, hidden), nn.ReLU(), nn.Linear(hidden, hidden)
        )
        self.seat_encoder = nn.Sequential(
            nn.Linear(seat_dim, hidden), nn.ReLU(), nn.Linear(hidden, hidden)
        )
        self.edge_encoder = nn.Sequential(
            nn.Linear(edge_dim, hidden), nn.ReLU()
        )
        self.global_encoder = nn.Sequential(
            nn.Linear(global_dim, hidden), nn.ReLU()
        )
        # 消息传递：把边特征分别聚合到乘客侧与座位侧
        self.to_passenger = nn.Sequential(nn.Linear(hidden * 3, hidden), nn.ReLU())
        self.to_seat = nn.Sequential(nn.Linear(hidden * 3, hidden), nn.ReLU())
        self.norm_passenger = nn.LayerNorm(hidden)
        self.norm_seat = nn.LayerNorm(hidden)
        # 打分头：乘客向量 × 座位向量 × 边向量 × 全局向量
        self.score_head = nn.Sequential(
            nn.Linear(hidden * 4, hidden), nn.ReLU(), nn.Linear(hidden, 1)
        )
        self.value_head = nn.Sequential(
            nn.Linear(hidden * 2 + global_dim, hidden), nn.ReLU(), nn.Linear(hidden, 1)
        )
        self.dropout = nn.Dropout(dropout) if dropout > 0 else nn.Identity()

    # -- 前向 -------------------------------------------------------------
    def encode(self, tensors: GraphTensors) -> tuple["torch.Tensor", "torch.Tensor"]:
        """返回 (乘客嵌入, 座位嵌入)，形状均为 (B, hidden)。"""
        p = self.passenger_encoder(tensors.passenger_features)
        s = self.seat_encoder(tensors.seat_features)
        e = self.edge_encoder(tensors.edge_features)
        g = self.global_encoder(tensors.global_features)

        batch, n_pass, n_seat, edge_dim = (
            p.shape[0],
            p.shape[1],
            s.shape[1],
            e.shape[-1],
        )
        # (B, P, S, hidden)
        edges = e.view(batch, n_pass, n_seat, edge_dim)
        p_expanded = p.unsqueeze(2).expand(-1, -1, n_seat, -1)
        s_expanded = s.unsqueeze(1).expand(-1, n_pass, -1, -1)

        seat_mask = tensors.seat_mask.view(batch, 1, n_seat, 1)
        passenger_mask = tensors.passenger_mask.view(batch, n_pass, 1, 1)
        edge_mask = seat_mask * passenger_mask  # (B, P, S, 1)

        for _ in range(self.rounds):
            mixed = torch.cat([p_expanded, s_expanded, edges], dim=-1)
            # 座位 → 乘客
            to_p = self.to_passenger(mixed) * edge_mask
            pooled_p = to_p.sum(dim=2) / edge_mask.sum(dim=2).clamp(min=1.0)
            p = self.norm_passenger(p + self.dropout(pooled_p))
            # 乘客 → 座位
            to_s = self.to_seat(mixed) * edge_mask
            pooled_s = to_s.sum(dim=1) / edge_mask.sum(dim=1).clamp(min=1.0)
            s = self.norm_seat(s + self.dropout(pooled_s))
            # 刷新展开
            p_expanded = p.unsqueeze(2).expand(-1, -1, n_seat, -1)
            s_expanded = s.unsqueeze(1).expand(-1, n_pass, -1, -1)
            edges = self.edge_encoder(tensors.edge_features).view(
                batch, n_pass, n_seat, edge_dim
            )

        return p, s

    def forward(self, tensors: GraphTensors) -> tuple["torch.Tensor", "torch.Tensor"]:
        """返回 (logits (B,P,S), state_value (B,1))。"""
        p, s = self.encode(tensors)
        batch, n_pass, hidden = p.shape
        n_seat = s.shape[1]
        edges = self.edge_encoder(tensors.edge_features).view(batch, n_pass, n_seat, hidden)
        p_expanded = p.unsqueeze(2).expand(-1, -1, n_seat, -1)
        s_expanded = s.unsqueeze(1).expand(-1, n_pass, -1, -1)
        joined = torch.cat([p_expanded, s_expanded, edges, self._global_broadcast(tensors, p)], dim=-1)
        logits = self.score_head(joined).squeeze(-1)  # (B, P, S)
        # 掩掉不可用候选
        logits = logits.masked_fill(tensors.seat_mask.view(batch, 1, n_seat) < 0.5, -1e9)
        pooled = (p * tensors.passenger_mask.unsqueeze(-1)).sum(dim=1) / tensors.passenger_mask.sum(
            dim=1, keepdim=True
        ).clamp(min=1.0)
        pooled_s = (s * tensors.seat_mask.unsqueeze(-1)).sum(dim=1) / tensors.seat_mask.sum(
            dim=1, keepdim=True
        ).clamp(min=1.0)
        value = self.value_head(torch.cat([pooled, pooled_s, tensors.global_features], dim=-1))
        return logits, value

    def _global_broadcast(self, tensors: GraphTensors, p: "torch.Tensor") -> "torch.Tensor":
        batch, n_pass, hidden = p.shape
        n_seat = tensors.seat_features.shape[1]
        g = self.global_encoder(tensors.global_features)  # (B, hidden)
        return g.unsqueeze(1).unsqueeze(2).expand(-1, n_pass, n_seat, -1)

    # -- 便捷方法 ---------------------------------------------------------
    @torch.no_grad()
    def act(
        self,
        tensors: GraphTensors,
        greedy: bool = False,
        generator: "torch.Generator | None" = None,
    ) -> tuple[list[tuple[int, int]], "torch.Tensor", "torch.Tensor"]:
        """按策略采样一组 (乘客, 座位) 匹配（顺序贪心解码）。

        解码规则：按"该乘客最高 logits"从大到小依次确定座位，已被占用的座位
        跳过；这等价于对二部图做一次确定性/随机化的顺序匹配，
        既保证"一座一人"，又保留策略的随机性用于探索。
        """
        logits, value = self.forward(tensors)
        logits = logits[0]  # (P, S)
        seat_mask = tensors.seat_mask[0]
        passenger_mask = tensors.passenger_mask[0]
        probabilities = F.softmax(logits, dim=1)
        matches: list[tuple[int, int]] = []
        taken_seats: set[int] = set()
        log_prob_sum = torch.zeros((), dtype=logits.dtype, device=logits.device)
        entropy_sum = torch.zeros((), dtype=logits.dtype, device=logits.device)

        confidence = logits.max(dim=1).values + (1.0 - passenger_mask) * -1e9
        order = torch.argsort(confidence, descending=True)
        for passenger_index in order.tolist():
            if passenger_mask[passenger_index] < 0.5:
                continue
            row = logits[passenger_index].clone()
            for seat_index in taken_seats:
                row[seat_index] = -1e9
            if seat_mask.sum() < 0.5 or torch.all(row <= -1e8):
                continue
            row_prob = F.softmax(row, dim=0)
            distribution = torch.distributions.Categorical(probs=row_prob)
            if greedy:
                seat_index = int(torch.argmax(row).item())
            else:
                seat_index = int(distribution.sample().item())
            log_prob_sum = log_prob_sum + distribution.log_prob(
                torch.as_tensor(seat_index, device=row_prob.device)
            )
            entropy_sum = entropy_sum + distribution.entropy()
            taken_seats.add(seat_index)
            matches.append((passenger_index, seat_index))
        return matches, log_prob_sum, value.reshape(-1)[0]


def build_model(**kwargs: object) -> BipartiteSeatEncoder:
    """工厂函数（便于按配置构造）。"""
    return BipartiteSeatEncoder(**kwargs)  # type: ignore[arg-type]


__all__ = ["BipartiteSeatEncoder", "GraphTensors", "MAX_PASSENGERS", "MAX_SEATS", "build_model"]
