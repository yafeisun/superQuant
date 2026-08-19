# 小盘股低成本动量轮动方案

## 当前策略

策略名称：`small_cap_momentum`

逻辑：

1. 使用小盘候选股票池。
2. 每个交易日更新所有标的收盘价。
3. 每隔固定交易日调仓一次。
4. 计算过去 `momentum_window` 个交易日涨幅。
5. 先做股票健康检查，过滤停牌、ST/退市风险、涨停不可买、流动性不足、PE 异常、流通市值不符合小盘范围的股票。
6. 在健康合格股票里，选择涨幅最高且大于 `min_momentum` 的前 `top_n` 只股票。
7. 按等权目标仓位买入/卖出，目标总仓位 `target_gross_exposure`。
8. 执行层模拟 A 股 100 股整手、T+1、佣金、最低佣金、卖出印花税、滑点、单票/单笔限制。

当前历史最优配置在 [configs/smallcap_best.yaml](/home/chery/Documents/Quant/configs/smallcap_best.yaml)：

```yaml
momentum_window: 40
rebalance_interval: 20
top_n: 5
target_gross_exposure: 0.85
```

模拟跟踪和准实盘配置最多持有 5 只，先做健康过滤，再在剩余股票里优中选优，避免组合太分散导致监控质量下降。

## 股票健康检查

健康检查配置在 `health` 段，当前会拉取并缓存 `data/health/latest.csv`。每天的本地虚拟实盘决策目录也会输出 `health.csv`，用于复盘每只股票为什么可买或被过滤。

主要过滤项：

- 停牌：停牌股票不允许买入。
- ST/退市风险：ST、*ST、名称含退的股票不允许买入。
- 涨停：默认不追买涨停股，避免无法成交或次日回撤风险。
- 流动性：换手率、成交额低于阈值的不买。
- 估值：PE 小于等于 0 或过高的不买。
- 小盘范围：流通市值不在配置区间内的不买。

健康数据采用买入侧失败关闭：如果公开数据源不可用或没有缓存，系统不会新开仓，只继续处理已有持仓的卖出和风险退出。

## 运行流程

```bash
source .venv/bin/activate
python -m aqt.cli fetch-smallcap-universe --limit 20 --min-market-cap 2000000000 --max-market-cap 12000000000
python -m aqt.cli fetch-akshare --config configs/smallcap_best.yaml --adjust qfq
python -m aqt.cli backtest --config configs/smallcap_best.yaml
python -m aqt.cli paper-run --config configs/smallcap_best.yaml
python -m aqt.cli daily-signal --config configs/smallcap_best.yaml
python -m aqt.cli report --run-dir runs/smallcap_best_paper
```

`daily-signal` 的口径是收盘后生成、下一交易日执行。它输出：

- `runs/daily_signal/summary.txt`：是否调仓、下次调仓还剩几天、买卖计划。
- `runs/daily_signal/orders.csv`：候选订单、估算成交金额、估算成本、是否允许下单及原因。
- `runs/daily_signal/positions.csv`：当前持仓、T+1 下可卖数量、浮动盈亏。
- `runs/daily_signal/rankings.csv`：全股票池动量排名。

## 技术位退出

小盘动量策略现在不再使用固定百分比止盈止损，而是使用支撑位和看涨目标位两个动态变量，配置在 `configs/smallcap_best.yaml`：

```yaml
support_window: 20
target_window: 60
trend_window: 5
risk_reward_ratio: 2.0
```

含义：

- `support_window`：用最近 20 个交易日低点作为动态支撑位。
- `target_window`：用最近 60 个交易日高点作为压力/看涨目标参考。
- `risk_reward_ratio`：用成本价到支撑位的风险距离，推导至少 2 倍风险回报的目标价。
- `trend_window`：用最近 5 个交易日趋势确认是否走弱。

卖出不再是“跌到某个固定百分比就砍”。只有跌破支撑位且短期趋势走弱，才生成 `support_break_trend_down`；涨到看涨目标位后，如果短期趋势衰减，才生成 `target_reached_trend_fade`，体现“趋势没坏继续拿，趋势衰减落袋为安”。

参数搜索：

```bash
python -m aqt.cli optimize-smallcap --config configs/smallcap.yaml --momentum-windows 40,60,90 --rebalance-intervals 10,20,30 --top-ns 3,5
```

排行榜：

```text
runs/optimize_smallcap/leaderboard.csv
```

## 当前结果

当前配置口径：最多 5 只，技术位退出。以下结果来自本地 `paper-run` 重算，只作为工程验证和后续跟踪基线。

回测区间：`2021-01-04` 到 `2026-08-18`

当前最佳配置结果：

```text
end_equity: 1175407.27
total_return: 17.54%
max_drawdown: -51.22%
fills: 445
closed_trades: 253
winning_trades: 104
win_rate: 41.11%
total_cost: 50049.4379
rejections: 23
```

样本外验证区间：`2025-01-02` 到 `2026-08-18`

```text
end_equity: 857462.11
total_return: -14.25%
max_drawdown: -29.89%
fills: 126
closed_trades: 73
winning_trades: 28
win_rate: 38.36%
total_cost: 11808.5404
rejections: 1
```

结论：最多 5 只和技术位退出更符合管控要求，但当前样本外收益为负，不能直接作为真实资金策略。当前更适合作为模拟跟踪基线：每天更新数据，观察后续 1-3 个月是否能稳定改善回撤和收益质量，再考虑继续优化参数和健康评分。

当前模拟持仓在：

```text
runs/smallcap_best_paper/positions.csv
```

闭合交易盈亏在：

```text
runs/smallcap_best_paper/trades.csv
```

## 风险边界

这个结果不能直接理解成未来可复现收益。当前版本仍有几个重要限制：

- 小盘池目前在行情列表接口不稳定时会 fallback 到内置候选池，文件里用 `source=bootstrap` 标记。
- 未做严格样本内/样本外切分，参数搜索仍可能过拟合。
- 已加入基础停牌、ST、涨停、流动性、PE、市值过滤，但公开数据源可能延迟或缺失；真实交易前仍必须用券商盘口和交易所状态复核。
- 当前用日线收盘价撮合，不是分钟/Tick 级成交模拟。
- 真实接券商账号前必须增加账户同步、撤单状态机、盘中风控和盘后对账。

下一步应该做更严格样本外验证：用 2021-2024 搜参数，用 2025-2026 只验证，不再调参。

当前已提供样本外验证配置：

```bash
python -m aqt.cli backtest --config configs/smallcap_oos.yaml
python -m aqt.cli report --run-dir runs/smallcap_oos_backtest
```
