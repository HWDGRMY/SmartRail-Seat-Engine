"""人员构成的规格验收：基础分组管总人数，残疾/孕妇为独立维度。

对应业务规格"测试重点"一节，逐条守护：

1. 总人数只等于基础分组之和；
2. 儿童细分、残疾、孕妇都不参与总人数；
3. 每个年龄段各残疾/孕妇程度之和 ≤ 该年龄段基础人数；
4. 婴儿/幼儿/儿童 + 成人 0 -> 拒票；
5. 青少年 + 成人 0 -> 通过；
6. 重度残疾 + 未约重点旅客 -> 拒票；
7. 重度残疾 + 已约重点旅客 + 成人 0 -> 拒票；
8. 重度残疾 + 已约重点旅客 + 成人 1 健康 -> 通过；
9. 成人 1 本身重度残疾 + 儿童 1 重度残疾 -> 可用健康成人 0，拒票；
10. 孕妇 10 个月 + 未约重点旅客 -> 拒票；
11. 孕妇 10 个月 + 已约重点旅客 + 成人 1 -> 通过；
12. 切换订单，各分组数据隔离。

双模式：``python tests/test_composition.py`` 或 ``pytest``。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from smartrail.composition import (  # noqa: E402
    OrderComposition,
    PlatformPolicy,
    check_composition,
    validate_disability,
    validate_minor_companion,
    validate_pregnant,
)

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


def make(adult: int = 0, youth: int = 0, child: int = 0, toddler: int = 0,
         infant: int = 0, **kwargs) -> OrderComposition:
    composition = OrderComposition()
    composition.base.update(
        adult=adult, youth=youth, child=child, toddler=toddler, infant=infant
    )
    for key, value in kwargs.items():
        setattr(composition, key, value)
    return composition


def test_total_is_base_groups_only() -> None:
    """总人数只由基础分组求和；其它维度都不参与。"""
    print("[构成] 总人数只由基础分组求和")
    composition = make(adult=2, youth=1, child=3, toddler=1, infant=2)
    check(composition.total_passengers == 9, f"总人数 = 2+1+3+1+2 = {composition.total_passengers}")
    composition.child_sub["child_quiet"] = 3
    composition.disability["severe"]["adult"] = 1
    composition.pregnant["term"]["adult"] = 1
    check(composition.total_passengers == 9,
          f"加了细分/残疾/孕妇后总人数仍为 {composition.total_passengers}")
    check(composition.minors_under_14 == 6,
          f"未满14周岁 = 幼儿1+儿童3+婴儿2 = {composition.minors_under_14}")
    check(make(adult=0, youth=1).minors_under_14 == 0,
          "青少年（14岁整）不计入未满14周岁")


def test_minor_requires_adult() -> None:
    """婴儿/幼儿/儿童单独购票拒票；青少年不拒票。"""
    print("[构成] 未满14周岁必须成人陪同")
    for label, kwargs in (("婴儿", dict(infant=1)), ("幼儿", dict(toddler=1)),
                          ("儿童", dict(child=1))):
        outcome = validate_minor_companion(make(**kwargs))
        check(outcome is not None, f"{label} 单独 -> 拒票（{outcome}）")
    check(validate_minor_companion(make(adult=1, child=1)) is None, "儿童 + 成人1 -> 通过")
    check(validate_minor_companion(make(youth=1)) is None, "青少年单独 -> 通过")


def test_disability_rules() -> None:
    """重度/极重度残疾：必须重点旅客 + 每位至少 1 名可用健康成人。"""
    print("[构成] 重度/极重度残疾强制规则")
    no_service = make(adult=1)
    no_service.disability["severe"]["adult"] = 1
    check(validate_disability(no_service) is not None,
          "重度 + 未约重点旅客 -> 拒票")

    no_adult = make(adult=0)
    no_adult.disability["severe"]["child"] = 1
    no_adult.key_passenger_service = True
    check(validate_disability(no_adult) is not None,
          "重度 + 已约重点旅客 + 成人0 -> 拒票")

    ok_case = make(adult=1)
    ok_case.disability["severe"]["child"] = 1
    ok_case.key_passenger_service = True
    check(validate_disability(ok_case) is None,
          "重度 + 已约重点旅客 + 成人1健康 -> 通过")

    # 一名成人最多能陪同几位重度残疾人：按 1 位算
    two = make(adult=1)
    two.disability["severe"]["child"] = 2
    two.key_passenger_service = True
    check(validate_disability(two) is not None,
          "2 位重度残疾只有 1 名健康成人 -> 拒票")


def test_disabled_adult_cannot_companion() -> None:
    """成人本身重度残疾 -> 不能作为陪同人。"""
    print("[构成] 残疾成人不能充当陪同人")
    trap = make(adult=1, child=1)
    trap.disability["severe"]["adult"] = 1
    trap.disability["severe"]["child"] = 1
    trap.key_passenger_service = True
    check(trap.healthy_adults() == 0, f"可用健康成人 = 1 - 1 = {trap.healthy_adults()}")
    check(validate_disability(trap) is not None, "成人1重度 + 儿童1重度 -> 拒票")
    check(not check_composition(trap).ok, "整体校验为不通过")


def test_pregnancy_rules() -> None:
    """足月孕妇必须重点旅客 + 成人陪同；7-9 个月由平台策略决定。"""
    print("[构成] 孕妇强制规则")
    term_no_service = make(adult=2)
    term_no_service.pregnant["term"]["adult"] = 1
    check(validate_pregnant(term_no_service) is not None,
          "孕妇10个月 + 未约重点旅客 -> 拒票")

    term_ok = make(adult=1)
    term_ok.pregnant["term"]["adult"] = 1
    term_ok.key_passenger_service = True
    check(validate_pregnant(term_ok) is None, "孕妇10个月 + 已约重点旅客 + 成人1 -> 通过")

    early = make(adult=1, youth=1)
    early.pregnant["early"]["youth"] = 1
    check(validate_pregnant(early) is None, "孕妇1-3个月 -> 通过（不强制）")

    late = make(adult=1)
    late.pregnant["late"]["adult"] = 1
    check(validate_pregnant(late) is None, "7-9个月：默认策略下通过")
    check(validate_pregnant(late, PlatformPolicy(late_pregnancy_requires_key_service=True))
          is not None, "7-9个月：开启策略后拒票")


def test_group_consistency() -> None:
    """各年龄段残疾/孕妇之和不得超过该年龄段基础人数。"""
    print("[构成] 维度人数不得超过年龄段基础人数")
    overflow = make(adult=1)
    overflow.disability["mild"]["adult"] = 2
    problems = check_composition(overflow).errors
    check(any("残疾人数" in item for item in problems),
          f"成人残疾2 > 成人1 -> 报错（{problems}）")

    pregnant_overflow = make(adult=1)
    pregnant_overflow.pregnant["early"]["adult"] = 2
    check(any("孕妇人数" in item for item in check_composition(pregnant_overflow).errors),
          "成人孕妇2 > 成人1 -> 报错")


def test_child_sub_is_label_only() -> None:
    """儿童细分只是标签：不参与总人数，且总数不超过儿童人数。"""
    print("[构成] 儿童细分只是标签")
    tag = make(adult=1, child=2)
    tag.child_sub["child_quiet"] = 1
    tag.child_sub["child_noisy"] = 1
    check(tag.total_passengers == 3, f"总人数仍为 {tag.total_passengers}")
    bad_tag = make(adult=1, child=1)
    bad_tag.child_sub["child_quiet"] = 2
    check(any("行为细分" in item for item in check_composition(bad_tag).errors),
          "细分合计超过儿童人数 -> 报错")


def test_companion_not_counted_in_total() -> None:
    """陪同人已计入基础分组成人，不额外加总人数。"""
    print("[构成] 陪同人不额外加总人数")
    composition = make(adult=1, child=1)
    composition.disability["severe"]["child"] = 1
    composition.key_passenger_service = True
    check(composition.total_passengers == 2,
          f"1成人(陪同)+1重度残疾儿童 -> 总人数 {composition.total_passengers}")
    check(composition.required_companions() == 1,
          f"所需陪同 {composition.required_companions()}")


def test_companion_count_is_optional() -> None:
    """未登记陪同人数时不应误判为"仅登记 0 名"。"""
    print("[构成] 陪同人数为可选登记项")
    composition = make(adult=1, child=2)
    check(composition.companion_count is None, "默认未登记（None）")
    check(check_composition(composition).ok,
          "成人1 + 儿童2 未登记陪同人数 -> 仍然通过（由可用健康成人决定）")
    composition.companion_count = 5
    check(not check_composition(composition).ok, "登记 5 名陪同但只有 1 名成人 -> 报错")


def test_empty_order_rejected() -> None:
    """空单不能出票。"""
    print("[构成] 空单")
    check(not check_composition(OrderComposition()).ok, "空单不通过")


def test_orders_are_isolated() -> None:
    """多订单数据隔离：一份构成的变化不影响另一份。"""
    print("[构成] 多订单数据隔离")
    first = make(adult=2)
    first.disability["severe"]["adult"] = 1
    second = make(adult=2)
    check(first.disability["severe"]["adult"] == 1, "A 单有 1 位重度残疾成人")
    check(second.disability["severe"]["adult"] == 0, "B 单不受影响")
    check(first.base is not second.base, "两份构成的字典不是同一对象")
    check(first.disability is not second.disability, "残疾矩阵不是同一对象")


def main() -> int:
    tests = [
        test_total_is_base_groups_only,
        test_minor_requires_adult,
        test_disability_rules,
        test_disabled_adult_cannot_companion,
        test_pregnancy_rules,
        test_group_consistency,
        test_child_sub_is_label_only,
        test_companion_not_counted_in_total,
        test_companion_count_is_optional,
        test_empty_order_rejected,
        test_orders_are_isolated,
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
