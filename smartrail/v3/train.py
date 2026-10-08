"""V3.0 训练/评估入口。

用法
----
    # 训练 PPO 策略（需要 torch），产物写入 artifacts/v3/
    python -m smartrail.v3.train --updates 80 --episodes 24

    # 只评估已训练策略（不训练）
    python -m smartrail.v3.train --eval-only --checkpoint artifacts/v3/v3_ppo.pt

    # 三版本同流对比（V1 启发式 / V2 CP-SAT / V3 学习型）
    python -m smartrail.v3.train --compare --steps 60

    # 纯仿真器自检（不需要 torch）
    python -m smartrail.v3.train --simulate --steps 200

产物（默认 ``artifacts/v3/``）
------------------------------
* ``v3_ppo.pt``               —— 训练好的策略权重（StateDict）
* ``v3_training_curve.json``  —— 学习曲线 + 最终评估（验收页直接读取画图）
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ARTIFACT_DIR = Path("artifacts/v3")


def _torch_available() -> bool:
    import importlib.util

    return importlib.util.find_spec("torch") is not None


def _numpy_available() -> bool:
    import importlib.util

    return importlib.util.find_spec("numpy") is not None


def run_simulate(steps: int = 200, seed: int = 7) -> dict[str, Any]:
    """纯仿真器自检（V1 策略），不需要 numpy/torch。"""
    from .simulator import RulePolicy, run_policy

    return run_policy(RulePolicy("v1_heuristic"), steps=steps, seed=seed)


def run_compare(steps: int = 60, seed: int = 7, fill: float = 0.0) -> list[dict[str, Any]]:
    """在同一订单流上并排跑 V1 / V2 / V3（缺依赖的后端会被跳过并说明原因）。"""
    from ..v2 import available_backends
    from .simulator import RulePolicy, run_policy

    backends = available_backends()
    results: list[dict[str, Any]] = []
    for name in ("v1_heuristic", "v2_cpsat", "v3_rl"):
        info = backends.get(name)
        if info is None:
            continue
        if not info["available"]:
            results.append(
                {
                    "policy": name,
                    "skipped": True,
                    "reason": f"缺少依赖：{', '.join(info['missing'])}",  # type: ignore[arg-type]
                }
            )
            continue
        results.append(run_policy(RulePolicy(name), steps=steps, seed=seed, fill=fill))
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SmartRail-Seat-Engine V3.0 训练/评估")
    parser.add_argument("--updates", type=int, default=60, help="PPO 更新轮数")
    parser.add_argument("--episodes", type=int, default=24, help="每轮采样 episode 数")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--fill", type=float, default=0.0, help="初始占座比例")
    parser.add_argument("--device", default=None)
    parser.add_argument("--out", default=str(ARTIFACT_DIR))
    parser.add_argument("--eval-only", action="store_true")
    parser.add_argument("--checkpoint", default=str(ARTIFACT_DIR / "v3_ppo.pt"))
    parser.add_argument("--compare", action="store_true", help="只做三版本同流对比")
    parser.add_argument("--simulate", action="store_true", help="只跑仿真器自检（无 torch）")
    parser.add_argument(
        "--behavior-cloning",
        action="store_true",
        help="行为克隆：用 V1 整单解当老师做监督预训练（V3 就座率提升最明显的一步）",
    )
    parser.add_argument("--bc-episodes", type=int, default=1500, help="行为克隆采集订单数")
    parser.add_argument("--bc-epochs", type=int, default=35, help="行为克隆训练轮数")
    parser.add_argument("--steps", type=int, default=60, help="对比/仿真模式下的订单数")
    parser.add_argument("--eval-episodes", type=int, default=20)
    args = parser.parse_args(argv)

    if args.simulate:
        summary = run_simulate(steps=args.steps, seed=args.seed)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0

    if args.compare:
        for item in run_compare(steps=args.steps, seed=args.seed, fill=args.fill):
            if item.get("skipped"):
                print(f"[skip] {item['policy']}：{item['reason']}")
                continue
            lat = item["latency_ms"]
            print(
                f"{item['policy']:<14} 就座率={item['seat_rate']:.3f} "
                f"亲和度={item['total_affinity']:>10.1f} Tier0={item['tier0_violations']} "
                f"P50={lat['p50']:.1f}ms P99={lat['p99']:.1f}ms"
            )
        return 0

    if not _torch_available():
        print("缺少 PyTorch，无法训练/评估学习型策略。")
        print("安装：pip install torch；或先跑 --simulate / --compare 验证 V1/V2。")
        return 2
    if not _numpy_available():
        print("缺少 numpy（特征工程依赖）。安装：pip install numpy")
        return 2

    from .rl import PpoConfig, evaluate_policy, train

    if args.eval_only:
        from ..config import EngineConfig
        from .gnn import BipartiteSeatEncoder
        from .policy import _load_model

        model, loaded = _load_model(args.checkpoint, device=args.device or "cpu")
        _ = BipartiteSeatEncoder
        if not loaded:
            print(f"警告：未找到权重 {args.checkpoint}，使用随机初始化策略评估。")
        metrics = evaluate_policy(
            model,
            EngineConfig(),
            args.device or "cpu",
            episodes=args.eval_episodes,
            seed=args.seed,
            fill=args.fill,
        )
        print(json.dumps({"checkpoint": args.checkpoint, "loaded": loaded, **metrics}, ensure_ascii=False, indent=2))
        return 0

    if args.behavior_cloning:
        from .behavior_cloning import run_behavior_cloning

        curve = run_behavior_cloning(
            episodes=args.bc_episodes,
            seed=args.seed,
            fill=args.fill,
            epochs=args.bc_epochs,
            device=args.device or "cpu",
            out_dir=args.out,
            verbose=True,
        )
        print("-" * 72)
        print(f"行为克隆完成：{curve['elapsed_s']}s")
        print(
            f"老师平均就座率 {curve['expert_seat_rate']:.3f}，"
            f"动作准确率 {curve['history'][-1]['action_accuracy']:.3f}"
        )
        print(f"最终评估：{json.dumps(curve['final_eval'], ensure_ascii=False)}")
        print(f"权重：{curve['checkpoint']}")
        return 0

    result = train(
        ppo=PpoConfig(
            updates=args.updates,
            episodes_per_batch=args.episodes,
            seed=args.seed,
            fill=args.fill,
        ),
        out_dir=args.out,
        device=args.device,
    )
    print("-" * 72)
    print(f"训练完成：{result['elapsed_s']}s，设备 {result['device']}")
    print(f"最终评估：{json.dumps(result['final_eval'], ensure_ascii=False)}")
    if "model_path" in result:
        print(f"权重：{result['model_path']}")
        print(f"曲线：{result['curve_path']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
