# trading · etflab

自用的 ETF 决策系统：风险预算 × 波动目标 × 趋势强度 × 缓冲区。每个交易日由 GitHub Actions 自动计算，信号文件提交在 `signals/` 下。

规则写在 [`PREREGISTRATION.md`](PREREGISTRATION.md) 和 [`config/portfolio.yaml`](config/portfolio.yaml)。网站（GitHub Pages）在下一批交付。

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
| PTNQ 实盘对照 | 下一批 |

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
tools/             本机对照脚本
signals/           每日信号（由 Actions 提交）
```

不构成投资建议，仅供本人研究使用。
