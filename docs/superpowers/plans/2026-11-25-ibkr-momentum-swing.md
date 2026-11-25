# 动量波段自动交易系统 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 按 `docs/design.md` 从零实现一个在 IBKR 上运行的确定性动量波段规则引擎，模拟盘和实盘共用代码。

**Architecture:** 一个与券商无关的 `Engine` 把「行情 → 策略 `decide()` → 风控闸门 → 执行层」串起来，只通过 `Broker` 协议和外界打交道。实盘用 `IbkrBroker`，回测用 `SimBroker`，两者驱动同一个 `Engine`、同一个 SQLite 状态库。LLM 旁路只读状态库，只能写观察名单的主题标签。

**Tech Stack:** Python 3.11、pandas、`ib_async`、`exchange_calendars`（XNYS 日历）、PyYAML、SQLite（标准库）、pytest、`anthropic`（可选）。

**Spec:** `docs/design.md`

## Global Constraints

- 旧的加密货币机器人代码全部删除，不保留兼容。
- 策略引擎是纯函数：同样输入永远同样输出；时间和阶段都从输入传入，不读时钟。
- 策略代码读不到运行环境（paper/live）。
- 所有时间为美东时间，带时区。日线索引为交易日（无时区），5 分钟线索引为 K 线开始时间。
- 数量向下取整为整数股（碎股是设计文档里的待确认项）。
- 止损单、止盈单为 GTC 并挂在券商侧；父单为 DAY。
- 单笔风险硬上限 1%，加载配置时校验。
- LLM 不出现在下单路径上，不可用时系统照常运行。
- 风控限制运行时只读。

## 文件结构

| 文件 | 职责 |
| --- | --- |
| `trader/models.py` | 数据类：`AccountState` `Position` `Order` `Fill` `TradePlan` `Action` `WatchItem` |
| `trader/config.py` | 读取 YAML，`Limits` `Profile` `StrategyParams` `Config`，校验 |
| `trader/indicators.py` | `ema` `adr_pct` `add_daily_indicators` |
| `trader/calendar.py` | `TradingCalendar`（开收盘、半日市）、`EarningsCalendar` |
| `trader/watchlist.py` | `screen()` 盘前筛选 |
| `trader/strategy.py` | `StrategyInput`、`decide()`、`inputs_hash()` |
| `trader/risk.py` | `RiskContext`、`RiskGate.check()` |
| `trader/store.py` | SQLite 七张表 |
| `trader/broker/base.py` | `Broker` 协议、`bracket_legs()` |
| `trader/broker/sim.py` | `SimBroker` 回测撮合 |
| `trader/broker/ibkr.py` | `IbkrBroker`（`ib_async`） |
| `trader/execution.py` | `Executor`：幂等提交、成交和订单状态处理、出场 |
| `trader/reconcile.py` | 对账、重启恢复、补挂止损 |
| `trader/alerts.py` | `Alerter`：日志 + events 表 + 可选 webhook |
| `trader/killswitch.py` | `HALT` 文件、`flatten()` |
| `trader/engine.py` | `Engine`：各时点任务的业务逻辑，实盘和回测共用 |
| `trader/scheduler.py` | 按开收盘时间生成当日时间表 |
| `trader/startup.py` | 启动检查清单 |
| `trader/app.py` | 实盘主循环 |
| `trader/backtest.py` | 回测驱动和统计 |
| `trader/llm/sidecar.py` | 主题标签、复盘报告 |
| `trader/cli.py` | `run` `check` `backtest` `status` `flatten` `review` |

## 设计文档没定、本计划做出的决定

这些都是可配置的默认值，写进 README 的「需要校准」一节。

- 形态 A 的「内包日或极窄幅日」：最近 3 个交易日内有内包日，或有一日振幅 ≤ 0.5 × ADR%。
- 「均线靠拢」：EMA9/21/50 的最大差 ≤ 1.0 × ADR（金额）。
- 形态 B 的「盘中转强」：15:50 时最新价高于前一日 EMA9，且位于当日振幅的上 30% 内；并要求 EMA9 > EMA21 > EMA50。
- 分批比例：A 组占 1/3。A 组或 B 组不足 1 股时只下 B 组。
- 允许最大滑点：触发价的 0.3%。
- 幂等键为「日期-代码」：同一只股票一天只入场一次。
- 单笔订单金额按整份计划的名义金额检查；每日下单次数按「入场计划数 + 出场单数」计。
- 财报日期未知的股票放行（数据源是待确认项），在 README 说明。
- 出场单在 09:25 提交：先撤掉该持仓的止损和止盈单，再发市价卖单。
- 对账差异里能自动修复的（补挂止损、补录成交、同步订单状态）修复后不算差异；修复不了的（未知来源持仓、数量对不上、未知订单）进入只读并报警。
- `Broker` 协议在设计文档基础上增加：`managed_accounts` `server_time` `market_data_realtime` `recent_fills` `place_stop` `modify_limit` `sleep`。

## Review Focus

1. 父单部分成交后当日作废：止损数量必须等于实际持仓，不能多挂导致反手做空。→ Task 5 测试。
2. 同一成交回报重复推送（重连后重放）：不能重复记账。→ Task 4 测试。
3. 跳空低开越过止损：回测按开盘价成交，亏损超过 1R。→ Task 6 测试。
4. 半日市：收盘相关任务按 13:00 收盘前移。→ Task 7 测试。
5. 日线里混入当日未完成的 K 线：盘中判定必须忽略它。→ Task 2 测试。

---

### Task 1: 配置、模型、指标、日历

**Files:** `trader/models.py` `trader/config.py` `trader/indicators.py` `trader/calendar.py` `config.example.yaml` `requirements.txt` · Test: `tests/test_config.py` `tests/test_indicators.py` `tests/test_calendar.py`

**Produces:** `load_config(path) -> Config`（`cfg.mode` `cfg.profile` `cfg.limits` `cfg.params`）；`ema(s, span)`；`adr_pct(df, n=20)`；`add_daily_indicators(df)` 加 `ema9 ema21 ema50 adr_pct dollar_vol`；`TradingCalendar.session(date) -> (open, close) | None`；`EarningsCalendar.sessions_to(symbol, today, cal) -> int | None`。

- [x] 测试：paper 账户号不以 DU 开头时报错；live 账户号以 DU 开头时报错；`risk_per_trade_pct > 1` 报错。
- [x] 测试：ADR% 对恒定 high/low=1.05 的序列为 5.0；2026-11-27（感恩节次日）收盘为 13:00。
- [x] 实现并通过。

### Task 2: 观察名单和策略引擎

**Files:** `trader/watchlist.py` `trader/strategy.py` · Test: `tests/test_watchlist.py` `tests/test_strategy.py`

**Produces:** `screen(daily, asof, params, earnings) -> list[WatchItem]`；`decide(bars: StrategyInput, account, positions, params) -> list[Action]`；`inputs_hash(bars, account, positions, params) -> str`。阶段：`OPEN_RANGE` `INTRADAY` `PRE_CLOSE` `POST_CLOSE`。

- [x] 测试：QQQ 在 EMA50 下方时没有 ENTER。
- [x] 测试：形态 A 产出止损限价计划，触发价 = 开盘区间高点 + 0.01，止损 = 开盘区间低点；止损距离 > ADR/2 时放弃。
- [x] 测试：数量 = floor(净值 × 0.5% ÷ 每股风险)，并按 30% 仓位上限截断。
- [x] 测试：形态 B 四个条件各自不满足时都不入场。
- [x] 测试：收盘跌破 EMA9 产出 `EXIT(when=NEXT_OPEN)`；财报前一日产出 `EXIT(when=NOW)`。
- [x] 测试：同一输入调用两次结果和 `inputs_hash` 相同；日线混入当日 K 线时结果不变。
- [x] 实现并通过。

### Task 3: 风控闸门

**Files:** `trader/risk.py` · Test: `tests/test_risk.py`

**Produces:** `RiskGate(limits).check(plan, ctx) -> RiskDecision(approved, plan, reasons, halt)`。

- [x] 测试：设计文档风控表的每一行各一个用例（拒绝、截断或停机）。
- [x] 实现并通过。

### Task 4: 状态库和执行层

**Files:** `trader/store.py` `trader/broker/base.py` `trader/execution.py` `trader/alerts.py` · Test: `tests/test_store.py` `tests/test_execution.py`

**Produces:** `Store(path)`；`bracket_legs(plan) -> list[Leg]`；`Executor.submit_entry(plan, today) -> bool`、`.handle_fill(fill)`、`.handle_order_status(ref, status, filled)`、`.queue_exit(symbol, reason)`、`.exit_now(symbol, reason)`、`.submit_queued_exits()`。

- [x] 测试：同一计划提交两次只下一次单。
- [x] 测试：父单成交后状态 OPEN，3R 价位按实际成交价重算并改单。
- [x] 测试：止损成交后 CLOSED，`r_multiple` ≈ -1；3R 成交后 `scaled_out=1`。
- [x] 测试：同一 `exec_id` 推送两次只记一次。
- [x] 测试：拒单后报警且不重试。
- [x] 实现并通过。

### Task 5: 对账和启动检查

**Files:** `trader/reconcile.py` `trader/startup.py` `trader/killswitch.py` · Test: `tests/test_reconcile.py` `tests/test_startup.py`

- [x] 测试：券商有本地没有的持仓 → 未解决差异；本地有券商没有 → 标记平仓；缺止损 → 补挂；止损数量多于持仓 → 重挂为持仓数量。
- [x] 测试：账户号不符、live 缺环境变量 → 致命；延迟行情、时钟偏差 → 只读。
- [x] 测试：`HALT` 文件存在时不开新仓；`flatten` 撤单并平仓。
- [x] 实现并通过。

### Task 6: SimBroker、Engine、回测

**Files:** `trader/broker/sim.py` `trader/engine.py` `trader/backtest.py` · Test: `tests/test_sim.py` `tests/test_backtest.py`

- [x] 测试：止损限价单触发、跳空超过限价不成交、止损跳空按开盘价成交、同一根 K 线止损优先于止盈、DAY 父单收盘作废。
- [x] 测试：合成数据上跑完整回测有成交；跑两次交易记录完全一致。
- [x] 实现并通过。

### Task 7: 调度器、实盘主循环、IbkrBroker、CLI

**Files:** `trader/scheduler.py` `trader/app.py` `trader/broker/ibkr.py` `trader/cli.py` · Test: `tests/test_scheduler.py` `tests/test_ibkr_orders.py`

- [x] 测试：常规日时间表与设计文档的表一致；半日市收盘相关任务前移；过期任务按宽限期跳过。
- [x] 测试：`IbkrBroker` 构造的括号单每组只有最后一张 `transmit=True`，子单带 `parentId` 和 OCA。
- [x] 实现并通过。`IbkrBroker` 没有 Gateway 可连，只做订单构造的单元测试。

### Task 8: LLM 旁路和 README

**Files:** `trader/llm/sidecar.py` `README.md` · Test: `tests/test_sidecar.py`

- [x] 测试：旁路用只读连接打开状态库，写入会失败；没有 API key 时生成纯统计的复盘报告。
- [x] 实现并通过；README 写清运行方式、需要校准的默认值和未验证的部分。
