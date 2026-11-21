"""命令行：
  python -m etflab daily     [--config ...] [--data-dir data] [--out signals] [--asof YYYY-MM-DD]
  python -m etflab backtest  [--config ...]     # 策略与三个基准、分段表现
  python -m etflab validate  [--config ...]     # 参数网格、DSR、PBO
  python -m etflab targets   --holdings 本地持仓.csv   # 仅本地使用，输出不写入仓库
离线（无网络）：--offline-parquet <数据集> --offline-csv-dir <FRED csv 目录>
"""
from __future__ import annotations

import argparse
import copy
import itertools
import json
import sys

import numpy as np
import pandas as pd

from .config import load_config, override
from .engine import backtest as bt
from .engine.sizing import Sizer, apply_buffer
from .engine.stats import dsr, metrics, pbo_cscv


def _fmt(rows: dict) -> str:
    df = pd.DataFrame(rows).T
    cols = ["cagr", "vol", "sharpe", "max_drawdown", "calmar", "avg_risky_weight", "turnover_per_year"]
    df = df[[c for c in cols if c in df]]
    pct = {"cagr", "vol", "max_drawdown", "avg_risky_weight", "turnover_per_year"}
    for c in df:
        df[c] = df[c].map(lambda v: "-" if v is None or pd.isna(v) else (f"{v*100:6.1f}%" if c in pct else f"{v:5.2f}"))
    return df.to_string()


def _raw(args, cfg):
    from .pipeline import collect_raw
    offline = None
    if args.offline_parquet or args.offline_csv_dir:
        offline = {"parquet": args.offline_parquet, "csv_dir": args.offline_csv_dir}
    use_store = args.cmd == "daily" or args.no_fetch
    return collect_raw(cfg, args.data_dir if use_store else None, offline, fetch=not args.no_fetch)


def cmd_backtest(cfg, raw):
    from .data.panel import build_panel
    p = build_panel(cfg, raw)
    res = bt.run(p, cfg)
    rows = {"策略": metrics(res.nav, res.weights, p.cash_ret, res.turnover_py)}
    nt = bt.run(p, override(cfg, {"trend.method": "none"}))
    cw = bt.run(p, cfg, const_weights=res.weights[p.ids].mean())
    bh = bt.run(p, cfg, const_weights=pd.Series({p.ids[0]: 1.0}))
    for name, r in [("无趋势（仅风险预算）", nt), ("恒定平均仓位", cw), (f"买入持有 {p.ids[0]}", bh)]:
        nav = r.nav.loc[res.nav.index[0]:]
        rows[name] = metrics(nav / nav.iloc[0], r.weights.loc[nav.index[0]:], p.cash_ret, r.turnover_py)
    print(f"基准货币 {cfg.get('base_currency')}，区间 {res.nav.index[0].date()} ~ {res.nav.index[-1].date()}")
    print(_fmt(rows))
    yrs = res.nav.index.year
    edges = np.linspace(yrs.min(), yrs.max() + 1, 4).astype(int)
    seg = {}
    for a, b in zip(edges[:-1], edges[1:]):
        s = res.nav[(yrs >= a) & (yrs < b)]
        if len(s) > 250:
            seg[f"{a}-{b-1}"] = metrics(s / s.iloc[0], res.weights.loc[s.index], p.cash_ret)
    print("\n分段（策略）：")
    print(_fmt(seg))
    return rows


def cmd_validate(cfg, raw):
    from .data.panel import build_panel
    p = build_panel(cfg, raw)
    grid = cfg.get("validation", {}).get("grid", {
        "sizing.vol_target": [0.10, 0.12, 0.14],
        "rebalance.buffer": [0.05, 0.10, 0.20],
        "trend.attenuation": [False, True],
    })
    keys = list(grid)
    rets, rows, cache = {}, {}, {}
    for vals in itertools.product(*[grid[k] for k in keys]):
        c = override(cfg, dict(zip(keys, vals)))
        ik = json.dumps([c.get("vol"), c.get("trend")], sort_keys=True, default=str)
        if ik not in cache:
            cache[ik] = bt.compute_indicators(p, c)
        r = bt.run(p, c, cache[ik])
        lab = " ".join(f"{k.split('.')[-1]}={v}" for k, v in zip(keys, vals))
        rets[lab] = r.nav.pct_change()
        rows[lab] = metrics(r.nav, r.weights, p.cash_ret, r.turnover_py)
    R = pd.DataFrame(rets).dropna()
    ex = R.sub(p.cash_ret.reindex(R.index), axis=0)
    tab = pd.DataFrame(rows).T.sort_values("sharpe", ascending=False)
    print(_fmt(tab.to_dict("index")))
    best = tab.index[0]
    pb = pbo_cscv(R, 16)
    print(f"\n最优：{best}；DSR={dsr(ex[best], (ex.mean() / ex.std()).values):.1%}；PBO={pb['pbo']:.1%}"
          f"；夏普 最高/中位/最低 = {tab['sharpe'].max():.2f}/{tab['sharpe'].median():.2f}/{tab['sharpe'].min():.2f}")
    return tab, pb


def cmd_targets(cfg, raw, holdings_csv):
    from .data.panel import build_panel
    p = build_panel(cfg, raw)
    vol, fc = bt.compute_indicators(p, cfg)
    t = p.ret_base.index[-1]
    x, avg, d = Sizer(p.ids, p.weights, vol, fc, p.tr_base, cfg.get("sizing", {})).target(t)
    h = pd.read_csv(holdings_csv).set_index("id")["value"]
    total = float(h.sum())
    cur = h.reindex(p.ids).fillna(0.0) / total
    new = apply_buffer(x, cur, avg, cfg.get("rebalance", {}).get("buffer", 0.10))
    tab = pd.DataFrame({"目标": x, "当前": cur, "调整后": new, f"交易金额({cfg.get('base_currency')})": (new - cur) * total})
    print(f"信号日 {t.date()}  IDM={d['idm']:.2f}  账户总值 {total:,.0f}")
    print(tab.round(4).to_string())


def main(argv=None):
    ap = argparse.ArgumentParser(prog="etflab")
    ap.add_argument("cmd", choices=["daily", "backtest", "validate", "targets"])
    ap.add_argument("--config", default="config/portfolio.yaml")
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--out", default="signals")
    ap.add_argument("--asof", default=None)
    ap.add_argument("--holdings", default=None)
    ap.add_argument("--no-fetch", action="store_true", help="不联网，只用 data-dir 中已存数据")
    ap.add_argument("--offline-parquet", default=None)
    ap.add_argument("--offline-csv-dir", default=None)
    ap.add_argument("--bootstrap", type=int, default=1000)
    args = ap.parse_args(argv)
    pd.set_option("display.width", 200)
    pd.set_option("display.unicode.east_asian_width", True)
    cfg = load_config(args.config)
    raw, info = _raw(args, cfg)
    if args.cmd == "daily":
        from .pipeline import build_signal, default_asof, write_outputs
        asof = pd.Timestamp(args.asof) if args.asof else default_asof(cfg.get("schedule_calendar", "XNYS"))
        sig, health = build_signal(cfg, raw, asof, info, args.bootstrap)
        path = write_outputs(sig, health, args.out)
        print(f"{sig['status']}  信号已写入 {path}")
        if sig["status"] == "blocked":
            print("blocked_by:", sig["health_summary"]["blocked_by"])
        return 0
    if args.cmd == "backtest":
        cmd_backtest(cfg, raw)
    elif args.cmd == "validate":
        cmd_validate(cfg, raw)
    else:
        if not args.holdings:
            ap.error("targets 需要 --holdings")
        cmd_targets(cfg, raw, args.holdings)
    return 0


if __name__ == "__main__":
    sys.exit(main())
