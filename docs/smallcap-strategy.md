# 小盘股低成本动量轮动方案

## 当前策略

策略名称：`small_cap_momentum`

逻辑：

1. 使用小盘候选股票池。
2. 每个交易日更新所有标的收盘价。
3. 全量评估股票池，而不是只在动量前几名里拍脑袋选股。
4. 对每只股票计算健康、主力资金、事件因子、宏观因子、动量、波动、回撤、买点位置、目标空间和流动性/估值得分。
5. 先做股票健康检查，过滤停牌、ST/退市风险、涨停不可买、流动性不足、PE 异常、流通市值不符合小盘范围的股票。
6. 再做主力资金确认，要求近期主力净流入持续性、净流入金额和净占比满足阈值。
7. 再看新闻/事件和宏观因子，来源必须结构化、可追溯、可校验，负面风险只做拦截或减分，不让模型直接下单。
8. 买入前必须通过买点评估：不能离支撑位太远，不能贴近目标位追高，不能刚短线急拉，趋势不能明显走弱。
9. 只有 `BUY_READY` 可以生成买入订单；`BUY_WAIT` 和 `BLOCK` 都会写明原因。
10. 已有持仓每天做卖点评估，输出止损支撑、止盈目标、距止损/目标空间、趋势和卖出原因。
11. 低效持仓触发资金效率退出：长期跑输、动量不足、已回到保本区或滞留过久的标的，优先卖出收回本金，给更强候选腾出仓位。
12. 执行层模拟 A 股 100 股整手、T+1、佣金、最低佣金、卖出印花税、滑点、单票/单笔限制。

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

## 主力资金确认

准实盘配置会拉取个股资金流，默认写入：

```text
data/flow/latest.csv
```

当前买入确认规则在 `configs/smallcap_live.yaml`：

```yaml
flow:
  enabled: true
  lookback_days: 5
  min_positive_days: 3
  min_main_net_inflow_ratio: 0.5
  min_main_net_inflow_amount: 0
```

含义：最近 5 个资金流交易日里，主力净流入至少 3 天为正，主力净流入合计不为负，平均主力净流入占比至少为正。资金流不是单独买入理由，只作为“有人持续进场”的确认项；如果价格位置不好，仍然只给 `BUY_WAIT`。

## 事件和宏观因子

事件因子和宏观因子默认通过 `factors` 段接入：

```yaml
factors:
  enabled: true
  event_path: data/factors/events.csv
  macro_path: data/factors/macro.csv
  event_lookback_days: 10
  min_event_confidence: 0.6
  negative_event_score_block: -35
  macro_risk_score_block: -40
```

`ingest-event-factors` 会把 DeepSeek 或其他 NLP 模型抽取的事件 JSONL/CSV 规整成统一结构。要求字段至少包含 `symbol`、`event_date`、`event_type`、`sentiment`、`impact_score`、`confidence`、`summary` 和 `source_url`。模型输出只能作为结构化因子进入评分和拦截，不能直接下单。

## 全量候选评估

每天本地虚拟实盘会输出：

```text
local_runs/paper_live/decisions/YYYYMMDD/selection_candidates.csv
```

关键字段：

- `selection_rank`：综合评分排名。
- `buy_decision`：`BUY_READY`、`BUY_WAIT` 或 `BLOCK`。
- `buy_reason`：不能买或需要等的明确原因。
- `positive_reason`：支持继续观察或买入的正面依据。
- `selection_score`：健康、资金流、动量、波动、回撤、流动性、估值和买点的综合分。
- `entry_support_level`、`entry_target_level`：买点相关支撑和目标参考。

这个文件用于回答“为什么选它”和“为什么没买它”。没有出现在订单里的股票也能被复盘。

## 运行流程

```bash
source .venv/bin/activate
python -m aqt.cli fetch-smallcap-universe --limit 20 --min-market-cap 2000000000 --max-market-cap 12000000000
python -m aqt.cli fetch-smallcap-universe --point-in-time --as-of 2026-08-24 --limit 20
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

卖出不再是“跌到某个固定百分比就砍”。只有跌破支撑位且短期趋势走弱，才生成卖出；涨到看涨目标位后，如果短期趋势衰减，才生成止盈卖出；ST/退市风险会触发健康退出。每日输出 `sell_points.csv` 和 `position_advice.md`，明确每个持仓的卖点、止盈目标、距止损/目标空间和继续持有理由。

## 资金效率退出

`small_cap_momentum` 现在会跟踪每个持仓进入组合后的持有天数。如果持仓已经过了最小观察期，但排名掉到有效候选范围外或动量低于阈值，并且已经回到保本缓冲区，策略会生成 `capital_recycle_breakeven` 卖出信号，优先收回本金。若持仓超过最长容忍天数仍然没有改善，会按 `capital_recycle_stale` 或 `capital_recycle_damage_control` 退出，避免资金长期低效占用。

当前 smallcap 系列配置：

```yaml
capital_recycle_enabled: true
capital_recycle_min_holding_days: 15
capital_recycle_max_holding_days: 45
capital_recycle_rank_multiplier: 2
capital_recycle_min_momentum: 0.00
capital_recycle_breakeven_buffer_pct: 0.003
capital_recycle_max_loss_pct: -0.06
```

如果当天因为技术位或资金效率规则卖出，策略允许同日把释放的资金买入排名更好的候选；非调仓日不会额外卖出其他正常持仓。

参数搜索：

```bash
python -m aqt.cli optimize-smallcap --config configs/smallcap.yaml --momentum-windows 40,60,90 --rebalance-intervals 10,20,30 --top-ns 3,5
```

排行榜：

```text
runs/optimize_smallcap/leaderboard.csv
```

Walk-forward 验证：

```bash
python -m aqt.cli walk-forward --config configs/smallcap.yaml --output runs/walk_forward --train-start 2021-01-04 --train-end 2024-12-31 --test-start 2025-01-02 --test-end 2026-08-18
```

输出：

- `runs/walk_forward/train/optimize/leaderboard.csv`：只用训练区间产生的参数排行榜。
- `runs/walk_forward/out_of_sample/metrics.csv`：冻结最优参数后在样本外区间的指标。
- `runs/walk_forward/summary.csv` 和 `summary.md`：训练 / 样本外对照。

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
- 已提供 walk-forward 命令拆分训练和样本外验证；如果只看普通 backtest 或 optimize 结果，参数搜索仍可能过拟合。
- 已加入基础停牌、ST、涨停、流动性、PE、市值过滤，但公开数据源可能延迟或缺失；真实交易前仍必须用券商盘口和交易所状态复核。
- 当前用日线收盘价撮合，不是分钟/Tick 级成交模拟。
- 真实接券商账号前必须增加账户同步、撤单状态机、盘中风控和盘后对账。

当前已提供样本外验证和压力测试命令：

```bash
python -m aqt.cli walk-forward --config configs/smallcap.yaml --output runs/walk_forward
python -m aqt.cli backtest --config configs/smallcap_oos.yaml
python -m aqt.cli report --run-dir runs/smallcap_oos_backtest
python -m aqt.cli stress-test --config configs/smallcap.yaml --output runs/stress_test
```
