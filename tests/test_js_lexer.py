"""页面 JS 的**词法级**检查验收（括号、模板串、正则、注释、重复声明）。

这一套检查来自两次真实事故，都属于"静态字符串检查完全免疫"的类型：

1. **坑点 22**：顶层 ``let`` 重复声明 → SyntaxError → 整个脚本不执行
   → 页面上"哪个按钮都点不动"；
2. **坑点 24**：词法扫描器不认识模板串 ``${}`` 插值与正则字面量
   → 把一个模板串当成跨 7 行、把正则当成注释开头
   → 报出一串"字符串未闭合/括号未配对"的**假阳性**。

因此本套件必须**双向验证**：该过的过（无假阳性），该报的报（无漏报）。
只验证"通过"的检查等于没有检查 —— 这是本项目反复吃过的亏。

双模式：``python tests/test_js_lexer.py`` 或 ``pytest``。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from smartrail.web.bracket_check import check_html_tags, check_js_brackets  # noqa: E402
from smartrail.web.js_lexer import scan, strip_non_code  # noqa: E402
from smartrail.web.js_lint import check_file, top_level_duplicates  # noqa: E402

UNDER_PYTEST = "PYTEST_CURRENT_TEST" in os.environ or "pytest" in sys.modules
_FAILURES: list[str] = []
WEB_DIR = ROOT / "smartrail" / "web"

#: 语法上完全合法、但朴素扫描器会误判的片段
LEGAL_SNIPPETS: tuple[tuple[str, str], ...] = (
    ("模板串插值", 'const a = `${list.map((x) => `<b>${x}</b>`).join("")}`;'),
    ("模板串里含引号", 'const a = `他说"你好"`;'),
    ("正则含双引号", 'const b = s.replace(/"/g, "&quot;");'),
    ("正则含单引号", "const c = s.replace(/'/g, '&#39;');"),
    ("正则含转义斜杠", 'const d = p.replace(/\\//g, "/");'),
    ("正则字符类含斜杠", 'const e = p.replace(/[/]/g, "_");'),
    ("除号不是正则", "const f = (a + b) / 2;"),
    ("行注释里含引号", '// 这里有一个 " 引号\nconst g = 1;'),
    ("块注释里含引号', ", '/* 这里有 \" 与 ` 与 \' */\nconst h = 1;'),
    ("return 后跟正则", "function k(){ return /x/.test(s); }"),
    ("嵌套模板串三层",
     'const i = `${a.map((y) => `${y.map((z) => `<i>${z}</i>`).join("")}`).join("")}`;'),
)


def check(condition: bool, message: str) -> None:
    if condition:
        print(f"  PASS  {message}")
        return
    print(f"  FAIL  {message}")
    if UNDER_PYTEST:
        raise AssertionError(message)
    _FAILURES.append(message)


def test_legal_snippets_are_clean() -> None:
    """合法片段不得被误报（无假阳性）。"""
    print("[词法] 合法片段无误报")
    for name, code in LEGAL_SNIPPETS:
        problems = scan(code).problems
        check(not problems, f"{name}：扫描无问题（{problems or '干净'}）")
        check(check_js_brackets(f"<script>{code}</script>") == [],
              f"{name}：括号检查通过")


def test_real_errors_are_reported() -> None:
    """真正的语法问题必须报出来（无漏报）。"""
    print("[词法] 真实问题被报出")
    check(scan('const a = "abc;').problems != [], "字符串未闭合")
    check(scan("const a = `abc;").problems != [], "模板串未闭合")
    check(scan("/* 没有结束").problems != [], "块注释未闭合")
    check(scan("const a = /abc;").problems != [], "正则未闭合")
    check(check_js_brackets("<script>function f( { return 1; }</script>") != [],
          "缺右括号")
    check(check_js_brackets("<script>const a = {1,2);</script>") != [], "括号不匹配")


def test_duplicate_declaration_rules() -> None:
    """顶层 let/const/class 重复是致命错误；var 与函数内重复合法。"""
    print("[词法] 顶层重复声明")
    check(top_level_duplicates("let a=1;\nlet a=2;") != [], "let 重复被报出")
    check(top_level_duplicates("let a=1;\nconst a=2;") != [], "let/const 同名被报出")
    check(top_level_duplicates("class A {}\nclass A {}") != [], "class 重复被报出")
    check(top_level_duplicates("var a=1;\nvar a=2;") == [],
          "var 重复合法（不报）")
    check(top_level_duplicates("function f(){ let a=1; let a=2; }") == [],
          "函数内部重复不误报（不同作用域）")


def test_template_string_counting() -> None:
    """模板串必须逐个识别 —— 曾经的根因是只认出 1 个。"""
    print("[词法] 模板串识别")
    page = WEB_DIR / "ticketing.html"
    text = page.read_text(encoding="utf-8")
    start = text.index("<script>") + len("<script>")
    end = text.rindex("</script>")
    code = text[start:end]
    result = scan(code)
    backticks = code.count("`")
    templates = sum(1 for item in result.strings if item[1] == "`")
    check(backticks % 2 == 0, f"反引号成对（共 {backticks} 个）")
    check(templates * 2 == backticks,
          f"识别出 {templates} 个模板串 = {backticks} / 2（早期只认出 1 个）")
    check(not result.problems, f"页面脚本扫描无问题（{result.problems or '干净'}）")


def test_strip_preserves_offsets() -> None:
    """strip_non_code 必须等长替换，否则按行号定位全错。"""
    print("[词法] 等长替换")
    sample = 'const a = `x${1}`;\nconst b = "y"; // c\n'
    stripped = strip_non_code(sample)
    check(len(stripped) == len(sample), f"长度不变（{len(stripped)}）")
    check(stripped.count("\n") == sample.count("\n"), "换行数不变")
    check("const" in stripped, "代码本身保留")
    check('"y"' not in stripped and "`x" not in stripped, "字符串/模板串被抹掉")


def test_all_pages_pass() -> None:
    """仓库里所有页面的 JS 必须括号配对、无顶层重复声明。"""
    print("[词法] 全部页面")
    pages = sorted(WEB_DIR.glob("*.html"))
    check(len(pages) >= 6, f"页面数 {len(pages)}")
    problems: list[str] = []
    for page in pages:
        text = page.read_text(encoding="utf-8")
        problems.extend(f"{page.name}: {p}" for p in check_js_brackets(text))
        problems.extend(f"{page.name}: {p}" for p in check_html_tags(text))
        problems.extend(check_file(page))
    unique = sorted(set(problems))
    check(not unique, f"全部页面通过（问题：{unique[:3] or '无'}）")


def test_lexer_rejects_bare_string_occupancy() -> None:
    """旁证：``str`` 是 ``Sequence[str]``，占用接口必须拦住单字符串入参。"""
    print("[词法] 字符串入参陷阱")
    from smartrail.clustering import _as_seat_ids

    try:
        _as_seat_ids("02车01A")
        check(False, "单个字符串应被拒绝")
    except TypeError as error:
        check("逐字符" in str(error), f"被拒（{error}）")
    check(_as_seat_ids(["02车01A"]) == ("02车01A",), "列表入参正常")


def main() -> int:
    tests = [
        test_legal_snippets_are_clean,
        test_real_errors_are_reported,
        test_duplicate_declaration_rules,
        test_template_string_counting,
        test_strip_preserves_offsets,
        test_all_pages_pass,
        test_lexer_rejects_bare_string_occupancy,
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
