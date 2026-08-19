# 开源项目调研记录

本阶段目标是搭建可虚拟实盘的 A 股系统，而不是直接接真实资金。按这个目标评估，推荐组合如下。

## 推荐组合

| 优先级 | 项目 | GitHub | 用法 |
| --- | --- | --- | --- |
| 1 | AKShare | https://github.com/akfamily/akshare | A 股公开行情和基础数据下载 |
| 2 | vn.py | https://github.com/vnpy/vnpy | 后续接真实交易网关、事件引擎、GUI 管理 |
| 3 | RQAlpha | https://github.com/ricequant/rqalpha | 成熟回测框架，用于交叉验证策略结果 |
| 4 | Qlib | https://github.com/microsoft/qlib | 因子研究、模型训练、组合优化 |

## 当前选择

先不直接克隆这些项目到本目录。原因是现在没有真实交易账号，且网络、依赖、GUI 组件会带来较多环境变量。当前系统先保留清晰边界：

- 数据层：当前 CSV，下一步用 AKShare 生成相同 schema。
- 数据下载：当前命令已实现东方财富直连、腾讯行情 fallback、AKShare 备用。
- 策略层：统一输出订单，后续也可以改成输出目标权重。
- 执行层：当前 PaperBroker，未来换成 vn.py 网关。
- 研究层：当前简单策略，未来接 Qlib 模型信号。

这样每一步都有可验证产物，不会被框架安装或券商接口卡住。
