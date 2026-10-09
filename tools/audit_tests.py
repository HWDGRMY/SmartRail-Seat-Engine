"""盘点现有测试：文件、断言数、覆盖了哪些"检查维度"。"""

import pathlib
import re
import sys

ROOT = pathlib.Path(r"F:\PycharmProjects\SmartRail-Seat-Engine")
TESTS = ROOT / "tests"

print("=" * 78)
print("现有测试文件盘点")
print("=" * 78)
total_checks = 0
rows = []
for path in sorted(TESTS.glob("test_*.py")):
    text = path.read_text(encoding="utf-8")
    checks = len(re.findall(r"\bcheck\(", text))
    tests = len(re.findall(r"^def test_", text, re.M))
    lines = len(text.splitlines())
    rows.append((path.name, tests, checks, lines))
    total_checks += checks
for name, tests, checks, lines in rows:
    print(f"  {name:34} test 函数 {tests:>2}  check 调用 {checks:>3}  行数 {lines:>4}")
print(f"  {'合计':34} {'':>10}  check 调用 {total_checks:>3}")

print()
print("=" * 78)
print("各测试文件关注什么（从文件名与文档串推断）")
print("=" * 78)
for path in sorted(TESTS.glob("test_*.py")):
    text = path.read_text(encoding="utf-8")
    doc = ""
    m = re.search(r'^"""(.+?)"""', text, re.S | re.M)
    if m:
        doc = " ".join(m.group(1).split())[:84]
    print(f"  {path.name:34} {doc}")

print()
print("=" * 78)
print("接口/入口清单（可被测试驱动的东西）")
print("=" * 78)
service = (ROOT / "smartrail" / "api" / "service.py").read_text(encoding="utf-8")
for name in sorted(re.findall(r"^def (\w+)\(", service, re.M)):
    print(f"  service.{name}")

print()
print("=" * 78)
print("三套求解器入口")
print("=" * 78)
for rel in ("smartrail/solver.py", "smartrail/v2/cpsat.py",
            "smartrail/v3/simulator.py", "smartrail/v3/__init__.py"):
    p = ROOT / rel
    if not p.exists():
        print(f"  {rel:34} (不存在)")
        continue
    text = p.read_text(encoding="utf-8")
    names = sorted(set(re.findall(r"^def (\w+)\(", text, re.M))
                   | set(re.findall(r"^class (\w+)", text, re.M)))
    keep = [n for n in names if not n.startswith("_")]
    print(f"  {rel:34} {keep}")
