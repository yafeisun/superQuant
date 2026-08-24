# TODO / Backlog

更新时间: 2026-08-24

说明: 当前活跃待办的唯一入口。后续新增、关闭、重排都只改这份文件。

## 当前症状

- P0 工程断点已基本收敛，下一步重点是样本外验证、真实交易前对账和监控自动刷新。

## 已完成

- [x] 日线数据新鲜度门禁和 last-good 回退：`automation/run_local_paper_live.py` 允许回退到最近一根不晚于目标日的 bar，并把 `stale_data_fallback` 写入摘要。
- [x] 盘中行情多源回退：`aqt/quotes.py` 已做 Eastmoney + AkShare + 本地 `quotes.csv` 缓存回退。
- [x] 资金流采集兜底和缓存保留：`aqt/flow.py` 现在会复用上一版 `latest.csv`，并额外落 `latest.status.json` 区分更新 / 缓存 / 空结果。
- [x] 健康数据 ST / 停牌来源补齐：`aqt/health.py` 已接入停牌和 ST 多来源。
- [x] 关键路径测试：已补 selection、health、flow、quote failover、latest bars 的自动测试。
- [x] 断点运行统一状态：`aqt/run_status.py` 统一落 `status/latest_*.json` 和 `status/events.jsonl`，已接入 after-close、paper-live、intraday-paper。
- [x] 本地实盘看板：`aqt/live_dashboard.py` 和 `aqt.cli live-dashboard` 已汇总状态、权益、持仓、操作、成交、拒单、候选和盘中行情缓存。
- [x] 资金流并行抓取：`aqt/flow.py` 已把逐股票资金流请求改成最多 4 worker 并发，账本写入仍保持串行。
- [x] 看板自动刷新：paper-live / intraday-paper 每次运行后默认刷新 `reports/live_dashboard.html`，可用 `--no-dashboard` 关闭。
- [x] 本地告警 outbox / webhook：`aqt/alerts.py` 和 `aqt.cli status-alerts` 会把 warning / error 去重后写入 `status/alerts.jsonl`，配置 `AQT_ALERT_WEBHOOK_URL` 后可推送外部 webhook。
- [x] 看板数据钻取：`aqt/live_dashboard.py` 已按单票展示最近 K 线、持仓/候选/资金流指标和订单/成交/拒单时间线。
- [x] 执行层保守撮合：`aqt/broker.py` 已加入限价触及检查、涨停买/跌停卖拦截、成交量参与率上限和部分成交剩余拒单。
- [x] 流动性压力测试：`aqt/stress.py` 已加入半成交量和 5% 成交量参与率场景，覆盖部分成交对收益/回撤/拒单的影响。
- [x] Point-in-time universe 消费层：`aqt/universe.py` 已支持 `universe_date` / `listed_date` / `valid_from` / `valid_to` / ST 区间解析，回测每日只允许当日合格标的开新仓。
- [x] Walk-forward 验证命令：`aqt.cli walk-forward` 已把训练区间搜参和样本外区间验证拆开，输出冻结参数和 OOS 指标。
- [x] 资金效率退出：`small_cap_momentum` 已加入低效持仓本金回收 / 滞留退出规则，退出后允许同日把资金轮动到排名更好的标的。

## P0 先修

- [ ] 真实告警目标配置：选择企业微信 / Telegram / 邮件中的一个，配置 `AQT_ALERT_WEBHOOK_URL` 并做一次真实推送演练。

## P1 策略和验证

- [ ] 真实 point-in-time 数据源：补可靠的上市日期、退市日期、历史 ST 区间和历史 board filter，替换当前 bootstrap / snapshot 小盘池。
- [ ] 外部因子追踪：给 event / macro 因子补 raw source、model、prompt version、hash 和 `source_url` 的落盘审计。
- [ ] 看板交互增强：增加只看持仓 / 只看候选 / 只看异常的筛选，以及单票历史导出。

## P2 真实接入准备

- [ ] 账户同步与对账：真实 broker 接入前补订单状态机、撤单、成交回报和盘后对账。
- [ ] 盘前 / 盘中 / 盘后 preflight：资金、持仓、可卖数量、交易日历、昨日收盘价和限额统一校验。
- [ ] 真实交易状态机：接入 broker 后用真实订单状态、撤单、部分成交回报替代本地撮合假设。
- [ ] 运行监控面板：把 cron / workflow 的最近一次成功抓数时间、失败原因、告警派发状态和当前状态集中展示。

## 更新规则

- 新增任务先放到最合适的优先级区间。
- 完成后把 `[ ]` 改成 `[x]`，并保留简短完成说明。
- 发现任务不再重要时，下沉到更低优先级，不要在多个文档里重复维护同一条。
