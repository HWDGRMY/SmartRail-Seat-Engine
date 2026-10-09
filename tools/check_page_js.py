"""在 Node 里真正执行页面脚本，抓出启动期崩溃与按钮未绑定。

为什么要这样测
--------------
"按钮点不动"是**脚本执行期**的问题，静态检查（字符串在不在、括号配不配）
对它完全免疫。本项目就被这个坑咬过两次：

* 顶层 ``let`` 重复声明 → SyntaxError → 整个 ``<script>`` 不执行（坑点 22）；
* 词法扫描器误报（坑点 24）—— 那是检查器自己的问题，与本脚本无关。

本脚本用最小 DOM 垫片 + 真实 fetch 跑一遍 ``boot()``，
检查关键按钮是否绑上、并**逐个点一遍**捕获运行时异常。

用法::

    python tools/check_page_js.py                # 检查全部页面
    python tools/check_page_js.py booking        # 只检查 booking.html
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "smartrail" / "web"

#: 每个页面必须绑上点击处理的按钮
REQUIRED_BUTTONS: dict[str, list[str]] = {
    "booking.html": [
        "addpax", "btnsubmit", "btnreset", "btnclear", "navreset", "btnscenario",
        "btnAddOrder", "btnClearOrders", "btnDemoOrders", "btnSubmitOrders",
        "btnComposeSubmit", "btnComposeAdd", "btnComposeDup", "btnComposeDemo",
        "btnComposeClear",
    ],
    "ticketing.html": [
        "navPax", "btnPickPax", "btnPickCancel", "btnPickOk",
        "btnAddPax", "btnAddCancel", "btnAddOk", "btnSubmit",
    ],
    "developer.html": ["btnReset"],
}

#: 需要真实请求后端；没有服务时跳过这些页面
NEEDS_BACKEND = {"booking.html", "ticketing.html", "developer.html"}

HARNESS = r"""
const fs = require('fs');
const html = fs.readFileSync(process.argv[2], 'utf8');
const script = html.slice(html.indexOf('<script>') + 8, html.lastIndexOf('</script>'));
const need = JSON.parse(process.argv[4] || '[]');

const ids = new Set([...html.matchAll(/id="([^"]+)"/g)].map(m => m[1]));
const elements = new Map();
const errors = [];
var document;

function makeEl(id) {
  const el = {
    id, textContent: '', innerHTML: '', value: '', checked: false,
    dataset: {}, style: {}, disabled: false,
    classList: { add(){}, remove(){}, toggle(){}, contains(){ return false; } },
    children: [], _handlers: {},
    addEventListener(type, fn) { (this._handlers[type] ||= []).push(fn); },
    removeEventListener() {},
    appendChild(child) { this.children.push(child); return child; },
    querySelector() { return makeEl('sel'); },
    querySelectorAll() { return []; },
    setAttribute() {}, getAttribute() { return null; },
    focus() {}, blur() {}, insertAdjacentHTML() {}, remove() {},
    scrollIntoView() {},
    get onclick() { return this._handlers.click && this._handlers.click[0]; },
    set onclick(fn) { (this._handlers.click = [fn]); },
  };
  return el;
}

document = {
  getElementById(id) {
    if (!ids.has(id)) {
      errors.push(`getElementById("${id}") -> null（HTML 中无此 id）`);
      return null;
    }
    if (!elements.has(id)) elements.set(id, makeEl(id));
    return elements.get(id);
  },
  querySelector(sel) { return makeEl('sel:' + sel); },
  querySelectorAll() { return []; },
  createElement(tag) { return makeEl('new:' + tag); },
  addEventListener() {},
  body: makeEl('body'),
  documentElement: makeEl('html'),
};
global.document = document;
global.window = { addEventListener(){}, location:{ href:'' }, setTimeout, clearTimeout };
global.navigator = { userAgent: 'node' };
global.alert = () => {};
global.confirm = () => true;

const base = process.argv[3] || 'http://127.0.0.1:8000';
const realFetch = global.fetch;
global.fetch = (url, opts) => {
  const target = url.startsWith('http') ? url : base + url;
  return realFetch(target, opts);
};

process.on('unhandledRejection', (reason) => {
  errors.push('unhandledRejection: ' + (reason && reason.stack ? reason.stack : reason));
});

(async () => {
  try {
    // 页面末尾会自己调 ``boot()``。我们要拿到**可 await** 的那次调用，
    // 所以在加载阶段先放一个同名的空壳把它接住，加载完再删掉空壳、
    // 通过 ``new Function`` 返回真正的 boot。
    // （直接用页面自己那次调用是不行的：它返回的 Promise 我们拿不到。）
    let started = false;
    const stub = function boot() { started = true; };
    globalThis.boot = stub;
    const runner = new Function(
      'boot',
      script + '\n; return { boot: typeof boot === "function" ? boot : null };'
    );
    const api = runner(stub);
    if (!api.boot) {
      errors.push('页面中没有定义 boot()');
    } else {
      await api.boot();
    }
    await new Promise((r) => setTimeout(r, 1200));   // 等挂起的 fetch 结束

    const unbound = need.filter(id => {
      const el = elements.get(id);
      return !el || !el._handlers.click || el._handlers.click.length === 0;
    });

    const clickErrors = [];
    for (const id of need) {
      const el = elements.get(id);
      if (!el || !el._handlers.click) continue;
      for (const fn of el._handlers.click) {
        try { await fn({ preventDefault(){}, stopPropagation(){}, target: el }); }
        catch (error) { clickErrors.push(`${id}: ${error && error.message ? error.message : error}`); }
      }
    }
    // 复选框类控件
    for (const id of ['chkQuiet', 'chkKeyService']) {
      const el = elements.get(id);
      if (el && el._handlers.change) {
        try { el.checked = true; await el._handlers.change[0]({ target: el }); }
        catch (error) { clickErrors.push(`${id}.change: ${error.message || error}`); }
      }
    }

    console.log(JSON.stringify({
      ok: errors.length === 0 && unbound.length === 0 && clickErrors.length === 0,
      errors, unbound, clickErrors,
      bound: need.length - unbound.length, total: need.length,
    }));
  } catch (error) {
    console.log(JSON.stringify({
      ok: false, fatal: String(error && error.stack ? error.stack : error), errors,
    }));
  }
})();
"""


def backend_alive(base: str) -> bool:
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(base + "/api/config", timeout=5) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError):
        return False


def run_page(path: Path, buttons: list[str], base: str) -> dict:
    harness_path = ROOT / ".page_js_harness.js"
    harness_path.write_text(HARNESS, encoding="utf-8")
    try:
        result = subprocess.run(
            ["node", str(harness_path), str(path), base, json.dumps(buttons)],
            capture_output=True, text=True, encoding="utf-8", errors="ignore",
        )
    finally:
        harness_path.unlink(missing_ok=True)
    output = (result.stdout or "").strip()
    if not output:
        return {"ok": False, "fatal": (result.stderr or "node 无输出")[:800]}
    try:
        return json.loads(output.splitlines()[-1])
    except json.JSONDecodeError:
        return {"ok": False, "fatal": output[:800]}


def main(argv: list[str]) -> int:
    base = "http://127.0.0.1:8000"
    targets = argv or [name for name in REQUIRED_BUTTONS]
    alive = backend_alive(base)
    print(f"后端 {base}：{'在线' if alive else '离线'}")
    print()

    failures: list[str] = []
    for name in targets:
        path = WEB / name if name.endswith(".html") else WEB / f"{name}.html"
        if not path.exists():
            print(f"!! 页面不存在：{path}")
            failures.append(str(path))
            continue
        buttons = REQUIRED_BUTTONS.get(path.name, [])
        # 先做静态 id 引用检查（不需要后端）
        html = path.read_text(encoding="utf-8")
        ids = set(re.findall(r'id="([^"]+)"', html))
        script = html[html.index("<script>") + len("<script>"):html.rindex("</script>")]
        referenced = set(re.findall(r'\$\("([^"]+)"\)', script))
        referenced |= set(re.findall(r'getElementById\("([^"]+)"\)', script))
        missing = sorted(referenced - ids)

        if path.name in NEEDS_BACKEND and not alive:
            print(f"{path.name}：跳过执行检查（后端离线）"
                  f"{'；但引用的 id 缺失：' + str(missing) if missing else ''}")
            if missing:
                failures.append(f"{path.name} 缺失 id {missing[:4]}")
            continue

        outcome = run_page(path, buttons, base)
        status = "OK" if outcome.get("ok") else "FAIL"
        print(f"{path.name}：{status}"
              f"  按钮绑定 {outcome.get('bound', '?')}/{outcome.get('total', '?')}")
        if missing:
            print(f"    引用的 id 缺失：{missing[:5]}")
            failures.append(f"{path.name} 缺失 id {missing[:4]}")
        if outcome.get("fatal"):
            print(f"    启动异常：{outcome['fatal'][:400]}")
            failures.append(f"{path.name} 启动异常")
        for item in (outcome.get("errors") or [])[:5]:
            print(f"    运行期错误：{str(item)[:200]}")
            failures.append(f"{path.name} 运行期错误")
        if outcome.get("unbound"):
            print(f"    未绑定按钮：{outcome['unbound']}")
            failures.append(f"{path.name} 未绑定按钮 {outcome['unbound']}")
        for item in (outcome.get("clickErrors") or [])[:5]:
            print(f"    点击报错：{str(item)[:200]}")
            failures.append(f"{path.name} 点击报错")
        print()

    if failures:
        print(f"发现 {len(failures)} 项问题：")
        for item in failures:
            print("  -", item)
        return 1
    print("所有页面脚本启动正常、按钮绑定完整、逐个点击无异常。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
