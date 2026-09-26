# -*- coding: utf-8 -*-
"""Claude Code SessionStart フック。標準出力がセッション文脈へ注入される。

注入するのは「記憶の索引（MEMORY.md）」と「トークン予算の警告」だけ。記憶本体は注入しない
（毎回全部読むのが混乱とコストの原因。本体は検索で必要時だけ引く）。
フックが落ちてもセッションは止めない（終了コード0）。ただし失敗は黙らず出力する。
"""
from __future__ import annotations

import sys
from pathlib import Path

BIN = Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(BIN))


def main() -> int:
    try:
        from _common import HARNESS_ROOT, memory_dir
        import token_budget

        mem = memory_dir()
        idx = mem / "MEMORY.md"
        print("## harness: memory / token")
        print(f"規約: {HARNESS_ROOT / 'memory' / 'AGENTS.md'} / {HARNESS_ROOT / 'token' / 'AGENTS.md'}（該当作業時に読む）")
        print(f"記憶検索: python \"{BIN / 'memory.py'}\" search \"<語>\" --type <種別>")
        if idx.exists():
            text = idx.read_text(encoding="utf-8")
            print(f"\n### 記憶の索引（{idx}）\n{text[:8000]}")
            if len(text) > 8000:
                print("⚠ MEMORY.md が 8,000 文字を超え、以降を省略した。global 層を domain へ降格すること。")
        else:
            print(f"⚠ 記憶フォルダ未初期化: python \"{BIN / 'memory.py'}\" init")
        token_budget.main(["--quiet"])
    except Exception as e:  # noqa: BLE001
        print(f"⚠ harness session_start 失敗: {type(e).__name__}: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
