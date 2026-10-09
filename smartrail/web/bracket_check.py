"""JS 括号配对检查（剔除字符串、模板串与注释）。

为什么不能对整份 HTML 直接数括号：正文里的中文全角括号 ``（`` ``）``
（U+FF08 / U+FF09）会被当成 ASCII 括号统计，产生"圆括号不平衡"的假阳性。
本模块只扫描 ``<script>`` 段，并剔除字符串、模板串与注释。
"""

from __future__ import annotations

import re
from pathlib import Path

PAIRS = {")": "(", "]": "[", "}": "{"}


def check_js_brackets(text: str) -> list[str]:
    """返回问题列表（空列表表示完全配对）。"""
    if "<script>" not in text:
        return ["页面中没有 <script> 段"]
    start = text.index("<script>") + len("<script>")
    end = text.rindex("</script>")
    code = text[start:end]

    stack: list[tuple[str, int]] = []
    problems: list[str] = []
    line = 1
    index = 0
    prev = ""

    while index < len(code):
        ch = code[index]
        if ch == "\n":
            line += 1
            index += 1
            continue
        if code.startswith("//", index) and prev != ":":
            newline = code.find("\n", index)
            index = len(code) if newline < 0 else newline
            continue
        if code.startswith("/*", index):
            close = code.find("*/", index + 2)
            block = code[index : close + 2] if close >= 0 else code[index:]
            line += block.count("\n")
            index = len(code) if close < 0 else close + 2
            continue
        if ch in "\"'`":
            quote = ch
            index += 1
            while index < len(code):
                if code[index] == "\\":
                    index += 2
                    continue
                if code[index] == quote:
                    index += 1
                    break
                if code[index] == "\n":
                    line += 1
                index += 1
            prev = quote
            continue
        if ch in "([{":
            stack.append((ch, line))
        elif ch in ")]}":
            if not stack:
                problems.append(f"L{line}: 多余的 {ch}")
            else:
                opener, opened_at = stack.pop()
                if opener != PAIRS[ch]:
                    problems.append(f"L{line}: {ch} 与 L{opened_at} 的 {opener} 不匹配")
        if not ch.isspace():
            prev = ch
        index += 1

    for opener, opened_at in stack:
        problems.append(f"L{opened_at}: {opener} 未闭合")
    return problems


def check_html_tags(text: str) -> list[str]:
    """检查成对标签是否配对（只查结构标签，不查自闭合标签）。

    注意 ``</script>`` 自身包含子串 ``<script``，所以开标签必须用
    ``<script`` **且前一个字符不是 ``/``** 来计数 —— 早期直接用
    ``text.count("<script")`` 会把闭标签也数进去，报出
    "``<script>`` 开 2 个 / 闭 1 个"的假阳性。
    """
    problems: list[str] = []
    for tag in ("section", "script", "style", "main", "body", "html", "table"):
        opened = len(re.findall(rf"<{tag}[\s>]", text))
        closed = len(re.findall(rf"</{tag}\s*>", text))
        if opened != closed:
            problems.append(f"<{tag}> 开 {opened} 个 / 闭 {closed} 个")
    return problems


if __name__ == "__main__":
    page = Path(__file__).resolve().parents[1] / "smartrail" / "web" / "booking.html"
    content = page.read_text(encoding="utf-8")
    issues = check_js_brackets(content) + check_html_tags(content)
    if issues:
        print(f"发现 {len(issues)} 项问题：")
        for item in issues:
            print("  -", item)
        raise SystemExit(1)
    print("JS 括号与 HTML 标签完全配对。")
