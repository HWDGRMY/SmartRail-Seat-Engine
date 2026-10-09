"""检查页面 JS 的**顶层重复声明**（会造成 SyntaxError 让整个脚本不执行）。

为什么需要这个检查
------------------
``let`` / ``const`` / ``class`` 在同一个作用域重复声明是 **SyntaxError**，
浏览器会拒绝执行**整个 ``<script>``**。表现是"页面上哪个按钮都点不动"，
而且 ```` 页面上完全看不出来，只在控制台报一行错。

本项目的静态检查（字符串在不在、括号配不配）**抓不到它** ——
括号是配对的、字符串也都在，只是脚本根本不运行。
``tools/check_page_js.py`` 用 Node 真跑一遍能抓到，但需要 Node；
本模块是纯 Python 的快速前置检查，两者互补。

判定要点
--------
* 只统计**顶层**（花括号深度为 0）的声明 —— 函数内部的同名变量是合法的；
* ``var`` 与 ``function`` 允许重复声明，只有 ``let`` / ``const`` / ``class`` 不行；
* 声明列表（``let a = 1, b = 2``）里每个名字都要算。
"""

from __future__ import annotations

import re
from pathlib import Path

# 只有这三类重复声明才是 SyntaxError
_STRICT = ("let", "const", "class")
_DECL = re.compile(
    r"\b(let|const|var|class)\s+([A-Za-z_$][\w$]*(?:\s*,\s*[A-Za-z_$][\w$]*)*)"
)


def _strip_non_code(code: str) -> str:
    """剔除字符串、模板串与注释，避免把文本里的词当成声明。"""
    out: list[str] = []
    index = 0
    length = len(code)
    while index < length:
        char = code[index]
        if code.startswith("//", index):
            newline = code.find("\n", index)
            index = length if newline < 0 else newline
            continue
        if code.startswith("/*", index):
            close = code.find("*/", index + 2)
            index = length if close < 0 else close + 2
            continue
        if char in "\"'`":
            quote = char
            index += 1
            while index < length:
                if code[index] == "\\":
                    index += 2
                    continue
                if code[index] == quote:
                    index += 1
                    break
                index += 1
            out.append(" ")  # 用空格占位，保持偏移
            continue
        out.append(char)
        index += 1
    return "".join(out)


def top_level_duplicates(code: str) -> list[tuple[str, str, int]]:
    """返回 ``[(名字, 声明关键字, 行号), ...]``，只含真正非法的重复。"""
    stripped = _strip_non_code(code)
    seen: dict[str, tuple[str, int]] = {}
    duplicates: list[tuple[str, str, int]] = []

    depth = 0
    line = 1
    index = 0
    while index < len(stripped):
        char = stripped[index]
        if char == "\n":
            line += 1
            index += 1
            continue
        if char in "{([":
            depth += 1
            index += 1
            continue
        if char in "})]":
            depth = max(0, depth - 1)
            index += 1
            continue
        if depth == 0:
            match = _DECL.match(stripped, index)
            if match and (index == 0 or not (stripped[index - 1].isalnum()
                                             or stripped[index - 1] in "_$")):
                keyword = match.group(1)
                names = [piece.strip() for piece in match.group(2).split(",")]
                for name in names:
                    if not name:
                        continue
                    if keyword in _STRICT and name in seen:
                        duplicates.append((name, keyword, line))
                    else:
                        seen.setdefault(name, (keyword, line))
                index = match.end()
                continue
        index += 1
    return duplicates


def check_file(path: Path) -> list[str]:
    """检查一个 HTML 文件内 ``<script>`` 段的顶层重复声明。"""
    text = path.read_text(encoding="utf-8")
    if "<script>" not in text or "</script>" not in text:
        return []
    script = text[text.index("<script>") + len("<script>"):text.rindex("</script>")]
    problems: list[str] = []
    for name, keyword, line in top_level_duplicates(script):
        problems.append(
            f"{path.name}: 顶层 {keyword} 重复声明 '{name}'（脚本第 {line} 行）"
            f" —— 会导致 SyntaxError，整个脚本不执行"
        )
    return problems


if __name__ == "__main__":
    web = Path(__file__).resolve().parent
    issues: list[str] = []
    for page in sorted(web.glob("*.html")):
        issues.extend(check_file(page))
    if issues:
        print(f"发现 {len(issues)} 项问题：")
        for item in issues:
            print("  -", item)
        raise SystemExit(1)
    print("所有页面脚本均无顶层重复声明。")
