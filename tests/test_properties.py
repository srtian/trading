"""仓位、缓冲区、总收益、统计量、日历、质量闸门的性质测试。"""
import numpy as np
import pandas as pd
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from etflab.calendar import period_end_flags, sessions
from etflab.data.quality import check_series, run_gates
from etflab.data.totalreturn import total_return_index, tr_reconciliation
from etflab.engine.sizing import apply_buffer
from etflab.engine.stats import dsr, pbo_cscv, psr

# ---------- 缓冲区 ----------
weights = st.lists(st.floats(0, 0.5), min_size=3, max_size=3)


@given(t=weights, c=weights, a=st.lists(st.floats(0.01, 0.4), min_size=3, max_size=3), b=st.floats(0, 0.3))
def test_buffer_properties(t, c, a, b):
    ids = ["x", "y", "z"]
    t, c, a = pd.Series(t, ids), pd.Series(c, ids), pd.Series(a, ids)
    if t.sum() > 1:      # 与 Sizer 一致：目标合计不超过 100%
        t = t / t.sum()
    if c.sum() > 1:      # 当前持仓合计不超过 100%
        c = c / c.sum()
    new = apply_buffer(t, c, a, b)
    assert (new >= -1e-12).all() and new.sum() <= 1 + 1e-9
    inside = (c >= t - b * a) & (c <= t + b * a)
    np.testing.assert_allclose(new[inside], c[inside], atol=1e-12)   # 缓冲区内的资产不交易
    moved = ~inside
    # 越界资产：被交易到边缘；若因总仓位上限压缩了买入，只会更靠近当前持仓
    lo, hi = (t - b * a).clip(lower=0), t + b * a
    assert ((new[moved] >= np.minimum(c[moved], lo[moved]) - 1e-9) & (new[moved] <= hi[moved] + 1e-9)).all()


# ---------- 总收益 ----------
def test_total_return_withholding():
    idx = pd.bdate_range("2020-01-01", periods=3)
    raw = pd.DataFrame({"close": [100.0, 100.0, 101.0], "dividends": [0, 1.0, 0], "splits": 0.0,
                        "adj_close": np.nan}, idx)
    tr = total_return_index(raw, 0.30)
    assert tr.iloc[1] == pytest.approx(1 + 0.7 / 100)
    assert tr.iloc[2] == pytest.approx((1 + 0.007) * 1.01)
    assert total_return_index(raw, 0.0).iloc[1] == pytest.approx(1.01)


def test_tr_reconciliation_zero_on_consistent_vendor(raw):
    rec = tr_reconciliation(raw["asset:eq_us"])
    assert rec["gap"].abs().max() < 1e-9


# ---------- 统计量 ----------
def test_psr_behaviour():
    rng = np.random.default_rng(0)
    good = pd.Series(0.001 + 0.01 * rng.standard_normal(2500))
    noise = pd.Series(0.01 * rng.standard_normal(2500))
    assert psr(good) > 0.99
    assert 0.05 < psr(noise) < 0.95
    assert psr(good, 0.5) < psr(good, 0.0)
    sharpes = [0.01 * rng.standard_normal() for _ in range(50)]
    assert dsr(good, sharpes) <= psr(good) + 1e-12


def test_pbo_detects_dominance_and_noise():
    rng = np.random.default_rng(1)
    T, N = 2000, 10
    noise = pd.DataFrame(0.01 * rng.standard_normal((T, N)))
    pb_noise = pbo_cscv(noise, 10)["pbo"]
    dom = noise.copy()
    dom[0] += 0.002
    pb_dom = pbo_cscv(dom, 10)["pbo"]
    assert pb_dom < 0.05
    assert 0.2 < pb_noise < 0.8


# ---------- 日历 ----------
def test_month_end_uses_exchange_calendar():
    idx = sessions("XNYS", "2024-03-20", "2024-12-31")
    f = period_end_flags(idx, "XNYS", "M")
    assert f[pd.Timestamp("2024-03-28")]          # 2024-03-29 是耶稣受难日，休市
    assert f[pd.Timestamp("2024-11-29")]
    assert not f[pd.Timestamp("2024-11-27")]
    # 数据只到月末那天时，月末仍要被识别（实盘当天运行）
    short = idx[idx <= "2024-11-29"]
    assert period_end_flags(short, "XNYS", "M").iloc[-1]


# ---------- 质量闸门 ----------
def _frame(dates, close):
    return pd.DataFrame({"close": close, "dividends": 0.0, "splits": 0.0, "adj_close": np.nan}, index=dates)


def test_gate_stale_and_jump():
    d = sessions("XNYS", "2023-01-03", "2024-06-28")
    px = np.linspace(100, 120, len(d))
    asof = pd.Timestamp("2024-07-15")
    r = check_series("asset:x", _frame(d, px), "XYZ", asof, {"stale_sessions": 3})
    assert r["status"] == "fail" and any("过期" in m for m in r["fail"])
    px2 = px.copy()
    px2[-5] *= 1.6
    r2 = check_series("asset:x", _frame(d, px2), "XYZ", d[-1], {"stale_sessions": 3})
    assert any("跳变" in m for m in r2["fail"])
    r3 = check_series("asset:x", _frame(d, px), "XYZ", d[-1], {"stale_sessions": 3})
    assert r3["status"] in ("pass", "warn")


def test_gate_blocks_signal(raw, cfg):
    asof = raw["asset:eq_us"].index[-1]
    assert run_gates(cfg, raw, asof)["status"] != "blocked"
    broken = dict(raw)
    broken["asset:bond"] = raw["asset:bond"].iloc[:-20]
    g = run_gates(cfg, broken, asof)
    assert g["status"] == "blocked" and "asset:bond" in g["blocked_by"]
