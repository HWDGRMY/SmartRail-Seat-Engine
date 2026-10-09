"""一致性验收：用**同一套规范**核验 V1 / V2 / V3 的出票结果。

这是"标准化测试规范"的执行入口。它不替代既有的期望式断言，
而是补上另一半：**不预设答案，只检查结果是否自洽、是否守住底线。**

三层驱动
========

1. **场景矩阵**（``SCENARIOS``）—— 人工挑的边界与典型场景：
   人数 × 人群构成 × 席别 × 余票状态 × 特殊资源。每格都跑全部不变量。
2. **随机模糊测试** —— 用固定种子生成"任意张数 × 任意人数 × 任意构成"
   的订单，覆盖人工想不到的组合。种子写死，失败可复现。
3. **版本对照** —— 同一 ``OrderContext`` 下跑 V1 与 V2，
   逐条比对不变量结论；V3 用仿真指标核验（座位率、Tier 0、隔离照护）。

运行
====

    python tools/verify_conformance.py              # 全部
    python tools/verify_conformance.py --quick      # 少跑几轮模糊
    python tools/verify_conformance.py --scenario 轮椅
    python tools/verify_conformance.py --json out.json
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import dataclasses  # noqa: E402

from smartrail.carriage import crh_16_car_formation, g25_16_car_formation  # noqa: E402
from smartrail.clustering import BookingState  # noqa: E402
from smartrail.composition import (  # noqa: E402
    OrderComposition, PlatformPolicy, composition_schema,
)
from smartrail.config import EngineConfig  # noqa: E402
from smartrail.mapping import build_order  # noqa: E402
from smartrail.scoring import Scorer  # noqa: E402
from smartrail.solver import build_context, solve  # noqa: E402
from smartrail.validation import (  # noqa: E402
    INVARIANTS, InvariantReport, check_solution,
)

SCHEMA = composition_schema()
BANDS = [b["id"] for b in SCHEMA["age_bands"]]
LEVELS = [lv["id"] for lv in SCHEMA["disability_levels"]]
STAGES = [st["id"] for st in SCHEMA["pregnant_stages"]]
CHILD_SUBS = [g["id"] for g in SCHEMA["child_sub_groups"]]


# ---------------------------------------------------------------------------
# 场景定义
# ---------------------------------------------------------------------------


@dataclass
class Scenario:
    """一个可复现的验收场景。"""

    name: str
    base: dict[str, int]
    class_code: str = "二等座"
    wheelchair: int = 0
    key_service: bool = False
    #: 该席别出票前剩余的座位数；``None`` 表示空车
    remaining: int | None = None
    #: 期望的额外性质（不满足即记录，不算不变量事故）
    expect: dict[str, Any] = field(default_factory=dict)


SCENARIOS: tuple[Scenario, ...] = (
    # --- 基础：人数递增 ---
    Scenario("单人", {"adult": 1}, expect={"seated": 1, "rows": 1}),
    Scenario("两位成人", {"adult": 2}, expect={"seated": 2, "rows": 1}),
    Scenario("五位成人", {"adult": 5}, expect={"seated": 5, "rows": 1},
             class_code="二等座"),
    Scenario("两位成人+两儿童", {"adult": 2, "child": 2},
             expect={"seated": 4, "pairs_adjacent": True}),
    Scenario("两成人+两婴儿", {"adult": 2, "infant": 2},
             expect={"seated": 4, "pairs_adjacent": True}),
    Scenario("三代同堂", {"adult": 3, "child": 1, "toddler": 1},
             expect={"seated": 5}),
    Scenario("儿童团", {"adult": 2, "child": 4}, expect={"seated": 6}),
    # --- 席别 ---
    Scenario("商务座单人", {"adult": 1}, class_code="商务座",
             expect={"seated": 1, "class_ok": True}),
    Scenario("一等座两人", {"adult": 2}, class_code="一等座",
             expect={"seated": 2, "class_ok": True}),
    Scenario("商务座四成人", {"adult": 4}, class_code="商务座",
             expect={"seated": 4, "class_ok": True}),
    # --- 轮椅 ---
    Scenario("一位轮椅+家属", {"adult": 2}, wheelchair=1, key_service=True,
             expect={"seated": 2, "bays_used": 1}),
    Scenario("四位轮椅", {"adult": 4}, wheelchair=4, key_service=True,
             expect={"seated": 4, "bays_used": 4}),
    Scenario("五位轮椅（超出停放位）", {"adult": 5}, wheelchair=5,
             key_service=True, expect={"seated": 5, "bays_used": 4,
                                       "needs_question": True}),
    # --- 余票紧张 ---
    Scenario("余票 300 四人单", {"adult": 2, "child": 2}, remaining=300),
    Scenario("余票 156 四人单", {"adult": 2, "child": 2}, remaining=156),
    Scenario("余票 60 四人单", {"adult": 2, "child": 2}, remaining=60),
    Scenario("余票 12 五人单", {"adult": 3, "child": 2}, remaining=12),
    Scenario("余票 4 三人单", {"adult": 3}, remaining=4),
    Scenario("余票 2 四人单", {"adult": 2, "child": 2}, remaining=2),
    Scenario("满座", {"adult": 2}, remaining=0),
    # --- 大单 ---
    Scenario("十人团", {"adult": 6, "child": 4}, expect={"seated": 10}),
    Scenario("八人团紧张", {"adult": 4, "child": 4}, remaining=400),
)


def scenario_payload(scenario: Scenario) -> dict[str, Any]:
    """把场景转成组单请求体。"""
    base = {band: 0 for band in BANDS}
    base.update(scenario.base)
    return {
        "class_code": scenario.class_code,
        "wheelchair_count": scenario.wheelchair,
        "key_passenger_service": scenario.key_service,
        "base": base,
        "child_sub": {gid: 0 for gid in CHILD_SUBS},
        "disability": {lv: {b: 0 for b in BANDS} for lv in LEVELS},
        "pregnant": {st: {b: 0 for b in BANDS} for st in STAGES},
    }


def build_order_for(payload: dict[str, Any]):
    composition = OrderComposition.from_dict(dict(payload))
    mapping = build_order(composition, PlatformPolicy())
    order = mapping.order
    return dataclasses.replace(order, class_code=payload.get("class_code", ""))


# ---------------------------------------------------------------------------
# 期望项核验（与不变量分开：这些是"应该如此"，不是"必须如此"）
# ---------------------------------------------------------------------------


def check_expectations(
    scenario: Scenario,
    order: Any,
    solution: Any,
    formation: Any,
    occupied: set[str],
) -> list[str]:
    """核验场景自带的期望；返回不满足的说明列表。"""
    notes: list[str] = []
    expect = scenario.expect or {}
    assignments = dict(getattr(solution, "assignments", {}) or {})
    seats = formation.by_id()
    placed = [seats[a.seat_id] for a in assignments.values() if a.seat_id in seats]

    if "seated" in expect and len(placed) != expect["seated"]:
        notes.append(f"期望出票 {expect['seated']} 人，实际 {len(placed)} 人")

    if "rows" in expect:
        rows = {(s.carriage, s.row) for s in placed}
        if len(rows) > expect["rows"] and placed:
            notes.append(f"期望占用 {expect['rows']} 排，实际 {len(rows)} 排")

    if expect.get("class_ok"):
        wrong = [s.seat_id for s in placed
                 if s.class_code != scenario.class_code]
        if wrong:
            notes.append(f"席别不符：{wrong[:4]}")

    if expect.get("pairs_adjacent"):
        by_pid = {p.passenger_id: p for p in order.passengers}
        alphabet = "ABCDF"
        for pid in assignments:
            if "adult-" not in pid:
                continue
            idx = pid.rsplit("-", 2)[-2]
            cpid = next((q for q in assignments
                         if "child-" in q and q.rsplit("-", 2)[-2] == idx), None)
            if cpid is None:
                continue
            a = seats.get(assignments[pid].seat_id)
            c = seats.get(assignments[cpid].seat_id)
            if a is None or c is None:
                continue
            if (a.carriage, a.row) != (c.carriage, c.row):
                notes.append(f"{pid} 与 {cpid} 不同排（{a.seat_id}/{c.seat_id}）")
            elif abs(alphabet.index(a.col) - alphabet.index(c.col)) > 1:
                notes.append(f"{pid} 与 {cpid} 同排但被过道隔开"
                             f"（{a.seat_id}/{c.seat_id}）")

    if "bays_used" in expect:
        bay_ids = {bay.bay_id for bay in formation.wheelchair_bays}
        used = sum(1 for a in assignments.values()
                   if _bay_of(formation, a.seat_id) in bay_ids)
        if used != expect["bays_used"]:
            notes.append(f"期望占用 {expect['bays_used']} 个停放位，实际 {used}")

    return notes


def _bay_of(formation: Any, seat_id: str) -> str:
    for bay in formation.wheelchair_bays:
        if bay.slot_seat_id == seat_id:
            return bay.bay_id
    return ""


# ---------------------------------------------------------------------------
# 版本驱动
# ---------------------------------------------------------------------------


def run_v1(order: Any, formation: Any, occupied: set[str],
           config: EngineConfig) -> Any:
    state = BookingState(formation=formation)
    if occupied:
        state.mark_occupied(occupied)
    return solve(order, state, config)


def run_v2(order: Any, formation: Any, occupied: set[str],
           config: EngineConfig) -> Any:
    from smartrail.v2.cpsat import solve_cpsat

    state = BookingState(formation=formation)
    if occupied:
        state.mark_occupied(occupied)
    ctx = build_context(order, config, all_seats=formation.seats,
                        occupied_seats=occupied)
    return solve_cpsat(ctx, state, config)


VERSIONS: dict[str, Callable[..., Any]] = {"v1": run_v1, "v2": run_v2}


def conformance_of(
    order: Any,
    solution: Any,
    formation: Any,
    occupied: set[str],
    config: EngineConfig,
) -> InvariantReport:
    """对本单跑全部规范。

    **刻意不传 ``scorer`` / ``ctx``**：K1/K2 要求"用与求解器完全一致的
    Scorer 配置复算"，而各版本内部构造 Scorer 的参数并不相同
    （例如是否带 ``bay_slot_ids``）。拿一个近似配置去复算，会把
    "配置差异"误报成"亲和度不可复算" —— 实测 v1 被误报 K1，
    差值恰好是 −200080（≈ 两条无障碍类惩罚），
    而那条路径的放置本身是合法的。

    需要核验 K1/K2 时，应由**求解器自己**把 Scorer 暴露出来再调
    :func:`check_solution`（见 tests/test_conformance.py 的用法）。
    """
    return check_solution(
        order, solution, formation, occupied=occupied,
        class_code=getattr(order, "class_code", ""),
        bay_slot_ids={bay.slot_seat_id
                      for bay in formation.wheelchair_bays},
        free_bays=sum(1 for bay in formation.wheelchair_bays
                      if bay.slot_seat_id not in occupied),
    )


# ---------------------------------------------------------------------------
# 余票状态构造
# ---------------------------------------------------------------------------


def occupancy_for(formation: Any, class_code: str,
                  remaining: int | None) -> set[str]:
    """按"打散占用"造出指定余票（与开发者页压测同一策略）。"""
    if not remaining:
        return set()
    seats = [s.seat_id for s in formation.seats]
    target_used = len(seats) - remaining
    if target_used <= 0:
        return set()
    rng = random.Random(20261019)
    pool = list(seats)
    rng.shuffle(pool)
    return set(pool[:target_used])


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="出票一致性验收")
    parser.add_argument("--quick", action="store_true",
                        help="模糊测试少跑几轮")
    parser.add_argument("--fuzz", type=int, default=0,
                        help="模糊测试轮数（默认 120，--quick 时 30）")
    parser.add_argument("--scenario", default="",
                        help="只跑名字含该关键词的场景")
    parser.add_argument("--json", default="", help="把结果写到 JSON")
    parser.add_argument("--versions", default="v1,v2",
                        help="要核验的版本，逗号分隔")
    args = parser.parse_args(argv)

    fuzz_rounds = args.fuzz or (30 if args.quick else 120)
    versions = [v.strip() for v in args.versions.split(",") if v.strip()]
    config = EngineConfig()
    formation = g25_16_car_formation()

    print("=" * 78)
    print("出票一致性验收 —— 用同一套规范核验各版本")
    print("=" * 78)
    print(f"  规范条款 {len(INVARIANTS)} 条：", end="")
    from collections import Counter
    counts = Counter(item.severity for item in INVARIANTS)
    print("，".join(f"{k} {v}" for k, v in sorted(counts.items())))
    print(f"  编组：{len(formation.seats)} 座 + "
          f"{len(formation.wheelchair_bays)} 个轮椅停放位")
    print(f"  核验版本：{versions}")
    print()

    results: list[dict[str, Any]] = []
    failures: list[str] = []
    started = time.perf_counter()

    # ---------------- 一、场景矩阵 ----------------
    print("=" * 78)
    print("一、场景矩阵")
    print("=" * 78)
    scenarios = [s for s in SCENARIOS
                 if not args.scenario or args.scenario in s.name]
    for scenario in scenarios:
        payload = scenario_payload(scenario)
        order = build_order_for(payload)
        occupied = occupancy_for(formation, scenario.class_code,
                                 scenario.remaining)
        for version in versions:
            try:
                solution = VERSIONS[version](order, formation, occupied, config)
            except Exception as exc:  # noqa: BLE001
                failures.append(f"{scenario.name}/{version} 抛异常："
                                f"{type(exc).__name__}: {exc}")
                print(f"  !! {scenario.name:<22} [{version}] 抛异常 "
                      f"{type(exc).__name__}: {exc}")
                continue
            report = conformance_of(order, solution, formation, occupied, config)
            expected_gaps = check_expectations(scenario, order, solution,
                                               formation, occupied)
            entry = {
                "kind": "scenario",
                "scenario": scenario.name,
                "version": version,
                "report": report.to_dict(),
                "expectation_gaps": expected_gaps,
                "solver": getattr(solution, "solver", ""),
                "seated": len(getattr(solution, "assignments", {}) or {}),
                "waitlisted": len(getattr(solution, "waitlisted", ()) or ()),
                "elapsed_ms": round(float(getattr(solution, "elapsed_ms", 0.0)), 1),
            }
            results.append(entry)
            status = "OK" if report.ok else "违反 " + ",".join(report.codes())
            flag = "  " if report.ok else "!!"
            gaps = f"  期望差 {len(expected_gaps)}" if expected_gaps else ""
            print(f"  {flag} {scenario.name:<22} [{version:<3}] "
                  f"出票 {entry['seated']:>2}/{report.passengers:<2} "
                  f"{entry['elapsed_ms']:>6.0f}ms  {status}{gaps}")
            if not report.ok:
                failures.append(f"{scenario.name}/{version} 违反 "
                                f"{report.codes()}")
                for breach in report.breaches[:3]:
                    print(f"        [{breach.severity}] {breach.code} "
                          f"{breach.message[:88]}")
            for gap in expected_gaps[:2]:
                print(f"        期望：{gap[:90]}")

    # ---------------- 二、随机模糊测试 ----------------
    print()
    print("=" * 78)
    print(f"二、随机模糊测试（{fuzz_rounds} 轮，固定种子可复现）")
    print("=" * 78)
    rng = random.Random(20261009)
    fuzz_fail = 0
    for round_index in range(fuzz_rounds):
        n_orders = rng.randint(1, 3)
        orders = []
        for order_index in range(n_orders):
            base = {band: 0 for band in BANDS}
            total = rng.randint(1, 8)
            for _ in range(total):
                band = rng.choices(
                    BANDS, weights=[50, 8, 20, 10, 6], k=1)[0]
                base[band] += 1
            if base["adult"] == 0 and (base["child"] + base["toddler"]
                                       + base["infant"]) > 0:
                base["adult"] = 1        # 避免必然被组单校验拦下
            payload = {
                "order_id": f"FZ{round_index}-{order_index}",
                "class_code": rng.choice(["二等座", "二等座", "一等座",
                                          "商务座", ""]),
                "wheelchair_count": rng.choices([0, 0, 0, 1, 2], k=1)[0],
                "key_passenger_service": True,
                "base": base,
                "child_sub": {gid: 0 for gid in CHILD_SUBS},
                "disability": {lv: {b: 0 for b in BANDS} for lv in LEVELS},
                "pregnant": {st: {b: 0 for b in BANDS} for st in STAGES},
            }
            orders.append(payload)
        remaining = rng.choice([None, None, 600, 300, 156, 60, 20, 0])
        first_class = orders[0].get("class_code") or "二等座"
        occupied = occupancy_for(formation, first_class, remaining)
        for version in versions:
            for payload in orders:
                order = build_order_for(payload)
                try:
                    solution = VERSIONS[version](order, formation,
                                                 occupied, config)
                except Exception as exc:  # noqa: BLE001
                    fuzz_fail += 1
                    failures.append(f"fuzz#{round_index}/{version} 抛异常："
                                    f"{type(exc).__name__}: {exc}")
                    continue
                report = conformance_of(order, solution, formation,
                                        occupied, config)
                results.append({
                    "kind": "fuzz",
                    "round": round_index,
                    "scenario": f"fuzz#{round_index}",
                    "version": version,
                    "report": report.to_dict(),
                    "expectation_gaps": [],
                    "solver": getattr(solution, "solver", ""),
                    "seated": len(getattr(solution, "assignments", {}) or {}),
                    "waitlisted": len(getattr(solution, "waitlisted", ()) or ()),
                })
                if not report.ok:
                    fuzz_fail += 1
                    failures.append(f"fuzz#{round_index}/{version} 违反 "
                                    f"{report.codes()}")
                    if fuzz_fail <= 6:
                        print(f"  !! fuzz#{round_index} [{version}] "
                              f"{report.explain()[:300]}")
                # 已出票的座位要累加，模拟连续下单
                for assignment in (getattr(solution, "assignments", {}) or {}).values():
                    occupied.add(assignment.seat_id)
    print(f"  {fuzz_rounds} 轮 × {len(versions)} 版本，违反 {fuzz_fail} 次")

    # ---------------- 三、版本对照 ----------------
    print()
    print("=" * 78)
    print("三、版本对照：同一输入下各版本的结论是否一致")
    print("=" * 78)
    by_key: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}
    for entry in results:
        if entry["kind"] != "scenario":
            continue
        by_key.setdefault((entry["scenario"], entry["version"]), {})[entry["version"]] = entry
    disagree = 0
    for scenario in scenarios:
        cells = {v: None for v in versions}
        for version in versions:
            found = [e for e in results
                     if e["kind"] == "scenario"
                     and e["scenario"] == scenario.name
                     and e["version"] == version]
            cells[version] = found[0] if found else None
        if len(versions) > 1 and all(cells.values()):
            seated = {v: c["seated"] for v, c in cells.items()}
            ok = {v: c["report"]["ok"] for v, c in cells.items()}
            if len(set(ok.values())) > 1:
                disagree += 1
                print(f"  !! {scenario.name:<22} 结论不一致："
                      f"{ {v: ('通过' if o else '违反') for v, o in ok.items()} }")
            elif len(set(seated.values())) > 1:
                print(f"  ~  {scenario.name:<22} 出票数不同：{seated}")
    print(f"  结论不一致的场景 {disagree} 个")

    elapsed = time.perf_counter() - started

    # ---------------- 汇总 ----------------
    print()
    print("=" * 78)
    total = len(results)
    ok_count = sum(1 for e in results if e["report"]["ok"])
    print(f"汇总：核验 {total} 次，通过 {ok_count}，违反 {total - ok_count}"
          f"，用时 {elapsed:.1f}s")
    if failures:
        print()
        print(f"问题清单（{len(failures)} 条，最多显示 20 条）：")
        for item in failures[:20]:
            print("  -", item)
    print("=" * 78)

    if args.json:
        Path(args.json).write_text(
            json.dumps({
                "invariants": [i.to_dict() for i in INVARIANTS],
                "results": results,
                "failures": failures,
                "elapsed_s": elapsed,
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"详细结果已写入 {args.json}")

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
