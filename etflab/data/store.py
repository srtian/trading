"""原始数据存储与修订记录。

供应商会回溯修改历史数据（复权、更正）。每次抓取都与已存数据逐行比较：
- 新增行直接追加；
- 已有日期的数值变化记入 revisions.csv（旧值、新值、发现时间），再采用新值；
- manifest.json 记录每个文件的 sha256，信号文件引用 manifest 的哈希，证明当天用的是哪份数据。
存储目录可以是私有数据仓库的工作区，也可以是 Actions 缓存目录。
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from .sources import RAW_COLS


def _fname(key: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.=-]", "_", key) + ".csv"


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_store(root: str | Path) -> dict:
    root = Path(root)
    out = {}
    man = root / "manifest.json"
    if not man.exists():
        return out
    for key, meta in json.loads(man.read_text()).get("files", {}).items():
        p = root / meta["file"]
        if p.exists():
            df = pd.read_csv(p, parse_dates=["date"]).set_index("date")
            out[key] = df[RAW_COLS].astype(float)
    return out


def merge_and_save(root: str | Path, fetched: dict, rel_tol: float = 1e-7) -> dict:
    """合并新抓取的数据到存储，返回 {'manifest_sha256', 'revisions': n, 'new_rows': n}。"""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    old = load_store(root)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rev_rows, new_rows = [], 0
    files = {}
    for key, df in fetched.items():
        prev = old.get(key)
        if prev is not None and len(prev):
            common = prev.index.intersection(df.index)
            for col in ("close", "dividends", "splits"):
                a = prev.loc[common, col].to_numpy()
                b = df.loc[common, col].to_numpy()
                diff = ~np.isclose(a, b, rtol=rel_tol, atol=1e-12, equal_nan=True)
                for d, x, y in zip(common[diff], a[diff], b[diff]):
                    rev_rows.append({"found_at": now, "key": key, "date": d.date(), "field": col,
                                     "old": x, "new": y})
            missing_now = prev.index.difference(df.index)
            merged = pd.concat([prev.loc[missing_now], df]).sort_index()
            new_rows += len(df.index.difference(prev.index))
        else:
            merged = df
            new_rows += len(df)
        f = root / _fname(key)
        out = merged.copy()
        out.index.name = "date"
        out.to_csv(f, float_format="%.10g")
        files[key] = {"file": f.name, "sha256": sha256_file(f), "rows": int(len(out)),
                      "first": str(out.index[0].date()), "last": str(out.index[-1].date())}
    if rev_rows:
        rp = root / "revisions.csv"
        pd.DataFrame(rev_rows).to_csv(rp, mode="a", header=not rp.exists(), index=False)
    manifest = {"updated_at": now, "files": files}
    mp = root / "manifest.json"
    mp.write_text(json.dumps(manifest, indent=1, ensure_ascii=False))
    return {"manifest_sha256": sha256_file(mp), "revisions": len(rev_rows), "new_rows": new_rows,
            "files": files}
