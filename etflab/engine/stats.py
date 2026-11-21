"""绩效与统计检验：PSR、DSR（Bailey & López de Prado 2014）、CSCV-PBO（Bailey et al. 2017）、平稳块自助法置信区间。"""
from __future__ import annotations

import itertools
import math

import numpy as np
import pandas as pd
from scipy import stats

ANN = 252  # 绩效年化按实际交易日


def psr(excess: pd.Series, sr_bench_annual: float = 0.0) -> float:
    r = pd.Series(excess).dropna()
    if len(r) < 30 or r.std() == 0:
        return float("nan")
    sr = r.mean() / r.std()
    sk = stats.skew(r)
    ku = stats.kurtosis(r, fisher=False)
    sb = sr_bench_annual / math.sqrt(ANN)
    den = 1 - sk * sr + (ku - 1) / 4 * sr ** 2
    if den <= 0:
        return float("nan")
    return float(stats.norm.cdf((sr - sb) * math.sqrt(len(r) - 1) / math.sqrt(den)))


def expected_max_sharpe(n_trials: int, sr_std: float) -> float:
    g = 0.5772156649015329
    if n_trials < 2:
        return 0.0
    return sr_std * ((1 - g) * stats.norm.ppf(1 - 1 / n_trials) + g * stats.norm.ppf(1 - 1 / (n_trials * math.e)))


def dsr(best_excess: pd.Series, trial_sharpes_daily) -> float:
    s = np.asarray(trial_sharpes_daily, dtype=float)
    if len(s) < 2:
        return psr(best_excess)
    sr0 = expected_max_sharpe(len(s), float(np.std(s, ddof=1)))
    return psr(best_excess, sr0 * math.sqrt(ANN))


def pbo_cscv(R: pd.DataFrame, S: int = 16) -> dict:
    X = R.dropna().to_numpy()
    T, N = X.shape
    S -= S % 2
    blocks = np.array_split(np.arange(T), S)

    def sharpe(m):
        sd = m.std(axis=0)
        return np.where(sd > 0, m.mean(axis=0) / np.where(sd > 0, sd, 1), -np.inf)

    logits = []
    for comb in itertools.combinations(range(S), S // 2):
        is_idx = np.concatenate([blocks[j] for j in comb])
        oos_idx = np.concatenate([blocks[j] for j in range(S) if j not in comb])
        best = int(np.argmax(sharpe(X[is_idx])))
        rank = stats.rankdata(sharpe(X[oos_idx]))[best]
        w = rank / (N + 1)
        logits.append(math.log(w / (1 - w)))
    lg = np.array(logits)
    return {"pbo": float((lg <= 0).mean()), "logit_median": float(np.median(lg)), "n_comb": len(lg)}


def stationary_bootstrap_sharpe_ci(excess: pd.Series, n_boot: int = 2000, mean_block: int = 20,
                                   alpha: float = 0.05, seed: int = 7) -> tuple[float, float]:
    """Politis–Romano 平稳块自助法：保留收益的自相关与波动聚集，给年化夏普的置信区间。"""
    x = pd.Series(excess).dropna().to_numpy()
    n = len(x)
    if n < 100:
        return float("nan"), float("nan")
    rng = np.random.default_rng(seed)
    p = 1.0 / mean_block
    out = np.empty(n_boot)
    ar = np.arange(n)
    for b in range(n_boot):
        jumps = rng.random(n) < p
        jumps[0] = True
        starts = rng.integers(n, size=n)
        last = np.maximum.accumulate(np.where(jumps, ar, 0))   # 每个位置所在块的起点
        idx = (starts[last] + (ar - last)) % n
        s = x[idx]
        sd = s.std()
        out[b] = s.mean() / sd * math.sqrt(ANN) if sd > 0 else np.nan
    lo, hi = np.nanquantile(out, [alpha / 2, 1 - alpha / 2])
    return float(lo), float(hi)


def _sb_indices(n: int, rng, mean_block: int) -> np.ndarray:
    ar = np.arange(n)
    jumps = rng.random(n) < 1.0 / mean_block
    jumps[0] = True
    starts = rng.integers(n, size=n)
    last = np.maximum.accumulate(np.where(jumps, ar, 0))
    return (starts[last] + (ar - last)) % n


def paired_sharpe_diff_ci(ex_a: pd.Series, ex_b: pd.Series, n_boot: int = 2000, mean_block: int = 20,
                          alpha: float = 0.05, seed: int = 11) -> dict:
    """配对平稳块自助法：两条超额收益用同一组重抽样下标，得到年化夏普差（a − b）的置信区间。"""
    df = pd.concat([ex_a, ex_b], axis=1).dropna()
    x = df.to_numpy()
    n = len(x)
    rng = np.random.default_rng(seed)

    def sh(m):
        sd = m.std(axis=0)
        return m.mean(axis=0) / np.where(sd > 0, sd, np.nan) * math.sqrt(ANN)

    point = sh(x)
    diffs = np.empty(n_boot)
    for b in range(n_boot):
        s = sh(x[_sb_indices(n, rng, mean_block)])
        diffs[b] = s[0] - s[1]
    lo, hi = np.nanquantile(diffs, [alpha / 2, 1 - alpha / 2])
    return {"diff": float(point[0] - point[1]), "ci95": [float(lo), float(hi)],
            "p_le_0": float(np.mean(diffs <= 0)), "n": int(n)}


def metrics(nav: pd.Series, weights: pd.DataFrame | None, cash_ret: pd.Series, turnover_py=float("nan")) -> dict:
    r = nav.pct_change().dropna()
    ex = r - cash_ret.reindex(r.index).fillna(0.0)
    yrs = (nav.index[-1] - nav.index[0]).days / 365.25
    cagr = nav.iloc[-1] ** (1 / yrs) - 1 if yrs > 0 else float("nan")
    vol = r.std() * math.sqrt(ANN)
    sh = ex.mean() / ex.std() * math.sqrt(ANN) if ex.std() > 0 else float("nan")
    dd = float((nav / nav.cummax() - 1).min())
    downside = ex[ex < 0].std() * math.sqrt(ANN)
    out = {"start": str(nav.index[0].date()), "end": str(nav.index[-1].date()),
           "cagr": float(cagr), "vol": float(vol), "sharpe": float(sh),
           "sortino": float(ex.mean() * ANN / downside) if downside > 0 else float("nan"),
           "max_drawdown": dd, "calmar": float(cagr / abs(dd)) if dd < 0 else float("nan"),
           "turnover_per_year": float(turnover_py), "psr_gt0": psr(ex)}
    if weights is not None:
        out["avg_risky_weight"] = float(weights.drop(columns=["cash"], errors="ignore").sum(axis=1).mean())
    return out
