# GitHub Actions 自动跟踪任务

## 收盘后选股

Workflow：`.github/workflows/a-share-after-close.yml`

时间：周一到周五北京时间 16:15。

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

## 盘中观察

Workflow：`.github/workflows/a-share-intraday-watch.yml`

时间：周一到周五 A 股盘中近似每 5 分钟运行一次。

输出目录：

```text
reports/intraday/YYYYMMDD/HHMM/
```

固定文件名：

```text
summary.txt
orders.csv
positions.csv
rankings.csv
watchlist.csv
metrics.csv
manifest.txt
```

用途：监控观察股和模拟持仓股，判断按当前策略是否需要买入、卖出或继续持有。

盘中任务同样会尝试拉取当天数据。当前策略仍以日线信号为主，因此盘中快照主要用于观察持仓、观察池和是否触发既定调仓计划，不做盘口级追涨杀跌。

## GitHub 设置

仓库需要允许 workflow 写入报告文件：

```text
Settings -> Actions -> General -> Workflow permissions -> Read and write permissions
```

如果自动提交报告失败，优先检查这个设置。

## 重要边界

当前 GitHub Actions 任务只做研究、复盘、模拟跟踪，不做真实下单。GitHub Actions 不是实盘交易系统，网络、时延、任务调度都不可控。

后续接真实账号时，建议：

1. GitHub Actions 继续负责研究和日报。
2. 实盘交易服务部署在本地机器或 VPS。
3. 实盘服务读取 `orders.csv`，再执行账户同步、风控、人工确认或自动下单。
4. 真实成交回报必须落库，并反向校验模拟持仓。
