# 分阶段搭建路线

## 第 0 阶段：本地闭环

目标：先证明工程链路能跑通，而不是证明策略能赚钱。

```bash
python3 -m aqt.cli init-data
python3 -m aqt.cli backtest --config configs/demo.yaml
python3 -m aqt.cli paper-run --config configs/demo.yaml --cycles 30
```

验收标准：

- `runs/backtest/equity.csv` 有每日权益。
- `runs/backtest/fills.csv` 有成交记录或 `rejections.csv` 能解释为什么没有成交。
- `runs/paper/summary.txt` 能看到虚拟实盘区间和最终权益。

## 第 1 阶段：接入真实 A 股公开数据

建议先用 `AKShare`，原因是安装和调用成本低，适合个人研究启动。

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip pandas numpy pyyaml akshare
```

接入要求：无论数据来自 AKShare、Tushare 还是券商，都先落成统一 CSV：

```text
date,open,high,low,close,volume
2024-01-02,9.91,10.05,9.82,9.96,12345678
```

这能避免策略层直接依赖某个数据供应商。

安装完成后可以直接运行：

```bash
python3 -m aqt.cli fetch-akshare --config configs/demo.yaml --adjust qfq
python3 -m aqt.cli backtest --config configs/demo.yaml
```

本机当前缺少 `python3.10-venv`，如果无法创建 `.venv`，先用用户级安装也可以启动：

```bash
python3 -m pip install --user -U pandas numpy pyyaml akshare
```

如果要补齐标准虚拟环境，需要在终端里手动输入 sudo 密码：

```bash
sudo apt-get update
sudo apt-get install -y python3.10-venv
```

## 第 2 阶段：补齐 A 股交易规则

当前骨架已经包含：

- 100 股整手。
- 手续费、最低佣金、卖出印花税。
- 简化版 T+1 可卖数量。
- 单票持仓上限和单笔下单金额上限。

后续需要补齐：

- 涨跌停不能成交或只能按可成交价成交。
- 停牌股票不能交易。
- ST、退市整理、北交所等差异化规则。
- 前复权/后复权处理。
- 精确交易日历，而不是当前样例使用的工作日。

## 第 3 阶段：引入成熟开源框架

推荐顺序：

1. `AKShare`：替换样例数据源。
2. `RQAlpha`：用成熟回测框架对照本系统的回测结果。
3. `Qlib`：引入因子库、模型训练、组合优化。
4. `vn.py`：有真实账号后接证券/期货柜台或模拟柜台。

边界建议：

- 策略研究输出“目标持仓”或“目标权重”。
- 风控层负责把目标转成可交易订单。
- 执行层负责连接虚拟经纪商或真实网关。
- 对账层负责复核成交、资金和持仓。

## 第 4 阶段：走向真实交易前的检查表

真实交易前至少需要：

- 每日启动前校验资金、持仓、昨日收盘价、交易日历。
- 盘中订单、撤单、成交全量落库。
- 交易所回报和本地订单状态机一致性校验。
- 强制风控：单票、行业、账户、策略级限额。
- 一键熔断和人工确认流程。
- 盘后清算对账报表。

不要在这些能力缺失时接真实资金。
