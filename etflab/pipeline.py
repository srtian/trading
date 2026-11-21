"""每日运行：取数 → 存储与修订记录 → 质量闸门 → 指标 → 模型组合 → 信号文件。

信号文件只含百分比和指标，不含任何账户金额（仓库与网站公开）。
"""
from __future__ import annotations

import json
import os
import platform
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from . import __version__
from .calendar import last_session_on_or_before, next_session, period_end_flags
from .config import config_hash
from .data.panel import build_panel, fetch_all
from .data.quality import run_gates
from .data.store import load_store, merge_and_save
from .data.totalreturn import withholding_drag
from .engine import backtest as bt
from .engine.sizing import Sizer, apply_buffer
from .engine.stats import metrics, stationary_bootstrap_sharpe_ci


def _git_sha() -> str | None:
    if os.environ.get("GITHUB_SHA"):
        return os.environ["GITHUB_SHA"]
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL).decode().strip()
    except Exception:
        return None


def _f(x, nd=6):
    if x is None or (isinstance(x, float) and not np.isfinite(x)):
        return None
    return round(float(x), nd)


def default_asof(cal="XNYS") -> pd.Timestamp:
    now_ny = pd.Timestamp.now(tz="America/New_York")
    d = now_ny.normalize().tz_localize(None)
    if now_ny.hour < 17:          # 美股收盘（16:00）+ 缓冲之前，用上一个交易日
        d = d - pd.Timedelta(days=1)
    return last_session_on_or_before(cal, d)


def collect_raw(cfg, data_dir=None, offline=None, fetch=True) -> tuple[dict, dict]:
    store_info = {}
    raw = {}
    if fetch:
        fetched = fetch_all(cfg, offline)
        errors = fetched.pop("_errors", {})
        good = {k: v for k, v in fetched.items() if v is not None}
        if data_dir:
            store_info = merge_and_save(data_dir, good)
            raw = load_store(data_dir)
        else:
            raw = good
        store_info["fetch_errors"] = errors
    else:
        raw = load_store(data_dir)
    return raw, store_info


def build_signal(cfg: dict, raw: dict, asof, store_info: dict | None = None, bootstrap: int = 1000) -> dict:
    cal = cfg.get("schedule_calendar", "XNYS")
    asof = pd.Timestamp(asof)
    health = run_gates(cfg, raw, asof)
    prov = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "code_version": __version__, "git_sha": _git_sha(), "config_sha256": config_hash(cfg),
        "data_manifest_sha256": (store_info or {}).get("manifest_sha256"),
        "data_revisions_found": (store_info or {}).get("revisions"),
        "python": platform.python_version(), "pandas": pd.__version__,
    }
    out = {"asof": str(asof.date()), "base_currency": cfg.get("base_currency", "USD"),
           "status": health["status"], "provenance": prov, "health_summary": {
               "status": health["status"], "blocked_by": health["blocked_by"]}}
    if health["status"] == "blocked":
        out["message"] = "数据质量闸门未通过，今天不给出建议。原因见 health 文件。"
        return out, health

    panel = build_panel(cfg, raw, asof)
    ind = bt.compute_indicators(panel, cfg)
    vol, fc = ind
    res = bt.run(panel, cfg, ind)
    t = panel.ret_base.index[-1]
    sizer = Sizer(panel.ids, panel.weights, vol, fc, panel.tr_base, cfg.get("sizing", {}))
    x, avg, d = sizer.target(t)
    cur = res.weights.iloc[-1].reindex(panel.ids)
    buf = cfg.get("rebalance", {}).get("buffer", 0.10)
    buffered = apply_buffer(x, cur, avg, buf)
    is_rebal = bool(period_end_flags(panel.ret_base.index[-5:], cal, cfg.get("rebalance", {}).get("freq", "M")).iloc[-1])
    outside = (cur < x - buf * avg - 1e-12) | (cur > x + buf * avg + 1e-12)

    assets = []
    for a in cfg["assets"]:
        i = a["id"]
        steps = {k: _f(v.get(i, 0.0)) for k, v in d.get("steps", {}).items()}
        raw_a = raw.get(f"asset:{i}")
        assets.append({
            "id": i, "name": a.get("name", i), "signal_symbol": a["signal"].get("symbol"),
            "exec": a.get("exec", []), "currency": a.get("currency", "USD"),
            "risk_budget_input": _f(a.get("weight", 0.0)),
            "sigma_annual": _f(vol[i].loc[t]) if i in vol else None,
            "forecast": _f(fc[i].loc[t]) if i in fc else None,
            "attribution": steps,
            "target": _f(x[i]), "buffer_low": _f(max(x[i] - buf * avg[i], 0.0)), "buffer_high": _f(x[i] + buf * avg[i]),
            "model_current": _f(cur[i]), "model_after_trade": _f(buffered[i]),
            "outside_buffer": bool(outside[i]),
            "withholding_rate": a.get("dividend_withholding", 0.0),
            "withholding_drag_annual": _f(withholding_drag(raw_a, a.get("dividend_withholding", 0.0))) if raw_a is not None else None,
        })
    m = metrics(res.nav, res.weights, panel.cash_ret, res.turnover_py)
    ex = res.nav.pct_change().dropna() - panel.cash_ret.reindex(res.nav.index[1:]).fillna(0)
    if bootstrap:
        lo, hi = stationary_bootstrap_sharpe_ci(ex, n_boot=bootstrap)
        m["sharpe_ci95"] = [_f(lo, 3), _f(hi, 3)]
    nxt = t
    while True:
        nxt = next_session(cal, nxt)
        if period_end_flags(pd.DatetimeIndex([nxt]), cal, cfg.get("rebalance", {}).get("freq", "M")).iloc[0]:
            break
    out.update({
        "signal_session": str(t.date()),
        "is_rebalance_day": is_rebal,
        "action": ("今天是调仓信号日：下一交易日按“模型调仓后仓位”执行（只调整越出缓冲区的资产）"
                   if is_rebal and outside.any() else
                   "今天是调仓信号日，但所有资产都在缓冲区内，不需要交易" if is_rebal else
                   "非调仓日：只观察，不交易"),
        "next_rebalance_signal_date": str(nxt.date()),
        "idm": _f(d.get("idm")), "total_scale": _f(d.get("scale")),
        "target_cash": _f(1 - x.sum()),
        "assets": assets,
        "model_portfolio_backtest": m,
        "last_trades": [{"date": str(r.date.date()), "signal_date": str(r.signal_date.date()),
                         "turnover": _f(r.turnover, 4)} for r in res.trades.tail(6).itertuples()],
    })
    return out, health


def write_outputs(signal: dict, health: dict, out_dir: str | Path) -> Path:
    """信号文件一经写入不再覆盖：同一天重跑会另存为 <日期>.rerun-N.json，原文件保持不变。"""
    out_dir = Path(out_dir)
    day = signal["asof"]
    p = out_dir / day[:4] / f"{day}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    txt = json.dumps(signal, ensure_ascii=False, indent=1, default=str)
    if p.exists():
        n = 1
        while (q := p.with_name(f"{day}.rerun-{n}.json")).exists():
            n += 1
        p = q
    p.write_text(txt)
    (out_dir / "latest.json").write_text(txt)
    (out_dir / "health").mkdir(exist_ok=True)
    (out_dir / "health" / f"{day}.json").write_text(json.dumps(health, ensure_ascii=False, indent=1, default=str))
    (out_dir / "health" / "latest.json").write_text(json.dumps(health, ensure_ascii=False, indent=1, default=str))
    return p
