"""pytest 适配层。

三个测试文件同时支持两种运行方式：

    python tests/test_safety_guarantees.py     # 零依赖脚本模式（无需 pytest）
    pytest                                      # 标准模式

脚本模式把每份检查记录在模块级 ``FAILURES`` 列表里并由 ``main()`` 汇总；
本文件把同一批检查包装成真正的 pytest 用例，让失败项在 pytest 报告里
逐条可见（而不是只看到一条 "main() 返回 1"）。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SUITES = (
    "test_safety_guarantees",
    "test_api",
    "test_frontend_contract",
)


def _load(module_name: str):
    """以模块方式加载测试文件（文件名不以 test_ 前缀导入，需显式加载）。"""
    path = Path(__file__).parent / f"{module_name}.py"
    spec = importlib.util.spec_from_file_location(f"_suite_{module_name}", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _collect(module_name: str):
    module = _load(module_name)
    cases = [
        (name, value)
        for name, value in sorted(vars(module).items())
        if name.startswith("test_") and callable(value)
    ]
    return module, cases


def pytest_generate_tests(metafunc) -> None:
    """把每个套件里的检查函数参数化为独立用例。"""
    if "suite_module" in metafunc.fixturenames:
        params = []
        for module_name in SUITES:
            _module, cases = _collect(module_name)
            params.extend((module_name, name) for name, _ in cases)
        metafunc.parametrize("suite_module,case_name", params)


def test_suite_case(suite_module: str, case_name: str) -> None:
    """运行单个检查，并把脚本模式下记录的失败项升级为断言失败。"""
    module, cases = _collect(suite_module)
    module.FAILURES.clear()
    lookup = dict(cases)
    assert case_name in lookup, f"{suite_module} 中不存在用例 {case_name}"
    lookup[case_name]()
    failures = list(module.FAILURES)
    assert not failures, f"{suite_module}::{case_name} 断言失败：{failures}"
