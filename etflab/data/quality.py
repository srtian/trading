"""数据质量闸门：宁可当天不出建议，也不出错误的建议。

每条序列给出 pass / warn / fail 和原因；组合用到的任何序列 fail，整体即为 blocked。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..calendar import last_session_on_or_before, sessions
from ..config import exchange_for
from .totalreturn import tr_reconciliation

RECENT_SESSIONS = 30


def check_series(key: str, raw: pd.DataFrame, symbol: str, asof, qc: dict, has_actions: bool = True) -> dict:
    res = {"key": key, "symbol": symbol, "status": "pass", "fail": [], "warn": [], "info": {}}
    if raw is None or raw.empty:
        res["fail"].append("无数据")
        res["status"] = "fail"
        return res
    c = raw["close"]
    res["info"]["first"] = str(raw.index[0].date())
    res["info"]["last"] = str(raw.index[-1].date())
    res["info"]["rows"] = int(len(raw))
    if not raw.index.is_monotonic_increasing or raw.index.has_duplicates:
        res["fail"].append("日期非递增或有重复")
    if (c.dropna() <= 0).any():
        res["fail"].append("存在非正价格")
    n_nan = int(c.isna().sum())
    if n_nan:
        res["warn"].append(f"{n_nan} 个缺失收盘价")
    if len(raw) < qc.get("min_history_rows", 300):
        res["fail"].append(f"历史太短（{len(raw)} 行）")

    ex = exchange_for(symbol)
    try:
        expected = last_session_on_or_before(ex, asof)
        lag = len(sessions(ex, raw.index[-1], expected)) - 1
        res["info"]["exchange"] = ex
        res["info"]["sessions_behind"] = int(max(lag, 0))
        if lag > qc.get("stale_sessions", 3):
            res["fail"].append(f"数据过期：落后 {lag} 个交易日（应到 {expected.date()}）")
    except Exception as e:  # 日历不支持的交易所：只记录
        res["warn"].append(f"无法检查时效：{e}")

    ret = c.pct_change(fill_method=None)
    if has_actions:
        event = (raw["dividends"] > 0) | (raw["splits"] > 0)
    else:
        event = pd.Series(False, index=raw.index)
    big = ret[(ret.abs() > qc.get("max_abs_return", 0.25)) & ~event].dropna()
    recent = big[big.index >= raw.index[-min(RECENT_SESSIONS, len(raw))]]
    if len(recent):
        res["fail"].append("近期异常跳变：" + ", ".join(f"{d.date()} {v:+.1%}" for d, v in recent.items()))
    old = big.drop(recent.index)
    if len(old):
        res["warn"].append(f"历史上 {len(old)} 次单日变动超过阈值（最早 {old.index[0].date()}）")

    if has_actions and raw["adj_close"].notna().any():
        rec = tr_reconciliation(raw)
        if len(rec):
            worst = rec["gap"].abs().max()
            res["info"]["tr_vs_vendor_max_annual_gap"] = float(worst)
            if worst > qc.get("tr_check_max_annual_gap", 0.002):
                yr = int(rec["gap"].abs().idxmax())
                res["warn"].append(f"自建总收益与供应商复权价不一致：{yr} 年差 {rec.loc[yr, 'gap']:+.3%}")
    res["status"] = "fail" if res["fail"] else ("warn" if res["warn"] else "pass")
    return res


def fx_overlap_check(hist, recent, window: int = 250) -> dict:
    """双源对账：近一年重叠日的相对差。"""
    if hist is None or recent is None or hist.empty or recent.empty:
        return {"overlap_days": 0}
    a = hist["close"].iloc[-window:]
    b = recent["close"].reindex(a.index).dropna()
    a = a.reindex(b.index)
    if len(b) == 0:
        return {"overlap_days": 0}
    d = (b / a - 1).abs()
    return {"overlap_days": int(len(d)), "median_abs_rel_diff": float(d.median()),
            "max_abs_rel_diff": float(d.max())}


def run_gates(cfg: dict, raw: dict, asof) -> dict:
    qc = cfg.get("quality", {})
    checks = []
    for a in cfg["assets"]:
        k = f"asset:{a['id']}"
        r = check_series(k, raw.get(k), a["signal"].get("symbol", a["id"]), asof, qc)
        r["required"] = a.get("weight", 0) > 0
        checks.append(r)
    c = cfg.get("cash")
    if c:
        k = f"cash:{c['id']}"
        r = check_series(k, raw.get(k), c["signal"].get("symbol", c["id"]), asof, qc)
        r["required"] = True
        checks.append(r)
    used_ccy = {a.get("currency", "USD") for a in cfg["assets"]} | {cfg.get("base_currency", "USD")}
    used_ccy |= {cfg.get("cash", {}).get("currency", "USD")}
    for ccy, spec in cfg.get("fx", {}).items():
        need = ccy in used_ccy
        hk, rk = f"fx:{ccy}:history", f"fx:{ccy}:recent"
        # FRED 每周更新，时效门槛放宽；Yahoo 负责最近几天，按正常门槛检查
        if "history" in spec:
            r = check_series(hk, raw.get(hk), spec["history"].get("series", ccy), asof,
                             {**qc, "min_history_rows": 100, "stale_sessions": 15}, has_actions=False)
            r["required"] = need
            checks.append(r)
        if "recent" in spec:
            r = check_series(rk, raw.get(rk), spec["recent"].get("symbol", ccy), asof,
                             {**qc, "min_history_rows": 20}, has_actions=False)
            r["required"] = need
            ov = fx_overlap_check(raw.get(hk), raw.get(rk))
            r["info"].update(ov)
            if ov.get("median_abs_rel_diff", 0) > 0.005:
                r["warn"].append(f"FRED 与 Yahoo 汇率在重叠区间中位差 {ov['median_abs_rel_diff']:.2%}")
                if r["status"] == "pass":
                    r["status"] = "warn"
            checks.append(r)
    blocked = [r["key"] for r in checks if r["required"] and r["status"] == "fail"]
    return {
        "asof": str(pd.Timestamp(asof).date()),
        "status": "blocked" if blocked else ("warn" if any(r["status"] == "warn" for r in checks) else "pass"),
        "blocked_by": blocked,
        "checks": checks,
        "notes": [
            "自建毛总收益与供应商复权价的对账只能证明两者口径一致；若供应商复权价本身由同一公式算出，它不构成独立验证。"
            "独立验证见 reference/issuer_annual_returns.csv（发行商公布的年度总收益）。",
        ],
    }
