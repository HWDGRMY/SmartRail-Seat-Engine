"""JS 括号配对检查（用词法扫描器，正确跳过字符串、模板串插值与注释）。

为什么不能对整份 HTML 直接数括号
--------------------------------
1. 正文里的中文全角括号 ``（`` ``）``（U+FF08 / U+FF09）会被当成 ASCII 括号，
   产生"圆括号不平衡"的假阳性；
2. 模板串 ``${...}`` 里可以嵌套任意 JS（包括嵌套模板串），
   朴素扫描器遇到插值就会错乱 —— 实测曾把**一个**模板串当成跨 7 行，
   导致其后所有字符串的起止位置全错，最后报出一堆假阳性。

因此这里只扫描 ``<script>`` 段，并交给 :mod:`smartrail.web.js_lexer`
做词法级处理。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# 既能作为包内模块导入（``python -m smartrail.web.bracket_check``），
# 也能直接当脚本跑（``python smartrail/web/bracket_check.py``）。
# 早期只有相对导入，直接跑脚本会 ImportError: attempted relative import
# with no known parent package —— 而验收脚本习惯上就是直接跑的。
if __package__ in (None, ""):  # pragma: no cover - 脚本模式
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from smartrail.web.js_lexer import scan
else:
    from .js_lexer import scan

PAIRS = {")": "(", "]": "[", "}": "{"}


def check_js_brackets(text: str) -> list[str]:
    """返回问题列表（空列表表示完全配对）。"""
    if "<script>" not in text or "</script>" not in text:
        return []
    start = text.index("<script>") + len("<script>")
    end = text.rindex("</script>")
    result = scan(text[start:end])

    problems = list(result.problems)
    stack: list[tuple[str, int]] = []
    for char, line in result.ordered:
        if char in "([{":
            stack.append((char, line))
        else:
            if not stack:
                problems.append(f"L{line}: 多余的 {char}")
                continue
            opener, opened_at = stack.pop()
            if opener != PAIRS[char]:
                problems.append(f"L{line}: {char} 与 L{opened_at} 的 {opener} 不匹配")
    for opener, opened_at in stack:
        problems.append(f"L{opened_at}: {opener} 未闭合")
    return problems


def check_html_tags(text: str) -> list[str]:
    """检查成对标签是否配对。

    必须**先把「非 HTML 区域」挖空**再数：

    * ``</script>`` 自身包含子串 ``<script`` —— 直接 ``count("<script")``
      会把闭标签也数进去（报"开 2 个 / 闭 1 个"）；
    * 脚本/样式里出现的标签字面量（例如检查脚本自己的提示文字）也会被误计。
    """
    masked = text
    for tag in ("script", "style"):
        masked = re.sub(
            rf"<{tag}\b[^>]*>.*?</{tag}\s*>",
            lambda m: " " * len(m.group(0)),
            masked,
            flags=re.S | re.I,
        )
    problems: list[str] = []
    for tag in ("section", "script", "style", "main", "body", "html", "table"):
        opened = len(re.findall(rf"<{tag}[\s>]", masked))
        closed = len(re.findall(rf"</{tag}\s*>", masked))
        if opened != closed:
            problems.append(f"<{tag}> 开 {opened} 个 / 闭 {closed} 个")
    return problems


def check_file(path: Path) -> list[str]:
    return check_js_brackets(path.read_text(encoding="utf-8")) + check_html_tags(
        path.read_text(encoding="utf-8")
    )


if __name__ == "__main__":
    # ``Path(__file__).parent`` 就是 web 目录自身 —— 早期写成
    # ``parents[1] / "smartrail" / "web"``，从仓库根跑能对，但换个 cwd 就会
    # 拼出 ``smartrail/smartrail/web`` 这种路径而报 FileNotFoundError。
    web_dir = Path(__file__).resolve().parent
    issues: list[str] = []
    checked = 0
    for page in sorted(web_dir.glob("*.html")):
        content = page.read_text(encoding="utf-8")
        checked += 1
        for item in check_js_brackets(content) + check_html_tags(content):
            issues.append(f"{page.name}: {item}")
    if issues:
        print(f"发现 {len(issues)} 项问题：")
        for item in issues:
            print("  -", item)
        raise SystemExit(1)
    print(f"{checked} 个页面的 JS 括号与 HTML 标签完全配对。")
