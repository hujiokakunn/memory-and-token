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
import functools
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


START_REASONS = ("session_start", "include")  # 起動時に載るもの（遅延読込・圧縮後の再読込は除く）


SAFE_NAME = re.compile(r"[^\w.\-+ ()　-ヿ一-鿿]")  # 英数・記号少々・かな・漢字だけを残す
NAME_MAX = 40


def shown_name(path: str) -> str:
    """警告欄（データ枠の外＝文脈では信頼される側）に載せるファイル名。

    ファイル名は他人のリポジトリ由来でありうる。改行や文章を混ぜて偽の指示行を作らせないよう、
    許可した文字以外を `?` に置き換え、長さを切る。
    """
    # 空白も `_` に変える（文章型の名前を、読める一文のまま信頼側の欄に載せない）
    name = SAFE_NAME.sub("?", Path(path).name).replace(" ", "_")
    return name if len(name) <= NAME_MAX else name[:NAME_MAX - 1] + "…"


@functools.lru_cache(maxsize=4096)  # 台帳の各行で呼ぶ。resolve は1回約1msかかる（850行で約1秒・Windows実測）
def norm_path(s: str) -> str:
    """比較用のパス表記。Windows・macOS は大文字小文字を区別しない。"""
    try:
        s = str(Path(s).resolve())
    except (OSError, ValueError, RuntimeError):  # RuntimeError: 3.12 以前の resolve でのリンク循環
        pass
    return s.lower() if sys.platform in ("win32", "darwin") else s


def actual_last_session(log: Path, cwd: str, exclude_sid: str = "") -> dict | None:
    """InstructionsLoaded の台帳から、同じフォルダの直近セッションが起動時に実際に読んだファイルを返す（実測）。

    exclude_sid（今始まったセッション）は除く。InstructionsLoaded と SessionStart の発火順は公式に明記が無く、
    読込途中の自分自身を「前回」として数えると過小計上と誤警告になる。
    """
    if not log.exists():
        return None
    with log.open("rb") as f:
        f.seek(max(0, log.stat().st_size - LOG_TAIL))
        lines = f.read().decode("utf-8", errors="replace").splitlines()
    want = norm_path(cwd)
    recs = []
    for line in lines:
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not (isinstance(r, dict) and all(isinstance(r.get(k), str) and r[k] for k in ("session_id", "file_path", "cwd"))):
            continue
        if r["session_id"] != exclude_sid and r.get("load_reason") in START_REASONS \
                and norm_path(r["cwd"]) == want:  # 別フォルダのセッションと比べない
            recs.append(r)
    if not recs:
        return None
    sid = recs[-1]["session_id"]
    files: dict[str, dict] = {}
    for r in recs:
        if r["session_id"] == sid:
            files[norm_path(r["file_path"])] = r
    sized = [r["bytes"] for r in files.values()
             if isinstance(r.get("bytes"), int) and not isinstance(r["bytes"], bool) and r["bytes"] >= 0]
    return {"session_id": sid, "ts": recs[-1].get("ts", ""), "files": list(files.values()),
            "total_bytes": sum(sized), "unsized": len(files) - len(sized)}


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
    ap.add_argument("--exclude-session", default="", help="実測の集計から除くセッションID（今始まったセッション）")
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
        warns.append(f"{max_lines}行超: " + ", ".join(f"{shown_name(f['path'])} {f['lines']}行" for f in long_files[:5]))
    try:  # 実測は補助。台帳が壊れていても推定の警告は出す
        act = actual_last_session(state_dir(cfg) / "instructions-loaded.jsonl", rec["cwd"], a.exclude_session)
    except (OSError, ValueError) as e:
        act = None
        warns.append(f"実測の台帳を読めなかった（{type(e).__name__}）")
    if act:
        if act["total_bytes"] > budget:
            warns.append(f"前回セッションの実測 {act['total_bytes']:,}B が予算 {budget:,}B を超過")
        if act["unsized"]:
            warns.append(f"実測のうち {act['unsized']} ファイルはサイズを測れず合計に入っていない（4MiB超・消失・読込失敗）")
        estimated = {norm_path(f["path"]) for f in rec["files"]}
        missed = [r for r in act["files"] if norm_path(r["file_path"]) not in estimated]
        if missed:  # 推定の規則が公式の読込規則からずれた合図
            warns.append("推定から漏れた常時読込（実測にだけある）: "
                         + ", ".join(shown_name(r["file_path"]) for r in missed[:5]))
    if warns:
        top = sorted(rec["files"], key=lambda f: -f["bytes"])[:3]
        warns.append("大きい順: " + ", ".join(f"{shown_name(f['path'])} {f['bytes']:,}B" for f in top))
        warns.append("直近で足した行を「消したら間違えるか」で見直すこと。")

    if not a.quiet:
        print(f"常時読込（推定）: {rec['total_bytes']:,}B（≒{rec['approx_tokens']:,} tokens）/ 予算 {budget:,}B")
        for f in rec["files"]:
            via = f"  ←@取込{f['hop']}段" if f["hop"] else ""
            print(f"  {f['bytes']:>9,}B {f['lines']:>5}行  {f['path']}{via}")
        if act:
            print(f"実測（前回セッション起動時・InstructionsLoaded）: {act['total_bytes']:,}B / {len(act['files'])} ファイル（{act['ts']}）")
        else:
            print("実測: 記録なし（InstructionsLoaded フック未登録か、登録後にセッションを起動していない。`py bin/install.py`）")
        print("実際の内訳は Claude Code の `/context` で確認できる（上の推定は静的、実測は起動時に読まれたファイルの合計）。")
    for w in warns:
        print(f"⚠ token-budget: {w}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
