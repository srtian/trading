"""配置加载与指纹。配置的 sha256 写进每一份信号，用于证明当天用的是哪一版规则。"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import yaml

SUFFIX_EXCHANGE = {
    ".SS": "XSHG", ".SZ": "XSHE", ".HK": "XHKG", ".L": "XLON",
    ".SI": "XSES", ".T": "XTKS", ".DE": "XETR",
}


def exchange_for(symbol: str) -> str:
    for suf, ex in SUFFIX_EXCHANGE.items():
        if symbol.upper().endswith(suf):
            return ex
    return "XNYS"


def load_config(path: str | Path) -> dict:
    with open(path, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    cfg = expand_watchlist(cfg)
    validate_config(cfg)
    return cfg


def expand_watchlist(cfg: dict) -> dict:
    """观察列表并入 assets：风险预算为 0，只计算指标、不参与仓位，数据问题不阻断信号。"""
    cfg = copy.deepcopy(cfg)
    for w in cfg.pop("watchlist", None) or []:
        cfg["assets"].append({**w, "weight": 0.0, "watch": True})
    return cfg


NON_RULE_KEYS = {"site", "research", "validation", "watchlist", "_说明"}


def config_hash(cfg: dict) -> str:
    """只对"规则"部分取指纹：网站、研究、网格设置和观察列表的变化不改变指纹。"""
    rules = {k: v for k, v in cfg.items() if k not in NON_RULE_KEYS}
    rules["assets"] = [a for a in cfg.get("assets", []) if not a.get("watch")]
    blob = json.dumps(rules, sort_keys=True, ensure_ascii=False, default=str).encode()
    return hashlib.sha256(blob).hexdigest()


def override(cfg: dict, dotted: dict) -> dict:
    """{'sizing.vol_target': 0.1} 形式的覆盖，返回新配置。"""
    c = copy.deepcopy(cfg)
    for k, v in dotted.items():
        node = c
        parts = k.split(".")
        for p in parts[:-1]:
            node = node.setdefault(p, {})
        node[parts[-1]] = v
    return c


def validate_config(cfg: dict) -> None:
    ids = [a["id"] for a in cfg["assets"]]
    if len(ids) != len(set(ids)):
        raise ValueError("资产 id 重复")
    for a in cfg["assets"]:
        if not 0 <= a.get("dividend_withholding", 0) < 1:
            raise ValueError(f"{a['id']}: dividend_withholding 需在 [0,1)")
        if a.get("weight", 0) < 0:
            raise ValueError(f"{a['id']}: weight 不能为负")
        ccy = a.get("currency", "USD")
        if ccy not in ("USD",) and ccy not in cfg.get("fx", {}):
            raise ValueError(f"{a['id']}: 缺少 {ccy} 的汇率配置")
    base = cfg.get("base_currency", "USD")
    if base != "USD" and base not in cfg.get("fx", {}):
        raise ValueError(f"缺少基准货币 {base} 的汇率配置")
