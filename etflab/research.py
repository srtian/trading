"""研究层：预注册假设检验、对照组、参数网格、候选改动的样本外检验、PTNQ 实盘对照。

输出 research/latest.json（结论与数字）和 research/series.json（周频净值与指标，供网站画图）。
所有序列都是本系统计算出的衍生结果（净值、仓位、指标），不含供应商原始价格。
"""
from __future__ import annotations

import copy
import itertools
import json
import math
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from .config import config_hash, override
from .data.panel import build_panel
from .data.sources import fetch
from .data.totalreturn import total_return_index
from .engine import backtest as bt
from .engine.stats import dsr, metrics, paired_sharpe_diff_ci, pbo_cscv, stationary_bootstrap_sharpe_ci


def _clean(o):
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    if isinstance(o, (float, np.floating)):
        return None if not np.isfinite(o) else round(float(o), 6)
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.bool_,)):
        return bool(o)
    return o


def _excess(nav, cash):
    r = nav.pct_change().dropna()
    return r - cash.reindex(r.index).fillna(0.0)


def _slice(res, start):
    nav = res.nav.loc[start:]
    return nav / nav.iloc[0], res.weights.loc[start:]


# ---------------------------------------------------------------- PTNQ 规则
def ptnq_exposure(tr: pd.Series) -> pd.Series:
    """Pacer NASDAQ-100 Trendpilot 规则（依据公开方法说明的简化实现）：
    - 收盘连续 5 天高于 200 日均线 → 100% 股票
    - 处于 100% 时，收盘连续 5 天低于均线 → 50/50
    - 处于 50/50 时，200 日均线低于 5 天前的均线 → 100% 国库券
    - 处于国库券时，只有连续 5 天高于均线才回到 100%
    当日收盘判定，次日生效。"""
    sma = tr.rolling(200).mean()
    above = (tr > sma).astype(int)
    below = (tr < sma).astype(int)
    a5 = above.rolling(5).sum() == 5
    b5 = below.rolling(5).sum() == 5
    falling = sma < sma.shift(5)
    state, out = 1.0, []
    for i in range(len(tr)):
        if pd.isna(sma.iloc[i]):
            out.append(np.nan)
            continue
        if a5.iloc[i]:
            state = 1.0
        elif state == 1.0 and b5.iloc[i]:
            state = 0.5
        elif state == 0.5 and falling.iloc[i]:
            state = 0.0
        out.append(state)
    return pd.Series(out, index=tr.index).shift(1)


def ptnq_check(raw_qqq: pd.DataFrame, raw_ptnq: pd.DataFrame | None, cash_daily: pd.Series,
               fee: float = 0.0065, start: str = "2015-06-12") -> dict:
    tr = total_return_index(raw_qqq, 0.0)
    expo = ptnq_exposure(tr)
    r = tr.pct_change()
    c = cash_daily.reindex(r.index).fillna(0.0)
    sim = (expo * r + (1 - expo) * c - fee / 252).loc[start:].dropna()
    out = {"rule": "200 日均线 + 5 日确认 + 均线下行转国库券", "fee": fee, "start": start}
    yrs = (sim.index[-1] - sim.index[0]).days / 365.25
    out["simulated_cagr"] = float((1 + sim).prod() ** (1 / yrs) - 1)
    out["simulated_exposure_now"] = float(expo.iloc[-1]) if pd.notna(expo.iloc[-1]) else None
    if raw_ptnq is None or raw_ptnq.empty:
        out["status"] = "pending"
        out["note"] = "未取得 PTNQ 实盘数据"
        return out
    act = total_return_index(raw_ptnq, 0.0).pct_change().loc[start:]
    both = pd.concat([sim, act], axis=1, keys=["sim", "act"]).dropna()
    yrs = (both.index[-1] - both.index[0]).days / 365.25
    sc = (1 + both["sim"]).prod() ** (1 / yrs) - 1
    ac = (1 + both["act"]).prod() ** (1 / yrs) - 1
    out.update({"period": [str(both.index[0].date()), str(both.index[-1].date())],
                "simulated_cagr": float(sc), "actual_cagr": float(ac), "gap": float(sc - ac),
                "daily_corr": float(both.corr().iloc[0, 1]),
                "status": "pass" if abs(sc - ac) <= 0.005 else "fail",
                "threshold": 0.005})
    out["series"] = {"dates": [str(d.date()) for d in both.index[::5]],
                     "simulated": list((1 + both["sim"]).cumprod().iloc[::5].round(5)),
                     "actual": list((1 + both["act"]).cumprod().iloc[::5].round(5))}
    return out


# ---------------------------------------------------------------- 主流程
def run_research(cfg: dict, raw: dict, offline: dict | None = None, n_boot: int = 2000) -> tuple[dict, dict]:
    rc = cfg.get("research", {})
    p = build_panel(cfg, raw)
    ind = bt.compute_indicators(p, cfg)
    res = bt.run(p, cfg, ind)
    start = res.nav.index[0]
    cash = p.cash_ret

    runs = {"strategy": res,
            "no_trend": bt.run(p, override(cfg, {"trend.method": "none"})),
            "const_avg": bt.run(p, cfg, const_weights=res.weights[p.ids].mean())}
    eq_ids = [a["id"] for a in cfg["assets"] if a.get("weight", 0) > 0]
    runs["buy_hold_first"] = bt.run(p, cfg, const_weights=pd.Series({eq_ids[0]: 1.0}))
    labels = {"strategy": "主策略", "no_trend": "无趋势（仅风险预算）", "const_avg": "恒定平均仓位",
              "buy_hold_first": f"买入持有 {p.names[eq_ids[0]]}"}
    navs, mets, exs = {}, {}, {}
    for k, r in runs.items():
        nav, w = _slice(r, start)
        navs[k] = nav
        mets[k] = metrics(nav, w, cash, r.turnover_py)
        exs[k] = _excess(nav, cash)
        lo, hi = stationary_bootstrap_sharpe_ci(exs[k], n_boot=n_boot)
        mets[k]["sharpe_ci95"] = [lo, hi]

    # 预注册假设
    h1_ratio = mets["strategy"]["max_drawdown"] / mets["no_trend"]["max_drawdown"]
    h2 = paired_sharpe_diff_ci(exs["strategy"], exs["no_trend"], n_boot)
    h3 = paired_sharpe_diff_ci(exs["strategy"], exs["const_avg"], n_boot)
    hyp = [
        {"id": "H1", "claim": "趋势层把最大回撤缩小至少三分之一（相对无趋势）",
         "value": h1_ratio, "result": "通过" if h1_ratio <= 2 / 3 else "不通过",
         "detail": f"主策略 {mets['strategy']['max_drawdown']:.1%}，无趋势 {mets['no_trend']['max_drawdown']:.1%}"},
        {"id": "H2", "claim": "趋势层不显著降低夏普（夏普差的 95% 置信区间包含 0）",
         "value": h2, "result": "通过" if h2["ci95"][0] <= 0 <= h2["ci95"][1] or h2["ci95"][0] > 0 else "不通过",
         "detail": f"夏普差 {h2['diff']:+.2f}，95% 区间 [{h2['ci95'][0]:+.2f}, {h2['ci95'][1]:+.2f}]"},
        {"id": "H3", "claim": "择时本身有价值（夏普高于恒定平均仓位，区间下沿 > 0）",
         "value": h3, "result": "通过" if h3["ci95"][0] > 0 else ("方向一致但不显著" if h3["diff"] > 0 else "不通过"),
         "detail": f"夏普差 {h3['diff']:+.2f}，95% 区间 [{h3['ci95'][0]:+.2f}, {h3['ci95'][1]:+.2f}]"},
    ]

    # 分段
    yrs = res.nav.index.year
    edges = np.linspace(yrs.min(), yrs.max() + 1, 4).astype(int)
    segs = []
    for a, b in zip(edges[:-1], edges[1:]):
        row = {"period": f"{a}-{b - 1}"}
        for k in ("strategy", "no_trend", "const_avg"):
            nv = navs[k][(navs[k].index.year >= a) & (navs[k].index.year < b)]
            if len(nv) > 250:
                m = metrics(nv / nv.iloc[0], None, cash)
                row[k] = {"sharpe": m["sharpe"], "max_drawdown": m["max_drawdown"], "cagr": m["cagr"]}
        segs.append(row)

    # 参数网格 + PBO/DSR
    grid = cfg.get("validation", {}).get("grid", {})
    keys = list(grid)
    grid_rows, grid_rets, cache = [], {}, {}
    for vals in itertools.product(*[grid[k] for k in keys]):
        c = override(cfg, dict(zip(keys, vals)))
        ik = json.dumps([c.get("vol"), c.get("trend")], sort_keys=True, default=str)
        if ik not in cache:
            cache[ik] = bt.compute_indicators(p, c)
        r = bt.run(p, c, cache[ik])
        lab = ", ".join(f"{k.split('.')[-1]}={v}" for k, v in zip(keys, vals))
        grid_rets[lab] = r.nav.pct_change()
        m = metrics(r.nav, r.weights, cash, r.turnover_py)
        grid_rows.append({"params": dict(zip(keys, vals)), "label": lab, **{k: m[k] for k in
                          ("sharpe", "cagr", "max_drawdown", "turnover_per_year")}})
    gridinfo = {}
    if grid_rows:
        R = pd.DataFrame(grid_rets).dropna()
        ex = R.sub(cash.reindex(R.index), axis=0)
        best = max(grid_rows, key=lambda x: x["sharpe"])
        sh = [g["sharpe"] for g in grid_rows]
        gridinfo = {"n": len(grid_rows), "rows": sorted(grid_rows, key=lambda x: -x["sharpe"]),
                    "best": best["label"], "sharpe_max": max(sh), "sharpe_median": float(np.median(sh)),
                    "sharpe_min": min(sh), "pbo": pbo_cscv(R, 16)["pbo"],
                    "dsr": dsr(ex[best["label"]], (ex.mean() / ex.std()).values)}

    # 候选改动：样本外市场（单资产）是否同向
    cand_out = []
    oos = rc.get("oos_markets", [])
    oos_raw = {}
    for spec in oos:
        try:
            oos_raw[spec["symbol"]] = fetch({"source": "yahoo", "symbol": spec["symbol"]}, cfg.get("start", "1999-01-01"), offline)
        except Exception as e:  # noqa: BLE001
            oos_raw[spec["symbol"]] = None
    for cand in rc.get("candidates", []):
        rows = []
        for spec in oos:
            rw = oos_raw.get(spec["symbol"])
            if rw is None:
                rows.append({"market": spec["symbol"], "status": "无数据"})
                continue
            c0 = copy.deepcopy(cfg)
            c0["base_currency"] = "USD"
            c0["fx"] = {}
            c0["assets"] = [{"id": "m", "name": spec["symbol"], "signal": {"source": "yahoo", "symbol": spec["symbol"]},
                             "currency": "USD", "dividend_withholding": spec.get("withholding", 0.15), "weight": 1.0}]
            r2 = dict(raw)
            r2 = {k: v for k, v in r2.items() if not k.startswith("asset:") and not k.startswith("fx:")}
            r2["asset:m"] = rw
            pp = build_panel(c0, r2)
            base = bt.run(pp, c0)
            alt = bt.run(pp, override(c0, cand["change"]))
            mb = metrics(base.nav, base.weights, pp.cash_ret)
            ma = metrics(alt.nav.loc[base.nav.index[0]:] / alt.nav.loc[base.nav.index[0]], None, pp.cash_ret)
            rows.append({"market": spec["symbol"], "sharpe_base": mb["sharpe"], "sharpe_candidate": ma["sharpe"],
                         "improves": ma["sharpe"] > mb["sharpe"]})
        valid = [r for r in rows if "improves" in r]
        verdict = ("全部同向，可提交采纳" if valid and all(r["improves"] for r in valid) else
                   "未在所有样本外市场同向，不采纳" if valid else "数据不足")
        cand_out.append({"name": cand["name"], "change": cand["change"], "markets": rows, "verdict": verdict})

    # PTNQ 对照
    ptnq = None
    pc = rc.get("ptnq")
    if pc:
        und = raw.get(f"asset:{pc.get('underlying_asset', 'nasdaq100')}")
        try:
            act = fetch({"source": "yahoo", "symbol": pc.get("symbol", "PTNQ")}, "2015-01-01", offline)
        except Exception:  # noqa: BLE001
            act = None
        if und is not None:
            usd_cash = cash  # 现金收益已按基准货币；PTNQ 为美元，误差来自汇率，用于方向性检验
            if cfg.get("base_currency", "USD") != "USD" and f"cash:{cfg['cash']['id']}" in raw:
                ctr = total_return_index(raw[f"cash:{cfg['cash']['id']}"], 0.0)
                usd_cash = ctr.pct_change().fillna(0.0)
            ptnq = ptnq_check(und, act, usd_cash, pc.get("fee", 0.0065), pc.get("start", "2015-06-12"))

    # 序列（周频）
    wk = lambda s: s.resample("W-FRI").last().dropna()  # noqa: E731
    dd = {k: (v / v.cummax() - 1) for k, v in navs.items()}
    roll = {}
    exw = exs["strategy"]
    win = 252 * 3
    if len(exw) > win:
        rs = exw.rolling(win).mean() / exw.rolling(win).std() * math.sqrt(252)
        roll["strategy"] = wk(rs.dropna())
    vol, fc = ind
    last5 = res.nav.index[-1] - pd.Timedelta(days=365 * 5)
    series = {
        "nav": {k: {"dates": [str(d.date()) for d in wk(v).index], "values": list(wk(v).round(5))} for k, v in navs.items()},
        "drawdown": {k: {"dates": [str(d.date()) for d in wk(v).index], "values": list(wk(v).round(5))} for k, v in dd.items()},
        "rolling_sharpe_3y": {k: {"dates": [str(d.date()) for d in v.index], "values": list(v.round(4))} for k, v in roll.items()},
        "risky_weight": {"dates": [str(d.date()) for d in wk(res.weights[p.ids].sum(axis=1)).index],
                         "values": list(wk(res.weights[p.ids].sum(axis=1)).round(4))},
        "forecast_5y": {i: {"dates": [str(d.date()) for d in wk(fc[i].loc[last5:]).index],
                            "values": list(wk(fc[i].loc[last5:]).round(3))} for i in p.ids if fc[i].notna().any()},
        "labels": labels, "names": p.names,
    }
    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "config_sha256": config_hash(cfg), "base_currency": cfg.get("base_currency"),
        "period": [str(start.date()), str(res.nav.index[-1].date())],
        "labels": labels, "metrics": mets, "hypotheses": hyp, "segments": segs,
        "grid": gridinfo, "candidates": cand_out, "ptnq": ptnq,
        "evidence_cards": evidence_cards(cfg, mets, hyp, gridinfo, cand_out, ptnq),
    }
    return _clean(out), _clean(series)


def evidence_cards(cfg, mets, hyp, grid, cands, ptnq) -> list:
    s, nt, ca = mets["strategy"], mets["no_trend"], mets["const_avg"]
    cards = [
        {"name": "波动目标 + 风险预算", "role": "参与推荐", "status": "采用",
         "hypothesis": "按波动率分配风险，组合整体波动稳定在目标附近，比按资金平均分配更分散。",
         "literature": ["Moreira & Muir (2017) Volatility-Managed Portfolios",
                        "Cederburg et al. (2020) On the Performance of Volatility-Managed Portfolios（反方）",
                        "Harvey et al. (2018) The Impact of Volatility Targeting"],
         "evidence": f"无趋势版本：夏普 {nt['sharpe']:.2f}，波动 {nt['vol']:.1%}（目标 {cfg['sizing']['vol_target']:.0%}）。",
         "caveat": "目标波动只决定平均仓位，不能保证收益。"},
        {"name": "EWMAC 趋势强度", "role": "参与推荐", "status": "采用",
         "hypothesis": "趋势能压低回撤，代价是少量收益（预注册 H1、H2）。",
         "literature": ["Moskowitz, Ooi, Pedersen (2012) Time Series Momentum",
                        "Hurst, Ooi, Pedersen (2017) A Century of Evidence on Trend-Following Investing"],
         "evidence": f"最大回撤 {s['max_drawdown']:.1%} vs 无趋势 {nt['max_drawdown']:.1%}；"
                     f"夏普 {s['sharpe']:.2f} vs {nt['sharpe']:.2f}。" + " ".join(f"{h['id']} {h['result']}" for h in hyp),
         "caveat": "快速 V 型反弹中会错过一部分涨幅。"},
        {"name": "缓冲区（交易到边缘）", "role": "参与推荐", "status": "采用",
         "hypothesis": "减少小额交易，降低换手而不损害风险控制。",
         "literature": ["Carver, Systematic Trading (2015)"],
         "evidence": f"主策略年换手 {s['turnover_per_year']:.0%}。",
         "caveat": "缓冲越宽，实际仓位偏离目标越多。"},
    ]
    if grid:
        cards.append({"name": "参数稳健性", "role": "检验", "status": "平原" if grid["sharpe_max"] - grid["sharpe_median"] < 0.1 else "敏感",
                      "hypothesis": "结果不依赖于某组特定参数。", "literature": ["Bailey et al. (2017) The Probability of Backtest Overfitting",
                                                                    "Bailey & López de Prado (2014) The Deflated Sharpe Ratio"],
                      "evidence": f"{grid['n']} 组参数夏普 {grid['sharpe_min']:.2f}～{grid['sharpe_max']:.2f}（中位 {grid['sharpe_median']:.2f}）；"
                                  f"PBO {grid['pbo']:.0%}，DSR {grid['dsr']:.0%}。",
                      "caveat": "PBO 高而夏普平稳时，说明“挑最优”无意义，应保留默认参数。"})
    for c in cands:
        cards.append({"name": f"候选：{c['name']}", "role": "只展示", "status": c["verdict"],
                      "hypothesis": "在主样本中观察到改善，需要在未使用过的市场上同向才采纳。",
                      "literature": [], "evidence": "；".join(
                          f"{m['market']} {m.get('sharpe_base', float('nan')):.2f}→{m.get('sharpe_candidate', float('nan')):.2f}"
                          if "improves" in m else f"{m['market']} {m['status']}" for m in c["markets"]),
                      "caveat": "按预注册规则，改动必须登记在变更记录里。"})
    if ptnq:
        cards.append({"name": "PTNQ 实盘对照", "role": "外部证据", "status": {"pass": "通过", "fail": "不通过"}.get(ptnq.get("status"), "待在线运行"),
                      "hypothesis": "用同样的引擎复现一只真实 ETF 的规则，若模拟收益明显高于实盘，说明回测偏乐观。",
                      "literature": ["Pacer NASDAQ-100 Trendpilot Index 方法说明"],
                      "evidence": (f"{ptnq['period'][0]} 起：模拟年化 {ptnq['simulated_cagr']:.2%}，实盘 {ptnq['actual_cagr']:.2%}，"
                                   f"差 {ptnq['gap']:+.2%}（门槛 ±0.5%）；日收益相关 {ptnq['daily_corr']:.3f}。")
                      if ptnq.get("status") in ("pass", "fail") else ptnq.get("note", ""),
                      "caveat": "规则为依据公开说明的简化实现，与指数公司的细节可能有出入。"})
    cards.append({"name": "情绪指标（VIX、AAII、融资余额）", "role": "未接入", "status": "待研究",
                  "hypothesis": "极端情绪对未来收益有反向预测力。",
                  "literature": ["Baker & Wurgler (2006) Investor Sentiment and the Cross-Section of Stock Returns"],
                  "evidence": "尚未完成信息系数与 Newey-West 检验，不参与推荐。",
                  "caveat": "按预注册规则，未通过检验的指标只能展示。"})
    return cards


def write_research(out: dict, series: dict, out_dir: str | Path = "research") -> None:
    d = Path(out_dir)
    d.mkdir(parents=True, exist_ok=True)
    (d / "latest.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))
    (d / "series.json").write_text(json.dumps(series, ensure_ascii=False))
