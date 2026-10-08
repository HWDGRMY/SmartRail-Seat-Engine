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

## 脚本清单

| 脚本 | 作用 | 输出 |
| :--- | :--- | :--- |
| `draw_simulation.py` | 画"按类型批量生成订单"的结果（座位图按订单着色） | `docs/screenshots/concurrent-simulation.png` |
| `draw_order_submission.py` | 画"批量提交订单"的结果（含未满足订单与原因） | `docs/screenshots/order-submission.png` |
| `draw_ticket_first.py` | 画出票优先策略的 6 个场景对比 | `docs/screenshots/ticket-first.png` |
| `verify_order_page.py` | 页面级验收（自带服务，跑真实 HTTP + 真实 HTML） | 控制台结论 |

## 用法

前三个脚本需要**后端在跑**（它们调用真实接口）：

```bash
# 另开一个终端启动后端
python -m smartrail.api.stdlib_server --port 8000

# 然后画图
python tools/draw_simulation.py                       # 全类型各 1 位，空车
python tools/draw_simulation.py --fill 0.6            # 六成上座率起售
python tools/draw_simulation.py --counts adult=6 child=2 wheelchair=1
python tools/draw_order_submission.py                 # 内置示例（含 1 张不可行订单）
python tools/draw_order_submission.py --orders my.json
python tools/draw_ticket_first.py
```

页面验收脚本**不需要**手动启动后端（它自己起一个随机端口的服务）：

```bash
python tools/verify_order_page.py
```

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
