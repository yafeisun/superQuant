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
configs/demo.yaml  示例配置
docs/roadmap.md    分阶段搭建指南
```

## 下一步

1. 安装 `akshare`，把 `data/raw` 中的样例 CSV 换成真实 A 股日线。
2. 增加更多 A 股约束：涨跌停、停牌、ST、复权、交易日历、T+1 精细化。
3. 接入 `vn.py` 的 CTP/证券网关前，先用本系统产出的目标持仓和风控检查作为真实交易前置层。
4. 把策略研究迁到 `Qlib`，将模型输出转成每日目标权重，再交给本系统或 `vn.py` 执行。

## 小盘策略方案

当前已加入小盘动量轮动策略，模拟跟踪最多 5 只，买入前会过滤停牌、ST/退市风险、涨停、流动性不足、PE 异常和市值范围异常。说明见 [docs/smallcap-strategy.md](/home/chery/Documents/Quant/docs/smallcap-strategy.md)。常用命令：

```bash
source .venv/bin/activate
python -m aqt.cli fetch-akshare --config configs/smallcap_best.yaml --adjust qfq
python -m aqt.cli paper-run --config configs/smallcap_best.yaml
python -m aqt.cli report --run-dir runs/smallcap_best_paper
```

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
```

账本默认写入 `local_runs/paper_live/`，包含账户、持仓、成交、权益曲线和每日决策。该目录默认不提交到 GitHub。

本地定时任务安装：

```bash
bash scripts/install_local_paper_cron.sh
```

安装后本机会在 16:15 收盘后跑虚拟实盘/复盘，并在 A 股正式交易时段 `09:30-11:30`、`13:00-15:00` 每 5 分钟盯实时盘口；脚本内部会自动跳过节假日和非交易时间。

仅供工程学习和研究验证，不构成投资建议。
