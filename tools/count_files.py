"""精确统计 smartrail 包的文件构成，用于修正 README 的目录结构说明。"""

from pathlib import Path

ROOT = Path(r"F:\PycharmProjects\SmartRail-Seat-Engine")
SKIP = {".venv", ".vendor", ".git", "__pycache__", ".idea", ".tmppip", ".dsh-acl-recovery"}


def collect(base: Path) -> list[Path]:
    return sorted(
        p for p in base.rglob("*")
        if p.is_file() and not any(part in SKIP for part in p.parts)
        and p.suffix in {".py", ".html"}
    )


def lines(paths: list[Path]) -> int:
    return sum(len(p.read_text(encoding="utf-8", errors="ignore").splitlines()) for p in paths)


print("=== smartrail 包构成 ===")
all_files = collect(ROOT / "smartrail")
py = [p for p in all_files if p.suffix == ".py"]
html = [p for p in all_files if p.suffix == ".html"]
print(f"  合计 {len(all_files)} 个文件 / {lines(all_files)} 行")
print(f"    其中 .py   {len(py)} 个 / {lines(py)} 行")
print(f"    其中 .html {len(html)} 个 / {lines(html)} 行")

print("\n=== 各子包 ===")
for sub in ("v2", "v3", "api", "web"):
    base = ROOT / "smartrail" / sub
    if not base.exists():
        continue
    files = collect(base)
    print(f"  {sub + '/':<6} {len(files):>2} 个 / {lines(files):>6} 行")

print("\n=== 顶层子包之外的核心模块 ===")
top = [p for p in py if p.parent == ROOT / "smartrail"]
print(f"  {len(top)} 个 / {lines(top)} 行")

print("\n=== 其他目录 ===")
for name in ("tests", "tools", "benchmarks", "artifacts"):
    base = ROOT / name
    files = [p for p in base.rglob("*") if p.is_file()
             and not any(part in SKIP for part in p.parts)]
    code = [p for p in files if p.suffix in {".py", ".html", ".json", ".md"}]
    print(f"  {name:<10} {len(files):>2} 个文件 / 其中可计行 {lines(code):>6} 行")

print("\n=== 仓库总计（git 跟踪）===")
import subprocess

tracked = subprocess.run(
    ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True
).stdout.split()
tracked_lines = 0
for rel in tracked:
    path = ROOT / rel
    if path.suffix in {".py", ".html", ".json", ".md", ".toml", ".txt"} and path.exists():
        try:
            tracked_lines += len(path.read_text(encoding="utf-8", errors="ignore").splitlines())
        except OSError:
            pass
print(f"  文件 {len(tracked)} 个 / 可计行 {tracked_lines} 行")
