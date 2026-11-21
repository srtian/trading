"""把原始数据组装成回测与信号用的面板。

- 时间轴：纽交所交易日（XNYS）。港股、A 股收盘早于美股收盘，按"当日已知"前向填充，不产生前视。
- 趋势信号用本币税后总收益指数；风险与回测用折算成基准货币（默认新元）后的收益。
- 汇率口径：q[C] = 每 1 美元兑多少 C；本币价格折算为基准货币：P_base = P_local × q[base] / q[C]。
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from ..calendar import sessions
from .sources import fetch
from .totalreturn import total_return_index

ANN = 252


@dataclass
class Panel:
    ids: list
    names: dict
    tr_local: pd.DataFrame       # 本币税后总收益指数（趋势用）
    tr_base: pd.DataFrame        # 基准货币税后总收益指数
    ret_base: pd.DataFrame       # 基准货币日收益
    cash_ret: pd.Series          # 现金类资产基准货币日收益
    weights: pd.Series
    raw: dict = field(default_factory=dict)       # 原始数据，供质量检查与快照
    meta: dict = field(default_factory=dict)


def fetch_all(cfg: dict, offline: dict | None = None) -> dict:
    """取全部原始序列：资产、现金、现金回补利率、汇率。返回 {key: DataFrame}。"""
    start = cfg.get("start", "1999-01-01")
    out = {}

    def _try(key, spec):
        try:
            out[key] = fetch(spec, start, offline)
        except Exception as e:  # 单个序列失败不中断：由质量闸门判定 blocked 并写出记录
            out[key] = None
            out.setdefault("_errors", {})[key] = str(e)

    for a in cfg["assets"]:
        _try(f"asset:{a['id']}", a["signal"])
    c = cfg.get("cash")
    if c:
        _try(f"cash:{c['id']}", c["signal"])
        if c.get("fallback_rate"):
            try:
                out["rate:fallback"] = fetch(c["fallback_rate"], start, offline)
            except Exception:
                pass  # 回补利率缺失时，BIL 上市前现金收益按 0，并在报告中披露
    for ccy, spec in cfg.get("fx", {}).items():
        for part in ("history", "recent"):
            if part in spec:
                try:
                    out[f"fx:{ccy}:{part}"] = fetch(spec[part], start, offline)
                except Exception as e:  # 缺一个源时由质量闸门判断能否继续
                    out[f"fx:{ccy}:{part}"] = None
                    out.setdefault("_errors", {})[f"fx:{ccy}:{part}"] = str(e)
    return out


def splice_fx(hist: pd.DataFrame | None, recent: pd.DataFrame | None) -> pd.Series:
    """FRED 历史 + Yahoo 补最近：重叠部分以 FRED 为准，FRED 最后一天之后用 Yahoo。"""
    parts = []
    if hist is not None and len(hist):
        parts.append(hist["close"])
    if recent is not None and len(recent):
        r = recent["close"]
        if parts:
            r = r[r.index > parts[0].index[-1]]
        parts.append(r)
    if not parts:
        return pd.Series(dtype=float)
    return pd.concat(parts).sort_index()


def _fx_quotes(cfg, raw, index) -> dict:
    q = {"USD": pd.Series(1.0, index=index)}
    for ccy in cfg.get("fx", {}):
        sp = splice_fx(raw.get(f"fx:{ccy}:history"), raw.get(f"fx:{ccy}:recent"))
        if len(sp):
            q[ccy] = sp.reindex(index.union(sp.index)).ffill().reindex(index)
    return q


def _to_index(series: pd.Series, index: pd.DatetimeIndex) -> pd.Series:
    """对齐到时间轴：只向前填充（首个有效值之前保持 NaN）。"""
    s = series.reindex(index.union(series.index)).ffill().reindex(index)
    first = series.first_valid_index()
    if first is not None:
        s[s.index < first] = np.nan
    return s


def build_panel(cfg: dict, raw: dict, asof=None) -> Panel:
    base = cfg.get("base_currency", "USD")
    cal = cfg.get("schedule_calendar", "XNYS")
    starts = [raw[f"asset:{a['id']}"].index[0] for a in cfg["assets"]]
    ends = [raw[f"asset:{a['id']}"].index[-1] for a in cfg["assets"]]
    end = pd.Timestamp(asof) if asof is not None else max(ends)
    index = sessions(cal, min(starts), end)
    index = index[index <= end]
    q = _fx_quotes(cfg, raw, index)
    if base not in q:
        raise ValueError(f"缺少基准货币 {base} 的汇率")

    ids, names = [], {}
    tl, tb = {}, {}
    for a in cfg["assets"]:
        aid = a["id"]
        r = raw[f"asset:{aid}"]
        r = r[r.index <= end]
        tr = total_return_index(r, a.get("dividend_withholding", 0.0))
        loc = _to_index(tr, index)
        ccy = a.get("currency", "USD")
        tl[aid] = loc
        tb[aid] = loc * q[base] / q[ccy]
        ids.append(aid)
        names[aid] = a.get("name", aid)
    tr_local = pd.DataFrame(tl, index=index)
    tr_base = pd.DataFrame(tb, index=index)
    ret_base = tr_base.pct_change(fill_method=None)

    cash_ret = pd.Series(0.0, index=index)
    meta = {"cash_source": []}
    c = cfg.get("cash")
    if c:
        cr = raw[f"cash:{c['id']}"]
        cr = cr[cr.index <= end]
        ctr = _to_index(total_return_index(cr, c.get("dividend_withholding", 0.0)), index)
        r_local = ctr.pct_change(fill_method=None)
        if "rate:fallback" in raw:
            rate = _to_index(raw["rate:fallback"]["close"], index) / 100.0 / ANN
            gap = r_local.isna()
            r_local[gap] = rate.shift(1)[gap]  # 用前一日利率计当日利息
            meta["cash_source"].append("fallback_rate_before_cash_etf")
        r_local = r_local.fillna(0.0)
        ccy = c.get("currency", "USD")
        fxr = (q[base] / q[ccy]).pct_change(fill_method=None).fillna(0.0)
        cash_ret = (1 + r_local) * (1 + fxr) - 1
    w = pd.Series({a["id"]: float(a.get("weight", 0.0)) for a in cfg["assets"]})
    meta["base_currency"] = base
    return Panel(ids, names, tr_local, tr_base, ret_base, cash_ret, w, raw, meta)
