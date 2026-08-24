# 本地虚拟实盘

本地虚拟实盘用于在没有真实交易账号时模拟运行策略。它不连接券商，不真实下单；策略产生的订单会交给本地 `PaperBroker` 撮合，按 bar 区间、限价、涨跌停、成交量参与率、滑点、佣金、印花税、100 股手数和 T+1 可卖约束保守模拟。

## 启动方式

首次运行建议先用已有本地行情验证：

```bash
source .venv/bin/activate
python -m automation.run_local_paper_live --config configs/smallcap_live.yaml --date 20260818 --market-end 2026-08-18 --no-fetch
python -m automation.run_local_paper_live --config configs/smallcap_live.yaml --date 20260818 --market-end 2026-08-18 --no-fetch --no-refresh-factors
```

每天真实跟踪时去掉 `--no-fetch`，脚本会先拉取最新日线：

```bash
source .venv/bin/activate
python -m automation.run_local_paper_live --config configs/smallcap_live.yaml
```

资金配置在 `configs/smallcap_live.yaml`，当前初始资金为 100000 RMB。

## 输出目录

默认账本目录：

```text
local_runs/paper_live/
```

关键文件：

```text
account.json                   当前模拟账户现金、权益、持仓
equity.csv                     每日权益曲线和收益率
positions.csv                  当前持仓、成本、浮盈、收益率
fills.csv                      模拟成交流水
rejections.csv                 风控拒单流水
processed_dates.csv            已执行日期，防止同一天重复成交
quotes.csv                     盘中行情缓存，用于实时接口失败时兜底
status/latest_*.json           最近一次运行状态
status/events.jsonl            运行状态事件流
decisions/YYYYMMDD/summary.txt 当日运行摘要
decisions/YYYYMMDD/selection_candidates.csv 全股票池候选评分、买入/等待/阻断原因
decisions/YYYYMMDD/orders.csv  当日买卖计划、成交状态、原因
decisions/YYYYMMDD/sell_points.csv 持仓卖点评估和卖出原因
decisions/YYYYMMDD/position_advice.csv 持仓操作总结和后续建议
decisions/YYYYMMDD/position_advice.md  适合人工复盘阅读的持仓建议
decisions/YYYYMMDD/external_factors.csv 新闻/事件/宏观因子评分和原因
```

同一天重复运行默认不会再次执行买卖，防止重复成交。确实需要重放测试时加 `--rerun`。

每天虚拟实盘结束后会固定输出候选和持仓总结。候选总结覆盖全股票池，标记 `BUY_READY`、`BUY_WAIT` 或 `BLOCK`，并写清楚健康、主力资金、动量、波动、回撤、买点位置和目标空间。持仓总结包含成本、现价、浮盈亏、止损支撑位、止盈目标位、短期趋势、健康状态、卖出判断和后续建议。

## 本地看板

生成操作和实盘跟踪看板：

```bash
source .venv/bin/activate
python -m aqt.cli live-dashboard --state-dir local_runs/paper_live --output reports/live_dashboard.html
```

看板会汇总最近运行状态、账户权益、持仓、操作建议、订单、候选股、盘中行情缓存、盘中事件、成交和拒单。默认读取 `configs/smallcap_live.yaml`，给每只持仓 / 候选 / 最近操作标的补最近 K 线、本地资金流指标和订单 / 成交 / 拒单时间线；其他配置可加 `--config path/to/config.yaml`。

它是静态 HTML，不需要启动服务；每次运行命令会刷新文件。

`automation.run_local_paper_live` 和 `automation.run_local_intraday_paper` 默认会在每次运行结束后刷新这份看板。需要跳过时加 `--no-dashboard`，需要换输出位置时加 `--dashboard-output`。

## 状态告警

运行状态统一写在 `local_runs/paper_live/status/`：

```text
latest_paper_live.json
latest_intraday_paper.json
events.jsonl
alerts.jsonl
```

本地检查并派发 warning / error：

```bash
python -m aqt.cli status-alerts --state-dir local_runs/paper_live
```

如果配置 `AQT_ALERT_WEBHOOK_URL`，脚本会把新 warning / error POST 到这个 webhook；没有配置时会写入本地 `alerts.jsonl` outbox，避免静默丢失。自动实盘入口会在每次运行结束后尝试派发一次。

## 盘中盯盘口

盘中虚拟交易入口：

```bash
source .venv/bin/activate
python -m automation.run_local_intraday_paper --config configs/smallcap_live.yaml --no-fetch
```

它会在 A 股交易时间内拉实时行情，监控观察股和持仓股：

- 空仓时，只从全量候选评分里的 `BUY_READY` 选择最多 5 只，用实时价模拟首次建仓；如果买点或主力资金不确认，就继续等待。
- 持仓后，每次运行按实时价更新市值。
- 跌破动态支撑位且趋势走弱时模拟卖出。
- 涨到看涨目标位且趋势衰减时模拟卖出。
- 触发 ST/退市风险硬闸时模拟卖出或等待 T+1 可卖。
- 新闻事件或宏观风险因子转弱时也会降低评分，严重负面时会直接触发卖出或拦截买入。
- 只有发生成交、拒单或权益变化超过 50 RMB 时才追加 `intraday_events.csv`。

常驻运行：

```bash
source .venv/bin/activate
python -m automation.run_local_intraday_paper --config configs/smallcap_live.yaml --no-fetch --loop
```

更稳的方式是交给本机 cron 每 5 分钟调用一次：

```bash
bash scripts/install_local_paper_cron.sh
```

安装后会增加两类本机定时任务：周一到周五 16:15 跑一次收盘后虚拟实盘/复盘；A 股盘中 `09:30-11:30`、`13:00-15:00` 每 5 分钟跑一次盘口观察。脚本内部仍会识别节假日和交易时间，不满足条件会自动跳过。

## 股票健康检查

买入前会先拉取并缓存 `data/health/latest.csv`，用于过滤所有会影响交易的关键状态。当前检查项包括：

- 停牌：实时价或成交额异常时不允许买入。
- ST、退市风险：ST、*ST、名称含退、本地硬风险名单里的股票不允许买入。
- 涨跌停：涨停默认不追买，跌停可继续观察但真实接券商时要按盘口确认能否卖出。
- 流动性：换手率、成交额低于阈值的不买。
- 估值和规模：PE 异常、流通市值超出小盘策略范围的不买。

每天 `decisions/YYYYMMDD/health.csv` 会保存健康评分、是否可交易和拦截原因，用于复盘为什么某只股票没有入选。已知风险名单在 `data/risk/blocked_symbols.csv`。

## 主力资金和买点

准实盘配置会拉取并缓存 `data/flow/latest.csv`。当前要求近 5 个资金流交易日里主力净流入至少 3 天为正，净流入合计和平均净占比满足配置阈值。主力资金只是确认项，不是单独买入理由。

买点评估会检查：

- 当前价格距离支撑位不能太远。
- 当前价格不能贴近目标位追高。
- 最近短线涨幅不能过热。
- 短期趋势不能明显走弱。

因此高动量股票也可能只给 `BUY_WAIT`，系统会等价格和资金流更合适再买。

健康数据是买入前置条件：如果停牌、ST、换手率、PE、市值等关键数据源不可用，系统不会新开仓，只继续监控已有持仓并允许按技术位卖出。

## 串行和并行边界

交易决策、订单执行、账本写入和同日防重跑保持串行，确保每一次操作可复盘、可解释、可重放。数据抓取和因子更新可以并行；当前主力资金抓取已经按股票并发，默认最多 4 个 worker，避免接口抖动导致整条链路卡死。

## 本地撮合假设

本地 `PaperBroker` 仍然只是纸盘撮合，不代表真实成交。它现在会：

- 拒绝未触及 bar 区间的限价单。
- 拒绝涨停买入和跌停卖出。
- 按 `risk.max_volume_participation_pct` 限制单笔成交量，超过部分在允许部分成交时写成 `partial_fill_volume_limit` 拒单。
- 用 `risk.volume_unit_multiplier` 把数据源成交量换算为股；小盘实盘配置按 1 手=100 股处理。

## 交易日和策略规则

脚本会自动识别 A 股非交易日，周末或节假日直接跳过，不写账本。

当前虚拟实盘使用小盘多因子候选评估加动量轮动执行框架：全量评分、优中选优、只买 `BUY_READY`；退出使用动态支撑位和看涨目标位，不使用固定百分比止盈止损。盘中程序会用实时价判断是否跌破支撑、是否到达目标后趋势衰减，以及是否出现健康硬风险、资金流恶化、新闻事件风险或宏观 risk-off。

实盘/模拟跟踪配置最多持有 5 只股票，避免分散过度、管理困难。

## 后续接真实账号

真实账号接入时不要复用“默认成交成功”的逻辑。应把 `PaperBroker` 替换为券商 broker：先同步账户和可卖数量，再做风控检查，提交订单后等待真实成交回报，最后以成交回报更新本地账本。
