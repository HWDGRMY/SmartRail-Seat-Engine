"""OrderEditor 页面结构的验收：模块划分、规则体现、数据隔离。

对应规格第七节的模块化 UI 结构：
``OrderSwitcher / BaseGroup / ChildSubGroup / DisabilityGroup / PregnantGroup /
CompanionCounter / ServiceGroup / OrderSummary / OrderActions``。

这里只做**静态结构**校验（不启浏览器）；真实 HTTP 行为由
``tools/verify_order_page.py`` 覆盖。

双模式：``python tests/test_order_editor.py`` 或 ``pytest``。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from smartrail.web.bracket_check import check_html_tags, check_js_brackets  # noqa: E402
from smartrail.web.js_lint import check_file as lint_js_file  # noqa: E402

UNDER_PYTEST = "PYTEST_CURRENT_TEST" in os.environ or "pytest" in sys.modules
_FAILURES: list[str] = []
PAGE = ROOT / "smartrail" / "web" / "booking.html"
WEB_DIR = ROOT / "smartrail" / "web"


def check(condition: bool, message: str) -> None:
    if condition:
        print(f"  PASS  {message}")
        return
    print(f"  FAIL  {message}")
    if UNDER_PYTEST:
        raise AssertionError(message)
    _FAILURES.append(message)


def test_brackets_and_tags() -> None:
    """JS 括号与 HTML 标签必须配对。

    刻意**不**对整份 HTML 直接数 ASCII 括号：正文里的中文全角括号
    ``（`` ``）`` 会被误统计，产生"圆括号不平衡"的假阳性。
    """
    print("[编辑器] 结构配对")
    html = PAGE.read_text(encoding="utf-8")
    issues = check_js_brackets(html)
    check(not issues, f"JS 括号配对（问题：{issues[:3] or '无'}）")
    tag_issues = check_html_tags(html)
    check(not tag_issues, f"结构标签配对（问题：{tag_issues[:3] or '无'}）")


def test_all_modules_present() -> None:
    """规格第七节的九个模块都要存在。"""
    print("[编辑器] 模块划分")
    html = PAGE.read_text(encoding="utf-8")
    for testid, label in (
        ("orderSwitcher", "OrderSwitcher 订单切换"),
        ("base-group", "BaseGroup 基础分组"),
        ("child-sub-group", "ChildSubGroup 儿童细分"),
        ("disability-group", "DisabilityGroup 残疾分组"),
        ("pregnant-group", "PregnantGroup 孕妇分组"),
        ("companion", "CompanionCounter 陪同人数"),
        ("service-group", "ServiceGroup 重点旅客与服务"),
        ("order-summary", "OrderSummary 汇总"),
        ("order-actions", "OrderActions 操作区"),
    ):
        check(testid in html, label)


def test_rules_reflected_in_page() -> None:
    """关键规则必须在前端体现。"""
    print("[编辑器] 规则体现")
    html = PAGE.read_text(encoding="utf-8")
    for label, ok in (
        ("总人数只由基础分组求和", "totalPassengers" in html),
        ("未满14周岁 = 婴儿+幼儿+儿童", "minorsUnder14" in html),
        ("重度/极重度判定", "severeCount" in html),
        ("足月孕妇判定", "termCount" in html),
        ("可用健康成人", "healthyAdults" in html),
        ("所需陪同", "requiredCompanions" in html),
        ("校验走后端同一套纯函数", "/api/composition/check" in html),
        ("提交走构成接口", "/api/composition/submit" in html),
        ("字段定义单一来源", "/api/composition/schema" in html),
    ):
        check(ok, label)


def test_data_isolation_constructs() -> None:
    """前端要有保证"多订单数据隔离"的构造。"""
    print("[编辑器] 数据隔离")
    html = PAGE.read_text(encoding="utf-8")
    check("emptyComposition" in html, "独立的空构成构造器（每单一份）")
    check("JSON.parse(JSON.stringify" in html, "复制订单用深拷贝，避免共享引用")
    check("activeCompose" in html, "有当前订单索引")
    check("trimDimensions" in html, "基础人数减少时同步裁剪维度计数（防不自洽）")


def test_no_top_level_redeclaration() -> None:
    """页面脚本不得有顶层重复声明。

    这条来自一次**真实事故**：``booking.html`` 里 ``presetCatalog`` 与
    ``concurrentCar`` 各被 ``let`` 声明了两次，构成 SyntaxError，
    浏览器拒绝执行**整个 <script>** —— 用户看到的是"哪个按钮都点不动"。

    当时的静态检查（字符串在不在、括号配不配）**全部通过**：括号是配对的、
    字符串也都在，唯独脚本一行都没运行。只有真正执行 JS 才能发现。
    """
    print("[编辑器] 顶层重复声明（会导致整个脚本不执行）")
    problems: list[str] = []
    for page in sorted(WEB_DIR.glob("*.html")):
        problems.extend(lint_js_file(page))
    check(not problems, f"四个页面均无顶层重复声明（问题：{problems[:2] or '无'}）")


def test_every_referenced_id_exists() -> None:
    """JS 里 ``$("id")`` 引用的元素必须真的存在于 HTML 中。

    缺 id 会让 ``$("x").onclick = ...`` 抛 TypeError，后续的按钮绑定
    全部中断 —— 表现同样是"按钮点不动"，但只影响它之后的那一段。
    """
    print("[编辑器] JS 引用的 id 都存在")
    import re

    html = PAGE.read_text(encoding="utf-8")
    ids = set(re.findall(r'id="([^"]+)"', html))
    script = html[html.index("<script>") + len("<script>"):html.rindex("</script>")]
    referenced = set(re.findall(r'\$\("([^"]+)"\)', script))
    referenced |= set(re.findall(r'getElementById\("([^"]+)"\)', script))
    missing = sorted(referenced - ids)
    check(not missing, f"引用的 id 全部存在（缺失：{missing[:5] or '无'}）")


def main() -> int:
    tests = [
        test_brackets_and_tags,
        test_no_top_level_redeclaration,
        test_every_referenced_id_exists,
        test_all_modules_present,
        test_rules_reflected_in_page,
        test_data_isolation_constructs,
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
