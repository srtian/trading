"""波动率与趋势指标。公式按 pysystemtrade（Rob Carver）文档与源码中的定义独立实现，未复制其代码（GPL-3.0）。

约定：
- 年化用 business_days_per_year = 256（与 pysystemtrade 一致，√256 = 16）。
- 所有计算只使用当日及以前的数据；tests/test_no_lookahead.py 用截断测试保证这一点。
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd


def ew_vol(x: pd.Series, span: int = 35, min_periods: int = 10) -> pd.Series:
    """指数加权标准差（adjust=True），与 pysystemtrade simple_ewvol_calc 定义相同。"""
    return x.ewm(adjust=True, span=span, min_periods=min_periods).std()


def apply_abs_min(vol: pd.Series, abs_min: float = 1e-10) -> pd.Series:
    v = vol.copy()
    v[v < abs_min] = abs_min
    return v


def apply_vol_floor(vol: pd.Series, q: float = 0.05, min_periods: int = 100, days: int = 500) -> pd.Series:
    """波动下限：过去 days 天波动的 q 分位；不足 min_periods 时下限为 0。"""
    vmin = vol.rolling(window=days, min_periods=min_periods).quantile(q)
    vmin.iloc[0] = 0.0
    vmin = vmin.ffill()
    return np.maximum(vol, vmin)


def robust_vol(diffs: pd.Series, days: int = 35, min_periods: int = 10, floor: bool = True) -> pd.Series:
    """价格单位的稳健波动（用于 EWMAC 归一化）= EW 波动 + 绝对下限 + 5% 分位下限。"""
    v = apply_abs_min(ew_vol(diffs, days, min_periods))
    return apply_vol_floor(v) if floor else v


def mixed_vol(returns: pd.Series, span: int = 35, min_periods: int = 10, slow_years: int = 20,
              slow_weight: float = 0.3, bdays: int = 256) -> pd.Series:
    """百分比收益的日波动：(1−p)·近期 EW 波动 + p·其 slow_years 年 EWMA 均值（pysystemtrade mixed_vol_calc）。"""
    v = ew_vol(returns, span, min_periods)
    long = v.ewm(span=slow_years * bdays).mean()
    return apply_abs_min(long * slow_weight + v * (1 - slow_weight))


def annualized_vol(returns: pd.DataFrame, vc: dict) -> pd.DataFrame:
    bd = vc.get("business_days_per_year", 256)
    if vc.get("method", "mixed") == "simple":   # 原 SOP：N 日简单收益总体标准差
        w = vc.get("window", 42)
        return returns.rolling(w).std(ddof=0) * math.sqrt(vc.get("annualization", 252))
    out = {c: mixed_vol(returns[c], vc.get("span", 35), vc.get("min_periods", 10),
                        vc.get("slow_years", 20), vc.get("slow_weight", 0.3), bd)
           for c in returns}
    return pd.DataFrame(out, index=returns.index) * math.sqrt(bd)


def ewmac_raw(price: pd.Series, fast: int, slow: int, vol: pd.Series) -> pd.Series:
    f = price.ewm(span=fast, min_periods=1).mean()
    s = price.ewm(span=slow, min_periods=1).mean()
    return (f - s) / vol.ffill()


def ewmac_forecast(price: pd.Series, speeds, scalars, fdm: float, floor: bool = True,
                   vol_days: int = 35) -> pd.Series:
    vol = robust_vol(price.diff(), vol_days, floor=floor)
    parts = [ewmac_raw(price, f, s, vol) * k for (f, s), k in zip(speeds, scalars)]
    return pd.concat(parts, axis=1).mean(axis=1, skipna=False) * fdm


def sma_vote_forecast(price: pd.Series, windows=(50, 100, 200)) -> pd.Series:
    """原 SOP：F = 10 × (价格 ≥ SMA 的比例)；任一均线未就绪则为 NaN。"""
    votes = []
    for n in windows:
        sma = price.rolling(n).mean()
        votes.append((price >= sma).astype(float).where(sma.notna()))
    return 10.0 * pd.concat(votes, axis=1).mean(axis=1, skipna=False)


def vol_attenuation(vol: pd.Series) -> pd.Series:
    """波动越处于历史高位，预测越打折：倍数 2 − 1.5q（q 为扩展窗口分位数），再平滑。"""
    norm = vol / vol.rolling(2500, min_periods=250).mean()
    q = norm.expanding(min_periods=250).rank(pct=True)
    return (2 - 1.5 * q).ewm(span=10).mean().fillna(1.0)


def forecasts(tr_local: pd.DataFrame, vol_ann: pd.DataFrame, tc: dict) -> tuple[pd.DataFrame, dict]:
    """返回 (封顶后的预测 F ∈ [0,cap]，各资产预热期)。预热期内为 NaN。"""
    method = tc.get("method", "ewmac")
    cap = tc.get("cap", 20.0)
    lo = 0.0 if tc.get("long_only", True) else -cap
    out, warm = {}, {}
    for c in tr_local:
        px = tr_local[c].dropna()
        if method == "none":
            f = pd.Series(10.0, index=px.index)
            warm[c] = 0
        elif method == "ewmac":
            speeds = tc.get("speeds", [[16, 64], [32, 128], [64, 256]])
            f = ewmac_forecast(px, speeds, tc.get("scalars", [3.75, 2.65, 1.87]),
                               tc.get("fdm", 1.05), tc.get("price_vol_floor", True))
            if tc.get("attenuation", False):
                f = f * vol_attenuation(vol_ann[c].reindex(px.index))
            warm[c] = max(s for _, s in speeds)
        elif method == "sma_vote":
            win = tc.get("sma_windows", [50, 100, 200])
            f = sma_vote_forecast(px, win)
            warm[c] = max(win)
        else:
            raise ValueError(method)
        f = f.clip(lower=lo, upper=cap)
        n = pd.Series(np.arange(1, len(px) + 1), index=px.index)
        f[n < max(warm[c], 60)] = np.nan
        out[c] = f
    return pd.DataFrame(out).reindex(tr_local.index), warm
