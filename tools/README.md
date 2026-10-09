# tools —— 可视化与验收辅助脚本

这些脚本**不属于业务代码**，它们是"把结果画出来给人看"和"一条命令跑完页面验收"的工具。
所有数据都来自真实求解/真实接口，脚本本身不做任何美化或伪造。

## 为什么不用无头浏览器截图

部分受限环境不允许浏览器启动 `crashpad` / `mojo` 子进程：

```
OpenProcess: 拒绝访问。 (0x5)
FATAL:mojo\public\cpp\platform\platform_channel.cc: Check failed: . : 拒绝访问。
```

`--headless`、`--single-process`、`--no-sandbox` 都试过，仍然失败。
因此改为 **Pillow 按真实接口返回重绘同样的布局** ——
数字、座位、订单归属全都是真的，只是渲染器从浏览器换成了 Pillow。
在线交互仍然请打开 `http://127.0.0.1:8000/booking`。

依赖：`Pillow`（系统 Python 3.13 自带；项目 `.venv` 未装）。

## 为什么必须真跑一遍 JS

浏览器起不来，不代表没法验证页面脚本 —— **Node.js 可以跑**。

这不是锦上添花，而是因为踩过一次真实的坑：`booking.html` 里
`presetCatalog` 被 `let` 声明了两次，构成 `SyntaxError`，
浏览器拒绝执行**整个脚本**，页面上**哪个按钮都点不动**。

当时所有静态检查**全部通过**：

| 检查 | 结果 | 为什么抓不到 |
| :--- | :--- | :--- |
| 括号配对 | 通过 | 括号确实配对，只是脚本不执行 |
| 标签配对 | 通过 | 标签确实配对 |
| 关键字符串存在 | 通过 | 字符串都在，包括那两行重复声明 |
| 接口 `/api/composition/*` | 通过 | 后端没问题，坏的是前端脚本 |

**教训**："代码写对了"和"代码会执行"是两件事。
语法错误会让整段脚本静默失效，而静态检查对它完全免疫。

所以现在有三层：

1. `smartrail/web/js_lint.py` —— 纯 Python，查**顶层重复声明**（快，可进 CI）；
2. `tools/check_page_js.py` —— Node 真跑 `boot()`、检查按钮绑定、**逐个点一遍**；
3. `tools/verify_served_pages.py` —— 检查**服务实际下发**的页面，
   防"本地修好了，进程还在发旧版"。

## 脚本清单

| 脚本 | 作用 | 输出 |
| :--- | :--- | :--- |
| `draw_simulation.py` | 画"按类型批量生成订单"的结果（座位图按订单着色） | `docs/screenshots/concurrent-simulation.png` |
| `draw_order_submission.py` | 画"批量提交订单"的结果（含需确认例外与现场处理） | `docs/screenshots/order-submission.png` |
| `draw_composer.py` | 画 OrderEditor（人员构成组单）的校验与出票结果 | `docs/screenshots/order-editor.png` |
| `draw_composer_ui.py` | 画 OrderEditor 的**组件布局**（分组控件本身） | `docs/screenshots/order-editor-ui.png` |
| `draw_ticket_first.py` | 画出票优先策略的 6 个场景对比 | `docs/screenshots/ticket-first.png` |
| `verify_order_page.py` | 批量提交页的页面级验收（自带服务） | 控制台结论 |
| `verify_composer.py` | OrderEditor 的页面级验收（自带服务） | 控制台结论 |
| `check_page_js.py` | **在 Node 里真跑页面 JS**：启动、按钮绑定、逐个点击 | 控制台结论 |
| `verify_served_pages.py` | 检查**服务实际下发**的六个页面（防"本地好了、线上还是旧版"） | 控制台结论 |
| `verify_ticketing.py` | 12306 购票流程的 HTTP 端到端验收（自带服务） | 控制台结论 |
| `verify_goal_12306.py` | **逐条验收 12306 重构需求**的 12 项（需后端在跑） | 控制台结论 |
| `verify_readme.py` | **校验 README 里的可核查声明**（路径、断言数、文件数） | 控制台结论 |
| `verify_goal.py` | 目标总验收（读 GitHub API 独立核验） | 控制台结论 |
| `count_files.py` | 统计各目录的文件数/行数（写文档时取数用） | 控制台结论 |
| `repo_overview.py` | 总览提交内容，确认"该提交的提交、该忽略的忽略" | 控制台结论 |

## 用法

前四个脚本需要**后端在跑**（它们调用真实接口）：

```bash
# 另开一个终端启动后端
python -m smartrail.api.stdlib_server --port 8000

# 然后画图
python tools/draw_simulation.py                       # 全类型各 1 位，空车
python tools/draw_simulation.py --fill 0.6            # 六成上座率起售
python tools/draw_simulation.py --counts adult=6 child=2 wheelchair=1
python tools/draw_order_submission.py                 # 内置示例（含 1 张不可行订单）
python tools/draw_order_submission.py --orders my.json
python tools/draw_composer.py                         # 人员构成组单（8 张示例）
python tools/draw_composer_ui.py                      # 组单界面的组件布局
python tools/draw_ticket_first.py
```

页面验收脚本**不需要**手动启动后端（它自己起一个随机端口的服务）：

```bash
python tools/verify_order_page.py     # 批量提交页
python tools/verify_composer.py       # OrderEditor（人员构成组单）
python tools/verify_ticketing.py      # 12306 购票流程（用户模式 + 开发者模式）
```

**页面脚本执行检查**（需要 Node.js，且后端要在跑）：

```bash
python tools/check_page_js.py         # 在 Node 里真跑每个页面的 JS 并逐个点按钮
python tools/check_page_js.py booking # 只检查某一页
python tools/verify_served_pages.py   # 检查服务实际下发的六个页面
```

**需求逐条验收**（需后端在跑）：

```bash
python tools/verify_goal_12306.py     # 12306 重构需求的 12 项逐条核对
```

这个脚本的存在理由：README 与实际代码容易各说各话，而**需求条目**
更容易被漏掉。它把需求原文拆成 12 条可机器验证的断言，
全部针对运行中的服务，而不是读代码猜测意图。

这两个脚本的由来见下面"为什么必须真跑一遍 JS"。

文档与仓库自检脚本**完全离线**（不连网、不起服务）：

```bash
python tools/verify_readme.py     # README 里的数字与路径是否还对得上
python tools/repo_overview.py     # 提交内容构成 + LICENSE 字节一致性
python tools/count_files.py       # 各目录文件数/行数（改文档时取数）
```

> `verify_readme.py` 的存在理由：README 里的"127 条断言""49 个文件"这类数字
> **会随代码改动过期**，而人不会每次改代码都回去改文档。与其靠自觉，
> 不如让机器每次都能验一遍 —— 这也是本项目"配置卫生不变量"思路的延伸。

## 通用参数

三个绘图脚本都支持：

| 参数 | 说明 |
| :--- | :--- |
| `--out PATH` | 输出 PNG 路径，默认写到 `docs/screenshots/` |
| `--api URL` | 接口地址，默认 `http://127.0.0.1:8000/api/...` |
| `--seed N` | 随机种子（保证可复现） |
| `--fill R` | 初始上座率 0~0.95 |

`draw_simulation.py` 额外支持 `--counts key=N ...`；
`draw_order_submission.py` 额外支持 `--orders FILE.json`。

## 与测试的关系

`verify_order_page.py` 与 `tests/test_concurrent.py` 覆盖同一批接口契约，但层次不同：

* **单元级**（`tests/`）：直接调业务函数，快、无网络；
* **页面级**（本目录）：起真实 HTTP 服务 + 抓真实 HTML，验证"用户实际看到的东西"。

两者都要跑。历史上多次出现"逻辑对、页面字段名错"的问题
（例如把 `chosen_seats` 的键值顺序写反，直到页面级校验才发现）。
