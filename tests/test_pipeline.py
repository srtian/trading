"""端到端：合成数据 → 信号文件。检查结构、归因一致性、公开文件不含金额、信号文件不可覆盖。"""
import json

import numpy as np
import pytest

from etflab.data.store import load_store, merge_and_save
from etflab.pipeline import build_signal, write_outputs


@pytest.fixture(scope="module")
def signal(raw, cfg):
    asof = raw["asset:eq_us"].index[-1]
    return build_signal(cfg, raw, asof, {"manifest_sha256": "x"}, bootstrap=200)


def test_signal_structure(signal):
    s, health = signal
    assert s["status"] in ("pass", "warn")
    assert {"assets", "provenance", "model_portfolio_backtest", "next_rebalance_signal_date"} <= set(s)
    assert s["provenance"]["config_sha256"]
    tot = sum(a["target"] for a in s["assets"])
    assert tot <= 1 + 1e-9 and abs(tot + s["target_cash"] - 1) < 1e-5
    for a in s["assets"]:
        assert a["attribution"]["final"] == pytest.approx(a["target"], abs=1e-6)
        assert a["buffer_low"] <= a["target"] + 1e-9 <= a["buffer_high"] + 2e-9


def _keys(o):
    if isinstance(o, dict):
        for k, v in o.items():
            yield k
            yield from _keys(v)
    elif isinstance(o, list):
        for v in o:
            yield from _keys(v)


def test_no_amounts_in_public_output(signal):
    forbidden = {"value", "amount", "market_value", "balance", "account", "holdings", "nav_value", "金额"}
    assert not forbidden & set(_keys(signal[0]))


def test_signal_file_is_immutable(signal, tmp_path):
    s, h = signal
    p1 = write_outputs(s, h, tmp_path)
    p2 = write_outputs(dict(s, note="rerun"), h, tmp_path)
    assert p1 != p2 and p2.name.endswith(".rerun-1.json")
    assert "rerun" not in p1.read_text()


def test_store_records_revisions(raw, tmp_path):
    first = {"asset:eq_us": raw["asset:eq_us"].iloc[:-10]}
    merge_and_save(tmp_path, first)
    second = raw["asset:eq_us"].copy()
    second.iloc[100, second.columns.get_loc("close")] *= 1.01   # 供应商回溯修改一条历史数据
    info = merge_and_save(tmp_path, {"asset:eq_us": second})
    assert info["revisions"] == 1 and info["new_rows"] == 10
    assert (tmp_path / "revisions.csv").exists()
    back = load_store(tmp_path)["asset:eq_us"]
    assert len(back) == len(second)


def test_missing_series_blocks_instead_of_crashing(raw, cfg):
    broken = {k: v for k, v in raw.items() if k != "asset:gold"}
    asof = raw["asset:eq_us"].index[-1]
    s, h = build_signal(cfg, broken, asof, bootstrap=0)
    assert s["status"] == "blocked" and "asset:gold" in h["blocked_by"]
    assert "assets" not in s     # blocked 时不输出任何仓位建议
