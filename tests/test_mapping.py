"""人员构成 → 订单映射的验收：总人数守恒、标签不新增人、陪同靠绑定。

这是 composition 层与选座引擎之间的唯一桥梁，因此要守住两件事：

1. **总人数守恒** —— 展开后的乘客数必须等于基础分组之和。
   细分/残疾/孕妇都是给已有乘客贴标签，不能凭空多出人；
2. **陪同靠绑定而不是新增人** —— "需要 1 名成人陪同"翻译成硬绑定，
   因为那位成人已经算在基础分组成人里了。

双模式：``python tests/test_mapping.py`` 或 ``pytest``。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from smartrail.composition import OrderComposition  # noqa: E402
from smartrail.mapping import build_order  # noqa: E402
from smartrail.models import BondType, SupportNeed  # noqa: E402

UNDER_PYTEST = "PYTEST_CURRENT_TEST" in os.environ or "pytest" in sys.modules
_FAILURES: list[str] = []


def check(condition: bool, message: str) -> None:
    if condition:
        print(f"  PASS  {message}")
        return
    print(f"  FAIL  {message}")
    if UNDER_PYTEST:
        raise AssertionError(message)
    _FAILURES.append(message)


def make(**base: int) -> OrderComposition:
    composition = OrderComposition(order_id="T")
    for key in ("adult", "youth", "child", "toddler", "infant"):
        if key in base:
            composition.base[key] = base[key]
    return composition


def test_total_is_conserved() -> None:
    """基础分组之和 == 展开后的乘客数 == 总人数。"""
    print("[映射] 总人数守恒")
    for base in (
        {"adult": 2, "child": 1},
        {"adult": 1, "youth": 1, "child": 2, "toddler": 1, "infant": 1},
        {"youth": 3},
        {"adult": 1},
    ):
        mapping = build_order(make(**base))
        expected = sum(base.values())
        check(
            len(mapping.order.passengers) == expected == mapping.total_passengers,
            f"{base} -> 基础 {expected} / 展开 {len(mapping.order.passengers)}",
        )


def test_attributes_are_labels_not_people() -> None:
    """残疾/孕妇是标签：不新增人，且二者落在**不同的人**身上。"""
    print("[映射] 残疾与孕妇是标签且互不重叠")
    composition = make(adult=3, child=1)
    composition.disability["severe"]["adult"] = 1
    composition.disability["mild"]["child"] = 1
    composition.pregnant["term"]["adult"] = 1
    mapping = build_order(composition)
    check(len(mapping.order.passengers) == 4,
          f"4 人 + 标签 -> 仍是 {len(mapping.order.passengers)} 位乘客")
    for passenger in mapping.order.passengers:
        needs = ",".join(sorted(n.value for n in passenger.support_needs)) or "-"
        print(f"        {passenger.passenger_id:<14} {passenger.name:<26} {needs}")
    both = [p for p in mapping.order.passengers if "残疾" in p.name and "孕妇" in p.name]
    check(not both, f"没有人同时是残疾与孕妇（命中 {len(both)} 位）")
    disabled = [p for p in mapping.order.passengers
                if "残疾" in p.name and "adult" in p.passenger_id]
    pregnant = [p for p in mapping.order.passengers if "孕妇" in p.name]
    check(
        bool(disabled) and bool(pregnant)
        and disabled[0].passenger_id != pregnant[0].passenger_id,
        f"重度残疾成人 {disabled[0].passenger_id if disabled else '—'} 与足月孕妇 "
        f"{pregnant[0].passenger_id if pregnant else '—'} 是不同的人",
    )


def test_companion_is_bond_not_person() -> None:
    """陪同关系翻译成硬绑定，不新增乘客。"""
    print("[映射] 陪同靠硬绑定表达")
    mapping = build_order(make(adult=1, child=1))
    check(len(mapping.order.passengers) == 2,
          f"成人1+儿童1 -> {len(mapping.order.passengers)} 人")
    check(mapping.info["bond_count"] >= 1, f"生成硬绑定 {mapping.info['bond_count']} 条")
    check(all(b is BondType.MANDATORY for b in mapping.order.bonds.values()),
          "绑定强度均为 MANDATORY")


def test_support_needs_mapped() -> None:
    """婴儿/幼儿/孕晚期映射到引擎侧的支持需求。"""
    print("[映射] 支持需求映射")
    mapping = build_order(make(adult=1, infant=1, toddler=1))
    needs = {n.value for p in mapping.order.passengers for n in p.support_needs}
    check(SupportNeed.INFANT.value in needs, "婴儿带 infant 需求")
    check(SupportNeed.TODDLER.value in needs, "幼儿带 toddler 需求")

    composition = make(adult=1)
    composition.pregnant["term"]["adult"] = 1
    mapping = build_order(composition)
    needs = {n.value for p in mapping.order.passengers for n in p.support_needs}
    check(SupportNeed.PREGNANT_LATE.value in needs, "足月孕妇带 pregnant_late 需求")


def test_disabled_adult_is_not_caregiver() -> None:
    """自身重度残疾的成人不能充当陪同人。"""
    print("[映射] 残疾成人不当陪同人")
    composition = make(adult=1, child=1)
    composition.disability["severe"]["adult"] = 1
    composition.key_passenger_service = True
    mapping = build_order(composition)
    check(not mapping.check.ok, f"整体校验不通过（{mapping.check.errors}）")
    check(mapping.info["healthy_adults"] == [],
          f"可用健康成人为空（{mapping.info['healthy_adults']}）")


def test_child_behavior_labels() -> None:
    """儿童细分贴到儿童身上。"""
    print("[映射] 儿童行为细分")
    composition = make(adult=1, child=3)
    composition.child_sub["child_quiet"] = 2
    composition.child_sub["child_noisy"] = 1
    mapping = build_order(composition)
    behaviors = [
        p.declared_behavior.value for p in mapping.order.passengers
        if "child" in p.passenger_id
    ]
    check(behaviors.count("quiet") == 2, f"安静 {behaviors.count('quiet')} 位")
    check(behaviors.count("lively") == 1, f"吵闹 {behaviors.count('lively')} 位")


def test_empty_order() -> None:
    """空单展开为 0 位乘客且校验不通过。"""
    print("[映射] 空单")
    mapping = build_order(OrderComposition(order_id="EMPTY"))
    check(len(mapping.order.passengers) == 0, "空单展开为 0 位乘客")
    check(not mapping.check.ok, "空单校验不通过")


def main() -> int:
    tests = [
        test_total_is_conserved,
        test_attributes_are_labels_not_people,
        test_companion_is_bond_not_person,
        test_support_needs_mapped,
        test_disabled_adult_is_not_caregiver,
        test_child_behavior_labels,
        test_empty_order,
    ]
    if UNDER_PYTEST:
        for test in tests:
            test()
        return 0
    passed = 0
    for test in tests:
        before = len(_FAILURES)
        try:
            test()
        except AssertionError:
            continue
        if len(_FAILURES) == before:
            passed += 1
    print("-" * 72)
    if _FAILURES:
        print(f"失败 {len(_FAILURES)} 项：")
        for item in _FAILURES:
            print("  -", item)
        return 1
    print(f"全部 {passed} 组断言通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
