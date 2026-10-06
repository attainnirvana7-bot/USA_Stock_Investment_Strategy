"""設定載入：config.yaml + 環境變數。"""
from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config.yaml"

_DEFAULTS: dict[str, Any] = {
    "fmp": {
        "base_url": "https://financialmodelingprep.com/stable",
        "requests_per_minute": 250,
        "timeout": 30,
        "max_retries": 3,
    },
    "storage": {"db_path": "data/usequity.db"},
    "crawl": {
        "universe": "list",
        "symbols": [],
        "period": "quarter",
        "statement_limit": 40,
        "price_start": "2014-01-01",
        "benchmark": "SPY",
        "ttl_hours": {"statements": 168, "prices": 20, "profile": 720},
    },
    "screen": {"filters": [], "rank": {}, "top_n": 10},
    "backtest": {
        "start": "2016-01-01",
        "end": None,
        "rebalance": "quarterly",
        "weighting": "equal",
        "cost_bps": 10,
        "filing_lag_days": 45,
    },
}


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    path = Path(path) if path else DEFAULT_CONFIG
    raw: dict[str, Any] = {}
    if path.exists():
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    cfg = _merge(_DEFAULTS, raw)
    db = Path(cfg["storage"]["db_path"])
    if not db.is_absolute():
        cfg["storage"]["db_path"] = str(ROOT / db)
    return cfg


def api_key() -> str | None:
    # 空字串視同未設定（GitHub Actions 未設定的 vars 會傳入空字串）
    return os.environ.get("FMP_API_KEY") or None
