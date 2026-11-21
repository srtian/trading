"""黄金样本（二）：原 QQQ SOP 的公式 w = min(1, 0.1625/σ) × k 必须被精确复现。"""
import numpy as np
import pandas as pd

from etflab.config import override
from etflab.data.panel import build_panel
from etflab.engine import backtest as bt
from etflab.engine.sizing import Sizer


def test_legacy_formula_exact(raw, cfg):
    c = override(cfg, {
        "vol": {"method": "simple", "window": 42, "annualization": 252},
        "trend.method": "sma_vote",
        "sizing.vol_target": 0.1625, "sizing.idm": 1.0, "sizing.max_weight": 1.0,
        "sizing.cap_before_trend": True, "base_currency": "USD",
    })
    c["assets"] = [dict(c["assets"][0], weight=1.0, dividend_withholding=0.0)]
    p = build_panel(c, raw)
    vol, fc = bt.compute_indicators(p, c)
    sz = Sizer(p.ids, p.weights, vol, fc, p.tr_base, c["sizing"])
    px = p.tr_local["eq_us"].dropna()
    worst = 0.0
    for t in px.index[250::37]:
        P = px.loc[:t]
        r = P.pct_change().iloc[-42:]
        sigma = r.std(ddof=0) * np.sqrt(252)
        k = np.mean([P.iloc[-1] >= P.iloc[-n:].mean() for n in (50, 100, 200)])
        ref = min(1.0, 0.1625 / sigma) * k
        x, _, _ = sz.target(t)
        worst = max(worst, abs(x["eq_us"] - ref))
    assert worst < 1e-12
