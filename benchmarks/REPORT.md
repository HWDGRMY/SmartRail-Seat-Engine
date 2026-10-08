# SmartRail-Seat-Engine V1.0 · 实现与验证报告

*生成方式：`python -m smartrail.benchmark --orders 500 --seed 7 --json benchmarks/offpeak.json`*
*环境：Windows / Python 3.10.11 / 纯 Python 单线程（无 numpy 加速）/ 16 节编组，1,156 座*

---

## 1. 交付内容

| 层 | 模块 | 说明 |
| :--- | :--- | :--- |
| 数据模型 | `models.py` `carriage.py` | 乘客图谱（多维向量 + 关系拓扑）、座位图谱（三维坐标 + 设施标签 + 车厢标签） |
| 代价函数 | `scoring.py` `config.py` | Tier 0-5 分层惩罚/奖励，全部权重集中在 `EngineConfig` |
| 求解器 | `solver.py` `clustering.py` `fastscore.py` | 单元拆解 → 余票分块 → 单元内指派置换 → 分支限界（12ms 预算） |
| 决策层 | `router.py` `engine.py` | 分层决策状态机（模式一/二/三）、降级细则、编排门面 |
| 人文机制 | `credit.py` `gov_api.py` `free_seat.py` | 动态信用分闭环、政务数据抽象层（含审计）、自由选座隐性拦截 |
| 服务层 | `api/service.py` `api/app.py` `api/stdlib_server.py` | 零依赖业务核心 + FastAPI 实现 + 离线同构备用服务 |
| 前端 | `web/index.html` | 可视化座位图（静音/无障碍/占用/本次分配四态着色）+ 代价明细 + 可解释性面板 |
| 工具 | `demo.py` `benchmark.py` | 场景演练、随机订单流仿真与分位时延 |

## 2. 验证结果

### 2.1 底线承诺（37 条断言，全部通过）

```
python tests/test_safety_guarantees.py   ->  全部 28 组断言通过（含配置卫生不变量）
python tests/test_api.py                 ->  全部 5 组断言通过（含 HTTP 端到端）
python tests/test_frontend_contract.py   ->  全部 4 组断言通过
python tests/pytest_shim.py -q           ->  37 passed（无 pytest 的离线环境）
pytest -q                                ->  逐条用例报告（需已安装 pytest）
```

关键断言与实测输出：

| 承诺 | 实测证据 |
| :--- | :--- |
| 带娃家庭不被拆分 | `儿童 C1 与家长同车厢（1 ∈ [1]）`，且未触发 `T0_BOND_SEPARATED` |
| 孩子身边有人 | `未触发'需照护者被孤立'惩罚（实际 0 条）`；`儿童座位 1B 与某位家长紧邻` |
| 轮椅只坐无障碍专区 | `轮椅乘客分到无障碍专区座位 01车01B` |
| 无障碍售罄则候补 | `轮椅乘客进入候补` / `未给轮椅乘客发放普通座位` / 候补原因进入 notes |
| 降级不降底线 | `降级模式下儿童仍与家长同车厢`，求解器为 `greedy` |
| 静音车厢屏蔽 | 信用分 55 时 `前端不展示静音车厢`、`后续购票不再分配静音车厢` |
| 约束优先于偏好 | `单人总奖励 50 ≤ 上限 60`；Tier 权重严格分层 |
| 孕晚期有人陪同 | `T0_PREGNANT_NO_COMPANION` 被触发（无可同行人时） |
| 最优解不劣于贪心 | `family: exact(130) ≥ greedy(130)`、`multigen: exact(195) ≥ greedy(195)` |
| 调参一定生效 | 全部 22 个配置字段均被引用；`near_door_rows` 1→4 使近门座位数增加；`aisle_crossing_weight` 1→4 使跨过道距离 1→4；抬高奖励地板后不再发放奖励 |

### 2.2 场景演练（节选）

```
family_with_child  smart    代价=  -130  01车[A1@01A A2@01C C1@01B]   ← 孩子坐在两位家长中间
multi_gen          smart    代价=  -195  01车[A1@01C A2@01A B1@01B G1@01F G2@01D]
wheelchair         smart    代价=   -70  01车[A1@01A W1@01B]          ← 无障碍专区 + 陪同相邻
pregnant           smart    代价=  -190  01车[A1@01B P1@01C]          ← 硬绑定 + 过道
blind              smart    代价=   -60  01车[B1@01C]                 ← 过道位，未强配陌生人
solo               smart    代价=   -50  05车[S1@01A]                 ← 主动利用静音车厢（+50）
```

代价为负表示该方案整体带奖励（约束满足良好）；Tier 0 违规在所有场景中均为 0。

### 2.3 决策时延（500 单随机订单流）

| 场景 | P50 | P95 | P99 | 模式分布 | Tier 0 违规 |
| :--- | ---: | ---: | ---: | :--- | ---: |
| 平峰（初始空车） | **6.7 ms** | 23.3 ms | 49.2 ms | free 293 / smart 131 / degraded 12 | **0** |
| 高峰（预占 85%） | **4.8 ms** | 24.9 ms | 26.9 ms | smart 46 / degraded 11 | **0** |

按订单规模拆解（平峰同分布）：

| 订单规模 | 占比 | P50 | 最大 |
| :--- | ---: | ---: | ---: |
| 1 人 | 19% | 2.6 ms | 5.6 ms |
| 2 人 | 31% | 6.1 ms | 9.7 ms |
| 3 人（家庭） | 38% | 12.9 ms | 22.1 ms |
| 4-8 人（团体/多代） | 12% | 16-32 ms | 34 ms |

**结论：**

* ✅ **P50 = 4.8-6.7 ms**，达到设计目标 5-10 ms（README 6.1）。
* ⚠️ **P99 = 26.9-49.2 ms，未达 P99 < 15 ms 目标**。长尾全部来自 3-8 人团体与多代家庭：
  单元内指派置换、跨车厢候选评估与分支限界的组合开销在纯 Python 下无法压到 15ms 以内。
* 🔧 已实施的缓解：`max_exact_order_size = 8`（更大订单直接用贪心解）、
  每车厢候选限额、按需惰性单项打分（向量化行构造）、成对项与距离缓存、可采纳上界剪枝。
* 📌 达标路径（V2.0）：OR-Tools CP-SAT 批量求解 + numpy 向量化打分；
  预计长尾订单可降至个位数毫秒，同时保留 Tier 0/1 硬约束语义。

## 3. 工程复盘：被测试抓住的四个真实缺陷

| # | 现象 | 根因 | 修复 |
| :--- | :--- | :--- | :--- |
| 1 | 16 位乘客被"塞进"10 个座位，结果层完全静默 | 测试夹具把每节车厢编号写成常量 `1`，座位 ID 重复 | 编组构造期校验车厢编号与座位 ID 唯一；输出前校验一人一座 |
| 2 | 带娃家庭被优先安排进静音车厢 | 代价函数"惩罚为负、奖励为正"却被按"取最小值"优化，等价于**最大化奖励** | 全链路统一 affinity 语义（越大越优），新增"精确解不劣于贪心"回归测试 |
| 3 | 所有带娃家庭被叠加 -10,000 巨惩罚，代价函数失去分辨力 | Tier 1「冲突隔离」被误用于全部儿童；同时静音车厢奖励按人头累加 | Tier 1 只对轮椅/视障/智力障碍等**硬冲突群体**生效；静音车厢改按**出行单元**计价；新增单乘客奖励硬上限 |
| 4 | 4 个配置项"声明了但从未被读取"，调参毫无作用 | 参数先写进 `EngineConfig`、算法实现却用了硬编码常量（`near_door_rows` 等） | 参数真正接入 `build_formation` 与打分器；新增静态扫描 + 行为验证双重回归测试 |

## 4. 复现方式

```bash
# 底线断言（脚本模式，零依赖）
python tests/test_safety_guarantees.py
python tests/test_api.py
python tests/test_frontend_contract.py

# pytest 模式（无 pytest 的离线机器用内置最小运行器）
pytest -q
python tests/pytest_shim.py -q
python tests/pytest_shim.py -q -k wheelchair      # 关键字过滤

# 场景演练（含数学解释）
python -m smartrail.demo --compare-modes
python -m smartrail.demo --scenario family_with_child --explain

# 时延基准（结果写入 benchmarks/*.json）
python -m smartrail.benchmark --orders 500 --seed 7  --json benchmarks/offpeak.json
python -m smartrail.benchmark --orders 500 --seed 11 --fill 0.85 --json benchmarks/peak.json

# 可视化校验
python -m smartrail.api.stdlib_server      # 零依赖，http://127.0.0.1:8000/
# 或（需先在该环境 pip install fastapi "uvicorn[standard]"）
python -m smartrail.api.app
```

> 说明：本次开发环境为受限沙箱，`pip install fastapi` 无法完成，
> 因此 FastAPI 版本通过 AST 路由校验 + 零依赖同构服务器做了端到端验证；
> `api/app.py` 与 `api/stdlib_server.py` 的路由、请求体与响应体一一对应。
