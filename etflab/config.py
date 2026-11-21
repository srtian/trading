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
    validate_config(cfg)
    return cfg


def config_hash(cfg: dict) -> str:
    blob = json.dumps(cfg, sort_keys=True, ensure_ascii=False, default=str).encode()
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
