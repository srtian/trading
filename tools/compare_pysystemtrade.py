"""在你本机运行：用 pysystemtrade 的原代码和 etflab 在同一份 QQQ 数据上逐日对照。

pysystemtrade 是 GPL-3.0，本仓库不包含它的任何代码；这个脚本只在运行时导入你自己克隆的副本。

用法：
    git clone --depth 1 https://github.com/pst-group/pysystemtrade ~/pysystemtrade
    pip install yfinance
    python tools/compare_pysystemtrade.py ~/pysystemtrade

通过标准（任务书第 4 节）：每项最大相对误差 < 1e-6。
"""
import sys

import numpy as np
import pandas as pd


def main(pst_path: str) -> int:
    sys.path.insert(0, pst_path)
    from sysquant.estimators.vol import mixed_vol_calc, robust_vol_calc
    from systems.provided.rules.ewmac import ewmac_calc_vol

    sys.path.insert(0, ".")
    from etflab.engine.indicators import ewmac_raw, mixed_vol, robust_vol

    import yfinance as yf
    px = yf.Ticker("QQQ").history(start="2005-01-01", auto_adjust=True)["Close"]
    px.index = px.index.tz_localize(None)
    r = px.pct_change()

    rows = []
    a = robust_vol_calc(px.diff(), days=35)
    b = robust_vol(px.diff())
    rows.append(("robust_vol（价格差）", a, b))
    a = mixed_vol_calc(r, days=35, slow_vol_years=20, proportion_of_slow_vol=0.3)
    b = mixed_vol(r, 35, 10, 20, 0.3, 256)
    rows.append(("mixed_vol（百分比收益）", a, b))
    for f, s in [(16, 64), (32, 128), (64, 256)]:
        a = ewmac_calc_vol(px, Lfast=f, Lslow=s, vol_days=35)
        b = ewmac_raw(px, f, s, robust_vol(px.diff()))
        rows.append((f"ewmac {f}/{s}", a, b))

    ok = True
    for name, a, b in rows:
        df = pd.concat([a, b], axis=1).dropna()
        rel = ((df.iloc[:, 0] - df.iloc[:, 1]).abs() / df.iloc[:, 0].abs().clip(lower=1e-12)).max()
        status = "通过" if rel < 1e-6 else "不通过"
        ok &= rel < 1e-6
        print(f"{name:24s} 样本 {len(df):5d}  最大相对误差 {rel:.2e}  {status}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "../pysystemtrade"))
