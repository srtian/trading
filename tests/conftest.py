"""测试夹具：用真实交易所日历生成的合成数据（不含任何供应商数据）。"""
from __future__ import annotations

import copy
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

from etflab.calendar import sessions

ROOT = Path(__file__).resolve().parents[1]


def _raw_frame(dates, prices, div_every=None, div_yield=0.004, rng=None):
    df = pd.DataFrame({"close": prices, "dividends": 0.0, "splits": 0.0}, index=dates)
    if div_every:
        idx = np.arange(div_every, len(dates), div_every)
        df.iloc[idx, df.columns.get_loc("dividends")] = df["close"].iloc[idx].to_numpy() * div_yield
    # 供应商复权价：按同一毛总收益口径构造，用于对账测试
    r = (df["close"] + df["dividends"]) / df["close"].shift(1) - 1
    r.iloc[0] = 0.0
    df["adj_close"] = (1 + r).cumprod() * df["close"].iloc[-1] / (1 + r).cumprod().iloc[-1]
    df.index.name = "date"
    return df


def make_raw(start="2003-01-02", end="2014-12-31", seed=3) -> dict:
    rng = np.random.default_rng(seed)
    us = sessions("XNYS", start, end)
    hk = sessions("XHKG", start, end)
    n = len(us)
    regime = (np.arange(n) // 300) % 4 == 0
    C = np.array([[1, .6, -.2, .1], [.6, 1, -.1, .1], [-.2, -.1, 1, .2], [.1, .1, .2, 1]])
    z = rng.standard_normal((n, 4)) @ np.linalg.cholesky(C).T
    vol = np.array([.22, .24, .14, .16]) / np.sqrt(252)
    mu = np.array([.09, .05, .03, .05]) / 252
    mult = np.where(regime[:, None], 2.0, 1.0)
    drift = np.where(regime[:, None], np.array([-3.0, -2.0, 1.0, 1.5]), 1.0)
    r = mu * drift + vol * mult * z
    px = 100 * np.cumprod(1 + r, axis=0)
    raw = {
        "asset:eq_us": _raw_frame(us, px[:, 0], div_every=63),
        "asset:bond": _raw_frame(us, px[:, 2], div_every=21, div_yield=0.003),
        "asset:gold": _raw_frame(us, px[:, 3]),
    }
    hk_px = pd.Series(px[:, 1], index=us).reindex(hk, method="ffill").bfill().to_numpy() * 0.2
    raw["asset:eq_hk"] = _raw_frame(hk, hk_px, div_every=120, div_yield=0.015)
    raw["cash:tbill"] = _raw_frame(us, 100 * np.cumprod(np.full(n, 1 + 0.02 / 252)), div_every=21, div_yield=0.0)
    fx_dates = pd.bdate_range(start, end)
    sgd = pd.Series(1.4 * np.cumprod(1 + 0.003 * rng.standard_normal(len(fx_dates))), index=fx_dates)
    hkd = pd.Series(7.8 + 0.01 * rng.standard_normal(len(fx_dates)), index=fx_dates)
    for ccy, s in [("SGD", sgd), ("HKD", hkd)]:
        hist = s.iloc[:-6]                       # FRED：最后几天还没发布
        rec = s.iloc[-300:] * (1 + 0.0005 * rng.standard_normal(300))
        raw[f"fx:{ccy}:history"] = pd.DataFrame({"close": hist, "dividends": 0.0, "splits": 0.0, "adj_close": np.nan})
        raw[f"fx:{ccy}:recent"] = pd.DataFrame({"close": rec, "dividends": 0.0, "splits": 0.0, "adj_close": np.nan})
    return raw


def make_cfg() -> dict:
    base = yaml.safe_load((ROOT / "config" / "portfolio.yaml").read_text(encoding="utf-8"))
    cfg = copy.deepcopy(base)
    cfg["start"] = "2003-01-02"
    cfg["assets"] = [
        {"id": "eq_us", "name": "美股", "signal": {"source": "yahoo", "symbol": "EQUS"}, "currency": "USD",
         "dividend_withholding": 0.15, "weight": 0.25},
        {"id": "eq_hk", "name": "港股", "signal": {"source": "yahoo", "symbol": "EQHK.HK"}, "currency": "HKD",
         "dividend_withholding": 0.0, "weight": 0.25},
        {"id": "bond", "name": "长债", "signal": {"source": "yahoo", "symbol": "BOND"}, "currency": "USD",
         "dividend_withholding": 0.0, "weight": 0.25},
        {"id": "gold", "name": "黄金", "signal": {"source": "yahoo", "symbol": "GOLD"}, "currency": "USD",
         "dividend_withholding": 0.0, "weight": 0.25},
    ]
    cfg["cash"] = {"id": "tbill", "name": "现金", "signal": {"source": "yahoo", "symbol": "TBILL"},
                   "currency": "USD", "dividend_withholding": 0.0}
    cfg["fx"] = {c: {"history": {"source": "fred", "series": f"X{c}"}, "recent": {"source": "yahoo", "symbol": f"{c}=X"}}
                 for c in ("SGD", "HKD")}
    return cfg


@pytest.fixture(scope="session")
def raw():
    return make_raw()


@pytest.fixture(scope="session")
def cfg():
    return make_cfg()
