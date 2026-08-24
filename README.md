# A 股量化交易系统最小骨架

这个目录先搭建一套可以本地运行的 A 股量化系统，用于在没有真实交易账号时完成“数据 -> 策略 -> 回测 -> 虚拟实盘”的闭环。

当前阶段不追求微秒级交易链路。优先目标是把系统边界做清楚，后续再接入 `vn.py` 交易网关、`RQAlpha` 回测生态、`Qlib` 机器学习研究平台或 `AKShare` 行情数据。

## 开源项目选型

已核对的主流项目：

| 项目 | 适合用途 | 本系统中的位置 |
| --- | --- | --- |
| `vnpy/vnpy` | 国内交易网关、CTA、实盘交易框架 | 未来接真实账号和柜台接口 |
| `ricequant/rqalpha` | 多品种回测框架 | 未来替换或对照本地回测引擎 |
| `microsoft/qlib` | AI 量化研究、特征、模型、组合优化 | 未来做因子研究和机器学习流水线 |
| `akfamily/akshare` | A 股公开数据接口 | 未来替换当前样例 CSV 数据源 |

建议路线：先用本仓库骨架跑通虚拟实盘，再接 `AKShare` 拉真实日线数据，然后并行学习 `vn.py` 的网关和事件引擎。不要一开始就把所有框架混在一起，否则很难定位策略问题、数据问题和执行问题。

## 快速开始

当前环境已经有 `pandas`、`numpy`、`yaml`，可以直接运行：

```bash
python3 -m aqt.cli init-data
python3 -m aqt.cli backtest --config configs/demo.yaml
python3 -m aqt.cli paper-run --config configs/demo.yaml --cycles 3
```

安装 AKShare 后可改用真实 A 股日线：

```bash
pip install akshare
python3 -m aqt.cli fetch-akshare --config configs/demo.yaml --adjust qfq
```

当前下载命令会优先尝试东方财富直连接口；如果网络或接口断开，会自动 fallback 到腾讯行情接口，最后才尝试 AKShare。这样做是为了让本地系统在公开数据源短时不稳定时仍能落盘统一 CSV。

输出会写入：

- `data/raw/`：样例 A 股日线 CSV
- `runs/backtest/`：回测权益曲线、成交和持仓
- `runs/paper/`：虚拟实盘账本、订单、成交和账户状态

## 目录结构

```text
aqt/
  broker.py        虚拟经纪商、撮合、手续费、A 股交易约束
  cli.py           命令行入口
  config.py        配置加载与校验
  data.py          CSV 数据加载、样例数据生成、可扩展数据源边界
  engine.py        回测和虚拟实盘主循环
  models.py        订单、成交、账户、持仓等领域对象
  strategy.py      策略接口和均线交叉样例策略
  universe.py      point-in-time 股票池解析和每日资格判断
  walk_forward.py  训练区间搜参、样本外冻结验证
configs/demo.yaml  示例配置
docs/roadmap.md    分阶段搭建指南
```

## 下一步

1. 安装 `akshare`，把 `data/raw` 中的样例 CSV 换成真实 A 股日线。
2. 增加更多 A 股约束：涨跌停、停牌、ST、复权、交易日历、T+1 精细化。
3. 接入 `vn.py` 的 CTP/证券网关前，先用本系统产出的目标持仓和风控检查作为真实交易前置层。
4. 把策略研究迁到 `Qlib`，将模型输出转成每日目标权重，再交给本系统或 `vn.py` 执行。

## 小盘策略方案

当前已加入小盘多因子候选评估和动量轮动执行框架，模拟跟踪最多 5 只。买入前会全量评估股票池，过滤停牌、ST/退市风险、涨停、流动性不足、PE 异常和市值范围异常，并结合主力资金持续流入、新闻/事件因子、宏观风险、波动、回撤、买点位置和目标空间决定 `BUY_READY`/`BUY_WAIT`/`BLOCK`。回测和压力测试只负责筛掉明显不行的策略，不证明策略有效。说明见 [docs/smallcap-strategy.md](/home/chery/Documents/Quant/docs/smallcap-strategy.md) 和 [docs/strategy-optimization.md](/home/chery/Documents/Quant/docs/strategy-optimization.md)。常用命令：

```bash
source .venv/bin/activate
python -m aqt.cli fetch-akshare --config configs/smallcap_best.yaml --adjust qfq
python -m aqt.cli fetch-money-flow --config configs/smallcap_live.yaml
python -m aqt.cli fetch-smallcap-universe --point-in-time --as-of 2026-08-24 --limit 20
python -m aqt.cli ingest-event-factors --input data/factors/events.jsonl --output data/factors/events.csv
python -m aqt.cli selection-candidates --config configs/smallcap_live.yaml --output reports/selection_candidates.csv
python -m aqt.cli paper-run --config configs/smallcap_best.yaml
python -m aqt.cli walk-forward --config configs/smallcap.yaml --output runs/walk_forward
python -m aqt.cli stress-test --config configs/smallcap.yaml --output runs/stress_test
python -m aqt.cli report --run-dir runs/smallcap_best_paper
python -m aqt.cli history-report --run-dir runs/smallcap_best_paper --output /tmp/smallcap_best_paper_history.html
```

`history-report` 会把 `positions.csv`、`equity.csv`、`fills.csv` 和 `trades.csv` 生成一个按股票展开的 HTML 历史看板。

## GitHub 自动化

已加入 GitHub Actions 自动任务，说明见 [docs/github-automation.md](/home/chery/Documents/Quant/docs/github-automation.md)。

- 收盘后选股：`reports/daily/YYYYMMDD/`
- 盘中 5 分钟观察：`reports/intraday/YYYYMMDD/`，状态变化时追加到 `events.csv`

这些任务用于研究、复盘和模拟跟踪，不直接执行真实下单。自动任务会跳过 A 股非交易日；模拟成交默认按策略信号成交成功记账，后续接真实账号时再由本机实盘程序根据盘口和券商成交回报确认。

## 本地虚拟实盘

已加入本地虚拟实盘入口，说明见 [docs/local-paper-live.md](/home/chery/Documents/Quant/docs/local-paper-live.md)。当前配置使用 100000 RMB 初始资金：

```bash
source .venv/bin/activate
python -m automation.run_local_paper_live --config configs/smallcap_live.yaml
python -m automation.run_local_paper_live --config configs/smallcap_live.yaml --no-fetch --no-refresh-factors
```

账本默认写入 `local_runs/paper_live/`，包含账户、持仓、成交、权益曲线和每日决策。每日决策目录会保存 `selection_candidates.csv`、`orders.csv`、`position_advice.csv`、`sell_points.csv`、`health.csv`、`money_flow.csv` 和 `external_factors.csv`，用于复盘每只股票为什么买、为什么等、为什么卖或继续持有。小盘策略还会把低效持仓的资金回收退出写成 `capital_recycle_*` 成交原因。该目录默认不提交到 GitHub。

本地定时任务安装：

```bash
bash scripts/install_local_paper_cron.sh
```

安装后本机会在 16:15 收盘后跑虚拟实盘/复盘，并在 A 股正式交易时段 `09:30-11:30`、`13:00-15:00` 每 5 分钟盯实时盘口；脚本内部会自动跳过节假日和非交易时间。

把本地虚拟实盘账本里的实际操作和实际持仓刷新到 README：

```bash
python -m aqt.cli account-activity --state-dir local_runs/paper_live --output reports/account_activity.md --update-readme
```

生成本地操作和实盘跟踪看板：

```bash
python -m aqt.cli live-dashboard --state-dir local_runs/paper_live --output reports/live_dashboard.html --config configs/smallcap_live.yaml
```

看板会按单票补最近 K 线、资金流摘要和订单 / 成交 / 拒单时间线。

运行状态告警会写入 `local_runs/paper_live/status/`。配置 webhook 后可推送 warning/error：

```bash
export AQT_ALERT_WEBHOOK_URL="https://example.com/webhook"
python -m aqt.cli status-alerts --state-dir local_runs/paper_live
```

<!-- account-activity:start -->
### 账户实际操作和持仓

数据源：`local_runs/paper_live`。原始账本目录默认不提交，README 只保存这份摘要快照。

最新数据日期：`2026-08-18`。

#### 账户概览

| 日期 | 现金 | 持仓市值 | 账户权益 | 已实现盈亏 | 收益率 | 累计成交 |
| --- | --- | --- | --- | --- | --- | --- |
| 2026-08-18 | 22,189.68 | 77,737.00 | 99,926.68 | 0.00 | -0.07% | 10 |

#### 每个股票的实际操作和实际持仓

| 股票 | 最近实际操作 | 操作日期 | 累计买入 | 累计卖出 | 当前持仓 | 可卖 | 成本价 | 现价 | 市值 | 浮盈亏 | 收益率 | 卖出判断 | 卖点理由 | 止损位 | 止盈位 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 002883.SZ | 买入 | 2026-08-18 | 900 | 0 | 900 | 900 | 9.3928 | 9.3900 | 8,451.00 | -2.54 | -0.03% | 卖出 | ST/退市风险，必须退出 | 8.0100 | 12.1585 |
| 002896.SZ | 买入 | 2026-08-18 | 100 | 0 | 100 | 100 | 84.9155 | 84.8900 | 8,489.00 | -2.55 | -0.03% | 持有 | 仍在支撑上方，未到卖点 | 58.5100 | 137.7264 |
| 002923.SZ | 买入 | 2026-08-18 | 700 | 0 | 700 | 700 | 11.6635 | 11.6600 | 8,162.00 | -2.45 | -0.03% | 持有 | 仍在支撑上方，未到卖点 | 10.0100 | 14.9705 |
| 003015.SZ | 买入 | 2026-08-18 | 600 | 0 | 600 | 600 | 12.6038 | 12.6000 | 7,560.00 | -2.27 | -0.03% | 持有 | 仍在支撑上方，未到卖点 | 10.0700 | 17.6713 |
| 003016.SZ | 买入 | 2026-08-18 | 1,200 | 0 | 1,200 | 1,200 | 7.0221 | 7.0200 | 8,424.00 | -2.53 | -0.03% | 持有 | 仍在支撑上方，未到卖点 | 6.1000 | 8.8663 |
| 003017.SZ | 买入 | 2026-08-18 | 300 | 0 | 300 | 300 | 23.3170 | 23.3100 | 6,993.00 | -2.10 | -0.03% | 持有 | 仍在支撑上方，未到卖点 | 19.1100 | 31.7310 |
| 003020.SZ | 买入 | 2026-08-18 | 400 | 0 | 400 | 400 | 19.4158 | 19.4100 | 7,764.00 | -2.33 | -0.03% | 持有 | 仍在支撑上方，未到卖点 | 15.5100 | 27.2275 |
| 003023.SZ | 买入 | 2026-08-18 | 300 | 0 | 300 | 300 | 27.7583 | 27.7500 | 8,325.00 | -2.50 | -0.03% | 持有 | 仍在支撑上方，未到卖点 | 16.7100 | 49.8550 |
| 603655.SH | 买入 | 2026-08-18 | 200 | 0 | 200 | 200 | 33.7601 | 33.7500 | 6,750.00 | -2.02 | -0.03% | 观察卖点 | 阶段高点回撤较大，观察卖点 | 23.7000 | 53.8804 |
| 603679.SH | 买入 | 2026-08-18 | 300 | 0 | 300 | 300 | 22.7368 | 22.7300 | 6,819.00 | -2.05 | -0.03% | 持有 | 仍在支撑上方，未到卖点 | 15.3800 | 37.4505 |

#### 每日按股票实际成交

| 日期 | 股票 | 方向 | 买入股数 | 买入均价 | 卖出股数 | 卖出均价 | 成交次数 | 原因 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 2026-08-18 | 603679.SH | 买入 | 300 | 22.7368 | 0 |  | 1 | initial_entry |
| 2026-08-18 | 603655.SH | 买入 | 200 | 33.7601 | 0 |  | 1 | initial_entry |
| 2026-08-18 | 003023.SZ | 买入 | 300 | 27.7583 | 0 |  | 1 | initial_entry |
| 2026-08-18 | 003020.SZ | 买入 | 400 | 19.4158 | 0 |  | 1 | initial_entry |
| 2026-08-18 | 003017.SZ | 买入 | 300 | 23.3170 | 0 |  | 1 | initial_entry |
| 2026-08-18 | 003016.SZ | 买入 | 1,200 | 7.0221 | 0 |  | 1 | initial_entry |
| 2026-08-18 | 003015.SZ | 买入 | 600 | 12.6038 | 0 |  | 1 | initial_entry |
| 2026-08-18 | 002923.SZ | 买入 | 700 | 11.6635 | 0 |  | 1 | initial_entry |
| 2026-08-18 | 002896.SZ | 买入 | 100 | 84.9155 | 0 |  | 1 | initial_entry |
| 2026-08-18 | 002883.SZ | 买入 | 900 | 9.3928 | 0 |  | 1 | initial_entry |

#### 最近实际成交明细

| 日期 | 股票 | 方向 | 数量 | 成交价 | 手续费 | 印花税 | 原因 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 2026-08-18 | 002883.SZ | 买入 | 900 | 9.3928 | 5.0000 | 0.0000 | initial_entry |
| 2026-08-18 | 002896.SZ | 买入 | 100 | 84.9155 | 5.0000 | 0.0000 | initial_entry |
| 2026-08-18 | 002923.SZ | 买入 | 700 | 11.6635 | 5.0000 | 0.0000 | initial_entry |
| 2026-08-18 | 003015.SZ | 买入 | 600 | 12.6038 | 5.0000 | 0.0000 | initial_entry |
| 2026-08-18 | 003016.SZ | 买入 | 1,200 | 7.0221 | 5.0000 | 0.0000 | initial_entry |
| 2026-08-18 | 003017.SZ | 买入 | 300 | 23.3170 | 5.0000 | 0.0000 | initial_entry |
| 2026-08-18 | 003020.SZ | 买入 | 400 | 19.4158 | 5.0000 | 0.0000 | initial_entry |
| 2026-08-18 | 003023.SZ | 买入 | 300 | 27.7583 | 5.0000 | 0.0000 | initial_entry |
| 2026-08-18 | 603655.SH | 买入 | 200 | 33.7601 | 5.0000 | 0.0000 | initial_entry |
| 2026-08-18 | 603679.SH | 买入 | 300 | 22.7368 | 5.0000 | 0.0000 | initial_entry |
<!-- account-activity:end -->

仅供工程学习和研究验证，不构成投资建议。
