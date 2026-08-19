# 本地虚拟实盘

本地虚拟实盘用于在没有真实交易账号时模拟运行策略。它不连接券商，不真实下单；策略产生的订单会交给本地 `PaperBroker` 撮合，按收盘价加滑点默认成交，并记录佣金、印花税、100 股手数和 T+1 可卖约束。

## 启动方式

首次运行建议先用已有本地行情验证：

```bash
source .venv/bin/activate
python -m automation.run_local_paper_live --config configs/smallcap_live.yaml --date 20260818 --market-end 2026-08-18 --no-fetch
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
decisions/YYYYMMDD/summary.txt 当日运行摘要
decisions/YYYYMMDD/orders.csv  当日买卖计划、成交状态、原因
decisions/YYYYMMDD/position_advice.csv 持仓操作总结和后续建议
decisions/YYYYMMDD/position_advice.md  适合人工复盘阅读的持仓建议
```

同一天重复运行默认不会再次执行买卖，防止重复成交。确实需要重放测试时加 `--rerun`。

每天虚拟实盘结束后会固定输出持仓总结：每只持仓包含成本、现价、浮盈亏、动态支撑位、看涨目标位、短期趋势、健康状态、今日操作和后续建议。建议只按系统规则生成，例如继续持有、关注支撑、到达目标后趋势衰减则落袋、健康状态变差则不加仓只监控退出。

## 盘中盯盘口

盘中虚拟交易入口：

```bash
source .venv/bin/activate
python -m automation.run_local_intraday_paper --config configs/smallcap_live.yaml --no-fetch
```

它会在 A 股交易时间内拉实时行情，监控观察股和持仓股：

- 空仓时，在股票健康检查合格的候选池里最多选择 5 只，用实时价模拟首次建仓。
- 持仓后，每次运行按实时价更新市值。
- 跌破动态支撑位且趋势走弱时模拟卖出，原因写 `intraday_support_break`。
- 涨到看涨目标位且趋势衰减时模拟卖出，原因写 `intraday_target_fade`。
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

- 停牌：来自 AKShare/东方财富停牌接口，停牌股票不允许买入。
- ST、退市风险：ST、*ST、名称含退的股票不允许买入。
- 涨跌停：涨停默认不追买，跌停可继续观察但真实接券商时要按盘口确认能否卖出。
- 流动性：换手率、成交额低于阈值的不买。
- 估值和规模：PE 异常、流通市值超出小盘策略范围的不买。

每天 `decisions/YYYYMMDD/health.csv` 会保存健康评分、是否可交易和拦截原因，用于复盘为什么某只股票没有入选。

健康数据是买入前置条件：如果停牌、ST、换手率、PE、市值等关键数据源不可用，系统不会新开仓，只继续监控已有持仓并允许按技术位卖出。

## 交易日和策略规则

脚本会自动识别 A 股非交易日，周末或节假日直接跳过，不写账本。

当前虚拟实盘使用小盘动量策略：动态排名选股，定期调仓；退出使用动态支撑位和看涨目标位，不使用固定百分比止盈止损。盘中程序会用实时价判断是否跌破支撑、是否到达目标后趋势衰减。

实盘/模拟跟踪配置最多持有 5 只股票，避免分散过度、管理困难。

## 后续接真实账号

真实账号接入时不要复用“默认成交成功”的逻辑。应把 `PaperBroker` 替换为券商 broker：先同步账户和可卖数量，再做风控检查，提交订单后等待真实成交回报，最后以成交回报更新本地账本。
