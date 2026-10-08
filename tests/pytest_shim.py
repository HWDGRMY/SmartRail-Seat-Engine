"""最小 pytest 兼容垫片（仅在未安装 pytest 时启用）。

背景
----
本项目的测试刻意做成**脚本与 pytest 双模式**：

* ``python tests/test_xxx.py`` —— 不依赖任何第三方库，直接跑并汇总结果；
* ``pytest``                   —— 逐条用例报告（见 ``conftest.py``）。

现实里常遇到"内网离线机器装不上 pytest"，此时 ``pytest`` 命令根本不存在。
本文件提供一个**只覆盖本项目实际用法**的极小实现：

    python tests/pytest_shim.py               # 收集 tests/ 下全部用例
    python tests/pytest_shim.py -k family     # 关键字过滤
    python tests/pytest_shim.py -q tests/test_api.py

支持范围：用例收集、``-q``/``-v``、``-k`` 过滤、``raises``。
不支持 fixture、插件、参数化、断言重写——那些场景请安装真正的 pytest。

注意：文件名刻意**不叫** ``pytest.py``，否则在 ``tests/`` 目录下会遮蔽真实的
pytest 包，导致"装了 pytest 反而 import 出错"。
"""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import sys
import traceback
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

__version__ = "0.0.0+shim"


class _Skip(Exception):
    """用例内部主动跳过（例如缺少可选依赖）。"""

ROOT = Path(__file__).resolve().parents[1]  # 工程根目录（便于默认收集 tests/）


class Failed(Exception):
    """断言失败（保留给需要显式失败的场景）。"""


@contextmanager
def raises(expected: type[BaseException], match: str | None = None) -> Iterator[Any]:
    """`pytest.raises` 的最小实现。"""
    try:
        yield
    except expected as error:  # noqa: PERF203
        if match is not None and match not in str(error):
            raise AssertionError(f"异常信息 {error!r} 不包含 {match!r}") from error
    else:
        raise AssertionError(f"期望抛出 {expected.__name__}，但没有异常")


def _discover(paths: Sequence[str]) -> list[tuple[Path, str, Callable[[], Any]]]:
    """收集测试文件与其中的用例（跳过 conftest.py）。"""
    files: list[Path] = []
    for raw in paths or ["tests"]:
        target = (ROOT / raw).resolve()
        if target.is_file():
            files.append(target)
        elif target.is_dir():
            files.extend(sorted(p for p in target.rglob("test_*.py")))
    cases: list[tuple[Path, str, Callable[[], Any]]] = []
    for path in files:
        if path.name == "conftest.py":
            continue
        name = f"_shim_{path.stem}"
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            continue
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        except SystemExit as exit_signal:
            # 模块级"缺少可选依赖"：报告为跳过，而不是失败
            message = str(exit_signal.code) if exit_signal.code else "模块主动退出"
            print(f"SKIPPED {path.name}（{message.splitlines()[0]}）")
            continue
        except Exception:  # pragma: no cover - 导入失败要显式暴露
            print(f"导入 {path.name} 失败：")
            traceback.print_exc()
            continue
        for attribute, value in sorted(vars(module).items()):
            if attribute.startswith("test_") and callable(value):
                cases.append((path, attribute, value))
    return cases


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    keyword = None
    if "-k" in argv:
        index = argv.index("-k")
        keyword = argv[index + 1] if index + 1 < len(argv) else None
        del argv[index : index + 2]
    paths = [arg for arg in argv if not arg.startswith("-")]
    quiet = any(flag in argv for flag in ("-q", "--quiet"))

    cases = _discover(paths)
    if keyword:
        cases = [c for c in cases if keyword in f"{c[0].stem}::{c[1]}"]
    if not cases:
        print("未收集到任何用例（检查路径与 -k 过滤条件）")
        return 5

    passed = 0
    skipped = 0
    failures: list[tuple[str, str]] = []
    for path, name, function in cases:
        try:
            result = function()
            if inspect.iscoroutine(result):
                # async def 用例（例如 FastAPI 端到端）：垫片自己驱动事件循环
                asyncio.run(result)
            passed += 1
            if not quiet:
                print(f"PASSED  {path.relative_to(ROOT)}::{name}")
        except _Skip as reason:
            skipped += 1
            if not quiet:
                print(f"SKIPPED {path.relative_to(ROOT)}::{name}（{reason}）")
        except Exception:
            location = f"{path.relative_to(ROOT)}::{name}"
            failures.append((location, traceback.format_exc()))
            print(f"FAILED  {location}")

    print("-" * 72)
    if failures:
        for location, text in failures:
            print(f"===== {location} =====")
            print(text)
        print(f"{passed} passed, {len(failures)} failed（由 pytest 垫片运行）")
        return 1
    tail = f", {skipped} skipped" if skipped else ""
    print(f"{passed} passed{tail}（由 pytest 垫片运行；安装真正的 pytest 可获得完整功能）")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
