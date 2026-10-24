# -*- coding: utf-8 -*-
"""ハーネス共通: 置き場所の解決と設定読込。パスはすべてハーネス自身の位置から決める（固定パス禁止）。

個人データ（記憶・索引・予算台帳）は既定で**ハーネスのフォルダの外**（~/.claude-harness/）に置く。
フォルダごとZIPやWebアップロードで共有すると .gitignore は効かないため、
リポジトリの中に置いた個人データはそのまま公開されてしまう。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HARNESS_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = HARNESS_ROOT / "harness.config.json"
LOCAL_CONFIG_PATH = HARNESS_ROOT / "harness.config.local.json"  # PC固有の上書き（git管理外）
# 2026-10 以前の既定（リポジトリ内）。install.py が新しい既定へ移す
LEGACY_MEMORY_DIR = HARNESS_ROOT / "data" / "memory"
LEGACY_STATE_DIR = HARNESS_ROOT / ".state"

DEFAULTS: dict = {
    "memory_dir": "~/.claude-harness/memory",
    "state_dir": "~/.claude-harness/state",
    "budget_bytes": 120000,
    "growth_warn_ratio": 0.10,
    "max_lines_per_file": 200,
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
            cfg.update(json.loads(p.read_text(encoding="utf-8-sig")))
    return cfg


def _expand(s: str) -> Path:
    p = Path(os.path.expandvars(os.path.expanduser(s)))
    return p if p.is_absolute() else (HARNESS_ROOT / p)


def resolve(path_str: str, cfg: dict | None = None) -> Path:
    """`~`・環境変数・{memory_dir} を展開し、相対ならハーネス基準で解決する。"""
    cfg = load_config() if cfg is None else cfg
    return _expand(path_str.replace("{memory_dir}", memory_dir(cfg).as_posix()))


def memory_dir(cfg: dict | None = None) -> Path:
    cfg = load_config() if cfg is None else cfg
    return _expand(os.environ.get("HARNESS_MEMORY_DIR") or str(cfg["memory_dir"]))


def state_dir_path(cfg: dict | None = None) -> Path:
    """索引・台帳の置き場（作らない）。"""
    cfg = load_config() if cfg is None else cfg
    return _expand(os.environ.get("HARNESS_STATE_DIR") or str(cfg["state_dir"]))


def ensure_private_dir(d: Path) -> Path:
    """個人データの置き場を作る。POSIX では本人だけが読めるようにする（Windows はプロファイルの ACL を継承）。"""
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    return d


def state_dir(cfg: dict | None = None) -> Path:
    return ensure_private_dir(state_dir_path(cfg))


def inside_harness(p: Path) -> bool:
    """個人データの置き場がリポジトリの中にあるか（共有時に一緒に出ていく）。"""
    try:
        p.resolve().relative_to(HARNESS_ROOT.resolve())
        return True
    except ValueError:
        return False
