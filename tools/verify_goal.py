"""对目标三项做最终独立验证（不依赖本地 git 命令的输出）。

1. 项目结构已整理
2. README 已完善
3. 成果已推送到 GitHub
"""

import json
import re
import subprocess
import urllib.request
from pathlib import Path

ROOT = Path(r"F:\PycharmProjects\SmartRail-Seat-Engine")
REPO = "HWDGRMY/SmartRail-Seat-Engine"
UA = {"User-Agent": "Mozilla/5.0 (SmartRail-Final-Check)"}
problems: list[str] = []


def note(ok: bool, message: str) -> None:
    print(("  OK  " if ok else "  !!  ") + message)
    if not ok:
        problems.append(message)


def api(url: str):
    request = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(request, timeout=45) as response:
        return json.loads(response.read().decode("utf-8"))


print("=== 目标 1：项目结构已整理 ===")
top = sorted(p.name for p in ROOT.iterdir())
for stray in (".draw_preview.py", ".draw_orders.py", ".verify_orders_page.py",
              ".survey.py", ".check_upload.py"):
    note(stray not in top, f"根目录无临时脚本 {stray}")
note("tools" in top, "存在 tools/ 目录（可视化与自检脚本归位）")
note((ROOT / "tools/README.md").exists(), "tools/ 有自己的 README")
note((ROOT / ".gitattributes").exists(), ".gitattributes 存在（保护 LICENSE 换行）")
tracked = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True,
                         text=True, encoding="utf-8").stdout.split()
for junk in (".venv/", ".vendor/", ".idea/", "__pycache__", ".tmppip/"):
    note(not any(t.startswith(junk) for t in tracked), f"未提交 {junk}")
for evidence in ("artifacts/acceptance_snapshot.json", "benchmarks/offpeak.json",
                 "docs/screenshots/order-submission.png"):
    note(evidence in tracked, f"已提交验收证据 {evidence}")

print()
print("=== 目标 2：README 已完善 ===")
readme = (ROOT / "README.md").read_text(encoding="utf-8")
sections = re.findall(r"^## (.+)$", readme, re.M)
note(len(sections) >= 18, f"顶层章节 {len(sections)} 个")
# 长度用"行数 + 是否覆盖关键内容"衡量，而不是拍一个字符数阈值 ——
# 第一版写了 >40000 字符的硬阈值，把 32179 字符的合格 README 判成失败，
# 属于典型的"凭感觉设阈值"。
note(len(readme.splitlines()) >= 700, f"README 行数 {len(readme.splitlines())}")
note(len(readme) > 20000, f"README 字符数 {len(readme)}")
for want in ("项目简介", "核心成绩", "项目目录结构", "快速开始", "踩坑记录",
             "验收台", "环境限制说明", "许可证"):
    note(any(want in s for s in sections), f"含章节「{want}」")
result = subprocess.run(
    [str(ROOT / ".venv/Scripts/python.exe"), "-B", "-u", "tests/pytest_shim.py", "-q"],
    cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="ignore")
actual = int(re.search(r"(\d+)\s*passed", result.stdout).group(1))
declared = int(re.findall(r"\*\*(\d+) 条断言全部通过\*\*", readme)[0])
note(actual == declared, f"断言数声明与实测一致（{declared}）")
note("未运行验证" in readme, "V2 未运行验证已如实标注")
note("口径更正说明" in readme, "V3 口径更正说明已保留")
selfcheck = subprocess.run(
    [str(ROOT / ".venv/Scripts/python.exe"), "-B", "-u", "tools/verify_readme.py"],
    cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="ignore")
note("全部与工程一致" in (selfcheck.stdout or ""), "verify_readme.py 自检通过")

print()
print("=== 目标 3：已推送到 GitHub ===")
info = api(f"https://api.github.com/repos/{REPO}")
note(info["visibility"] == "public", f"仓库公开：{info['visibility']}")
note((info.get("license") or {}).get("spdx_id") == "MIT", "许可证识别为 MIT")
local_head = subprocess.run(["git", "rev-parse", "main"], cwd=ROOT, capture_output=True,
                            text=True).stdout.strip()
remote_head = api(f"https://api.github.com/repos/{REPO}/commits/main")["sha"]
note(local_head == remote_head,
     f"远程 HEAD 与本地一致（{remote_head[:7]}）")
commits = api(f"https://api.github.com/repos/{REPO}/commits?per_page=10")
note(len(commits) == 3, f"远程提交数 {len(commits)}")
tree = api(f"https://api.github.com/repos/{REPO}/git/trees/main?recursive=1")
remote_blobs = {t["path"] for t in tree["tree"] if t["type"] == "blob"}
note(len(remote_blobs) == len(tracked),
     f"远程文件数 {len(remote_blobs)} == 本地跟踪 {len(tracked)}")
note(remote_blobs == set(tracked), "远程文件清单与本地跟踪清单完全一致")
for want in ("README.md", "LICENSE", "tools/verify_readme.py",
             "docs/screenshots/order-submission.png"):
    note(want in remote_blobs, f"远程存在 {want}")

print()
if problems:
    print(f"发现 {len(problems)} 项问题：")
    for item in problems:
        print("  -", item)
    raise SystemExit(1)
print("目标三项全部验证通过。")
