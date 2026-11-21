"""仓位：风险预算 → 波动调整 → 趋势 → 封顶 → 缓冲。每一步都保留下来，用于逐层归因。

x_i = min(w_i · IDM · σ*/σ_i · F_i/10, 单资产上限)，合计超过上限时按比例缩小。
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd


class Sizer:
    def __init__(self, ids, weights: pd.Series, vol: pd.DataFrame, fc: pd.DataFrame,
                 tr_base: pd.DataFrame, sc: dict):
        self.ids = list(ids)
        self.w_raw = weights.reindex(self.ids).fillna(0.0)
        self.vol, self.fc = vol, fc
        self.vt = sc.get("vol_target", 0.12)
        self.idm_mode = sc.get("idm", "estimate")
        self.idm_max = sc.get("idm_max", 2.5)
        self.max_w = sc.get("max_weight", 0.5)
        self.max_total = sc.get("max_total", 1.0)
        self.corr_min_weeks = sc.get("corr_min_weeks", 104)
        self.corr_default = sc.get("corr_default", 0.5)
        self.cap_before_trend = sc.get("cap_before_trend", False)
        self.wk = tr_base.resample("W-FRI").last().pct_change(fill_method=None)

    def idm(self, t, avail, w) -> float:
        if self.idm_mode != "estimate":
            return float(self.idm_mode)
        if len(avail) == 1:
            return 1.0
        hist = self.wk.loc[self.wk.index <= t, avail]  # 只用 t 及以前已结束的周
        c = hist.corr(min_periods=self.corr_min_weeks).fillna(self.corr_default).to_numpy(copy=True)
        np.fill_diagonal(c, 1.0)
        c = np.clip(c, 0.0, 1.0)  # 负相关按 0 处理，避免 IDM 过度放大
        risk = math.sqrt(float(w @ c @ w))
        return min(1.0 / risk, self.idm_max) if risk > 1e-12 else 1.0

    def target(self, t) -> tuple[pd.Series, pd.Series, dict]:
        """返回 (目标权重 x, 平均仓位 a〔缓冲基数〕, 归因明细)。"""
        v = self.vol.loc[t].reindex(self.ids)
        f = self.fc.loc[t].reindex(self.ids)
        avail = [i for i in self.ids if pd.notna(v[i]) and pd.notna(f[i]) and v[i] > 0 and self.w_raw[i] > 0]
        zero = pd.Series(0.0, index=self.ids)
        if not avail:
            return zero, zero.copy(), {"avail": [], "idm": float("nan"), "scale": float("nan"), "steps": {}}
        w = zero.copy()
        w[avail] = self.w_raw[avail] / self.w_raw[avail].sum()
        idm = self.idm(t, avail, w[avail].to_numpy())
        a = zero.copy()
        a[avail] = w[avail] * idm * self.vt / v[avail]
        if self.cap_before_trend:
            a = a.clip(upper=self.max_w)
        trend = zero.copy()
        trend[avail] = a[avail] * f[avail] / 10.0
        capped = trend.clip(upper=self.max_w)
        tot = capped.sum()
        scale = self.max_total / tot if tot > self.max_total else 1.0
        x = capped * scale
        steps = {
            "risk_budget": w, "vol_scaled": a, "after_trend": trend,
            "after_asset_cap": capped, "final": x,
        }
        return x, a * scale, {"avail": avail, "idm": idm, "scale": scale, "steps": steps,
                              "sigma": v, "forecast": f}


def apply_buffer(target: pd.Series, current: pd.Series, avg: pd.Series, buffer: float) -> pd.Series:
    """交易到缓冲边缘：仓位落在 [x−b, x+b] 内不交易；落在外面只交易到最近的边缘。b = buffer × 平均仓位。"""
    target = target.reindex(current.index).fillna(0.0)
    avg = avg.reindex(current.index).fillna(0.0)
    b = buffer * avg
    lo, hi = target - b, target + b
    new = current.copy()
    new[current < lo] = lo[current < lo]
    new[current > hi] = hi[current > hi]
    new[avg <= 0] = 0.0
    new = new.clip(lower=0.0)
    excess = new.sum() - 1.0
    if excess > 1e-15:
        # 只压缩买入部分，缓冲区内（不交易）和卖出的资产保持不变；当前持仓合计 ≤ 1 时买入部分总能吸收超出量
        buys = (new - current).clip(lower=0.0)
        if buys.sum() >= excess - 1e-12:
            new = new - buys * min(1.0, excess / buys.sum())
        else:
            new = new / new.sum()
    return new
