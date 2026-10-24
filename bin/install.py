# -*- coding: utf-8 -*-
"""install.py — 新しいPCで1回実行すると、このハーネスを Claude Code に配線する。

  python bin/install.py            配線する（~/.claude/settings.json を .bak を取ってから更新）
  python bin/install.py --dry-run  変更内容を表示するだけ
  python bin/install.py --uninstall

やること（冪等。何度実行しても重複しない）:
  1. 記憶フォルダを初期化（既存は触らない）。旧版の既定（ハーネス内 data/memory・.state）に
     個人データがあれば、ハーネスの外（既定 ~/.claude-harness/）へ移す
  2. ~/.claude/settings.json の hooks.SessionStart にフックを1本登録（このPCでの絶対パス・python実体）
  3. ~/.claude/CLAUDE.md に @import 1行を追記（規約本文は常時読込に入れず、入口の1行だけ）
ハーネスを移動したら再実行すれば新しい場所へ張り替わる。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.dont_write_bytecode = True  # .pyc には実ユーザー名入りの絶対パスが埋め込まれる
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import HARNESS_ROOT, LEGACY_MEMORY_DIR, LEGACY_STATE_DIR, ensure_private_dir, \
    inside_harness, load_config, memory_dir, state_dir_path  # noqa: E402

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
            skip = False
            if ln.startswith("@"):  # 利用者が @ 行だけ消していたら、次の無関係な行は残す
                continue
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
        orig = path.with_suffix(path.suffix + ".bak.orig")  # 最初の原本は再実行で上書きしない
        if not orig.exists():
            shutil.copy2(path, orig)
        shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
    path.write_text(content, encoding="utf-8")
    print(f"更新: {path}" + ("（.bak あり）" if old is not None else ""))


def migrate(old: Path, new: Path, kind: str, dry: bool) -> bool:
    """旧版の既定だったハーネス内の個人データを外へ移す。失敗しても旧データは消さない。成功/不要なら True。"""
    if not old.is_dir() or not any(old.iterdir()):
        return True
    if inside_harness(new):
        print(f"⚠ {kind}の置き場がハーネスの中に設定されている（{new}）。移さない。フォルダごと共有すると一緒に出ていく")
        return True
    target = new
    if new.exists() and any(new.iterdir()):
        if kind == "記憶":
            print(f"NG 移せない: {old} と {new} の両方に記憶がある。{old} の中身を {new} へ手で移し（同名は中身を見て統合）、"
                  f"{old} を消してから再実行すること")
            return False
        # 索引・台帳は作り直せる。新しい側を壊さず、旧台帳は履歴として下位フォルダへ退避する
        target = new / f"legacy-{datetime.now():%Y%m%d-%H%M%S}"
    if dry:
        print(f"[dry-run] 移動予定: {old} → {target}"); return True
    try:
        ensure_private_dir(target.parent)
        if target.exists():
            target.rmdir()  # 空のときだけ消える
        shutil.move(str(old), str(target))
    except OSError as e:
        # 別ドライブへの移動は「複写 → 旧の削除」なので、どの段で落ちたかで旧・新の状態が変わる
        print(f"NG {kind}の移動が途中で失敗: {old} → {target}（{type(e).__name__}: {e}）。"
              f"旧と新の両方を開いて中身を確かめ、完全な側を残してもう一方を消してから再実行すること"
              f"（新に全ファイルが揃っていれば、旧の削除だけが失敗している）")
        return False
    print(f"移動: {old} → {target}（ハーネスのフォルダに個人データを置かない）")
    return True


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--claude-dir", default=str(Path.home() / ".claude"))
    a = ap.parse_args(argv)
    cdir = Path(a.claude_dir)
    if sys.version_info < (3, 9):
        sys.exit("エラー: Python 3.9 以上が必要")

    # 個人データの移動を先に行う（設定だけ書き換わって移動が失敗する半端な状態を作らない）
    if not a.uninstall:
        cfg = load_config()
        ok = all([migrate(LEGACY_MEMORY_DIR, memory_dir(cfg), "記憶", a.dry_run),
                  migrate(LEGACY_STATE_DIR, state_dir_path(cfg), "索引・台帳", a.dry_run)])
        if not ok:
            return 1

    sp = cdir / "settings.json"
    try:
        # Windows PowerShell 5 の Set-Content -Encoding UTF8 は BOM を付ける。壊れていないので utf-8-sig で読む
        settings = json.loads(sp.read_text(encoding="utf-8-sig")) if sp.exists() else {}
    except json.JSONDecodeError as e:
        sys.exit(f"エラー: {sp} が壊れている（{e}）。手で直してから再実行。")
    write(sp, json.dumps(update_settings(settings, a.uninstall), ensure_ascii=False, indent=2) + "\n", a.dry_run)

    cm = cdir / "CLAUDE.md"
    write(cm, update_claude_md(cm.read_text(encoding="utf-8") if cm.exists() else "", a.uninstall), a.dry_run)

    if not a.uninstall and not a.dry_run:
        mem = ensure_private_dir(memory_dir())
        if not (mem / "MEMORY.md").exists():
            (mem / "MEMORY.md").write_text("# MEMORY（global層の索引。毎セッションこれだけ読まれる）\n\n",
                                           encoding="utf-8")
        print(f"記憶フォルダ: {mem}")
        if inside_harness(mem):
            print("⚠ 記憶フォルダがハーネスの中にある。フォルダごと共有すると記憶も出ていく")
        print("完了。Claude Code を再起動すると SessionStart で索引と予算警告が注入される。")
        print("公開・共有する前は `py bin/publish_check.py` を実行すること。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
