"""校验 README 里的可核查声明与实际工程是否一致。

只检查"能机器验证"的部分：文件路径存在性、目录结构、测试数量、
断言数量声明、截图与产物存在性。不检查散文表述。
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(r"F:\PycharmProjects\SmartRail-Seat-Engine")
readme = (ROOT / "README.md").read_text(encoding="utf-8")
problems: list[str] = []


def note(ok: bool, message: str) -> None:
    print(("  OK  " if ok else "  !!  ") + message)
    if not ok:
        problems.append(message)


print("=== 1) README 中引用的代码文件是否都存在 ===")
# 取目录结构代码块，抽出形如 xxx.py / xxx.html 的文件名。
# 注意：正则要把 ``-`` 与 ``.`` 都算作文件名的一部分，否则 ``ticket-first.html``
# 会被截成 ``first.html`` 而产生假阳性（本脚本第一版就踩了这个坑）。
block = re.search(r"```text\n(.*?)```", readme, re.S)
tree_text = block.group(1) if block else ""
names = re.findall(r"([A-Za-z_][A-Za-z0-9_.\-]*\.(?:py|html|json|md|toml))", tree_text)
missing = []
for name in sorted(set(names)):
    hits = [
        h for h in ROOT.rglob(name)
        if not any(part in {".venv", ".vendor", ".git", "__pycache__"} for part in h.parts)
    ]
    if not hits:
        missing.append(name)
note(not missing, f"目录结构中提到的文件全部存在（检查 {len(set(names))} 个）"
     + (f"；缺失 {missing}" if missing else ""))

print()
print("=== 2) 关键路径是否存在 ===")
for rel in (
    "smartrail/feasibility.py",
    "smartrail/v3/archetypes.py",
    "smartrail/web/booking.html",
    "smartrail/web/acceptance.html",
    "smartrail/web/ticket-first.html",
    "tools/README.md",
    "tools/verify_order_page.py",
    "tools/draw_simulation.py",
    "tools/draw_submission.py".replace("submission", "order_submission"),
    "tools/draw_ticket_first.py",
    "artifacts/acceptance_snapshot.json",
    "artifacts/v3/v3_ppo.pt",
    "artifacts/v3/v3_bc.pt",
    "benchmarks/offpeak.json",
    "benchmarks/peak.json",
    "benchmarks/REPORT.md",
    "benchmarks/REPORT_V2_V3.md",
    "docs/screenshots/order-submission.png",
    "docs/screenshots/concurrent-simulation.png",
    "docs/screenshots/ticket-first.png",
    ".gitattributes",
    ".gitignore",
    "LICENSE",
    "pyproject.toml",
):
    note((ROOT / rel).exists(), rel)

print()
print("=== 3) 断言数量声明 ===")
result = subprocess.run(
    [str(ROOT / ".venv/Scripts/python.exe"), "-B", "-u", "tests/pytest_shim.py", "-q"],
    cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="ignore",
)
line = (result.stdout or "").strip().splitlines()[-1] if result.stdout else ""
actual = re.search(r"(\d+)\s*passed", line)
count = int(actual.group(1)) if actual else -1
claimed = re.findall(r"\*\*(\d+) 条断言全部通过\*\*", readme)
note(bool(claimed), f"README 声明了断言总数：{claimed}")
if claimed:
    note(int(claimed[0]) == count,
         f"声明 {claimed[0]} 条 vs 实测 {count} 条")
    # 由声明值反查标题，而不是把数字写死在检查脚本里 ——
    # 写死会在每次新增断言后变成假失败（本脚本已踩过一次）。
    note(f"## 🛡️ 底线承诺（由 {claimed[0]} 条断言守护）" in readme,
         f"承诺章节标题与声明一致（{claimed[0]} 条）")

print()
print("=== 4) 目录结构数字声明 ===")
for pattern, label in (
    (r"smartrail/\s*# 核心源码包（(\d+) 个文件", "smartrail 文件数"),
    (r"tests/\s*# (\d+) 条断言", "tests 断言数"),
):
    match = re.search(pattern, readme)
    note(bool(match), f"{label}：{match.group(1) if match else '未声明'}")

SKIP = {".venv", ".vendor", ".git", "__pycache__", ".idea", ".tmppip", ".dsh-acl-recovery"}


def package_files(base: Path) -> list[Path]:
    return [
        p for p in base.rglob("*")
        if p.is_file() and not any(part in SKIP for part in p.parts)
        and p.suffix in {".py", ".html"}
    ]


declared = re.search(r"核心源码包（(\d+) 个文件", readme)
actual = len(package_files(ROOT / "smartrail"))
note(
    bool(declared) and int(declared.group(1)) == actual,
    f"smartrail 实际文件数 = {actual}（README 写 {declared.group(1) if declared else '—'}）",
    )

# 注释里的行数声明只做粗略一致性检查（±15%），因为行数会随改动漂移
for label, base, claimed in (
    ("smartrail 行数", ROOT / "smartrail", r"约 ([\d\s]+) 行"),
    ("v2 行数", ROOT / "smartrail/v2", None),
    ("v3 行数", ROOT / "smartrail/v3", None),
    ("api 行数", ROOT / "smartrail/api", None),
):
    files = package_files(base)
    total = sum(len(p.read_text(encoding="utf-8", errors="ignore").splitlines()) for p in files)
    if claimed:
        match = re.search(claimed, readme)
        if match:
            stated = int(match.group(1).replace(" ", ""))
            ok = abs(stated - total) / max(total, 1) <= 0.15
            note(ok, f"{label}：声明 {stated} vs 实际 {total}（容差 15%）")
    print(f"       {label}实际值 = {total}")

print()
print("=== 5) README 章节完整性 ===")
sections = re.findall(r"^## (.+)$", readme, re.M)
note(len(sections) >= 15, f"顶层章节 {len(sections)} 个")
# 「踩坑记录」已按需求迁到独立开发日志：README 面向"是什么 / 怎么用"，
# 调试过程的原始记录放 docs/DEVELOPMENT_LOG.md。这里改为检查两者都在，
# 且 README 只留链接、不展开正文。
for want in ("项目简介", "核心成绩", "项目目录结构", "快速开始", "许可证"):
    note(any(want in s for s in sections), f"含章节「{want}」")
log_path = ROOT / "docs" / "DEVELOPMENT_LOG.md"
note(log_path.exists(), "开发日志 docs/DEVELOPMENT_LOG.md 存在")
if log_path.exists():
    log_text = log_path.read_text(encoding="utf-8")
    entries = re.findall(r"^## 坑点 \d+", log_text, re.M)
    note(len(entries) >= 20, f"开发日志收录 {len(entries)} 条踩坑记录")
    note("DEVELOPMENT_LOG.md" in readme, "README 里有指向开发日志的链接")
    note("### 坑点" not in readme, "README 不再展开踩坑正文")

print()
print("=== 6) 反面结论 / 关键结论是否如实保留 ===")
# V2 已从"缺依赖未运行"变为"已实跑"：这里改为核对**新的**如实表述，
# 并额外确认旧的过时说法没有残留（否则读者会看到自相矛盾的两段话）。
for token, label in (
    ("已实跑", "V2 CP-SAT 已实跑"),
    ("AddBoolOr", "V2 记录的指示量缺陷"),
    ("AddImplication", "V2 记录的同车厢蕴含修正"),
    ("口径更正说明", "V3 口径更正说明"),
    ("未替代 V1+V2", "V3 未替代结论"),
    ("环境限制说明", "环境限制章节"),
):
    note(token in readme, label)
stale = [
    token for token in ("未运行验证", "从未真正运行过", "本机无法安装 `ortools`")
    if token in readme
]
note(not stale, f"无过时的 V2 表述（残留：{stale or '无'}）")
# 压测与并发口径
for token, label in (
    ("不在本系统责任范围内", "并发不在责任范围的声明"),
    ("压测必须打散占位", "压测打散占位说明"),
    ("自动分票：判定与动作必须一致", "自动分票说明"),
):
    note(token in readme, label)

print()
if problems:
    print(f"发现 {len(problems)} 项问题：")
    for item in problems:
        print("  -", item)
    raise SystemExit(1)
print("README 可核查声明全部与工程一致。")
