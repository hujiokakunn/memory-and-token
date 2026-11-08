# -*- coding: utf-8 -*-
"""Claude Code InstructionsLoaded フック。実際に読み込まれた指示ファイルを台帳へ記録する（推定ではなく実測）。

公式 hooks 文書（2026-11 確認）: 入力は file_path / memory_type / load_reason
（session_start・nested_traversal・path_glob_match・include・compact）。遮断はできない＝監査用。
AGENTS.md を Claude が直接読んだときは発火しない。
台帳: <state_dir>/instructions-loaded.jsonl（絶対パス＝ユーザー名が載るのでハーネスの外に置く）。
フックが落ちてもセッションは止めない（終了コード0。出力もしない＝文脈を汚さない）。
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

sys.dont_write_bytecode = True  # .pyc には実ユーザー名入りの絶対パスが埋め込まれる
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

LOG_NAME = "instructions-loaded.jsonl"


def main() -> int:
    try:
        from _common import state_dir
        import token_budget

        ev = json.loads(sys.stdin.buffer.read().decode("utf-8-sig") or "{}")
        fp = ev.get("file_path")
        if not isinstance(fp, str) or not fp:
            return 0
        p = Path(fp)
        size = None
        if p.is_file() and p.stat().st_size <= token_budget.MAX_FILE:
            size = len(token_budget.COMMENT_RE.sub("", token_budget.read(p)).encode("utf-8"))
        rec = {"ts": datetime.now().isoformat(timespec="seconds"),
               "session_id": str(ev.get("session_id", ""))[:100],
               "cwd": str(Path(ev["cwd"]).resolve()) if isinstance(ev.get("cwd"), str) and ev["cwd"] else "",
               "file_path": fp[:1000],
               "memory_type": str(ev.get("memory_type", ""))[:20],
               "load_reason": str(ev.get("load_reason", ""))[:40],
               "bytes": size}
        token_budget.append(state_dir() / LOG_NAME, rec)
    except Exception as e:  # noqa: BLE001  監査用。失敗してもセッションを止めない
        # stderr は文脈に入らない（終了コード0）が、`claude --debug` では見える。黙って消さない
        print(f"harness instructions_loaded 失敗: {type(e).__name__}: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
