# -*- coding: utf-8 -*-
"""ハーネス共通: 置き場所の解決と設定読込。パスはすべてハーネス自身の位置から決める（固定パス禁止）。"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HARNESS_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = HARNESS_ROOT / "harness.config.json"
LOCAL_CONFIG_PATH = HARNESS_ROOT / "harness.config.local.json"  # PC固有の上書き（git管理外）

DEFAULTS: dict = {
    "memory_dir": "data/memory",
    "state_dir": ".state",
    "budget_bytes": 120000,
    "growth_warn_ratio": 0.10,
    "always_loaded": [
        "~/.claude/CLAUDE.md",
        "{memory_dir}/MEMORY.md",
    ],
}

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except (ValueError, OSError):
        pass


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    for p in (CONFIG_PATH, LOCAL_CONFIG_PATH):
        if p.exists():
            cfg.update(json.loads(p.read_text(encoding="utf-8")))
    return cfg


def resolve(path_str: str, cfg: dict | None = None) -> Path:
    """`~`・環境変数・{memory_dir} を展開し、相対ならハーネス基準で解決する。"""
    cfg = cfg or load_config()
    s = path_str.replace("{memory_dir}", str(cfg["memory_dir"]))
    p = Path(os.path.expandvars(os.path.expanduser(s)))
    return p if p.is_absolute() else (HARNESS_ROOT / p)


def memory_dir(cfg: dict | None = None) -> Path:
    cfg = cfg or load_config()
    env = os.environ.get("HARNESS_MEMORY_DIR")
    return Path(env) if env else resolve(cfg["memory_dir"], cfg)


def state_dir(cfg: dict | None = None) -> Path:
    cfg = cfg or load_config()
    d = resolve(cfg["state_dir"], cfg)
    d.mkdir(parents=True, exist_ok=True)
    return d
