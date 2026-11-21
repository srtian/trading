"""日频回测：信号日收盘计算，exec_lag 个交易日后收盘成交；持仓每日漂移。

同一引擎也用于实盘"模型组合"：回测到今天，最后的持仓就是模型组合当前持仓，缓冲区相对它判断。
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from ..calendar import period_end_flags
from .indicators import annualized_vol, forecasts
from .sizing import Sizer, apply_buffer


@dataclass
class BTResult:
    weights: pd.DataFrame     # 每日收盘后持仓（风险资产），另含 cash
    nav: pd.Series
    trades: pd.DataFrame      # 每次成交：日期、换手
    pending: dict | None      # 尚未执行的信号（信号日、执行日、目标）
    turnover_py: float
    first: pd.Timestamp


def compute_indicators(panel, cfg):
    vol = annualized_vol(panel.ret_base, cfg.get("vol", {}))
    fc, warm = forecasts(panel.tr_local, vol, cfg.get("trend", {}))
    return vol, fc


def run(panel, cfg, ind=None, const_weights: pd.Series | None = None) -> BTResult:
    rc = cfg.get("rebalance", {})
    freq = rc.get("freq", "M")
    buffer = rc.get("buffer", 0.10)
    cost = rc.get("cost_bps", 10) / 1e4
    lag = rc.get("exec_lag", 1)
    cal = cfg.get("schedule_calendar", "XNYS")

    vol, fc = ind if ind is not None else compute_indicators(panel, cfg)
    sizer = Sizer(panel.ids, panel.weights, vol, fc, panel.tr_base, cfg.get("sizing", {}))
    idx = panel.ret_base.index
    sig = period_end_flags(idx, cal, freq).to_numpy()
    R = panel.ret_base.reindex(columns=panel.ids).fillna(0.0).to_numpy()
    RC = panel.cash_ret.reindex(idx).fillna(0.0).to_numpy()
    N = len(panel.ids)

    h = np.zeros(N)
    cash, V = 1.0, 1.0
    pending = {}
    nav = np.empty(len(idx))
    W = np.empty((len(idx), N))
    trades = []
    started = False
    for i in range(len(idx)):
        if i > 0:
            hv = h * (1 + R[i])
            cv = cash * (1 + RC[i])
            tot = hv.sum() + cv
            V *= tot
            h, cash = hv / tot, cv / tot
        if i in pending:
            p = pending.pop(i)
            cur = pd.Series(h, index=panel.ids)
            if const_weights is not None:
                new = p["target"].reindex(panel.ids).fillna(0.0)
            else:
                new = apply_buffer(p["target"], cur, p["avg"], buffer)
            new = new.to_numpy(dtype=float)
            to = float(np.abs(new - h).sum())
            V *= 1 - cost * to
            if to > 1e-12:
                trades.append({"date": idx[i], "signal_date": p["signal_date"], "turnover": to})
            h = new
            cash = 1.0 - h.sum()
        if sig[i] or not started:
            t = idx[i]
            if const_weights is not None:
                tgt, avg, ok = const_weights, None, True
            else:
                tgt, avg, d = sizer.target(t)
                ok = len(d["avail"]) > 0
            if ok:
                pending[i + lag] = {"target": tgt, "avg": avg, "signal_date": t}
                started = True
        nav[i] = V
        W[i] = h
    wdf = pd.DataFrame(W, index=idx, columns=panel.ids)
    wdf["cash"] = 1 - wdf.sum(axis=1)
    nav_s = pd.Series(nav, index=idx)
    invested = wdf[panel.ids].sum(axis=1).to_numpy() > 0
    first = idx[int(np.argmax(invested))] if invested.any() else idx[-1]
    nav_s = nav_s.loc[first:] / nav_s.loc[first]
    wdf = wdf.loc[first:]
    tr = pd.DataFrame(trades, columns=["date", "signal_date", "turnover"])
    yrs = max((idx[-1] - first).days / 365.25, 1e-9)
    left = None
    for k, p in pending.items():  # 只可能有一条：最后一个信号日的待执行目标
        left = {"signal_date": p["signal_date"], "exec_index": k, "target": p["target"], "avg": p["avg"]}
    return BTResult(wdf, nav_s, tr, left, float(tr["turnover"].sum() / yrs) if len(tr) else 0.0, first)
