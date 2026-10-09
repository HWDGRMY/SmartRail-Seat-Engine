"""从真实服务下载页面，直接在服务版本上跑 JS 检查。

为什么不在本地文件上检查：本地修好了、服务还在发旧版（进程缓存）是常见的
"我这儿好了、用户那儿还是坏的"。必须检查**用户实际拿到的那份**。
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from smartrail.web.bracket_check import check_html_tags, check_js_brackets
from smartrail.web.js_lint import top_level_duplicates

import tools.check_page_js as harness_module  # noqa: E402

BASE = "http://127.0.0.1:8000"
problems: list[str] = []


def check(ok: bool, message: str) -> None:
    print(("  PASS  " if ok else "  FAIL  ") + message)
    if not ok:
        problems.append(message)


PAGES: tuple[tuple[str, str], ...] = (
    ("ticketing", "/ticket"),
    ("developer", "/dev"),
    ("ticket-first", "/ticket-first"),
    ("acceptance", "/acceptance"),
    ("index", "/"),
)

for name, path in PAGES:
    with urllib.request.urlopen(BASE + path, timeout=30) as response:
        html = response.read().decode("utf-8")
    print(f"=== {path}（{len(html)} 字符）===")

    # 1) 顶层重复声明
    if "<script>" in html:
        script = html[html.index("<script>") + len("<script>"):html.rindex("</script>")]
        dups = top_level_duplicates(script)
        check(not dups, f"无顶层重复声明（{dups or '无'}）")
        # 2) 括号配对
        issues = check_js_brackets(html)
        check(not issues, f"JS 括号配对（{issues[:2] or '无'}）")
        # 3) 引用的 id 都存在
        import re

        ids = set(re.findall(r'id="([^"]+)"', html))
        referenced = set(re.findall(r'\$\("([^"]+)"\)', script))
        referenced |= set(re.findall(r'getElementById\("([^"]+)"\)', script))
        missing = sorted(referenced - ids)
        check(not missing, f"引用的 id 都存在（缺失 {missing[:4] or '无'}）")
    # 4) 标签配对
    check(not check_html_tags(html), f"标签配对（{check_html_tags(html)[:2] or '无'}）")
    print()

# 5) 用 Node 在服务版本上真跑一遍 boot()，并逐个点按钮
print("=== Node 实跑服务版页面的 JS ===")
harness = ROOT / "tools" / "check_page_js.py"
result = subprocess.run(
    [str(ROOT / ".venv/Scripts/python.exe"), "-B", "-u", str(harness)],
    capture_output=True, text=True, encoding="utf-8", errors="ignore", cwd=str(ROOT),
)
output = result.stdout or ""
print("\n".join(output.strip().splitlines()[-8:]))
# 交互页面数量**从 tools/check_page_js.py 的按钮清单推导**，不写死 ——
# 早期写死 3，删掉 booking.html 后就对不上了（剩 2 个交互页）。
interactive = len(harness_module.REQUIRED_BUTTONS)
check(output.count("：OK") >= interactive,
      f"{interactive} 个交互页面的 boot() 正常、按钮全部绑定"
      f"（OK 计数 {output.count('：OK')}）")
check("未绑定按钮" not in output and "点击报错" not in output,
      "没有未绑定按钮或点击异常")
_ = tempfile  # 保留 import 供将来落盘调试用

print()
if problems:
    print(f"发现 {len(problems)} 项问题：")
    for item in problems:
        print("  -", item)
    raise SystemExit(1)
print("服务实际下发的页面全部检查通过。")
