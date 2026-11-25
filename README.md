# trader

在 IBKR 上运行的确定性动量波段规则引擎。模拟盘和实盘共用同一套代码，只靠 `config.yaml` 里的 `mode` 切换。设计见 [docs/design.md](docs/design.md)。

这不是投资建议。策略规则是对公开访谈和第三方整理的归纳，机械化版本的表现与任何人的实际成绩没有对应关系。

## 现状

| 部分 | 状态 |
| --- | --- |
| 策略引擎、风控闸门、执行层、状态库、对账、启动检查、总开关、调度器 | 已实现，有单元测试 |
| 回测（`SimBroker`，和实盘共用 `Engine`） | 已实现，合成数据上跑通，重放两次结果一致 |
| `IbkrBroker`（`ib_async`） | 已实现，**没有连过真实的 IB Gateway**，只测了订单构造 |
| LLM 旁路（复盘报告、主题标签） | 已实现，没有实际调用过 API |
| IB Gateway 每日重启后的自动登录 | 未包含，需要另行配置（例如 IBC） |

第一次连模拟盘之前，先读下面的「上模拟盘前要核对的事」。

## 安装

```powershell
python -m venv .venv
```

```powershell
.venv\Scripts\python -m pip install -r requirements.txt
```

```powershell
Copy-Item config.example.yaml config.yaml
```

然后在 `config.yaml` 里填账户号。另外准备两个文件（都已加入 `.gitignore`）：

- `universe.txt`：候选股票池，每行一个代码。盘前扫描只在这个池子里排名和筛选。
- `earnings.csv`：两列 `symbol,date`。没有这个文件时系统会报警一次，并且**不执行财报窗口限制**。

## 命令

```powershell
python -m pytest
```

```powershell
python -m trader backtest --data data --start 2025-01-02 --end 2025-12-31 --replay-check
```

```powershell
python -m trader check
```

```powershell
python -m trader run
```

```powershell
python -m trader status
```

```powershell
python -m trader review --no-llm
```

```powershell
python -m trader flatten --confirm DUxxxxxxx
```

- `backtest`：`--data` 目录下放 `daily/代码.csv` 和 `5min/代码.csv`，列为 `ts,open,high,low,close,volume`。5 分钟线的时间是 K 线开始时间，不带时区时按美东时间处理。`--replay-check` 会跑两遍并比较交易记录。
- `check`：只跑一遍启动检查清单。发现持仓缺止损时会补挂，除此之外不下单。
- `run`：启动主循环。Ctrl+C 退出后，止损单仍然挂在券商服务器上。
- `flatten`：撤掉全部挂单并平掉全部持仓，必须带上账户号确认。
- 停止开新仓：在工作目录下建一个名为 `HALT` 的文件。程序不会自己删除它。

## 切到实盘

1. 把 `mode` 改成 `live`。
2. 设置环境变量 `TRADING_LIVE_CONFIRM` 为实盘账户号。
3. 其余不动。启动检查会核对网关登录的账户号：paper 必须以 `DU` 开头，live 不得以 `DU` 开头。

## 模块

| 文件 | 职责 |
| --- | --- |
| `trader/strategy.py` | `decide()`：纯函数，产出 ENTER / EXIT / NONE 和理由 |
| `trader/watchlist.py` | 盘前筛选 |
| `trader/risk.py` | 风控闸门 |
| `trader/execution.py` | 幂等提交括号单，处理成交和订单状态 |
| `trader/reconcile.py` | 对账、重启恢复、补挂止损 |
| `trader/engine.py` | 各时点任务的业务逻辑，实盘和回测共用 |
| `trader/broker/ibkr.py` | IBKR 适配 |
| `trader/broker/sim.py` | 回测撮合 |
| `trader/scheduler.py`、`trader/app.py` | 时间表和实盘主循环 |
| `trader/startup.py`、`trader/killswitch.py` | 启动检查、`HALT` 和 `flatten` |
| `trader/store.py` | SQLite 状态库 |
| `trader/llm/sidecar.py` | 复盘报告和主题标签，只读状态库 |

## 设计文档没定、这里先给了默认值的地方

这些都在 `config.yaml` 的 `strategy` 一节，需要用回测校准。

| 项目 | 默认 |
| --- | --- |
| 形态 A 的「内包日或极窄幅日」 | 最近 3 个交易日内有内包日，或有一日振幅不超过 ADR% 的一半 |
| 「均线靠拢」 | EMA9/21/50 的最大差不超过 1 个 ADR |
| 形态 B 的「盘中转强」 | 15:50 时最新价高于前一日 EMA9，且在当日振幅的上 30% 内，并要求 EMA9 > EMA21 > EMA50 |
| 3R 分批比例 | 三分之一。不足 1 股时不分批 |
| 允许最大滑点 | 触发价的 0.3% |

## 和直觉不一样的行为

- **单笔订单金额超限是拒绝，不是缩量**（按设计文档）。账户净值的 30% 一旦超过 `max_order_usd`，窄止损的交易会全部被拒。模拟盘默认 2000 美元上限，对应的净值上限约 6600 美元。
- **同一只股票一天只入场一次**。形态 A 的突破单挂出后，当天不会再对它做形态 B。
- **对账出现修复不了的差异后进入只读，直到重启**。未知来源的持仓、数量对不上、未知订单都属于这一类，程序只报警不处理。
- **整数股**。算出不足 1 股就放弃这笔交易。
- **回测的成交假设偏悲观**：同一根 K 线里止损先于止盈；跳空越过止损按开盘价成交。
- `MODIFY_STOP` 动作和 `subscribe_bars` 接口已定义，第一版策略没有用到：跟踪止损是「收盘跌破 EMA9 次日开盘卖出」，盘中检查靠每 5 分钟拉一次 K 线。

## 上模拟盘前要核对的事

`IbkrBroker` 是照 `ib_async` 的接口写的，没有连过真实网关。第一次连模拟盘时逐项确认：

- [ ] `python -m trader check` 能通过，账户号、实时行情、时钟偏差三项结果符合实际
- [ ] 括号单在 TWS 里显示为两组，子单在父单成交前不生效，A 组的止损和止盈互为 OCA
- [ ] 父单部分成交后当日作废时，对账把止损数量改成了实际持仓数量
- [ ] 账户基础货币不是美元时（IBKR Canada 默认加元），`account()` 换算出的美元净值正确
- [ ] 断网一次、重启 Gateway 一次，重连后对账零差异
- [ ] 股票池较大时，盘前扫描拉日线不触发 IB 的历史数据限速
- [ ] 订单被拒时能收到报警，且没有自动重试

设计文档末尾的待确认事项（账户类型、碎股、数据源、报警通道等）仍然有效。
