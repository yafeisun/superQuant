# 小盘股低成本动量轮动方案

## 当前策略

策略名称：`small_cap_momentum`

逻辑：

1. 使用小盘候选股票池。
2. 每个交易日更新所有标的收盘价。
3. 每隔固定交易日调仓一次。
4. 计算过去 `momentum_window` 个交易日涨幅。
5. 选择涨幅最高且大于 `min_momentum` 的前 `top_n` 只股票。
6. 按等权目标仓位买入/卖出，目标总仓位 `target_gross_exposure`。
7. 执行层模拟 A 股 100 股整手、T+1、佣金、最低佣金、卖出印花税、滑点、单票/单笔限制。

当前历史最优配置在 [configs/smallcap_best.yaml](/home/chery/Documents/Quant/configs/smallcap_best.yaml)：

```yaml
momentum_window: 40
rebalance_interval: 20
top_n: 10
target_gross_exposure: 0.85
```

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

参数搜索：

```bash
python -m aqt.cli optimize-smallcap --config configs/smallcap.yaml --momentum-windows 40,60,90 --rebalance-intervals 10,20,30 --top-ns 5,8,10
```

排行榜：

```text
runs/optimize_smallcap/leaderboard.csv
```

## 当前结果

回测区间：`2023-12-15` 到 `2026-08-18`

当前最佳配置结果：

```text
end_equity: 1896893.62
total_return: 89.69%
max_drawdown: -23.92%
fills: 285
closed_trades: 175
winning_trades: 97
win_rate: 55.43%
total_cost: 25212.6879
rejections: 10
```

样本外验证区间：`2025-01-02` 到 `2026-08-18`

```text
end_equity: 1041969.99
total_return: 4.20%
max_drawdown: -35.14%
fills: 178
closed_trades: 113
winning_trades: 59
win_rate: 52.21%
total_cost: 11761.6247
rejections: 2
```

结论：全区间历史最优参数表现好，但样本外收益/回撤不够理想，不能直接作为实盘策略。当前更适合作为模拟跟踪基线：每天更新数据，观察后续 1-3 个月是否能稳定改善回撤和收益质量。

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
- 未处理涨跌停、停牌、真实盘口冲击、流动性容量、分红配股细节。
- 当前用日线收盘价撮合，不是分钟/Tick 级成交模拟。
- 真实接券商账号前必须增加账户同步、撤单状态机、盘中风控和盘后对账。

下一步应该做更严格样本外验证：用 2021-2024 搜参数，用 2025-2026 只验证，不再调参。

当前已提供样本外验证配置：

```bash
python -m aqt.cli backtest --config configs/smallcap_oos.yaml
python -m aqt.cli report --run-dir runs/smallcap_oos_backtest
```
