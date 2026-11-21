"""从未复权收盘价 + 分红自建总收益，按持有人实际承担的预提税扣除。

除息日 t 的日收益：
    r_t = (C_t + D_t × (1 − w)) / C_{t−1} − 1
其中 C 为（已按拆股调整的）收盘价，D 为每股分红，w 为预提税率。
w = 0 时即为毛总收益，可与供应商复权价对账。
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def total_return_index(raw: pd.DataFrame, withholding: float = 0.0) -> pd.Series:
    c = raw["close"].astype(float)
    d = raw["dividends"].fillna(0.0).astype(float)
    r = (c + d * (1.0 - withholding)) / c.shift(1) - 1.0
    r.iloc[0] = 0.0
    idx = (1.0 + r).cumprod()
    return idx.rename("tr_index")


def vendor_adjusted_returns(raw: pd.DataFrame) -> pd.Series:
    return raw["adj_close"].pct_change()


def tr_reconciliation(raw: pd.DataFrame) -> pd.DataFrame:
    """按年比较自建毛总收益与供应商复权收益，返回每年的差值（自建 − 供应商）。"""
    if raw["adj_close"].isna().all():
        return pd.DataFrame(columns=["ours", "vendor", "gap"])
    ours = total_return_index(raw, 0.0)
    vend = raw["adj_close"] / raw["adj_close"].dropna().iloc[0]
    df = pd.DataFrame({"ours": ours, "vendor": vend}).dropna()
    yearly = df.groupby(df.index.year).agg(["first", "last"])
    out = pd.DataFrame({
        "ours": yearly[("ours", "last")] / yearly[("ours", "first")] - 1,
        "vendor": yearly[("vendor", "last")] / yearly[("vendor", "first")] - 1,
    })
    out["gap"] = out["ours"] - out["vendor"]
    return out


def withholding_drag(raw: pd.DataFrame, withholding: float) -> float:
    """预提税造成的年化收益损失（毛总收益 − 税后总收益），用于在报告里披露。"""
    g = total_return_index(raw, 0.0)
    n = total_return_index(raw, withholding)
    yrs = max((g.index[-1] - g.index[0]).days / 365.25, 1e-9)
    return float((g.iloc[-1] ** (1 / yrs)) - (n.iloc[-1] ** (1 / yrs)))
