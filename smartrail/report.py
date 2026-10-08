"""生成验收页面的数据快照（离线可用）。

为什么需要"预生成快照"
----------------------
验收页面必须满足两个约束：

1. **离线可直接打开**：用户双击 HTML 就能看，不需要起服务、不需要装 torch/ortools；
2. **数据必须真实**：页面上展示的对比数字、学习曲线、座位图都来自真实运行，
   不允许为了好看而硬编码。

做法：把"真实跑出来"的结果序列化成 JSON，注入到 HTML 的一个 ``<script>`` 里；
页面启动时先尝试 ``/api/compare`` 拿实时数据，失败则回退到内嵌快照，并明确标注
当前展示的是"快照"还是"实时"。

用法
----
    python -m smartrail.report --steps 40 --seed 7
    python -m smartrail.report --no-compare      # 只生成座位图与曲线快照
"""

from __future__ import annotations

import argparse
import json
import platform
import sys
import time
from pathlib import Path
from typing import Any

ARTIFACT_DIR = Path("artifacts")
SNAPSHOT_NAME = "acceptance_snapshot.json"
SNAPSHOT_MARKER = "__SNAPSHOT_JSON__"
TICKET_FIRST_MARKER = "__TF_SNAPSHOT__"


def _scenario_snapshot() -> dict[str, Any]:
    """三种引擎在同一场景（带娃家庭）上的座位图与代价明细。"""
    from .api import service
    from .v2 import available_backends, solve_with
    from .v2.registry import missing_modules

    engine = service.create_engine("crh16", service.CreditLedger())
    payload = {
        "order_id": "SNAPSHOT-FAMILY",
        "relation": "nuclear_family",
        "passengers": [
            {"passenger_id": "A1", "age": 36, "name": "家长A"},
            {"passenger_id": "A2", "age": 34, "name": "家长B"},
            {"passenger_id": "C1", "age": 5, "ticket_type": "child", "declared_behavior": "lively"},
        ],
        "bonds": [
            {"a": "A1", "b": "C1", "bond": "mandatory"},
            {"a": "A1", "b": "A2", "bond": "strong"},
            {"a": "A2", "b": "C1", "bond": "strong"},
        ],
    }
    order = service.order_from_payload(payload)
    backends = available_backends()
    runs: list[dict[str, Any]] = []
    for name in ("v1_heuristic", "v2_exact", "v2_cpsat", "v3_rl"):
        info = backends.get(name)
        if info is None:
            continue
        missing = missing_modules(tuple(info["requires"]))  # type: ignore[arg-type]
        if missing:
            runs.append({"backend": name, "skipped": True, "reason": f"缺少依赖：{', '.join(missing)}"})
            continue
        fresh = service.create_engine("crh16", service.CreditLedger())
        prepared = fresh.prepare_order(order)
        try:
            solution = solve_with(name, prepared, fresh.state, fresh.config, mode="smart")
        except Exception as error:  # pragma: no cover
            runs.append({"backend": name, "skipped": True, "reason": f"{type(error).__name__}: {error}"})
            continue
        body = solution.to_dict()
        body.update({"backend": name, "skipped": False})
        runs.append(body)

    return {
        "scenario": "family_with_child",
        "order": payload,
        "snapshot": _compact_snapshot(engine.snapshot()),
        "runs": runs,
    }


def _compact_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    """压缩座位图快照：只保留渲染必需的字段。

    完整快照含 1156 个座位的十几个字段（约 400KB）。页面在离线模式下用这份
    精简版本来着色（占用/静音/无障碍），需要更细的信息时再走 API。
    """
    return {
        "train_code": snapshot["train_code"],
        "carriages": snapshot["carriages"],
        "availability_ratio": snapshot["availability_ratio"],
        "occupied_count": snapshot["occupied_count"],
        "total_seats": snapshot["total_seats"],
        "latency": snapshot["latency"],
        "seats": [
            {
                "i": seat["seat_id"],
                "c": seat["carriage"],
                "r": seat["row"],
                "o": seat["col"],
                "q": seat["quiet"],
                "a": seat["accessible"],
                "x": seat["occupied"],
            }
            for seat in snapshot["seats"]
        ],
    }


def _ticket_first_scenarios() -> list[dict[str, Any]]:
    """构造"出票优先"演示场景：从宽松到极端，展示无相邻座位时如何仍出票。

    每个场景都记录：剩余空位、车厢内是否还存在相邻空位、分配结果、相邻结论、
    面向旅客/乘务员的提示。这些数据全部来自**真实求解**，页面只做渲染。
    """
    from . import crh_16_car_formation
    from .engine import SeatEngine
    from .fixtures import pregnant_order, wheelchair_order
    from .notices import adjacent_free_pair_in_carriage

    family_payload = {
        "order_id": "TF-FAMILY",
        "relation": "nuclear_family",
        "passengers": [
            {"passenger_id": "A1", "age": 36, "name": "家长A"},
            {"passenger_id": "A2", "age": 34, "name": "家长B"},
            {"passenger_id": "C1", "age": 5, "ticket_type": "child",
             "declared_behavior": "lively", "name": "儿童C"},
        ],
        "bonds": [
            {"a": "A1", "b": "C1", "bond": "mandatory"},
            {"a": "A1", "b": "A2", "bond": "strong"},
            {"a": "A2", "b": "C1", "bond": "strong"},
        ],
    }

    from .api import service

    couple_payload = {
        "order_id": "TF-COUPLE",
        "relation": "couple",
        "passengers": [
            {"passenger_id": "H1", "age": 32, "name": "旅客H"},
            {"passenger_id": "H2", "age": 31, "name": "旅客I"},
        ],
        "bonds": [{"a": "H1", "b": "H2", "bond": "strong"}],
    }

    def occupy_scatter(engine: SeatEngine, ratio: float) -> None:
        """按 ratio 隔位占座，制造不同程度的"空位零散化"。"""
        if ratio <= 0:
            return
        step = max(2, int(round(1.0 / ratio)))
        for index, seat in enumerate(engine.formation.seats):
            if index % step != 0:
                engine.state.occupied.add(seat.seat_id)

    def occupy_all_adjacency(engine: SeatEngine) -> None:
        """占掉每一对相邻座位中的一个：车厢内不再存在任何相邻空位。"""
        for seat in engine.formation.seats:
            if seat.col_index % 2 == 1 or seat.row % 2 == 1:
                engine.state.occupied.add(seat.seat_id)

    def fill_accessible(engine: SeatEngine) -> None:
        for seat in engine.formation.seats:
            if seat.in_accessible_zone():
                engine.state.occupied.add(seat.seat_id)

    factory = {
        "family": lambda: service.order_from_payload(family_payload),
        "couple": lambda: service.order_from_payload(couple_payload),
        "wheelchair": wheelchair_order,
        "pregnant": pregnant_order,
    }
    definitions = [
        ("夫妻同行（可满足相邻）", "couple", 0.0, None),
        ("带娃三人（空车起售）", "family", 0.0, None),
        ("半车（空位开始零散）", "family", 0.6, None),
        ("零散空位（无任何相邻）", "family", 0.0, occupy_all_adjacency),
        ("无障碍专区售罄", "wheelchair", 0.0, fill_accessible),
        ("孕晚期无相邻座位", "pregnant", 0.0, occupy_all_adjacency),
    ]

    results: list[dict[str, Any]] = []
    for title, kind, ratio, effect in definitions:
        engine = SeatEngine(formation=crh_16_car_formation())
        occupy_scatter(engine, ratio)
        if effect is not None:
            effect(engine)
        order = factory[kind]()
        prepared = engine.prepare_order(order)
        solution = engine.book(prepared, mode="smart").solution

        free_seats = list(engine.state.available_seats)
        seated = {
            pid: engine.formation.seat(a.seat_id)
            for pid, a in solution.assignments.items()
        }
        carriages_in_use = {seat.carriage for seat in seated.values()}
        results.append(
            {
                "title": title,
                "kind": kind,
                "occupancy": round(
                    1.0 - len(free_seats) / max(1, len(engine.formation.seats)), 3
                ),
                "free_seats": len(free_seats),
                "adjacent_pair_available": adjacent_free_pair_in_carriage(
                    free_seats, set(), 1
                ),
                "adjacency_level": solution.adjacency.level,
                "assignments": [
                    {
                        "passenger_id": pid,
                        "seat_id": seat.seat_id,
                        "carriage": seat.carriage,
                        "row": seat.row,
                        "col": seat.col_index,
                        "quiet": seat.is_quiet_carriage,
                        "accessible": seat.in_accessible_zone(),
                    }
                    for pid, seat in sorted(seated.items())
                ],
                "waitlisted": list(solution.waitlisted),
                "notices": [n.to_dict() for n in solution.notices],
                "notes": list(solution.notes),
                "total_cost": round(solution.total_cost, 1),
                "tier0": len([v for v in solution.violations if v.tier == 0]),
                "elapsed_ms": round(solution.elapsed_ms, 2),
                # 注意：**不再**为每个场景重复 1156 个座位的数据（6 份重复会让
                # 快照膨胀到 500KB）。座位布局与占座情况由顶层
                # ``layout`` / ``occupied_seats`` 统一提供。
                "occupied_seats": sorted(engine.state.occupied),
            }
        )
    return results


def ticket_first_layout() -> dict[str, Any]:
    """座位布局的**唯一一份**描述（座位 + 车厢），供页面共用。

    6 个演示场景的编组完全相同，若每个场景都带一份 1156 座的列表，
    快照会膨胀到 500KB（实测）。因此布局只存一份，场景只存差异。
    """
    from . import crh_16_car_formation

    formation = crh_16_car_formation()
    return {
        "train_code": formation.train_code,
        "seats": [
            {
                "i": seat.seat_id,
                "c": seat.carriage,
                "r": seat.row,
                "o": seat.col_index,
                "q": seat.is_quiet_carriage,
                "a": seat.in_accessible_zone(),
            }
            for seat in formation.seats
        ],
        "carriages": [
            {
                "number": car.number,
                "quiet": car.is_quiet_carriage,
                "accessible": car.has_accessible_zone,
                "rows": car.rows,
                "class_code": car.class_code,
                "columns": list(car.columns),
            }
            for car in formation.carriages
        ],
    }


def build_snapshot(steps: int = 40, seed: int = 7, with_compare: bool = True) -> dict[str, Any]:
    from .v3.evaluate import ComparisonRequest, run_comparison
    from .v3.simulator import RulePolicy, run_policy

    snapshot: dict[str, Any] = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "scenario": _scenario_snapshot(),
        "ticket_first": _ticket_first_scenarios(),
        "ticket_first_layout": ticket_first_layout(),
    }
    # 单策略仿真（不需要 torch，用于学习曲线对比基线）
    snapshot["baseline_simulation"] = run_policy(
        RulePolicy("v1_heuristic"), steps=steps, seed=seed
    )
    if with_compare:
        result = run_comparison(ComparisonRequest(steps=steps, seed=seed))
        snapshot["comparison"] = result.to_dict()
    from .api.service import training_curve

    snapshot["training_curve"] = training_curve()
    return snapshot


def write_snapshot(snapshot: dict[str, Any], target: Path) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


def restore_placeholder(page: Path, marker: str = SNAPSHOT_MARKER) -> bool:
    """把已注入的快照还原成占位标记（让注入可重复执行）。

    ``inject_into_page`` 是**幂等**的：先把 ``<script id="snapshot-data">`` 里的
    内容换成标记，再写入新快照。否则第二次注入会因为找不到标记而失败
    （开发中真实踩到过）。
    """
    return _restore_marker(page, r'<script id="snapshot-data" type="application/json">', marker)


def _restore_marker(page: Path, script_open: str, marker: str) -> bool:
    import re

    if not page.exists():
        return False
    text = page.read_text(encoding="utf-8")
    pattern = re.compile(
        re.escape(script_open) + r"(.*?)(</script>)",
        re.DOTALL,
    )
    replaced, count = pattern.subn(
        lambda match: f"{script_open}{marker}{match.group(2)}", text
    )
    if count:
        page.write_text(replaced, encoding="utf-8")
    return bool(count)


def inject_into_page(
    snapshot: dict[str, Any], page: Path, marker: str = SNAPSHOT_MARKER
) -> bool:
    """把快照注入 HTML（幂等：先还原占位标记再写入）。"""
    if not page.exists():
        return False
    restore_placeholder(page, marker)
    text = page.read_text(encoding="utf-8")
    if marker not in text:
        return False
    payload = json.dumps(snapshot, ensure_ascii=False)
    page.write_text(text.replace(marker, payload), encoding="utf-8")
    return True


def inject_ticket_first(
    snapshot: dict[str, Any],
    page: Path | None = None,
    marker: str = TICKET_FIRST_MARKER,
) -> bool:
    """把"出票优先"场景数据注入 ``ticket-first.html``（幂等）。"""
    target = page or (Path("smartrail") / "web" / "ticket-first.html")
    if not target.exists():
        return False
    script_open = '<script id="tf-data" type="application/json">'
    _restore_marker(target, script_open, marker)
    text = target.read_text(encoding="utf-8")
    if marker not in text:
        return False
    payload = json.dumps(
        {
            "generated_at": snapshot.get("generated_at"),
            "python": snapshot.get("python"),
            "platform": snapshot.get("platform"),
            "layout": snapshot.get("ticket_first_layout", {}),
            "scenarios": snapshot.get("ticket_first", []),
        },
        ensure_ascii=False,
    )
    target.write_text(text.replace(marker, payload), encoding="utf-8")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成可视化页面的数据快照")
    parser.add_argument("--steps", type=int, default=40)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--no-compare", action="store_true")
    parser.add_argument(
        "--inject", action="store_true", help="把快照注入验收页面与出票优先页面"
    )
    args = parser.parse_args(argv)

    snapshot = build_snapshot(args.steps, args.seed, with_compare=not args.no_compare)
    target = write_snapshot(snapshot, ARTIFACT_DIR / SNAPSHOT_NAME)
    print(f"快照已写入：{target}（{target.stat().st_size} 字节）")

    if args.inject:
        acceptance = Path("smartrail/web/acceptance.html")
        done = inject_into_page(snapshot, acceptance)
        print(f"注入验收台：{acceptance} -> {'成功' if done else '未找到占位标记'}")
        ticket_first = Path("smartrail/web/ticket-first.html")
        done2 = inject_ticket_first(snapshot, ticket_first)
        print(f"注入出票优先页：{ticket_first} -> {'成功' if done2 else '未找到占位标记'}")

    runs = snapshot["scenario"]["runs"]
    for run in runs:
        if run.get("skipped"):
            print(f"  {run['backend']:<14} 跳过：{run['reason']}")
        else:
            print(
                f"  {run['backend']:<14} 代价={run['total_cost']:>9.1f} "
                f"就座={len(run['assignments'])} 求解器={run['solver']}"
            )
    print("出票优先场景：")
    for scenario in snapshot.get("ticket_first", []):
        print(
            f"  {scenario['title']:<22} 出票={len(scenario['assignments'])} "
            f"拒票={len(scenario['waitlisted'])} "
            f"相邻={scenario['adjacency_level']:<11} Tier0={scenario['tier0']}"
        )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
