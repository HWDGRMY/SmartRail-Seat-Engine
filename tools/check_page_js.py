"""在 Node 里真正执行 booking.html 的脚本，抓出启动期崩溃。

为什么要这样测：之前只做"静态结构检查"（看字符串在不在），
而"按钮点不动"恰恰是**脚本执行期**的问题 —— 静态检查永远发现不了。
本脚本用最小 DOM 垫片 + 真实 fetch 跑一遍 boot()，把异常打出来。
"""

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(r"F:\PycharmProjects\SmartRail-Seat-Engine")
PAGE = ROOT / "smartrail" / "web" / "booking.html"
html = PAGE.read_text(encoding="utf-8")

# ---- 1) 先从 HTML 里收集所有 id ----
ids = set(re.findall(r'id="([^"]+)"', html))
print(f"HTML 中的 id 共 {len(ids)} 个")

# ---- 2) 找出 JS 里 $("xxx") / getElementById("xxx") 引用的 id ----
script = html[html.index("<script>") + len("<script>"):html.rindex("</script>")]
referenced = set(re.findall(r'\$\("([^"]+)"\)', script))
referenced |= set(re.findall(r'getElementById\("([^"]+)"\)', script))
print(f"JS 引用的 id 共 {len(referenced)} 个")

missing = sorted(referenced - ids)
print()
if missing:
    print(f"!! JS 引用了 HTML 中不存在的 id（{len(missing)} 个）：")
    for name in missing:
        # 找出引用所在行
        line_numbers = [
            index + 1
            for index, line in enumerate(script.splitlines())
            if f'"{name}"' in line and ("$(" in line or "getElementById" in line)
        ]
        print(f"   {name:<20} 出现在脚本第 {line_numbers[:4]} 行")
else:
    print("OK：JS 引用的所有 id 都存在于 HTML 中")

# ---- 3) 在 Node 里真实执行 ----
harness = r"""
const fs = require('fs');
const path = process.argv[2];
const html = fs.readFileSync(path, 'utf8');
const script = html.slice(html.indexOf('<script>') + 8, html.lastIndexOf('</script>'));

const ids = new Set([...html.matchAll(/id="([^"]+)"/g)].map(m => m[1]));
const elements = new Map();
const errors = [];
// 先声明 document（用 var 提升），避免 makeEl 里访问造成 TDZ 报错
var document;

function makeEl(id) {
  const el = {
    id,
    textContent: '', innerHTML: '', value: '', checked: false,
    dataset: {}, style: {}, disabled: false,
    classList: { add(){}, remove(){}, toggle(){}, contains(){ return false; } },
    children: [],
    _handlers: {},
    addEventListener(type, fn) { (this._handlers[type] ||= []).push(fn); },
    removeEventListener() {},
    appendChild(child) { this.children.push(child); return child; },
    querySelector() { return null; },
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

// 真实 fetch 打到本地服务
const realFetch = global.fetch;
global.fetch = (url, opts) => {
  const target = url.startsWith('http') ? url : 'http://127.0.0.1:8000' + url;
  return realFetch(target, opts);
};

process.on('unhandledRejection', (reason) => {
  errors.push('unhandledRejection: ' + (reason && reason.stack ? reason.stack : reason));
});

(async () => {
  try {
    // 用间接 eval 拿到全局作用域，便于随后检查绑定情况
    const runner = new Function(script + '\n; return { boot, state: () => ({ orders: typeof orders !== "undefined" ? orders.length : null }) };');
    const api = runner();
    await api.boot();

    // ---- 逐个"点一下"关键按钮，捕获运行时异常 ----
    const need = ['addpax','btnsubmit','btnreset','btnclear','navreset','btnscenario',
                  'btnAddOrder','btnClearOrders','btnDemoOrders','btnSubmitOrders',
                  'btnComposeSubmit','btnComposeAdd','btnComposeDup','btnComposeDemo','btnComposeClear'];
    const unbound = need.filter(id => {
      const el = elements.get(id);
      return !el || !el._handlers.click || el._handlers.click.length === 0;
    });

    const clickErrors = [];
    for (const id of need) {
      const el = elements.get(id);
      if (!el || !el._handlers.click) continue;
      for (const fn of el._handlers.click) {
        try {
          await fn({ preventDefault(){}, stopPropagation(){}, target: el });
        } catch (error) {
          clickErrors.push(`${id}: ${error && error.message ? error.message : error}`);
        }
      }
    }

    // ---- 点几个非按钮控件（键盘/复选框） ----
    for (const id of ['chkKeyService']) {
      const el = elements.get(id);
      if (el && el._handlers.change) {
        try { el.checked = true; await el._handlers.change[0]({ target: el }); }
        catch (error) { clickErrors.push(`${id}.change: ${error.message || error}`); }
      }
    }

    console.log(JSON.stringify({
      ok: errors.length === 0 && unbound.length === 0 && clickErrors.length === 0,
      errors,
      unbound,
      clickErrors,
      boundCount: need.length - unbound.length,
      totalNeeded: need.length,
    }));
  } catch (error) {
    console.log(JSON.stringify({
      ok: false,
      fatal: String(error && error.stack ? error.stack : error),
      errors,
    }));
  }
})();
"""

harness_path = ROOT / ".js_harness.js"
harness_path.write_text(harness, encoding="utf-8")
result = subprocess.run(
    ["node", str(harness_path), str(PAGE)],
    capture_output=True, text=True, encoding="utf-8", errors="ignore", cwd=str(ROOT),
)
print()
print("=== Node 执行结果 ===")
out = (result.stdout or "").strip()
if result.stderr:
    print("stderr:")
    print(result.stderr[:2000])
if out:
    try:
        payload = json.loads(out.splitlines()[-1])
    except json.JSONDecodeError:
        print(out[:3000])
    else:
        if payload.get("fatal"):
            print("!! 启动时抛出异常：")
            print(payload["fatal"][:1500])
        if payload.get("errors"):
            print(f"!! 记录到 {len(payload['errors'])} 条错误：")
            for item in payload["errors"][:12]:
                print("   -", str(item)[:220])
        if payload.get("unbound"):
            print(f"!! 有 {len(payload['unbound'])} 个按钮没绑上点击处理：")
            for name in payload["unbound"]:
                print("   -", name)
        if payload.get("clickErrors"):
            print(f"!! 点击/变更时有 {len(payload['clickErrors'])} 处报错：")
            for item in payload["clickErrors"][:12]:
                print("   -", str(item)[:220])
        if payload.get("ok"):
            print(f"OK：boot() 正常完成，{payload['boundCount']}/{payload['totalNeeded']} "
                  f"个按钮已绑定，逐个点击均无异常")

harness_path.unlink(missing_ok=True)
