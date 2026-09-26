# -*- coding: utf-8 -*-
"""token_budget.py — 毎ターン読み込まれる指示ファイルの総量を測り、予算超過・急増を警告する。

  python bin/token_budget.py            測定して台帳（.state/context-budget-log.jsonl）へ追記
  python bin/token_budget.py --quiet    警告があるときだけ出力（SessionStart フック用）

対象は harness.config.json の always_loaded と、カレントの CLAUDE.md / AGENTS.md。
トークン数は概算（UTF-8 バイト / 3）。精密値ではなく「増えたか」を見る道具。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import load_config, resolve, state_dir  # noqa: E402


def targets(cfg: dict, cwd: Path) -> list[Path]:
    seen, out = set(), []
    cands = [resolve(s, cfg) for s in cfg["always_loaded"]]
    cands += [cwd / "CLAUDE.md", cwd / ".claude" / "CLAUDE.md", cwd / "AGENTS.md"]
    for p in cands:
        key = str(p.resolve()) if p.exists() else str(p)
        if key not in seen:
            seen.add(key); out.append(p)
    return out


def measure(cfg: dict, cwd: Path) -> dict:
    files = [{"path": str(p), "bytes": p.stat().st_size} for p in targets(cfg, cwd) if p.is_file()]
    total = sum(f["bytes"] for f in files)
    return {"ts": datetime.now().isoformat(timespec="seconds"), "cwd": str(cwd),
            "total_bytes": total, "approx_tokens": total // 3, "files": files}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--cwd", default=".")
    a = ap.parse_args(argv)
    cfg = load_config()
    cwd = Path(a.cwd).resolve()
    rec = measure(cfg, cwd)
    log = state_dir(cfg) / "context-budget-log.jsonl"
    prev = None
    if log.exists():
        for line in reversed(log.read_text(encoding="utf-8").splitlines()):
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("cwd") == rec["cwd"]:
                prev = r; break
    with log.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    warns = []
    budget = int(cfg["budget_bytes"])
    if rec["total_bytes"] > budget:
        warns.append(f"常時読込 {rec['total_bytes']:,}B が予算 {budget:,}B を超過")
    if prev and prev["total_bytes"] and \
            (rec["total_bytes"] - prev["total_bytes"]) / prev["total_bytes"] > float(cfg["growth_warn_ratio"]):
        warns.append(f"前回 {prev['total_bytes']:,}B → 今回 {rec['total_bytes']:,}B に急増")
    if warns:
        top = sorted(rec["files"], key=lambda f: -f["bytes"])[:3]
        warns.append("大きい順: " + ", ".join(f"{Path(f['path']).name} {f['bytes']:,}B" for f in top))
        warns.append("直近で足した行を「消したら間違えるか」で見直すこと。")

    if not a.quiet:
        print(f"常時読込: {rec['total_bytes']:,}B（≒{rec['approx_tokens']:,} tokens）/ 予算 {budget:,}B")
        for f in rec["files"]:
            print(f"  {f['bytes']:>9,}B  {f['path']}")
    for w in warns:
        print(f"⚠ token-budget: {w}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
