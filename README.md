# trading · etflab

自用的 ETF 决策系统：风险预算 × 波动目标 × 趋势强度 × 缓冲区。每个交易日由 GitHub Actions 自动计算，信号文件提交在 `signals/` 下。

规则写在 [`PREREGISTRATION.md`](PREREGISTRATION.md) 和 [`config/portfolio.yaml`](config/portfolio.yaml)。网站：<https://srtian.github.io/trading/>（GitHub Pages）。

## 一次性设置

1. **启用网站**：Settings → Pages → Build and deployment → Source 选 **GitHub Actions**。
2. **生成第一份研究结果**：Actions → Research → Run workflow（约 10 分钟；之后每周六自动运行）。
3. （可选）Issues → Labels 新建标签 `add-etf`。不建也可以：标题以“添加 ETF”开头的 Issue 同样会被处理。
4. （可选）把原始数据长期保存到私有仓库：新建私有仓库，在本仓库 Settings → Secrets 设置 `DATA_REPO`（如 `srtian/trading-data`）和 `DATA_REPO_TOKEN`（对该仓库有写权限的 fine-grained token）。

## 网站五个页面

| 页面 | 内容 |
| --- | --- |
| 今日决策 | 每只资产从风险预算到最终仓位的逐层计算、缓冲区、是否需要交易；信号来源的代码、配置、数据指纹 |
| 证据 | 预注册假设 H1～H3、对照组、证据卡（含反方文献）、候选改动的样本外检验、PTNQ 实盘对照、参数网格 |
| 模型跟踪 | 净值、回撤、3 年滚动夏普、风险仓位、各资产趋势强度、信号记录 |
| 数据健康 | 每条序列的闸门结果、时效、双源对账 |
| 添加 ETF | 通过 Issue 表单申请；只处理仓库主人的 Issue，Actions 校验后建分支，由你决定是否合并 |

## 公开与私密

- 这个仓库和网站都是公开的，所以**只有百分比和指标**，没有任何金额、持仓或账户信息；测试 `test_no_amounts_in_public_output` 会检查这一点。
- 原始行情数据不提交到这里（供应商条款不允许再分发）。数据保存在 Actions 缓存里；如果设置了 `DATA_REPO` 和 `DATA_REPO_TOKEN` 两个 secret，就改存到私有数据仓库，并保留完整的修订历史。
- 个人持仓只在本机使用：`python -m etflab targets --holdings holdings.csv`。`holdings*.csv` 已在 `.gitignore` 中排除。

## 每天发生什么

1. 从 Yahoo 抓取未复权价、分红和拆股，从 FRED 抓取汇率与利率，最近几天的汇率用 Yahoo 补齐。
2. 与已存数据逐行比较：供应商修改过的历史数据记入 `revisions.csv`，各文件哈希写入 `manifest.json`。
3. 运行质量闸门：数据过期、近期异常跳变、非正价格、汇率双源不一致。任何必需序列失败，当天就输出 `blocked`，不给建议。
4. 自建税后总收益并折算成新元，计算波动、趋势、仓位，再从头回测到今天，得到"模型组合"的当前持仓。
5. 写出 `signals/<年>/<日期>.json`：每个资产从风险预算到最终仓位的逐层归因、缓冲区、是否需要交易，以及代码、配置、数据三者的哈希。已写出的文件不会被覆盖。

## 如何验证结果是对的

| 检查 | 状态 |
| --- | --- |
| 无前视：任意截断日之前的指标、仓位、持仓与完整数据下完全一致（hypothesis 随机抽样） | 通过；注入两种前视错误时测试均失败 |
| 原 QQQ SOP 公式精确复现 | 通过（误差 < 1e-12） |
| 向量化指标 vs 按公式逐行写的朴素实现 | 通过（相对误差 < 1e-10） |
| 自建毛总收益 vs 供应商复权价 | 一致（年差 < 0.0005%），但不构成独立验证 |
| 汇率 FRED vs Yahoo 对账 | 每天在健康报告中输出 |
| 与 pysystemtrade 原代码逐日对照 | 待在本机运行 `tools/compare_pysystemtrade.py` |
| 与发行商年度总收益对账 | 待补 `reference/issuer_annual_returns.csv` |
| PTNQ 实盘对照 | 研究工作流在线运行（门槛：模拟与实盘年化差 ≤ 0.5%） |
| 候选改动的样本外检验 | 离线：波动衰减、缓冲区 20% 均未通过，不采纳 |
| 网站页面不含金额 | 测试检查 |

## 本地运行

```bash
pip install -e ".[test]"
pytest -q
python -m etflab backtest
python -m etflab validate
python -m etflab daily --data-dir data --out signals
```

无网络环境可以用离线数据集复现：

```bash
python -m etflab backtest --config config/offline_usd.yaml --offline-parquet prices.parquet
```

## 结构

```
config/            组合配置（与预注册一同冻结）
etflab/data/       数据源、税后总收益、汇率拼接、质量闸门、存储与修订记录
etflab/engine/     指标（pysystemtrade 口径）、仓位与归因、回测、统计检验
etflab/pipeline.py 每日流程与信号文件
tests/             无前视、黄金样本、性质测试、端到端
etflab/research.py 假设检验、网格、样本外检验、PTNQ 对照、证据卡
etflab/site.py     静态网站（服务端 SVG 图表，适配手机与深色模式）
tools/             本机对照脚本、“添加 ETF”处理脚本
signals/           每日信号（由 Actions 提交）
research/          每周研究结果（由 Actions 提交）
```

不构成投资建议，仅供本人研究使用。
