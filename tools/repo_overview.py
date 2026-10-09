"""总览提交到 GitHub 的内容构成，确认"该提交的提交、该忽略的忽略"。"""

from __future__ import annotations

import subprocess
from collections import defaultdict
from pathlib import Path

ROOT = Path(r"F:\PycharmProjects\SmartRail-Seat-Engine")

tracked = subprocess.run(
    ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, encoding="utf-8"
).stdout.split()

print(f"=== 提交到 GitHub 的文件：{len(tracked)} 个 ===")
groups: dict[str, list[str]] = defaultdict(list)
for rel in tracked:
    top = rel.split("/")[0] if "/" in rel else "(根目录)"
    groups[top].append(rel)

total_size = 0
for name in sorted(groups, key=lambda n: (-len(groups[n]), n)):
    files = groups[name]
    size = sum((ROOT / f).stat().st_size for f in files if (ROOT / f).exists())
    total_size += size
    print(f"  {name:<14}{len(files):>3} 个   {size / 1024:>8.1f} KB")

print(f"\n  合计体积 {total_size / 1024 / 1024:.2f} MB")

print("\n=== 应当被忽略的目录（确认未提交）===")
for name in (".venv", ".vendor", ".idea", ".tmppip", ".dsh-acl-recovery", "__pycache__"):
    hits = [t for t in tracked if t.startswith(name) or f"/{name}/" in t]
    size = 0
    base = ROOT / name
    if base.exists():
        size = sum(p.stat().st_size for p in base.rglob("*") if p.is_file())
    status = "OK 未提交" if not hits else f"!! 提交了 {len(hits)} 个"
    print(f"  {name:<20}{size / 1024 / 1024:>7.1f} MB  {status}")

print("\n=== 承载验收证据的目录（应当提交）===")
for name in ("artifacts", "benchmarks", "docs"):
    files = [t for t in tracked if t.startswith(name)]
    size = sum((ROOT / f).stat().st_size for f in files if (ROOT / f).exists())
    print(f"  {name:<12}{len(files):>3} 个  {size / 1024:>8.1f} KB  "
          f"{'OK 已提交' if files else '!! 未提交'}")

print("\n=== LICENSE 字节一致性（与上游参考项目一致）===")
blob = subprocess.run(
    ["git", "cat-file", "blob", "HEAD:LICENSE"], cwd=ROOT, capture_output=True
).stdout
import hashlib

digest = hashlib.sha256(blob).hexdigest()
expected = "969ff1fd1ca209717682cec019d5d54c6dd7d1b54d3ac19c09f7af1b4e9374bc"
print(f"  大小 {len(blob)} 字节   CRLF {blob.count(bytes([13, 10]))} 处")
print(f"  SHA256 {digest}")
print(f"  {'OK：与上游逐字节一致' if digest == expected else '!! 与上游不一致'}")
