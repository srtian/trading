"""静态网站生成器（GitHub Pages）。

输入：signals/（每日信号与健康报告）、research/（研究结果与周频序列）、config。
输出：_site/ 下五个页面。图表在这里直接生成 SVG（不依赖外部脚本或 CDN），悬停提示用一小段内联 JS。
页面公开：只展示百分比与指标，不含任何金额。
"""
from __future__ import annotations

import html
import json
import os
from datetime import date
from pathlib import Path

E = html.escape

SERIES_COLORS = ["var(--s1)", "var(--s2)", "var(--s3)", "var(--s4)", "var(--s5)", "var(--s6)"]

CSS = """
:root{--bg:#fcfcfb;--card:#ffffff;--ink:#0b0b0b;--ink2:#52514e;--ink3:#8a8984;--line:#e6e5e1;--band:#eef3fb;
--s1:#2a78d6;--s2:#eb6834;--s3:#1baf7a;--s4:#eda100;--s5:#e87ba4;--s6:#6250d6;
--good:#0ca30c;--warn:#b07a00;--bad:#d03b3b}
@media (prefers-color-scheme:dark){:root{--bg:#1a1a19;--card:#232321;--ink:#f0efec;--ink2:#c3c2b7;--ink3:#8f8e86;
--line:#383835;--band:#22324a;--s1:#3987e5;--s2:#d95926;--s3:#199e70;--s4:#c98500;--s5:#d55181;--s6:#9085e9;
--good:#0ca30c;--warn:#fab219;--bad:#e66767}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.55 -apple-system,BlinkMacSystemFont,"PingFang SC","Noto Sans SC","Segoe UI",sans-serif}
main{max-width:860px;margin:0 auto;padding:16px 14px 60px}
nav{position:sticky;top:0;background:var(--bg);border-bottom:1px solid var(--line);z-index:5}
nav div{max-width:860px;margin:0 auto;display:flex;gap:4px;overflow-x:auto;padding:8px 10px}
nav a{white-space:nowrap;color:var(--ink2);text-decoration:none;padding:6px 10px;border-radius:8px;font-size:14px}
nav a.on{background:var(--card);color:var(--ink);font-weight:600;box-shadow:0 0 0 1px var(--line)}
h1{font-size:22px;margin:14px 0 4px}h2{font-size:17px;margin:26px 0 8px}h3{font-size:15px;margin:0 0 6px}
p{margin:6px 0}.muted{color:var(--ink2)}.small{font-size:13px}.tiny{font-size:12px;color:var(--ink3)}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px;margin:10px 0}
.grid2{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}.grid2 .card{padding:10px;margin:6px 0}.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:6px;margin-top:8px}.stats div{display:flex;flex-direction:column}.stats b{font-size:16px}
.kpi{font-size:22px;font-weight:650;letter-spacing:-.5px}
.badge{white-space:nowrap;flex-shrink:0;display:inline-flex;align-items:center;gap:5px;padding:2px 9px;border-radius:999px;font-size:13px;font-weight:600;
border:1px solid currentColor}
.ok{color:var(--good)}.warn{color:var(--warn)}.bad{color:var(--bad)}
table{border-collapse:collapse;width:100%;font-size:14px}th,td{padding:7px 6px;border-bottom:1px solid var(--line);
text-align:right;white-space:nowrap}th:first-child,td:first-child{text-align:left}th{color:var(--ink2);font-weight:600;font-size:13px}
.scroll{overflow-x:auto;-webkit-overflow-scrolling:touch}
.legend{display:flex;flex-wrap:wrap;gap:12px;font-size:13px;color:var(--ink2);margin:4px 0}
.legend i{display:inline-block;width:14px;height:3px;border-radius:2px;vertical-align:middle;margin-right:5px}
.chart{position:relative}.chart svg{width:100%;height:auto;display:block}
.tip{position:absolute;pointer-events:none;background:var(--card);border:1px solid var(--line);border-radius:8px;
padding:6px 8px;font-size:12px;box-shadow:0 2px 8px rgba(0,0,0,.12);display:none;white-space:nowrap;z-index:3}
.axis{fill:var(--ink3);font-size:12px}.chart{max-width:620px}.gridl{stroke:var(--line);stroke-width:1}
code{font-size:12px;background:var(--band);padding:1px 4px;border-radius:4px;word-break:break-all}
a{color:var(--s1)}ul{padding-left:20px;margin:6px 0}li{margin:3px 0}
.steps{display:flex;flex-wrap:wrap;gap:4px;font-size:12px;color:var(--ink2)}.steps span{background:var(--band);
padding:1px 6px;border-radius:5px}
"""

TIP_JS = """
document.querySelectorAll('.chart').forEach(function(c){
 var d=JSON.parse(c.querySelector('script').textContent),svg=c.querySelector('svg'),tip=c.querySelector('.tip'),
 vl=svg.querySelector('.vline'),dots=svg.querySelectorAll('.dot');
 function fmt(v){if(v===null||isNaN(v))return'-';return d.fmt==='pct'?(v*100).toFixed(d.dp)+'%':v.toFixed(d.dp)}
 function near(xs,x){var lo=0,hi=xs.length-1;while(hi-lo>1){var m=(lo+hi)>>1;if(xs[m]<x)lo=m;else hi=m}
  return Math.abs(xs[lo]-x)<Math.abs(xs[hi]-x)?lo:hi}
 function move(ev){var r=svg.getBoundingClientRect(),px=(ev.clientX-r.left)/r.width*d.W;
  if(px<d.L||px>d.W-d.R){hide();return}var x=d.x0+(px-d.L)/(d.W-d.L-d.R)*(d.x1-d.x0),rows=[],day=null;
  d.s.forEach(function(s,k){if(!s.x.length)return;var i=near(s.x,x),y=s.y[i];day=day||s.d[i];
   var sy=d.T+(d.y1-(d.log?Math.log(y):y))/(d.y1-d.y0)*(d.H-d.T-d.B);dots[k].setAttribute('cx',d.L+(s.x[i]-d.x0)/(d.x1-d.x0)*(d.W-d.L-d.R));
   dots[k].setAttribute('cy',sy);dots[k].style.display='';rows.push('<i style="background:'+s.c+'"></i>'+s.n+'：<b>'+fmt(y)+'</b>')});
  vl.setAttribute('x1',px);vl.setAttribute('x2',px);vl.style.display='';
  tip.innerHTML='<div style="color:var(--ink3)">'+day+'</div>'+rows.join('<br>');tip.style.display='block';
  var tx=(ev.clientX-r.left)+12;if(tx+tip.offsetWidth>r.width)tx=(ev.clientX-r.left)-tip.offsetWidth-12;
  tip.style.left=tx+'px';tip.style.top='8px'}
 function hide(){tip.style.display='none';vl.style.display='none';dots.forEach(function(e){e.style.display='none'})}
 svg.addEventListener('pointermove',move);svg.addEventListener('pointerleave',hide);svg.addEventListener('pointerdown',move);
});
"""


def _days(s: str) -> int:
    return date.fromisoformat(s).toordinal()


def pct(v, dp=1, sign=False):
    if v is None:
        return "-"
    return f"{v * 100:+.{dp}f}%" if sign else f"{v * 100:.{dp}f}%"


def num(v, dp=2, sign=False):
    if v is None:
        return "-"
    return f"{v:+.{dp}f}" if sign else f"{v:.{dp}f}"


def status_badge(s: str) -> str:
    m = {"pass": ("ok", "✓", "正常"), "warn": ("warn", "!", "有警告"), "blocked": ("bad", "✕", "已阻断"),
         "fail": ("bad", "✕", "失败"), "通过": ("ok", "✓", "通过"), "不通过": ("bad", "✕", "不通过")}
    cls, icon, label = m.get(s, ("warn", "•", s))
    return f'<span class="badge {cls}">{icon} {E(label)}</span>'


def nice_ticks(y0: float, y1: float, log: bool = False) -> list:
    """整齐刻度：线性用 1/2/2.5/5×10^k 的步长；对数用 …0.5, 1, 2, 5, 10…（返回值与输入同为变换后的坐标）。"""
    import math
    if log:
        cands = [m * 10 ** e for e in range(-2, 4) for m in (1, 2, 5)]
        t = [math.log(c) for c in cands if y0 <= math.log(c) <= y1]
        while len(t) > 6:
            t = t[::2]
        return t
    raw = (y1 - y0) / 4
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    start = math.ceil(y0 / step - 1e-9) * step
    out, v = [], start
    while v <= y1 + 1e-9:
        out.append(round(v, 10))
        v += step
    return out


def line_chart(series: list, fmt="pct", dp=1, height=230, log=False, zero=False, ymin=None, ymax=None) -> str:
    """series: [{name, dates[], values[]}]；≤4 条时在线尾直接标注名称。"""
    W, H, L, R, T, B = 440, height, 40, 10, 10, 22
    ss = [s for s in series if s.get("dates")]
    if not ss:
        return '<p class="muted small">暂无数据</p>'
    xs = [[_days(d) for d in s["dates"]] for s in ss]
    ys_all = [v for s in ss for v in s["values"] if v is not None]
    tf = (lambda v: __import__("math").log(v)) if log else (lambda v: v)
    y0 = min(tf(v) for v in ys_all) if ymin is None else tf(ymin)
    y1 = max(tf(v) for v in ys_all) if ymax is None else tf(ymax)
    if zero:
        y0, y1 = min(y0, 0), max(y1, 0)
    if y1 - y0 < 1e-9:
        y1 = y0 + 1
    pad = (y1 - y0) * 0.06
    y0 = y0 - (0 if (zero and y0 == 0) or ymin is not None else pad)
    y1 = y1 + (0 if ymax is not None else pad)
    x0, x1 = min(x[0] for x in xs), max(x[-1] for x in xs)
    sx = lambda x: L + (x - x0) / max(x1 - x0, 1) * (W - L - R)  # noqa: E731
    sy = lambda y: T + (y1 - y) / (y1 - y0) * (H - T - B)          # noqa: E731
    out = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="折线图">']
    for yv in nice_ticks(y0, y1, log):
        lab = __import__("math").exp(yv) if log else yv
        txt = f"{lab * 100:.0f}%" if fmt == "pct" else (f"{lab:g}" if abs(lab) < 100 else f"{lab:.0f}")
        out.append(f'<line class="gridl" x1="{L}" x2="{W - R}" y1="{sy(yv):.1f}" y2="{sy(yv):.1f}"/>'
                   f'<text class="axis" x="{L - 6}" y="{sy(yv) + 4:.1f}" text-anchor="end">{txt}</text>')
    if zero:
        out.append(f'<line x1="{L}" x2="{W - R}" y1="{sy(0):.1f}" y2="{sy(0):.1f}" stroke="var(--ink3)" stroke-width="1"/>')
    y_first, y_last = date.fromordinal(x0).year, date.fromordinal(x1).year
    step = max(1, round((y_last - y_first) / 6))
    for yr in range(y_first + 1, y_last + 1, step):
        xv = date(yr, 1, 1).toordinal()
        out.append(f'<text class="axis" x="{sx(xv):.1f}" y="{H - 6}" text-anchor="middle">{yr}</text>')
    labels = []
    for i, (s, x) in enumerate(zip(ss, xs)):
        col = s.get("color", SERIES_COLORS[i % len(SERIES_COLORS)])
        pts = " ".join(f"{sx(a):.1f},{sy(tf(v)):.1f}" for a, v in zip(x, s["values"]) if v is not None)
        out.append(f'<polyline fill="none" stroke="{col}" stroke-width="{2.2 if i == 0 else 1.6}" '
                   f'stroke-linejoin="round" points="{pts}"/>')
        # 手机屏幕上线尾标签会重叠，改由图例 + 悬停提示标识
    labels.sort()
    for j in range(1, len(labels)):                       # 末端标签防重叠
        labels[j][0] = max(labels[j][0], labels[j - 1][0] + 13)
    for yv, name, col in labels:
        out.append(f'<text x="{W - R + 6}" y="{yv + 4:.1f}" font-size="11" fill="var(--ink2)">{E(name)}</text>')
    out.append(f'<line class="vline" x1="0" x2="0" y1="{T}" y2="{H - B}" stroke="var(--ink3)" stroke-dasharray="3 3" style="display:none"/>')
    for i, _ in enumerate(ss):
        col = ss[i].get("color", SERIES_COLORS[i % len(SERIES_COLORS)])
        out.append(f'<circle class="dot" r="4" fill="{col}" stroke="var(--card)" stroke-width="2" style="display:none"/>')
    out.append("</svg>")
    data = {"W": W, "H": H, "L": L, "R": R, "T": T, "B": B, "x0": x0, "x1": x1, "y0": y0, "y1": y1, "fmt": fmt, "dp": dp,
            "log": log, "s": [{"n": s["name"], "c": s.get("color", SERIES_COLORS[i % len(SERIES_COLORS)]),
                               "x": x, "y": s["values"], "d": s["dates"]} for i, (s, x) in enumerate(zip(ss, xs))]}
    legend = "".join(f'<span><i style="background:{d["c"]}"></i>{E(d["n"])}</span>' for d in data["s"]) if len(ss) > 1 else ""
    return (f'<div class="legend">{legend}</div><div class="chart">{"".join(out)}<div class="tip"></div>'
            f'<script type="application/json">{json.dumps(data, ensure_ascii=False)}</script></div>')


def position_bar(target, low, high, current, scale=0.6) -> str:
    """目标（竖线）、缓冲区（色带）、模型当前持仓（圆点）。"""
    W, H = 320, 22
    s = lambda v: 4 + min(max(v or 0, 0), scale) / scale * (W - 8)  # noqa: E731
    return (f'<svg viewBox="0 0 {W} {H}" style="width:100%;height:auto;margin:8px 0 4px" aria-label="目标 {pct(target)}，当前 {pct(current)}">'
            f'<line x1="4" x2="{W - 4}" y1="11" y2="11" stroke="var(--line)" stroke-width="2"/>'
            f'<rect x="{s(low):.1f}" y="5" width="{max(s(high) - s(low), 2):.1f}" height="12" rx="3" fill="var(--band)"/>'
            f'<line x1="{s(target):.1f}" x2="{s(target):.1f}" y1="3" y2="19" stroke="var(--s1)" stroke-width="2.5"/>'
            f'<circle cx="{s(current):.1f}" cy="11" r="4.5" fill="var(--s2)" stroke="var(--card)" stroke-width="2"/></svg>')


def page(title: str, active: str, body: str, foot: str = "") -> str:
    tabs = [("index.html", "今日决策"), ("evidence.html", "证据"), ("tracking.html", "模型跟踪"),
            ("health.html", "数据健康"), ("add.html", "添加 ETF")]
    nav = "".join(f'<a href="{h}" class="{"on" if h == active else ""}">{t}</a>' for h, t in tabs)
    return (f'<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex">'
            f'<title>{E(title)} · etflab</title><style>{CSS}</style></head><body><nav><div>{nav}</div></nav>'
            f'<main>{body}<p class="tiny" style="margin-top:40px">{foot}仅供本人研究使用，不构成投资建议。'
            f'所有数字均为占组合的百分比，不含任何账户金额。</p></main><script>{TIP_JS}</script></body></html>')


# ---------------------------------------------------------------- 页面
def page_today(sig: dict, health: dict, repo: str | None) -> str:
    st = sig.get("status")
    head = (f'<h1>今日决策</h1><p class="muted small">信号日 {E(sig.get("signal_session", sig["asof"]))}（纽交所收盘）· '
            f'基准货币 {E(sig.get("base_currency", ""))} · {status_badge(st)}</p>')
    if st == "blocked":
        return head + (f'<div class="card"><h3>{status_badge("blocked")} 今天不给出建议</h3>'
                       f'<p>{E(sig.get("message", ""))}</p><p class="small">阻断原因：{E(", ".join(sig["health_summary"]["blocked_by"]))}'
                       f'，详见 <a href="health.html">数据健康</a>。</p></div>')
    a = sig["assets"]
    risky = sum(x["target"] for x in a if x.get("risk_budget_input", 0) > 0)
    cur = sum(x["model_current"] or 0 for x in a if x.get("risk_budget_input", 0) > 0)
    rebal = sig.get("is_rebalance_day")
    act_cls = "bad" if rebal and any(x["outside_buffer"] for x in a) else "ok"
    body = head + (
        f'<div class="card"><h3 class="{act_cls}">{"⚑" if act_cls == "bad" else "✓"} {E(sig["action"])}</h3>'
        f'<p class="small muted">下一个调仓信号日：<b>{E(sig["next_rebalance_signal_date"])}</b>。按规则只在月末信号日后的下一交易日交易，'
        f'并且只把越出缓冲区的资产调到缓冲区边缘。</p></div>'
        f'<div class="grid2"><div class="card"><div class="tiny">目标风险资产合计</div><div class="kpi">{pct(risky)}</div>'
        f'<div class="small muted">现金/短债 {pct(sig.get("target_cash"))}</div></div>'
        f'<div class="card"><div class="tiny">模型组合当前风险资产</div><div class="kpi">{pct(cur)}</div>'
        f'<div class="small muted">上次调仓后随价格漂移</div></div>'
        f'<div class="card"><div class="tiny">分散乘数 IDM</div><div class="kpi">{num(sig.get("idm"))}</div>'
        f'<div class="small muted">总仓位缩放 {num(sig.get("total_scale"))}</div></div></div>')
    cards = []
    for x in a:
        if x.get("risk_budget_input", 0) <= 0:
            continue
        at = x["attribution"]
        if not x["outside_buffer"]:
            flag = '<span class="badge ok">✓ 缓冲区内</span>'
        elif rebal:
            flag = '<span class="badge bad">⚑ 需要交易</span>'
        else:
            flag = '<span class="badge warn">! 月末若仍越界将调整</span>'
        cards.append(
            f'<div class="card"><div style="display:flex;justify-content:space-between;align-items:baseline;gap:8px">'
            f'<h3>{E(x["name"])} <span class="tiny">{E(" / ".join(x.get("exec") or []))}</span></h3>{flag}</div>'
            f'<div class="stats"><div><span class="tiny">年化波动</span><b>{pct(x["sigma_annual"])}</b></div>'
            f'<div><span class="tiny">趋势 F</span><b>{num(x["forecast"], 1)}</b></div>'
            f'<div><span class="tiny">目标</span><b>{pct(x["target"])}</b></div>'
            f'<div><span class="tiny">模型当前</span><b>{pct(x["model_current"])}</b></div></div>'
            f'{position_bar(x["target"], x["buffer_low"], x["buffer_high"], x["model_current"])}'
            f'<div class="steps"><span>风险预算 {pct(at.get("risk_budget"))}</span>→<span>波动调整 {pct(at.get("vol_scaled"))}</span>→'
            f'<span>× F/10 = {pct(at.get("after_trend"))}</span>→<span>单资产上限后 {pct(at.get("after_asset_cap"))}</span>→'
            f'<span>最终 {pct(at.get("final"))}</span></div>'
            f'<p class="tiny">缓冲区 {pct(x["buffer_low"])}～{pct(x["buffer_high"])} · 预提税损 {pct(x.get("withholding_drag_annual"), 2)}/年</p></div>')
    body += ('<h2>每只资产：从风险预算到最终仓位</h2>' + "".join(cards)
             + '<p class="tiny">仓位条：蓝线 = 目标，浅色带 = 缓冲区，橙点 = 模型组合当前持仓（刻度 0～60%）。'
             'F 为趋势强度（0～20，10 为正常仓位，0 为空仓）。</p>')
    watch = [x for x in a if x.get("risk_budget_input", 0) <= 0]
    if watch:
        body += ('<h2>观察列表</h2><div class="card scroll"><table><tr><th>标的</th><th>年化波动</th><th>趋势 F</th></tr>'
                 + "".join(f'<tr><td>{E(x["name"])}</td><td>{pct(x["sigma_annual"])}</td><td>{num(x["forecast"], 1)}</td></tr>' for x in watch)
                 + '</table><p class="tiny">只计算指标，不参与仓位。</p></div>')
    pv = sig.get("provenance", {})
    sha = pv.get("git_sha") or ""
    link = f'<a href="https://github.com/{repo}/commit/{sha}">{sha[:8]}</a>' if repo and sha else E(sha[:8])
    body += (f'<h2>这份信号是怎么来的</h2><div class="card small"><ul>'
             f'<li>代码版本：{link}（etflab {E(str(pv.get("code_version")))}）</li>'
             f'<li>配置指纹：<code>{E(str(pv.get("config_sha256"))[:16])}</code></li>'
             f'<li>数据清单指纹：<code>{E(str(pv.get("data_manifest_sha256"))[:16])}</code>；本次发现供应商修改历史数据 {E(str(pv.get("data_revisions_found")))} 条</li>'
             f'<li>生成时间：{E(str(pv.get("generated_at")))}</li></ul>'
             + (f'<p>原始信号文件：<a href="https://github.com/{repo}/blob/main/signals/{sig["asof"][:4]}/{sig["asof"]}.json">signals/{sig["asof"][:4]}/{sig["asof"]}.json</a></p>' if repo else "")
             + '</div>')
    return body


def page_evidence(res: dict | None) -> str:
    if not res:
        return '<h1>证据</h1><p class="muted">研究结果尚未生成（每周六自动运行，也可手动触发 Research 工作流）。</p>'
    L, M = res["labels"], res["metrics"]
    body = (f'<h1>证据</h1><p class="muted small">区间 {E(res["period"][0])} ～ {E(res["period"][1])} · 基准货币 {E(str(res.get("base_currency")))} · '
            f'生成于 {E(res["generated_at"][:10])}</p>')
    body += '<h2>预注册假设</h2>' + "".join(
        f'<div class="card"><div style="display:flex;justify-content:space-between;gap:8px;align-items:baseline">'
        f'<h3>{E(h["id"])} {E(h["claim"])}</h3>'
        f'{status_badge(h["result"]) if h["result"] in ("通过", "不通过") else chr(60) + "span class=" + chr(34) + "badge warn" + chr(34) + chr(62) + "~ " + E(h["result"]) + "</span>"}</div>'
        f'<p class="small muted">{E(h["detail"])}</p></div>' for h in res["hypotheses"])
    body += ('<h2>主策略与对照组</h2><div class="card scroll"><table><tr><th></th><th>年化</th><th>夏普</th>'
             '<th>最大回撤</th><th>平均风险仓位</th></tr>' + "".join(
                 f'<tr><td style="white-space:normal">{E(L[k])}</td><td>{pct(m["cagr"])}</td>'
                 f'<td>{num(m["sharpe"])}<div class="tiny">[{num(m["sharpe_ci95"][0])}, {num(m["sharpe_ci95"][1])}]</div></td>'
                 f'<td>{pct(m["max_drawdown"])}</td><td>{pct(m.get("avg_risky_weight"))}</td></tr>'
                 for k, m in M.items()) + '</table><p class="tiny">夏普下方为 95% 区间（平稳块自助法，平均块长 20 个交易日）。'
             + "；".join(f'{E(L[k])}年换手 {pct(m.get("turnover_per_year"), 0)}' for k, m in M.items() if k != "buy_hold_first") + '。</p></div>')
    body += '<h2>证据卡</h2>'
    for c in res.get("evidence_cards", []):
        lit = "".join(f"<li>{E(x)}</li>" for x in c.get("literature", []))
        body += (f'<div class="card"><h3>{E(c["name"])} <span class="tiny">· {E(c["role"])}</span></h3>'
                 f'<p class="small"><b>状态：</b>{E(c["status"])}</p><p class="small"><b>假设：</b>{E(c["hypothesis"])}</p>'
                 f'<p class="small"><b>证据：</b>{E(c["evidence"])}</p><p class="small muted"><b>注意：</b>{E(c["caveat"])}</p>'
                 + (f'<details class="small"><summary>文献</summary><ul>{lit}</ul></details>' if lit else "") + '</div>')
    p = res.get("ptnq")
    if p and p.get("series"):
        s = p["series"]
        body += ('<h2>PTNQ：模拟 vs 实盘</h2><div class="card">' + line_chart(
            [{"name": "本引擎模拟（扣 0.65% 费率）", "dates": s["dates"], "values": s["simulated"]},
             {"name": "PTNQ 实盘", "dates": s["dates"], "values": s["actual"]}], fmt="num", dp=3) + '</div>')
    g = res.get("grid") or {}
    if g:
        body += ('<h2>参数网格</h2><div class="card scroll"><table><tr><th>参数</th><th>夏普</th><th>年化</th><th>最大回撤</th><th>年换手</th></tr>'
                 + "".join(f'<tr><td>{E(r["label"])}</td><td>{num(r["sharpe"])}</td><td>{pct(r["cagr"])}</td><td>{pct(r["max_drawdown"])}</td>'
                           f'<td>{pct(r["turnover_per_year"], 0)}</td></tr>' for r in g["rows"])
                 + f'</table><p class="tiny">PBO {pct(g["pbo"], 0)}，DSR {pct(g["dsr"], 0)}。主策略始终使用预注册的默认参数，不使用网格中的最优值。</p></div>')
    seg = res.get("segments") or []
    if seg:
        body += '<h2>分段</h2><div class="card scroll"><table><tr><th>区间</th><th>主策略 夏普/回撤</th><th>无趋势</th><th>恒定仓位</th></tr>' + "".join(
            f'<tr><td>{E(r["period"])}</td>' + "".join(
                f'<td>{num(r[k]["sharpe"])} / {pct(r[k]["max_drawdown"])}</td>' if k in r else "<td>-</td>"
                for k in ("strategy", "no_trend", "const_avg")) + '</tr>' for r in seg) + '</table></div>'
    return body


def page_tracking(series: dict | None, signals_hist: list) -> str:
    body = '<h1>模型跟踪</h1>'
    if series:
        L = series["labels"]
        nav = [{"name": L[k], **series["nav"][k]} for k in ("strategy", "no_trend", "const_avg", "buy_hold_first") if k in series["nav"]]
        body += '<h2>净值（对数坐标）</h2><div class="card">' + line_chart(nav, fmt="num", dp=2, log=True) + '</div>'
        dd = [{"name": L[k], **series["drawdown"][k]} for k in ("strategy", "no_trend") if k in series["drawdown"]]
        body += '<h2>回撤</h2><div class="card">' + line_chart(dd, fmt="pct", dp=1, zero=True, ymax=0) + '</div>'
        if series.get("rolling_sharpe_3y"):
            rs = [{"name": "主策略 3 年滚动夏普", **series["rolling_sharpe_3y"]["strategy"]}]
            body += ('<h2>3 年滚动夏普</h2><div class="card">' + line_chart(rs, fmt="num", dp=2, zero=True)
                     + '<p class="tiny">停用条件之一：实盘满 36 个月后，滚动夏普低于回测自助法分布的 5% 分位。</p></div>')
        body += ('<h2>风险资产仓位</h2><div class="card">' + line_chart(
            [{"name": "主策略风险资产合计", **series["risky_weight"]}], fmt="pct", dp=0, zero=True, ymax=1) + '</div>')
        fc = series.get("forecast_5y", {})
        if fc:
            body += '<h2>近 5 年趋势强度 F</h2><p class="tiny">10 = 正常仓位，20 = 两倍上限，0 = 空仓。</p>'
            for i, (k, v) in enumerate(fc.items()):
                body += (f'<div class="card"><h3>{E(series["names"].get(k, k))}</h3>'
                         + line_chart([{"name": "趋势 F", "color": SERIES_COLORS[0], **v}], fmt="num", dp=1,
                                      height=140, zero=True, ymin=0, ymax=20) + '</div>')
    else:
        body += '<p class="muted">研究序列尚未生成。</p>'
    if signals_hist:
        body += ('<h2>信号记录</h2><div class="card scroll"><table><tr><th>日期</th><th>状态</th><th>调仓日</th><th>目标风险资产</th></tr>'
                 + "".join(f'<tr><td>{E(s["asof"])}</td><td>{status_badge(s["status"])}</td><td>{"是" if s.get("is_rebalance_day") else "-"}</td>'
                           f'<td>{pct(s.get("risky"))}</td></tr>' for s in signals_hist[:60])
                 + '</table><p class="tiny">个人的实际执行记录和决策日志只保存在本地，不进入公开仓库。</p></div>')
    return body


def page_health(health: dict | None) -> str:
    if not health:
        return '<h1>数据健康</h1><p class="muted">暂无健康报告。</p>'
    rows = []
    for c in health["checks"]:
        i = c.get("info", {})
        extra = []
        if i.get("tr_vs_vendor_max_annual_gap") is not None:
            extra.append(f"总收益对账最大年差 {i['tr_vs_vendor_max_annual_gap'] * 100:.3f}%")
        if i.get("median_abs_rel_diff") is not None:
            extra.append(f"FRED/Yahoo 中位差 {i['median_abs_rel_diff'] * 100:.2f}%（{i.get('overlap_days')} 天）")
        msgs = c.get("fail", []) + c.get("warn", [])
        rows.append(f'<tr><td>{E(c["key"])}<div class="tiny">{E(str(c.get("symbol")))}{"" if c.get("required") else " · 非必需"}</div></td>'
                    f'<td>{status_badge(c["status"])}</td><td>{E(str(i.get("first", "")))}～{E(str(i.get("last", "")))}</td>'
                    f'<td>{E(str(i.get("sessions_behind", "-")))}</td><td style="white-space:normal" class="small">{E("；".join(extra + msgs))}</td></tr>')
    notes = "".join(f"<li>{E(n)}</li>" for n in health.get("notes", []))
    return (f'<h1>数据健康</h1><p class="muted small">检查日 {E(health["asof"])} · 总体 {status_badge(health["status"])}</p>'
            '<div class="card scroll"><table><tr><th>序列</th><th>状态</th><th>区间</th><th>落后交易日</th><th>说明</th></tr>'
            + "".join(rows) + f'</table></div><div class="card small"><ul>{notes}'
            '<li>FRED 汇率每周发布一次，落后 5～10 个交易日属正常，最近几天由 Yahoo 补齐。</li>'
            '<li>任何必需序列失败时，当天信号为“已阻断”，不给出建议。</li></ul></div>')


def page_add(repo: str | None) -> str:
    url = f"https://github.com/{repo}/issues/new?template=add-etf.yml" if repo else "#"
    return ('<h1>添加 ETF</h1><div class="card"><p>静态网站不能直接写入配置，添加通过仓库的 Issue 表单完成：</p><ol>'
            f'<li>点击 <a href="{url}"><b>新建“添加 ETF”申请</b></a>，填写代码、币种、预提税率和用途（观察或纳入组合）。</li>'
            '<li>Actions 自动抓取数据并运行质量闸门，把结果回复在 Issue 里。</li>'
            '<li>校验通过时，Actions 会建好一个修改配置的分支，并在回复里附上创建合并请求的链接。由你决定是否合并。</li></ol></div>'
            '<div class="card small"><h3>观察 vs 纳入组合</h3><ul><li><b>观察</b>：只计算波动和趋势指标，在“今日决策”底部展示，不影响仓位。</li>'
            '<li><b>纳入组合</b>：需要填写风险预算，属于规则修改，必须登记在 PREREGISTRATION.md 的变更记录里，并重新运行研究工作流。</li></ul></div>')


def _signal_history(signals_dir: Path) -> list:
    out = []
    for f in sorted(signals_dir.glob("[0-9][0-9][0-9][0-9]/*.json"), reverse=True):
        if ".rerun-" in f.name:
            continue
        try:
            s = json.loads(f.read_text())
        except Exception:  # noqa: BLE001
            continue
        risky = sum(a["target"] for a in s.get("assets", []) if a.get("risk_budget_input", 0) > 0) if s.get("assets") else None
        out.append({"asof": s["asof"], "status": s["status"], "is_rebalance_day": s.get("is_rebalance_day"),
                    "risky": risky, "action": s.get("action") or s.get("message", "")})
    return out


def build_site(cfg: dict, signals_dir="signals", research_dir="research", out_dir="_site") -> Path:
    sd, rd, od = Path(signals_dir), Path(research_dir), Path(out_dir)
    od.mkdir(parents=True, exist_ok=True)
    rd_ = lambda p: json.loads(p.read_text()) if p.exists() else None  # noqa: E731
    sig, health = rd_(sd / "latest.json"), rd_(sd / "health" / "latest.json")
    res, series = rd_(rd / "latest.json"), rd_(rd / "series.json")
    repo = os.environ.get("GITHUB_REPOSITORY") or cfg.get("site", {}).get("repo")
    foot = f'源码与原始记录：<a href="https://github.com/{repo}">{repo}</a>。' if repo else ""
    pages = {
        "index.html": ("今日决策", page_today(sig, health, repo) if sig else "<h1>今日决策</h1><p>尚无信号。</p>"),
        "evidence.html": ("证据", page_evidence(res)),
        "tracking.html": ("模型跟踪", page_tracking(series, _signal_history(sd))),
        "health.html": ("数据健康", page_health(health)),
        "add.html": ("添加 ETF", page_add(repo)),
    }
    for fn, (title, body) in pages.items():
        (od / fn).write_text(page(title, fn, body, foot), encoding="utf-8")
    (od / ".nojekyll").write_text("")
    return od
