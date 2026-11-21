"""无前视测试：把数据截断在任意日期 c，c 及以前的所有指标、目标仓位和回测持仓必须与使用完整数据时完全相同。

只要有任何计算偷看了 c 之后的数据，这个测试就会失败。
"""
import numpy as np
import pandas as pd
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from etflab.data.panel import build_panel
from etflab.engine import backtest as bt
from etflab.engine.sizing import Sizer


def _truncate(raw, c):
    return {k: (v[v.index <= c] if v is not None else None) for k, v in raw.items()}


@settings(max_examples=6, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(frac=st.floats(min_value=0.45, max_value=0.97))
def test_truncation_invariance(raw, cfg, frac):
    full_p = build_panel(cfg, raw)
    idx = full_p.ret_base.index
    c = idx[int(frac * (len(idx) - 1))]
    cut_p = build_panel(cfg, _truncate(raw, c), asof=c)

    vf, ff = bt.compute_indicators(full_p, cfg)
    vc, fc_ = bt.compute_indicators(cut_p, cfg)
    common = vc.index
    pd.testing.assert_frame_equal(vf.loc[common], vc, check_freq=False)
    pd.testing.assert_frame_equal(ff.loc[common], fc_, check_freq=False)
    pd.testing.assert_frame_equal(full_p.ret_base.loc[common], cut_p.ret_base, check_freq=False)

    sf = Sizer(full_p.ids, full_p.weights, vf, ff, full_p.tr_base, cfg["sizing"])
    sc = Sizer(cut_p.ids, cut_p.weights, vc, fc_, cut_p.tr_base, cfg["sizing"])
    for t in [common[-1], common[len(common) // 2], common[-22]]:
        xf, af, _ = sf.target(t)
        xc, ac, _ = sc.target(t)
        pd.testing.assert_series_equal(xf, xc)
        pd.testing.assert_series_equal(af, ac)

    rf = bt.run(full_p, cfg, (vf, ff))
    rc = bt.run(cut_p, cfg, (vc, fc_))
    pd.testing.assert_frame_equal(rf.weights.loc[rc.weights.index], rc.weights, check_freq=False)
    np.testing.assert_allclose(rf.nav.loc[rc.nav.index].to_numpy() / rf.nav.loc[rc.nav.index[0]],
                               rc.nav.to_numpy(), rtol=1e-12)
