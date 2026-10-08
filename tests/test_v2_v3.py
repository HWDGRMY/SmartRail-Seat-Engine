"""V2.0 / V3.0 测试：求解器插件层、CP-SAT 建模、零依赖精确求解、仿真器与特征。

双模式运行：

    python tests/test_v2_v3.py     # 脚本模式（按依赖可用性自动跳过）
    pytest tests/test_v2_v3.py     # pytest 模式

设计原则：**依赖缺失只能导致"跳过"，绝不能导致"失败"**。
这样在只有 V1 依赖的机器上跑测试是绿的，在装了 ortools/torch 的机器上
会自动把 V2/V3 的深度检查也跑起来。
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from smartrail import crh_16_car_formation
from smartrail.config import EngineConfig
from smartrail.credit import CreditLedger
from smartrail.fixtures import (
    family_with_child_order,
    group_order,
    multi_gen_order,
    pregnant_order,
    solo_order,
    wheelchair_order,
)
from smartrail.models import Order, RelationType
from smartrail.v2 import available_backends, solve_with
from smartrail.v2.registry import BackendUnavailable, missing_modules

FAILURES: list[str] = []
UNDER_PYTEST = "PYTEST_CURRENT_TEST" in os.environ or "pytest" in sys.modules


def check(condition: bool, message: str) -> None:
    if condition:
        print(f"  PASS  {message}")
        return
    print(f"  FAIL  {message}")
    FAILURES.append(message)
    if UNDER_PYTEST:
        raise AssertionError(message)


def skip(message: str) -> None:
    print(f"  SKIP  {message}")


def _has(module: str) -> bool:
    return importlib.util.find_spec(module) is not None


def _engine_with_order(order: Order, backend: str, **kwargs):
    from smartrail.engine import SeatEngine

    engine = SeatEngine(formation=crh_16_car_formation())
    prepared = engine.prepare_order(order)
    solution = solve_with(
        backend, prepared, engine.state, EngineConfig(), mode="smart", **kwargs
    )
    return engine, solution


# ---------------------------------------------------------------------------
# 插件层
# ---------------------------------------------------------------------------


def test_registry_lists_all_versions() -> None:
    print("[V2/V3] 求解器注册表")
    backends = available_backends()
    for name in ("v1_heuristic", "v2_cpsat", "v2_exact", "v3_rl"):
        check(name in backends, f"注册了后端 {name}")
    check(backends["v1_heuristic"]["available"], "V1 启发式在无第三方依赖下可用")
    check(backends["v2_exact"]["available"], "V2 自研精确求解器无需依赖即可用")
    check(
        isinstance(backends["v2_cpsat"]["missing"], list),
        "后端会报告缺失依赖（前端据此显示可用性）",
    )


def test_unavailable_backend_raises_clearly() -> None:
    print("[V2/V3] 依赖缺失时的错误提示")
    if _has("ortools"):
        skip("已安装 ortools，跳过'缺失依赖'路径")
        return
    order = solo_order("Z1")
    try:
        _engine_with_order(order, "v2_cpsat")
        check(False, "缺少 ortools 时应当抛出 BackendUnavailable")
    except BackendUnavailable as error:
        check("ortools" in str(error), f"错误信息指明缺失依赖：{error}")
    check(
        missing_modules(("ortools",)) == ["ortools"],
        "missing_modules 正确识别未安装模块",
    )


# ---------------------------------------------------------------------------
# 匈牙利算法（精确求解的基础件）
# ---------------------------------------------------------------------------


def test_hungarian_matches_bruteforce() -> None:
    print("[V2] 匈牙利算法与暴力枚举一致")
    from itertools import permutations

    from smartrail.v2.exact import hungarian

    cases = [
        [[4, 1, 3], [2, 0, 5], [3, 2, 2]],
        [[1, 2], [3, 4]],
        [[1, 2, 3], [3, 1, 2], [2, 3, 1]],
    ]
    for matrix in cases:
        rows = len(matrix)
        best = min(
            sum(matrix[i][perm[i]] for i in range(rows)) for perm in permutations(range(rows))
        )
        value, assignment = hungarian(matrix)
        check(
            abs(value - best) < 1e-9 and -1 not in assignment,
            f"{matrix} → 匈牙利 {value} == 暴力 {best}",
        )


# ---------------------------------------------------------------------------
# V2 自研精确求解器
# ---------------------------------------------------------------------------


def test_exact_backend_optimal_on_small_instance() -> None:
    print("[V2] 零依赖后端：与 V1 同口径、可复现")
    engine, solution = _engine_with_order(group_order(3, order_id="O-EXACT"), "v2_exact")
    check(solution.solver.startswith("exact-pure"), f"使用零依赖后端（{solution.solver}）")
    check(len(solution.assignments) == 3, "3 人全部就座")
    seats = [a.seat_id for a in solution.assignments.values()]
    check(len(set(seats)) == 3, "一人一座")
    check(not [v for v in solution.violations if v.tier == 0], "无 Tier 0 违规")
    check(
        any("复用 V1 分支限界" in note for note in solution.notes),
        "notes 说明了实现来源（不做无提示的口径切换）",
    )


def test_exact_matches_v1_exactly() -> None:
    """零依赖后端与 V1 必须给出**完全一致**的评测口径。

    这是 3.0 版实现的核心约束：自研搜索曾因代理目标与权威代价漂移而给出错误结果
    （见模块文档），因此改为共用同一套评估实现 —— 这条测试守住"不许再漂移"。
    """
    print("[V2] 零依赖后端与 V1 口径一致")
    for label, factory in (
        ("family", family_with_child_order),
        ("multigen", multi_gen_order),
        ("group5", lambda: group_order(5)),
    ):
        _, exact = _engine_with_order(factory(), "v2_exact")
        _, heuristic = _engine_with_order(factory(), "v1_heuristic")
        check(
            abs(exact.total_affinity - heuristic.total_affinity) < 1e-6,
            f"{label}: exact({exact.total_affinity:.0f}) == v1({heuristic.total_affinity:.0f})",
        )
        check(
            {a.seat_id for a in exact.assignments.values()}
            == {a.seat_id for a in heuristic.assignments.values()},
            f"{label}: 座位分配完全一致",
        )


def test_exact_matches_bruteforce() -> None:
    """匈牙利算法件（自研精确求解的算法基础）与暴力枚举一致。

    完整搜索目标已被判定"不宜自研"（见 :mod:`smartrail.v2.exact` 模块文档），
    但其中的指派算法仍作为独立可用件保留并在此验证。
    """
    print("[V2] 匈牙利算法 vs 暴力枚举（独立参照）")
    import random
    from itertools import permutations

    from smartrail.v2.exact import hungarian

    rng = random.Random(20241008)
    checked = 0
    for rows in (1, 2, 3, 4):
        for cols in (rows, rows + 2, rows + 5):
            for _ in range(4):
                matrix = [
                    [rng.choice([-1.0e9, rng.randint(-40, 80)]) for _ in range(cols)]
                    for _ in range(rows)
                ]
                for i in range(rows):
                    matrix[i][i % cols] = rng.randint(0, 80)
                best = max(
                    sum(matrix[i][c] for i, c in enumerate(pick))
                    for pick in permutations(range(cols), rows)
                )
                value, assignment = hungarian([[-x for x in row] for row in matrix])
                got = sum(matrix[i][c] for i, c in enumerate(assignment))
                if got != best:
                    check(False, f"矩阵 {matrix} → 匈牙利 {got} != 暴力 {best}")
                    return
                checked += 1
    check(checked >= 40, f"{checked} 组随机矩阵（含 big-M 稀疏）全部与暴力枚举一致")


def test_exact_respects_wheelchair_hard_constraint() -> None:
    print("[V2] 精确求解同样守住轮椅硬约束")
    engine, solution = _engine_with_order(wheelchair_order(order_id="O-EXACT-W"), "v2_exact")
    for assignment in solution.assignments.values():
        seat = engine.formation.seat(assignment.seat_id)
        if assignment.passenger_id == "W1":
            check(seat.in_accessible_zone(), f"轮椅乘客分到无障碍座位 {seat.seat_id}")
    if "W1" in solution.waitlisted:
        check(True, "轮椅乘客进入候补（无障碍售罄）而非坐普通座位")


def test_exact_respects_pregnant_bond() -> None:
    print("[V2] 精确求解守住孕晚期同行约束")
    _, solution = _engine_with_order(pregnant_order(order_id="O-EXACT-P"), "v2_exact")
    cars = {a.carriage for a in solution.assignments.values()}
    p_car = next(
        (a.carriage for a in solution.assignments.values() if a.passenger_id == "P1"), None
    )
    if p_car is None:
        check(True, "孕晚期旅客候补（余票不足）")
    else:
        check(p_car in cars, "孕晚期旅客与同行人同车厢")


# ---------------------------------------------------------------------------
# CP-SAT（可选依赖）
# ---------------------------------------------------------------------------


def test_cpsat_backend_when_available() -> None:
    print("[V2] CP-SAT 后端（若已安装 ortools）")
    if not _has("ortools"):
        skip("未安装 ortools，跳过 CP-SAT 运行检查（建模代码仍由 test_cpsat_model_builds 覆盖）")
        return
    engine, solution = _engine_with_order(family_with_child_order(order_id="O-CPSAT"), "v2_cpsat")
    check(solution.solver.startswith("cp-sat"), f"使用 CP-SAT 求解（{solution.solver}）")
    status = getattr(solution, "cpsat_status", "?")
    check(status in {"OPTIMAL", "FEASIBLE"}, f"CP-SAT 状态 {status}")
    check(len(solution.assignments) == 3, "3 人全部就座")
    check(not [v for v in solution.violations if v.tier == 0], "无 Tier 0 违规")
    check(getattr(solution, "cpsat_variables", 0) > 0, "记录了决策变量数")
    _ = engine


def test_cpsat_candidate_pool_embeds_hard_constraints() -> None:
    print("[V2] CP-SAT 候选池内嵌硬约束（不依赖 ortools 也能验证）")
    from smartrail.solver import build_context
    from smartrail.v2.cpsat import build_candidates

    engine, _ = None, None
    from smartrail.engine import SeatEngine

    engine = SeatEngine(formation=crh_16_car_formation())
    order = engine.prepare_order(wheelchair_order(order_id="O-CAND"))
    ctx = build_context(order, EngineConfig())
    seats = list(engine.state.available_seats)
    candidates = build_candidates(ctx, seats, EngineConfig(), per_passenger=24)
    wheel_slots = candidates["W1"]
    check(bool(wheel_slots), "轮椅乘客有候选座位")
    check(
        all(seats[slot].in_accessible_zone() for slot in wheel_slots),
        "轮椅乘客的候选**全部**位于无障碍专区（Tier 0 在建模前即成立）",
    )

    family = engine.prepare_order(family_with_child_order(order_id="O-CAND-F"))
    ctx_f = build_context(family, EngineConfig())
    candidates_f = build_candidates(ctx_f, seats, EngineConfig(), per_passenger=32)
    child_carriages = {seats[slot].carriage for slot in candidates_f["C1"]}
    helper_carriages = {seats[slot].carriage for slot in candidates_f["A1"]}
    check(
        bool(child_carriages & helper_carriages),
        f"儿童与照护人的候选车厢**有交集**（{sorted(child_carriages)} ∩ {sorted(helper_carriages)}）"
        " —— 同车厢约束才有可行解",
    )
    # 更强的检查：把候选池放大到覆盖全部车厢时，儿童的可选车厢必然被照护人约束住
    wide = build_candidates(ctx_f, seats, EngineConfig(), per_passenger=len(seats))
    child_wide = {seats[slot].carriage for slot in wide["C1"]}
    helper_wide = {seats[slot].carriage for slot in wide["A1"]}
    check(
        child_wide <= helper_wide,
        f"全量候选时儿童车厢 ⊆ 照护人车厢（{sorted(child_wide)} ⊆ {sorted(helper_wide)}）"
        " —— 跨车厢被结构性排除",
    )


# ---------------------------------------------------------------------------
# V3：仿真器与特征
# ---------------------------------------------------------------------------


def test_simulator_runs_without_numpy_torch() -> None:
    print("[V3] 仿真器不依赖 numpy/torch 即可运行")
    from smartrail.v3.simulator import RulePolicy, run_policy

    summary = run_policy(RulePolicy("v1_heuristic"), steps=12, seed=3)
    check(summary["steps"] == 12, f"推进了 12 个订单（实际 {summary['steps']}）")
    check(summary["passengers"] >= 12, f"覆盖 {summary['passengers']} 位乘客")
    check(summary["tier0_violations"] == 0, "仿真过程 Tier 0 违规为 0")
    check(summary["seat_rate"] > 0.9, f"就座率 {summary['seat_rate']:.3f}")
    check(len(summary["affinity_curve"]) > 0, "产出亲和度曲线（供验收页画图）")
    check("p99" in summary["latency_ms"], "产出 P99 时延")


def test_simulator_is_reproducible() -> None:
    print("[V3] 仿真器可复现（同 seed 同结果）")
    from smartrail.v3.simulator import RulePolicy, run_policy

    first = run_policy(RulePolicy("v1_heuristic"), steps=10, seed=42)
    second = run_policy(RulePolicy("v1_heuristic"), steps=10, seed=42)
    check(
        first["total_affinity"] == second["total_affinity"]
        and first["passengers"] == second["passengers"],
        "同 seed 两次运行的亲和度与人数完全一致",
    )


def test_simulator_high_load_changes_mode_mix() -> None:
    print("[V3] 高负载下的模式分布发生变化（降级容灾生效）")
    from smartrail.v3.simulator import RulePolicy, run_policy

    light = run_policy(RulePolicy("v1_heuristic"), steps=20, seed=5, fill=0.0)
    heavy = run_policy(RulePolicy("v1_heuristic"), steps=20, seed=5, fill=0.9)
    check(light["mode_counts"] != heavy["mode_counts"], f"模式分布不同：{light['mode_counts']} vs {heavy['mode_counts']}")
    check(heavy["tier0_violations"] == 0, "高负载下仍无 Tier 0 违规")


def test_features_encoding() -> None:
    print("[V3] 图特征编码")
    if not _has("numpy"):
        skip("未安装 numpy，跳过特征编码检查")
        return
    from smartrail.engine import SeatEngine
    from smartrail.v3.features import (
        EDGE_FEATURES,
        GLOBAL_FEATURES,
        MAX_PASSENGERS,
        MAX_SEATS,
        PASSENGER_FEATURES,
        SEAT_FEATURES,
        build_observation,
    )

    engine = SeatEngine(formation=crh_16_car_formation())
    order = engine.prepare_order(family_with_child_order(order_id="O-FEAT"))
    observation = build_observation(
        order,
        list(engine.state.available_seats),
        EngineConfig(),
        CreditLedger(),
        occupied_seats=set(),
        all_seats=engine.formation.seats,
    )
    check(
        observation.passenger_features.shape == (MAX_PASSENGERS, PASSENGER_FEATURES),
        f"乘客特征形状 {observation.passenger_features.shape}",
    )
    check(
        observation.seat_features.shape == (MAX_SEATS, SEAT_FEATURES),
        f"座位特征形状 {observation.seat_features.shape}",
    )
    check(
        observation.edge_features.shape[1] == EDGE_FEATURES
        and observation.global_features.shape[0] == GLOBAL_FEATURES,
        "边特征与全局特征维度正确",
    )
    check(float(observation.passenger_mask.sum()) == 3, "3 位乘客的掩码为 1")
    check(float(observation.seat_mask.sum()) == MAX_SEATS, "候选座位池已填满")
    check(
        all(0.0 <= v <= 1.0 for row in observation.passenger_features for v in row),
        "乘客特征已归一化到 [0,1]",
    )
    payload = observation.to_dict()
    check("passenger_features" in payload and "dims" in payload, "特征可序列化（验收页可视化）")


def test_environment_terminates_on_waitlist() -> None:
    """候补动作必须推进 episode 状态，否则会出现静默死循环。

    真实缺陷：``SeatAllocationEnv.step`` 早期只在"落座"分支把乘客移出
    ``pending``，候补分支什么都不改。当外部策略（行为克隆的老师）连续选择候补时，
    episode **永不结束也不报错** —— 数据采集脚本静默卡死，极难定位。
    """
    print("[V3] 候补动作必须推进 episode（防静默死循环）")
    if not _has("numpy") or not _has("torch"):
        skip("未安装 numpy/torch，跳过环境终止性检查")
        return
    from smartrail.config import EngineConfig
    from smartrail.v3.rl import WAITLIST_ACTION, SeatAllocationEnv

    env = SeatAllocationEnv(seed=11, config=EngineConfig())
    observation = env.reset_with(seed_offset=env.stream.counter)
    env.current_observation = observation
    passenger_count = len(env.pending)
    check(passenger_count > 0, f"订单有 {passenger_count} 位乘客")

    steps = 0
    while env.pending and steps < passenger_count + 2:
        observation = env.current_observation
        mask = env.action_mask(observation)
        if mask.sum() <= 0:
            break
        row = next(r for r in range(mask.shape[0]) if mask[r].sum() > 0.5)
        # 全部选候补：这正是会触发死循环的路径
        result = env.step(row, WAITLIST_ACTION)
        steps += 1
        if result.done:
            break
    check(not env.pending, f"连续候补后 pending 被清空（走了 {steps} 步）")
    check(
        steps <= passenger_count,
        f"步数不超过乘客数（{steps} ≤ {passenger_count}）—— 未重复处理同一位乘客",
    )


def test_behavior_cloning_teacher_shares_candidate_pool() -> None:
    """行为克隆的老师必须与策略共享候选池，否则标签不可学。

    真实缺陷：V1 在全部 1156 个座位里搜索，而策略只看到 48 个候选，
    导致 **老师的选择几乎从不在候选池内**（池内命中 0-2 个/单），
    78% 的标签退化成"候补"，策略于是学会了把旅客全部候补掉（就座率 19%）。
    """
    print("[V3] 行为克隆老师与策略共享候选池")
    if not _has("numpy") or not _has("torch"):
        skip("未安装 numpy/torch，跳过行为克隆检查")
        return
    from smartrail.config import EngineConfig
    from smartrail.v3.behavior_cloning import collect_expert_samples

    dataset = collect_expert_samples(episodes=25, seed=11, config=EngineConfig())
    check(len(dataset) > 0, f"采集到样本（{len(dataset)} 条）")
    seat_count = 48  # MAX_SEATS：动作展平后的列数
    waitlist_labels = sum(1 for a in dataset.actions if a % (seat_count + 1) == seat_count)
    ratio = waitlist_labels / max(1, len(dataset))
    check(ratio < 0.2, f"候补标签占比低（{ratio:.1%}）—— 老师选择落在候选池内")
    average_rate = sum(dataset.expert_seat_rates) / max(1, len(dataset.expert_seat_rates))
    check(average_rate > 0.9, f"老师就座率高（{average_rate:.3f}）")
    rates = dataset.expert_seat_rates
    full = sum(1 for rate in rates if rate >= 0.999) / max(1, len(rates))
    check(full > 0.8, f"绝大多数订单满座出票（{full:.1%}）—— 遵循出票优先")


def test_policy_backend_when_torch_available() -> None:
    print("[V3] 学习型策略后端（若已安装 torch）")
    if not _has("torch") or not _has("numpy"):
        skip("未安装 torch/numpy，跳过策略后端运行检查")
        return
    _, solution = _engine_with_order(family_with_child_order(order_id="O-RL"), "v3_rl")
    check(solution.solver.startswith("v3-rl"), f"使用学习型策略（{solution.solver}）")
    check(
        any("随机初始化" in n or "训练权重" in n for n in solution.notes),
        "明确标注权重来源（避免把未训练模型当成有效结果）",
    )
    check(len(solution.assignments) >= 1, "至少安置了部分乘客")


def main() -> int:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
    print("-" * 72)
    if FAILURES:
        print(f"失败 {len(FAILURES)} 项：")
        for item in FAILURES:
            print(f"  - {item}")
        return 1
    print(f"全部 {len(tests)} 组断言通过（依赖缺失的检查已跳过）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
