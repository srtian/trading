"""交易日历。

月末判断不能用"数据的最后一行"：当天跑的时候，最后一行就是今天，会被误判为"月份未结束"而漏掉月末信号。
这里用交易所日历判断：某日是月末，当且仅当它之后的下一个交易日落在新的月份。
"""
from __future__ import annotations

from functools import lru_cache

import pandas as pd


@lru_cache(maxsize=16)
def _cal(code: str):
    import exchange_calendars as xcals
    # 默认只加载最近 20 年，回测需要更早的交易日
    return xcals.get_calendar(code, start="1990-01-02")


def sessions(code: str, start, end) -> pd.DatetimeIndex:
    cal = _cal(code)
    start = max(pd.Timestamp(start), cal.first_session)
    end = min(pd.Timestamp(end), cal.last_session)
    s = cal.sessions_in_range(start, end)
    return pd.DatetimeIndex(s).tz_localize(None)


def next_session(code: str, date) -> pd.Timestamp:
    cal = _cal(code)
    d = pd.Timestamp(date)
    if cal.is_session(d):
        return pd.Timestamp(cal.next_session(d)).tz_localize(None)
    return pd.Timestamp(cal.date_to_session(d, direction="next")).tz_localize(None)


def last_session_on_or_before(code: str, date) -> pd.Timestamp:
    cal = _cal(code)
    return pd.Timestamp(cal.date_to_session(pd.Timestamp(date).normalize(), direction="previous")).tz_localize(None)


def period_end_flags(index: pd.DatetimeIndex, code: str, freq: str = "M") -> pd.Series:
    """index 中每个日期是否为该交易所日历上的周期末交易日（M/W/Q）。"""
    if len(index) == 0:
        return pd.Series(dtype=bool)
    cal_sessions = sessions(code, index[0], index[-1] + pd.Timedelta(days=40))
    nxt = pd.Series(cal_sessions[1:].append(pd.DatetimeIndex([pd.NaT])), index=cal_sessions)
    nxt = nxt.reindex(index)
    pfreq = {"M": "M", "W": "W-FRI", "Q": "Q"}[freq]
    cur = index.to_period(pfreq)
    nx = pd.DatetimeIndex(nxt.values)
    flag = []
    for c, n in zip(cur, nx):
        flag.append(bool(pd.notna(n) and n.to_period(pfreq) != c))
    return pd.Series(flag, index=index)
