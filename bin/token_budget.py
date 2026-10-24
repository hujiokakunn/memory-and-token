# -*- coding: utf-8 -*-
"""token_budget.py — 毎ターン読み込まれる指示ファイルの総量を測り、予算超過・急増・長すぎるファイルを警告する。

  py bin/token_budget.py            測定して台帳（<state_dir>/context-budget-log.jsonl）へ追記
  py bin/token_budget.py --quiet    警告があるときだけ出力（SessionStart フック用）

数える対象（Claude Code 公式 memory 文書の読込規則に合わせた静的推定。2026-10 時点）:
  - harness.config.json の always_loaded（既定 ~/.claude/CLAUDE.md と記憶の索引）
  - 管理ポリシーの CLAUDE.md（OS ごとの既定の場所）
  - cwd から上位の全ディレクトリの CLAUDE.md / CLAUDE.local.md / .claude/CLAUDE.md
  - `paths:` を持たない ~/.claude/rules/**/*.md と <各ディレクトリ>/.claude/rules/**/*.md
  - AGENTS.md（そのディレクトリに CLAUDE.md が無いときだけ）
  - 上記からの `@path` 取込（最大4段）
HTML のブロックコメントは注入前に除かれるので数えない。コードスパン・フェンス内の `@` は取込として扱わない。4MiB 超のファイルは読まれないので数えない。トークン数は概算（UTF-8 バイト / 3）。
SessionStart フックの出力や `paths:` 付き rules の遅延読込は数えない（実測ではなく推定）。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

sys.dont_write_bytecode = True  # .pyc には実ユーザー名入りの絶対パスが埋め込まれる
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import load_config, resolve, state_dir  # noqa: E402

MANAGED = {
    "win32": Path("C:/Program Files/ClaudeCode/CLAUDE.md"),
    "darwin": Path("/Library/Application Support/ClaudeCode/CLAUDE.md"),
}.get(sys.platform, Path("/etc/claude-code/CLAUDE.md"))
IMPORT_RE = re.compile(r"(?<![\w`])@((?:~|\.{1,2}|[A-Za-z]:)?[/\\]?[\w.\-/\\]+\.md)\b")
COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
CODE_RE = re.compile(r"```.*?```|`[^`\n]*`", re.S)  # 取込はコードスパン・フェンスの中を読まない（公式）
MAX_HOPS = 4  # 公式 memory 文書「maximum depth of four hops」（2026-10-24 確認）
MAX_FILE = 4 * 1024 * 1024  # 4MiB を超えるファイルは Claude Code が読まない
LOG_TAIL = 256 * 1024  # 台帳は末尾だけ読む（毎セッション全体を読まない）
LOG_KEEP = 200  # 肥大したらこの件数だけ残す（1件は数KB。LOG_TAIL*8 を十分下回る）


def read(p: Path) -> str:
    return p.read_text(encoding="utf-8-sig", errors="replace")


def has_paths_frontmatter(text: str) -> bool:
    if not text.startswith("---"):
        return False
    end = text.find("\n---", 3)
    return end > 0 and re.search(r"^paths\s*:", text[:end], re.M) is not None


def rules_in(d: Path) -> list:
    if not d.is_dir():
        return []
    return [p for p in sorted(d.rglob("*.md")) if p.is_file() and not has_paths_frontmatter(read(p))]


def roots(cfg: dict, cwd: Path, ceiling: Path | None = None) -> list:
    home_claude = (Path.home() / ".claude" / "CLAUDE.md").resolve()
    ancestors = [cwd, *cwd.parents]
    if ceiling is not None:  # テスト用: ここより上は見ない
        ancestors = [d for d in ancestors if d == ceiling or ceiling in d.parents]
    cands = [MANAGED] + [resolve(s, cfg) for s in cfg["always_loaded"]] + rules_in(Path.home() / ".claude" / "rules")
    project_md = []
    for d in ancestors:
        project_md += [d / "CLAUDE.md", d / "CLAUDE.local.md", d / ".claude" / "CLAUDE.md"]
        cands += rules_in(d / ".claude" / "rules")
    cands += project_md
    # AGENTS.md は cwd と上位に CLAUDE.md 系が1つも無いときだけ読まれる（~/.claude/CLAUDE.md は数えない）
    if not any(p.is_file() and p.resolve() != home_claude for p in project_md):
        cands += [x for d in ancestors for x in (d / "AGENTS.md", d / ".claude" / "AGENTS.md")]
    return cands


def expand(start: list) -> list:
    """@取込を最大4段まで辿る。戻り値は (ファイル, 段数)。重複と循環は1回だけ数える。"""
    seen: dict = {}
    queue = [(p, 0) for p in start]
    while queue:
        p, hop = queue.pop(0)
        if not p.is_file() or p.stat().st_size > MAX_FILE:
            continue
        key = str(p.resolve()).lower() if sys.platform in ("win32", "darwin") else str(p.resolve())
        if key in seen:
            continue
        seen[key] = (p, hop)
        if hop >= MAX_HOPS:
            continue
        text = CODE_RE.sub("", COMMENT_RE.sub("", read(p)))
        for m in IMPORT_RE.finditer(text):
            s = m.group(1)
            q = Path(s).expanduser() if s.startswith("~") else Path(s)
            queue.append((q if q.is_absolute() else p.parent / q, hop + 1))
    return list(seen.values())


def measure(cfg: dict, cwd: Path, ceiling: Path | None = None) -> dict:
    files = []
    for p, hop in expand(roots(cfg, cwd, ceiling)):
        text = COMMENT_RE.sub("", read(p))
        files.append({"path": str(p), "bytes": len(text.encode("utf-8")), "lines": text.count("\n") + (bool(text) and not text.endswith("\n")), "hop": hop})
    total = sum(f["bytes"] for f in files)
    return {"ts": datetime.now().isoformat(timespec="seconds"), "cwd": str(cwd),
            "total_bytes": total, "approx_tokens": total // 3, "files": files}


def previous(log: Path, cwd: str) -> dict | None:
    if not log.exists():
        return None
    with log.open("rb") as f:
        f.seek(max(0, log.stat().st_size - LOG_TAIL))
        lines = f.read().decode("utf-8", errors="replace").splitlines()
    for line in reversed(lines):
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(r, dict) and r.get("cwd") == cwd and isinstance(r.get("total_bytes"), int):
            return r
    return None


def append(log: Path, rec: dict) -> None:
    with log.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    if log.stat().st_size > LOG_TAIL * 8:  # 肥大したら新しい側だけ残す
        lines = log.read_text(encoding="utf-8", errors="replace").splitlines()[-LOG_KEEP:]
        tmp = log.with_suffix(".tmp")
        tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        tmp.replace(log)  # 同時起動と競合しても、最悪で記録が1件欠けるだけ


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--cwd", default=".")
    a = ap.parse_args(argv)
    cfg = load_config()
    cwd = Path(a.cwd).resolve()
    rec = measure(cfg, cwd)
    log = state_dir(cfg) / "context-budget-log.jsonl"
    prev = previous(log, rec["cwd"])
    append(log, rec)

    warns = []
    budget = int(cfg["budget_bytes"])
    if rec["total_bytes"] > budget:
        warns.append(f"常時読込 {rec['total_bytes']:,}B が予算 {budget:,}B を超過")
    if prev and prev["total_bytes"] and \
            (rec["total_bytes"] - prev["total_bytes"]) / prev["total_bytes"] > float(cfg["growth_warn_ratio"]):
        warns.append(f"前回 {prev['total_bytes']:,}B → 今回 {rec['total_bytes']:,}B に急増")
    max_lines = int(cfg.get("max_lines_per_file", 200))
    long_files = [f for f in rec["files"] if f["lines"] > max_lines]
    if long_files:  # 公式の目安は1ファイル200行未満（長いほど守られにくくなる）
        warns.append(f"{max_lines}行超: " + ", ".join(f"{Path(f['path']).name} {f['lines']}行" for f in long_files[:5]))
    if warns:
        top = sorted(rec["files"], key=lambda f: -f["bytes"])[:3]
        warns.append("大きい順: " + ", ".join(f"{Path(f['path']).name} {f['bytes']:,}B" for f in top))
        warns.append("直近で足した行を「消したら間違えるか」で見直すこと。")

    if not a.quiet:
        print(f"常時読込（推定）: {rec['total_bytes']:,}B（≒{rec['approx_tokens']:,} tokens）/ 予算 {budget:,}B")
        for f in rec["files"]:
            via = f"  ←@取込{f['hop']}段" if f["hop"] else ""
            print(f"  {f['bytes']:>9,}B {f['lines']:>5}行  {f['path']}{via}")
        print("実際の内訳は Claude Code の `/context` で確認できる（この数字は静的な推定）。")
    for w in warns:
        print(f"⚠ token-budget: {w}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
