"""极简 JS 词法扫描器：正确跳过字符串、**模板串插值**、注释与正则字面量。

为什么需要它
------------
页面脚本里有大量模板串，而模板串可以嵌套任意 JS：

.. code-block:: javascript

   `${items.map((x) => `<b>${x}</b>`).join("")}`

早期用一个"见到反引号就找下一个反引号"的朴素扫描器，遇到 ``${}`` 就崩了 ——
它把第一个模板串从 L82 一路吞到 L90（跨了 7 行、吞掉了中间所有真实代码），
于是**后面每一个字符串的起止位置全错**，最终报出一堆
"字符串未闭合 / 括号未配对"的假阳性。实测：朴素扫描器只识别出
**1 个**模板串，而源码里有 **74 个反引号**。

修好模板串之后还剩一个坑：**正则字面量**。这一行

.. code-block:: javascript

   .replace(/"/g, "&quot;")

里面的 ``/"/g`` 是正则。不识别正则的扫描器会把 ``/&/g`` 的第一个 ``/``
当成注释开头，**整行被当注释吞掉**，于是这一行真实的引号全部错位 ——
又报出一串假阳性。

正则与除号的分歧靠"前一个有效记号"判断：

* 前面是标识符/数字/``)``/``]``/``}`` → 这是**除号**；
* 其它情况（``(`` ``,`` ``=`` ``return`` 等）→ 这是**正则开头**。

本模块被 :mod:`smartrail.web.bracket_check` 与 :mod:`smartrail.web.js_lint`
共用，避免两处各写一份、各错一处。
"""

from __future__ import annotations

from dataclasses import dataclass, field

_QUOTES = "\"'`"

#: 这些记号**产生一个值**，紧跟其后的 ``/`` 是除号。
#:
#: 反例（不产生值，所以 ``/`` 是正则开头）：``=`` ``,`` ``(`` ``[`` ``{``
#: ``return`` ``typeof`` 等。
#:
#: 早期版本把"标识符"一律当成值，于是 ``const a = /abc/`` 里的 ``/``
#: 被当成除号 —— 而 ``a`` 确实是标识符，但它后面隔着 ``=``，
#: 所以判断依据必须是**紧邻的前一个记号**，不能是"前面出现过标识符"。
_VALUE_TOKENS = frozenset({")", "]", "}", "value", "regex"})

#: 这些关键字之后可以跟正则（``return /x/``），因此不算"值"
_KEYWORDS_BEFORE_REGEX = frozenset({
    "return", "typeof", "instanceof", "in", "of", "new", "delete", "void",
    "throw", "case", "do", "else", "yield", "await",
})

#: 这些记号之后**不应该**出现正则；维持现状即可
_NEUTRAL_TOKENS = frozenset({"(", "[", "{", ",", ";", ":"})

_QUOTES = "\"'`"


@dataclass
class ScanResult:
    """扫描结果。"""

    ordered: list[tuple[str, int]] = field(default_factory=list)
    """按出现顺序记录的括号 ``(字符, 行号)``，**只含真正的代码括号**。"""

    problems: list[str] = field(default_factory=list)
    """扫描期发现的问题（未闭合的字符串/模板串/注释）。"""

    strings: list[tuple[int, str, int]] = field(default_factory=list)
    """已识别的字符串 ``(起始行, 引号, 结束行)``，便于自检。"""


def scan(code: str) -> ScanResult:
    """扫描 JS 代码，返回代码态括号序列与扫描期问题。"""
    result = ScanResult()
    index = 0
    length = len(code)
    line = 1
    # 用显式栈驱动：模板串里遇到 ${ 时压栈，回到代码态
    template_stack: list[int] = []   # 记录模板串起始行
    brace_depths: list[int] = []     # 每个模板串插值内的花括号深度
    prev_token = ""                  # 前一个有效记号（用于区分正则与除号）

    def advance(count: int) -> None:
        nonlocal index, line
        end = min(index + count, length)
        line += code.count("\n", index, end)
        index = end

    while index < length:
        char = code[index]

        # --- 注释 ---
        if code.startswith("//", index):
            newline = code.find("\n", index)
            index = length if newline < 0 else newline
            continue
        if code.startswith("/*", index):
            close = code.find("*/", index + 2)
            if close < 0:
                result.problems.append(f"L{line}: 块注释 /* 未闭合")
                break
            advance(close + 2 - index)
            continue

        # --- 正则字面量 vs 除号 ---
        if char == "/" and prev_token not in _VALUE_TOKENS:
            start_line = line
            advance(1)
            in_class = False
            closed = False
            while index < length:
                current = code[index]
                if current == "\\":
                    advance(2)
                    continue
                if current == "\n":
                    break
                if current == "[":
                    in_class = True
                elif current == "]":
                    in_class = False
                elif current == "/" and not in_class:
                    advance(1)
                    closed = True
                    break
                advance(1)
            if not closed:
                result.problems.append(
                    f"L{start_line}: 正则字面量未闭合（该行结束前没有配对的 /）"
                )
                break
            # 跳过标志位 gimsuy
            while index < length and code[index].isalpha():
                advance(1)
            prev_token = "regex"
            continue

        # --- 模板串：不在插值里时，按模板串规则扫描 ---
        if char == "`":
            start_line = line
            advance(1)
            closed = False
            while index < length:
                current = code[index]
                if current == "\\":
                    advance(2)
                    continue
                if current == "`":
                    advance(1)
                    closed = True
                    break
                if code.startswith("${", index):
                    # 插值开始：记录位置，回到"代码态"
                    template_stack.append(start_line)
                    brace_depths.append(0)
                    advance(2)
                    closed = None  # 交回主循环继续处理
                    break
                advance(1)
            if closed is False:
                result.problems.append(
                    f"L{start_line}: 模板串 ` 未闭合（吃到文件末尾）"
                )
                break
            if closed is True:
                result.strings.append((start_line, "`", line))
            prev_token = "string"
            continue

        # --- 普通字符串 ---
        if char in "\"'":
            start_line = line
            quote = char
            advance(1)
            closed = False
            while index < length:
                if code[index] == "\\":
                    advance(2)
                    continue
                if code[index] == quote:
                    advance(1)
                    closed = True
                    break
                if code[index] == "\n":
                    # JS 普通字符串不允许裸换行
                    break
                advance(1)
            if not closed:
                result.problems.append(
                    f"L{start_line}: 字符串 {quote} 未闭合（该行结束前没有配对引号）"
                )
                break
            result.strings.append((start_line, quote, line))
            prev_token = "string"
            continue

        # --- 花括号：模板串插值内需要计数才能知道何时回到模板串 ---
        if char == "{":
            if brace_depths:
                brace_depths[-1] += 1
            result.ordered.append(("{", line))
            advance(1)
            prev_token = "{"
            continue
        if char == "}":
            if brace_depths and brace_depths[-1] == 0:
                # 插值结束，回到模板串态
                brace_depths.pop()
                template_stack.pop()
                advance(1)
                # 继续按模板串扫描
                start_line = line
                closed = False
                while index < length:
                    current = code[index]
                    if current == "\\":
                        advance(2)
                        continue
                    if current == "`":
                        advance(1)
                        closed = True
                        break
                    if code.startswith("${", index):
                        template_stack.append(start_line)
                        brace_depths.append(0)
                        advance(2)
                        closed = None
                        break
                    advance(1)
                if closed is False:
                    result.problems.append(
                        f"L{start_line}: 模板串插值结束后未找到闭合反引号"
                    )
                    break
                if closed is True:
                    result.strings.append((start_line, "`", line))
                prev_token = "string"
                continue
            if brace_depths:
                brace_depths[-1] -= 1
            result.ordered.append(("}", line))
            advance(1)
            prev_token = "}"
            continue

        if char in "([":
            result.ordered.append((char, line))
            prev_token = char
        elif char in ")]":
            result.ordered.append((char, line))
            prev_token = char
        elif char.isalnum() or char in "_$":
            # 标识符/数字整段吃掉：判断"值 vs 关键字"必须看**整词**，
            # 不能只看首字母（``return`` 与 ``result`` 语义相反）。
            start = index
            while index < length and (code[index].isalnum() or code[index] in "_$."):
                index += 1
            word = code[start:index]
            if word in _KEYWORDS_BEFORE_REGEX:
                # ``return /x/`` —— 后面可以跟正则
                prev_token = "keyword"
            else:
                # 标识符、数字、true/false/null/this 等都是**值**
                prev_token = "value"
            continue
        elif not char.isspace():
            # 运算符（= + - * < > ! & | ? 等）不产生值，
            # 因此紧跟其后的 ``/`` 是正则开头
            prev_token = "operator"
        advance(1)

    if template_stack:
        result.problems.append(
            f"L{template_stack[-1]}: 模板串插值 ${{ 未闭合"
        )
    return result


def strip_non_code(code: str) -> str:
    """把字符串/模板串/注释替换为等长空格，保留偏移与换行。

    用于"按行号定位"的分析：替换后字符偏移与行号仍与原文件一致。
    """
    out = list(code)
    index = 0
    length = len(code)
    prev_token = ""
    while index < length:
        char = code[index]
        if code.startswith("//", index):
            newline = code.find("\n", index)
            end = length if newline < 0 else newline
            for position in range(index, end):
                out[position] = " "
            index = end
            continue
        if code.startswith("/*", index):
            close = code.find("*/", index + 2)
            end = length if close < 0 else close + 2
            for position in range(index, end):
                if out[position] != "\n":
                    out[position] = " "
            index = end
            continue
        # 正则字面量：里面的引号与斜杠都不是代码，必须整段抹掉
        if char == "/" and prev_token not in _VALUE_TOKENS:
            cursor = index + 1
            in_class = False
            closed = False
            while cursor < length:
                if code[cursor] == "\\":
                    cursor += 2
                    continue
                if code[cursor] == "\n":
                    break
                if code[cursor] == "[":
                    in_class = True
                elif code[cursor] == "]":
                    in_class = False
                elif code[cursor] == "/" and not in_class:
                    cursor += 1
                    closed = True
                    break
                cursor += 1
            if closed:
                while cursor < length and code[cursor].isalpha():
                    cursor += 1
                for position in range(index, cursor):
                    if out[position] != "\n":
                        out[position] = " "
                index = cursor
                prev_token = "regex"
                continue
        if char in _QUOTES:
            quote = char
            index += 1
            while index < length:
                if code[index] == "\\":
                    out[index] = " "
                    if index + 1 < length:
                        out[index + 1] = " "
                    index += 2
                    continue
                if code[index] == quote:
                    index += 1
                    break
                if out[index] != "\n":
                    out[index] = " "
                index += 1
            prev_token = "string"
            continue
        if char.isalnum() or char in "_$":
            start = index
            while index < length and (code[index].isalnum() or code[index] in "_$."):
                index += 1
            word = code[start:index]
            prev_token = "keyword" if word in _KEYWORDS_BEFORE_REGEX else "value"
            continue
        if not char.isspace():
            prev_token = char if char in _NEUTRAL_TOKENS else "operator"
        index += 1
    return "".join(out)


__all__ = ["ScanResult", "scan", "strip_non_code"]
