# -*- coding: utf-8 -*-
"""install.py — 新しいPCで1回実行すると、このハーネスを Claude Code に配線する。

  python bin/install.py            配線する（~/.claude/settings.json を .bak を取ってから更新）
  python bin/install.py --dry-run  変更内容を表示するだけ
  python bin/install.py --uninstall

やること（冪等。何度実行しても重複しない）:
  1. 記憶フォルダを初期化（既存は触らない）
  2. ~/.claude/settings.json の hooks.SessionStart にフックを1本登録（このPCでの絶対パス・python実体）
  3. ~/.claude/CLAUDE.md に @import 1行を追記（規約本文は常時読込に入れず、入口の1行だけ）
ハーネスを移動したら再実行すれば新しい場所へ張り替わる。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import HARNESS_ROOT, memory_dir  # noqa: E402

MARK = "/hooks/session_start.py"  # フォルダ名に依存させない
CLAUDE_MD_MARK = "<!-- harness:memory-token -->"


def hook_command() -> str:
    py = Path(sys.executable).as_posix()
    return f'"{py}" "{(HARNESS_ROOT / "hooks" / "session_start.py").as_posix()}"'


def is_ours(h: dict) -> bool:
    return MARK in h.get("command", "").replace("\\", "/")


def update_settings(settings: dict, uninstall: bool) -> dict:
    hooks = settings.setdefault("hooks", {})
    groups = hooks.get("SessionStart", [])
    cleaned = []
    for g in groups:
        g = dict(g)
        g["hooks"] = [h for h in g.get("hooks", []) if not is_ours(h)]
        if g["hooks"]:
            cleaned.append(g)
    if not uninstall:
        cleaned.append({"hooks": [{"type": "command", "command": hook_command(), "timeout": 30}]})
    if cleaned:
        hooks["SessionStart"] = cleaned
    else:
        hooks.pop("SessionStart", None)
    if not hooks:
        settings.pop("hooks")
    return settings


def claude_md_line() -> str:
    entry = (HARNESS_ROOT / "ENTRY.md").as_posix()
    return f"{CLAUDE_MD_MARK}\n@{entry}\n"


def update_claude_md(text: str, uninstall: bool) -> str:
    lines = text.splitlines(keepends=True)
    out, skip = [], False
    for ln in lines:
        if ln.startswith(CLAUDE_MD_MARK):
            skip = True; continue
        if skip:
            skip = False; continue
        out.append(ln)
    new = "".join(out)
    if not uninstall:
        if new and not new.endswith("\n"):
            new += "\n"
        new += claude_md_line()
    return new


def write(path: Path, content: str, dry: bool) -> None:
    old = path.read_text(encoding="utf-8") if path.exists() else None
    if old == content:
        print(f"変更なし: {path}"); return
    if dry:
        print(f"[dry-run] 更新予定: {path}\n{content}"); return
    path.parent.mkdir(parents=True, exist_ok=True)
    if old is not None:
        shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
    path.write_text(content, encoding="utf-8")
    print(f"更新: {path}" + ("（.bak あり）" if old is not None else ""))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--claude-dir", default=str(Path.home() / ".claude"))
    a = ap.parse_args(argv)
    cdir = Path(a.claude_dir)
    if sys.version_info < (3, 9):
        sys.exit("エラー: Python 3.9 以上が必要")

    sp = cdir / "settings.json"
    try:
        settings = json.loads(sp.read_text(encoding="utf-8")) if sp.exists() else {}
    except json.JSONDecodeError as e:
        sys.exit(f"エラー: {sp} が壊れている（{e}）。手で直してから再実行。")
    write(sp, json.dumps(update_settings(settings, a.uninstall), ensure_ascii=False, indent=2) + "\n", a.dry_run)

    cm = cdir / "CLAUDE.md"
    write(cm, update_claude_md(cm.read_text(encoding="utf-8") if cm.exists() else "", a.uninstall), a.dry_run)

    if not a.uninstall and not a.dry_run:
        mem = memory_dir()
        mem.mkdir(parents=True, exist_ok=True)
        if not (mem / "MEMORY.md").exists():
            (mem / "MEMORY.md").write_text("# MEMORY（global層の索引。毎セッションこれだけ読まれる）\n\n",
                                           encoding="utf-8")
        print(f"記憶フォルダ: {mem}")
        print("完了。Claude Code を再起動すると SessionStart で索引と予算警告が注入される。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
