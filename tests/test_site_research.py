"""研究层与网站：结构、假设检验、无金额、页面完整，以及“添加 ETF”的输入校验。"""
import json
import re

import pytest

from etflab.config import config_hash, expand_watchlist, override
from etflab.pipeline import build_signal, write_outputs
from etflab.research import ptnq_exposure, run_research, write_research
from etflab.site import build_site, nice_ticks
from tools.add_etf import insert_into_config, parse_issue_form, validate_form


@pytest.fixture(scope="module")
def research(raw, cfg):
    c = override(cfg, {"validation": {"grid": {"rebalance.buffer": [0.05, 0.1], "trend.attenuation": [False, True]}},
                       "research": {"oos_markets": [], "candidates": []}})
    return run_research(c, raw, n_boot=200)


def test_research_structure(research):
    out, series = research
    assert [h["id"] for h in out["hypotheses"]] == ["H1", "H2", "H3"]
    assert out["grid"]["n"] == 4 and 0 <= out["grid"]["pbo"] <= 1
    assert {"strategy", "no_trend", "const_avg", "buy_hold_first"} <= set(out["metrics"])
    assert series["nav"]["strategy"]["values"][0] == pytest.approx(1.0, abs=0.02)
    assert any(c["name"].startswith("情绪") and c["role"] == "未接入" for c in out["evidence_cards"])


def test_site_builds_without_amounts(research, raw, cfg, tmp_path):
    out, series = research
    write_research(out, series, tmp_path / "research")
    sig, health = build_signal(cfg, raw, raw["asset:eq_us"].index[-1], bootstrap=0)
    write_outputs(sig, health, tmp_path / "signals")
    site = build_site(cfg, tmp_path / "signals", tmp_path / "research", tmp_path / "_site")
    pages = {p.name: p.read_text(encoding="utf-8") for p in site.glob("*.html")}
    assert set(pages) == {"index.html", "evidence.html", "tracking.html", "health.html", "add.html"}
    assert "每只资产" in pages["index.html"] and "预注册假设" in pages["evidence.html"]
    for txt in pages.values():
        assert "<script" in txt and "</html>" in txt
        assert not re.search(r"(SGD|USD|S\$|\$)\s?\d{1,3}(,\d{3})+", txt)   # 不出现金额


def test_blocked_signal_page(raw, cfg, tmp_path):
    broken = {k: v for k, v in raw.items() if k != "asset:gold"}
    sig, health = build_signal(cfg, broken, raw["asset:eq_us"].index[-1], bootstrap=0)
    write_outputs(sig, health, tmp_path / "signals")
    site = build_site(cfg, tmp_path / "signals", tmp_path / "none", tmp_path / "_site")
    assert "今天不给出建议" in (site / "index.html").read_text(encoding="utf-8")


def test_nice_ticks():
    assert nice_ticks(0, 1) == [0, 0.25, 0.5, 0.75, 1.0]
    import math
    t = [round(math.exp(v), 6) for v in nice_ticks(math.log(0.3), math.log(21), log=True)]
    assert set(t) <= {0.5, 1, 2, 5, 10, 20}


def test_ptnq_rule_states():
    import numpy as np
    import pandas as pd
    up = pd.Series(np.linspace(100, 200, 400), index=pd.bdate_range("2015-01-01", periods=400))
    e = ptnq_exposure(up).dropna()
    assert e.iloc[-1] == 1.0
    down = pd.concat([up, pd.Series(np.linspace(199, 120, 200), index=pd.bdate_range(up.index[-1] + pd.Timedelta(days=1), periods=200))])
    e2 = ptnq_exposure(down).dropna()
    assert e2.iloc[-1] == 0.0 and (e2 == 0.5).any()      # 先 50/50，均线下行后全仓国库券


def test_watchlist_does_not_change_rule_fingerprint(cfg):
    c2 = dict(cfg, watchlist=[{"id": "spy", "name": "SPY", "signal": {"source": "yahoo", "symbol": "SPY"},
                               "currency": "USD", "dividend_withholding": 0.15}])
    assert config_hash(expand_watchlist(c2)) == config_hash(cfg)
    assert config_hash(override(cfg, {"sizing.vol_target": 0.13})) != config_hash(cfg)


BODY = """### 信号代码（Yahoo 代码）

EWJ

### 名称

日本股票

### 计价货币

USD

### 股息预提税率

0.15

### 用途

观察（只算指标，不参与仓位）

### 风险预算（仅纳入组合时填写）

_No response_

### 实际下单的标的（可选）

SJPA.L

### 理由

分散
"""


def test_add_etf_parse_and_insert():
    f = parse_issue_form(BODY)
    e, errs = validate_form(f, {"QQQ"})
    assert not errs and e["symbol"] == "EWJ" and not e["portfolio"] and e["exec"] == ["SJPA.L"]
    cfg_text = open("config/portfolio.yaml", encoding="utf-8").read()
    new = insert_into_config(cfg_text, e)
    import yaml
    parsed = expand_watchlist(yaml.safe_load(new))
    w = [a for a in parsed["assets"] if a.get("watch")]
    assert w and w[0]["signal"]["symbol"] == "EWJ" and w[0]["weight"] == 0.0
    assert "# 观察列表" in new          # 注释保留


@pytest.mark.parametrize("bad", ["EWJ; rm -rf /", "$(whoami)", "a" * 30, "EW J"])
def test_add_etf_rejects_injection(bad):
    _, errs = validate_form({"symbol": bad, "name": "x", "currency": "USD", "withholding": "0.1", "purpose": "观察"}, set())
    assert any("代码格式" in m for m in errs)
