# GitHub Actions 自动跟踪任务

## 收盘后选股

Workflow：`.github/workflows/a-share-after-close.yml`

时间：周一到周五北京时间 16:15。这个任务是收盘后选股/复盘，不是开盘任务；选择 16:15 是为了给公开日线数据源留出更新缓冲。

输出目录：

```text
reports/daily/YYYYMMDD/
```

固定文件名：

```text
summary.txt
orders.csv
positions.csv
rankings.csv
metrics.csv
trades.csv
equity.csv
manifest.txt
```

用途：每天收盘后根据最新日线重新动态选股，生成下一交易日的目标订单和原因，用于复盘。

脚本运行时会自动把配置里的行情结束日期覆盖为北京时间当天；如果当天不是交易日或数据源尚未更新，输出会落在最新可用交易日。

任务启动时会先判断目标日期是否为 A 股交易日。周末会直接跳过；工作日会优先使用 AKShare/Sina 交易日历确认，并缓存到 `data/calendar/a_share_trade_dates.csv`。如果判断为节假日或休市日，任务只输出 `skip` 日志，不下载行情、不运行策略、不写报告。

## 盘中观察

Workflow：`.github/workflows/a-share-intraday-watch.yml`

时间：周一到周五 A 股盘中 `09:30-11:30`、`13:00-15:00` 近似每 5 分钟运行一次。

输出目录：

```text
reports/intraday/YYYYMMDD/
```

固定文件名：

```text
events.csv
latest_state.json
watchlist.csv
manifest.txt
```

用途：监控观察股和模拟持仓股，判断按当前策略是否需要买入、卖出或继续持有。

盘中任务同样会尝试拉取当天数据。当前策略仍以日线信号为主，因此盘中任务主要用于观察持仓、观察池和是否触发既定调仓计划，不做盘口级追涨杀跌。

为了避免 5 分钟任务生成过多文件，盘中输出采用按天聚合：

- `events.csv`：只有策略状态发生变化时才追加一行，记录时间、权益、持仓数量、允许/阻止订单数量、订单股票、观察股票和核心绩效指标。
- `latest_state.json`：保存最近一次策略状态和指纹，用于判断下一次运行是否需要写入。
- `watchlist.csv`：保存当前需要观察的股票池，来自动量排名前 10 和当前模拟持仓。

如果策略状态没有变化，任务不会提交新报告。

盘中任务启动时同样会先判断是否为 A 股交易日。休市日直接跳过，不写 `events.csv`。

## GitHub 设置

仓库需要允许 workflow 写入报告文件：

```text
Settings -> Actions -> General -> Workflow permissions -> Read and write permissions
```

如果自动提交报告失败，优先检查这个设置。

## 重要边界

当前 GitHub Actions 任务只做研究、复盘、模拟跟踪，不做真实下单。GitHub Actions 不是实盘交易系统，网络、时延、任务调度都不可控。

当前模拟撮合假设信号价格附近可以成交，并按配置里的佣金、印花税、滑点、100 股手数、T+1 约束记账。后续接真实账号时，买卖提交和成交确认必须由本机或 VPS 上的实盘服务根据盘口、账户可用资金、可卖数量和券商成交回报来确认。

后续接真实账号时，建议：

1. GitHub Actions 继续负责研究和日报。
2. 实盘交易服务部署在本地机器或 VPS。
3. 实盘服务读取 `orders.csv`，再执行账户同步、风控、人工确认或自动下单。
4. 真实成交回报必须落库，并反向校验模拟持仓。
