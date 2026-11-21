"""黄金样本（一）：向量化实现 vs 按公式逐行写的朴素实现。

公式依据 pysystemtrade 源码中的定义（robust_vol_calc、mixed_vol_calc、ewmac）。
与 pysystemtrade 原代码的直接对照见 tools/compare_pysystemtrade.py（需在本机运行）。
"""
import math

import numpy as np
import pandas as pd
import pytest

from etflab.engine.indicators import ew_vol, ewmac_raw, mixed_vol, robust_vol


def naive_ewm_mean(x, span):
    a = 2 / (span + 1)
    out = []
    for t in range(len(x)):
        w = (1 - a) ** np.arange(t, -1, -1)
        xs = x[: t + 1]
        m = ~np.isnan(xs)
        out.append(np.sum(w[m] * xs[m]) / np.sum(w[m]))
    return np.array(out)


def naive_ewm_std(x, span, min_periods):
    """adjust=True 的偏差修正加权方差：Σw(x−μ)²/Σw × (Σw)²/((Σw)²−Σw²)。"""
    a = 2 / (span + 1)
    out = []
    for t in range(len(x)):
        xs = x[: t + 1]
        m = ~np.isnan(xs)
        if m.sum() < min_periods:
            out.append(np.nan)
            continue
        w = (1 - a) ** np.arange(t, -1, -1)
        w, v = w[m], xs[m]
        mu = np.sum(w * v) / np.sum(w)
        var = np.sum(w * (v - mu) ** 2) / np.sum(w)
        s1, s2 = np.sum(w), np.sum(w ** 2)
        var *= s1 ** 2 / (s1 ** 2 - s2)
        out.append(math.sqrt(var))
    return np.array(out)


@pytest.fixture(scope="module")
def series():
    rng = np.random.default_rng(11)
    r = 0.01 * rng.standard_normal(700)
    r[300:340] *= 4            # 一段高波动，让下限与混合真正起作用
    px = 100 * np.cumprod(1 + r)
    idx = pd.bdate_range("2010-01-01", periods=700)
    return pd.Series(px, idx), pd.Series(r, idx)


def test_ew_vol_matches_formula(series):
    _, r = series
    got = ew_vol(r, 35, 10).to_numpy()
    exp = naive_ewm_std(r.to_numpy(), 35, 10)
    np.testing.assert_allclose(got, exp, rtol=1e-10, equal_nan=True)


def test_robust_vol_floor_matches_formula(series):
    px, _ = series
    d = px.diff()
    got = robust_vol(d).to_numpy()
    v = naive_ewm_std(d.to_numpy(), 35, 10)
    v = np.where(v < 1e-10, 1e-10, v)
    floor = np.zeros(len(v))
    for t in range(len(v)):
        win = v[max(0, t - 499): t + 1]
        win = win[~np.isnan(win)]
        floor[t] = np.quantile(win, 0.05) if len(win) >= 100 else np.nan
    floor[0] = 0.0
    floor = pd.Series(floor).ffill().to_numpy()
    exp = np.maximum(v, floor)
    np.testing.assert_allclose(got, exp, rtol=1e-10, equal_nan=True)


def test_mixed_vol_matches_formula(series):
    _, r = series
    got = mixed_vol(r, 35, 10, slow_years=1, slow_weight=0.3, bdays=256).to_numpy()
    v = naive_ewm_std(r.to_numpy(), 35, 10)
    long = naive_ewm_mean(v, 256)
    exp = 0.3 * long + 0.7 * v
    np.testing.assert_allclose(got, exp, rtol=1e-10, equal_nan=True)


def test_ewmac_matches_formula(series):
    px, _ = series
    vol = robust_vol(px.diff())
    got = ewmac_raw(px, 16, 64, vol).to_numpy()
    exp = (naive_ewm_mean(px.to_numpy(), 16) - naive_ewm_mean(px.to_numpy(), 64)) / vol.ffill().to_numpy()
    np.testing.assert_allclose(got, exp, rtol=1e-10, equal_nan=True)
