"""处理“添加 ETF”Issue：解析表单 → 抓数据 → 质量闸门 → 计算指标 → 修改配置（文本插入，保留注释）。

Issue 内容是不可信输入：只从环境变量 ISSUE_BODY 读取，代码等字段用白名单正则校验，绝不拼进 shell。
输出：写入 config/portfolio.yaml（校验通过时），并把回复内容写到 add_etf_comment.md，结果写到 add_etf_result.json。
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

SYMBOL_RE = re.compile(r"^[A-Za-z0-9^][A-Za-z0-9.\-=^]{0,19}$")
LABELS = {
    "信号代码（Yahoo 代码）": "symbol", "名称": "name", "计价货币": "currency", "股息预提税率": "withholding",
    "用途": "purpose", "风险预算（仅纳入组合时填写）": "weight", "实际下单的标的（可选）": "exec", "理由": "reason",
}


def parse_issue_form(body: str) -> dict:
    out, cur = {}, None
    for line in body.splitlines():
        m = re.match(r"^###\s+(.+?)\s*$", line)
        if m:
            cur = LABELS.get(m.group(1).strip())
            if cur:
                out[cur] = ""
            continue
        if cur:
            out[cur] = (out[cur] + "\n" + line).strip()
    return {k: ("" if v == "_No response_" else v) for k, v in out.items()}


def validate_form(f: dict, existing_symbols: set) -> tuple[dict, list]:
    errs = []
    sym = f.get("symbol", "").strip()
    if not SYMBOL_RE.match(sym):
        errs.append("代码格式不合法（只允许字母、数字和 . - = ^，最长 20 个字符）")
    if sym.upper() in {s.upper() for s in existing_symbols}:
        errs.append(f"{sym} 已经在配置里")
    name = re.sub(r"[^\w一-鿿\s()（）/+\-&]", "", f.get("name", "")).strip()[:30]
    if not name:
        errs.append("名称为空")
    ccy = f.get("currency", "")
    if ccy not in ("USD", "SGD", "HKD", "CNY"):
        errs.append("币种只能是 USD、SGD、HKD、CNY")
    try:
        wht = float(f.get("withholding", ""))
        if not 0 <= wht < 1:
            raise ValueError
    except ValueError:
        errs.append("预提税率需为 0～1 之间的小数")
        wht = None
    portfolio = f.get("purpose", "").startswith("纳入组合")
    weight = None
    if portfolio:
        try:
            weight = float(f.get("weight", ""))
            if not 0 < weight <= 1:
                raise ValueError
        except ValueError:
            errs.append("纳入组合时，风险预算需为 0～1 之间的小数")
    execs = [e.strip() for e in f.get("exec", "").split(",") if e.strip()]
    if any(not SYMBOL_RE.match(e) for e in execs):
        errs.append("实际下单标的的代码格式不合法")
    aid = re.sub(r"[^a-z0-9]", "_", sym.lower()).strip("_")[:24] or "etf"
    return {"id": aid, "symbol": sym, "name": name, "currency": ccy, "withholding": wht,
            "portfolio": portfolio, "weight": weight, "exec": execs}, errs


def yaml_block(e: dict, indent: int = 2) -> str:
    sp = " " * indent
    lines = [f"{sp}- id: {e['id']}", f"{sp}  name: {json.dumps(e['name'], ensure_ascii=False)}",
             f"{sp}  signal: {{source: yahoo, symbol: {json.dumps(e['symbol'])}}}"]
    if e["exec"]:
        lines.append(f"{sp}  exec: {json.dumps(e['exec'])}")
    lines += [f"{sp}  currency: {e['currency']}", f"{sp}  dividend_withholding: {e['withholding']}"]
    if e["portfolio"]:
        lines.append(f"{sp}  weight: {e['weight']}")
    return "\n".join(lines) + "\n"


def insert_into_config(text: str, e: dict) -> str:
    block = yaml_block(e)
    if e["portfolio"]:
        i = text.index("\ncash:")
        return text[:i].rstrip("\n") + "\n" + block + text[i:]
    if "watchlist: []" in text:
        return text.replace("watchlist: []", "watchlist:\n" + block.rstrip("\n"), 1)
    i = text.index("watchlist:\n") + len("watchlist:\n")
    return text[:i] + block + text[i:]


def main() -> int:
    sys.path.insert(0, ".")
    import yaml

    from etflab.config import expand_watchlist
    from etflab.data.quality import check_series
    from etflab.data.sources import fetch
    from etflab.data.totalreturn import total_return_index
    from etflab.engine.indicators import ewmac_forecast, mixed_vol

    cfg_path = Path("config/portfolio.yaml")
    text = cfg_path.read_text(encoding="utf-8")
    cfg = expand_watchlist(yaml.safe_load(text))
    existing = {a["signal"].get("symbol", "") for a in cfg["assets"]}
    form = parse_issue_form(os.environ.get("ISSUE_BODY", ""))
    e, errs = validate_form(form, existing)
    report = [f"### 校验 `{e['symbol']}`"]
    ok = not errs
    if ok:
        try:
            raw = fetch({"source": "yahoo", "symbol": e["symbol"]}, cfg.get("start", "1999-01-01"))
            import pandas as pd
            g = check_series("asset:new", raw, e["symbol"], pd.Timestamp.now().normalize(), cfg.get("quality", {}))
            report.append(f"- 数据：{g['info'].get('first')} ～ {g['info'].get('last')}，{g['info'].get('rows')} 行；质量闸门 **{g['status']}**")
            for m in g["fail"] + g["warn"]:
                report.append(f"  - {m}")
            ok = g["status"] != "fail"
            tr = total_return_index(raw, e["withholding"] or 0.0)
            v = mixed_vol(tr.pct_change()) * 16
            fc = ewmac_forecast(tr, [[16, 64], [32, 128], [64, 256]], [3.75, 2.65, 1.87], 1.05).clip(0, 20)
            report.append(f"- 当前年化波动 {v.iloc[-1]:.1%}，趋势强度 F = {fc.iloc[-1]:.1f}")
        except Exception as ex:  # noqa: BLE001
            ok = False
            report.append(f"- 抓取数据失败：{ex}")
    else:
        report += [f"- ✕ {m}" for m in errs]
    if ok:
        cfg_path.write_text(insert_into_config(text, e), encoding="utf-8")
        report.append(f"- 已加入{'组合（风险预算 ' + str(e['weight']) + '）' if e['portfolio'] else '观察列表'}。")
        if e["portfolio"]:
            report.append("- ⚠ 这是规则修改：合并前请在 PREREGISTRATION.md 的变更记录里登记，并在合并后手动运行 Research 工作流。")
    Path("add_etf_comment.md").write_text("\n".join(report), encoding="utf-8")
    Path("add_etf_result.json").write_text(json.dumps({"ok": ok, "id": e["id"], "symbol": e["symbol"]}))
    print("\n".join(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
