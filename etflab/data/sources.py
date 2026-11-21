"""原始数据源。只取"原始"字段：未经分红调整的收盘价、分红、拆股；复权价只用于对账。

返回统一的 DataFrame：index=日期（无时区），列 close, dividends, splits, adj_close（可缺）。
"""
from __future__ import annotations

import pandas as pd

RAW_COLS = ["close", "dividends", "splits", "adj_close"]


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.index = pd.to_datetime(df.index).tz_localize(None).normalize()
    df = df[~df.index.duplicated(keep="last")].sort_index()
    for c in RAW_COLS:
        if c not in df:
            df[c] = 0.0 if c in ("dividends", "splits") else float("nan")
    df["dividends"] = df["dividends"].fillna(0.0)
    df["splits"] = df["splits"].fillna(0.0)
    return df[RAW_COLS].astype(float)


def fetch_yahoo(symbol: str, start: str = "1990-01-01") -> pd.DataFrame:
    """yfinance：auto_adjust=False 时 Close 已按拆股调整、未按分红调整；Dividends 为每股现金分红。"""
    import time

    import yfinance as yf
    h, err = None, None
    for attempt in range(3):          # Yahoo 对云端 IP 偶尔限流，重试 3 次
        try:
            h = yf.Ticker(symbol).history(start=start, auto_adjust=False, actions=True)
            if not h.empty:
                break
        except Exception as e:  # noqa: BLE001
            err = e
        time.sleep(5 * (attempt + 1))
    if h is None or h.empty:
        raise RuntimeError(f"yahoo 无数据：{symbol}（{err}）")
    df = pd.DataFrame({
        "close": h["Close"],
        "dividends": h.get("Dividends", 0.0),
        "splits": h.get("Stock Splits", 0.0),
        "adj_close": h.get("Adj Close"),
    })
    return _normalize(df)


def load_parquet_dataset(path: str, symbol: str) -> pd.DataFrame:
    """离线数据集（列 date,ticker,close,dividends,stock_splits,adj_close），用于无网络环境测试。"""
    import pyarrow.parquet as pq
    t = pq.read_table(path, filters=[("ticker", "==", symbol)]).to_pandas()
    if t.empty:
        raise RuntimeError(f"离线数据集中没有 {symbol}")
    t = t.set_index("date").rename(columns={"stock_splits": "splits"})
    return _normalize(t)


def load_csv(path: str) -> pd.DataFrame:
    df = pd.read_csv(path, parse_dates=["date"]).set_index("date")
    return _normalize(df)


def fetch_fred(series: str, start: str = "1990-01-01") -> pd.DataFrame:
    """FRED 公开 CSV（无需 API key）。汇率来自美联储 H.10，利率 DTB3 为 3 个月国库券二级市场利率（年化 %）。"""
    import io
    import urllib.request
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}&cosd={start}"
    with urllib.request.urlopen(url, timeout=60) as r:
        txt = r.read().decode()
    df = pd.read_csv(io.StringIO(txt))
    df.columns = ["date", "close"]
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df = df.dropna().set_index("date")
    return _normalize(df)


def fetch(spec: dict, start: str, offline: dict | None = None) -> pd.DataFrame:
    """spec: {source: yahoo|fred|csv, symbol|series|path}。
    offline={'parquet': path, 'csv_dir': dir} 时不联网：yahoo 读离线数据集，fred 读 csv_dir/<series>.csv。"""
    src = spec["source"]
    if src == "csv":
        return load_csv(spec["path"])
    if src == "yahoo":
        if offline and offline.get("parquet"):
            return load_parquet_dataset(offline["parquet"], spec["symbol"])
        return fetch_yahoo(spec["symbol"], start)
    if src == "fred":
        if offline and offline.get("csv_dir"):
            return load_csv(f"{offline['csv_dir']}/{spec['series']}.csv")
        return fetch_fred(spec["series"], start)
    raise ValueError(f"未知数据源 {src}")


def spec_label(spec: dict) -> str:
    return spec.get("symbol") or spec.get("series") or spec.get("path", "?")
