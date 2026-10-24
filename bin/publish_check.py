# -*- coding: utf-8 -*-
"""publish_check.py — ハーネスを GitHub 等へ公開・共有する前の検査。

  py bin/publish_check.py               検査する（NG があれば終了コード1）
  py bin/publish_check.py --no-history  コミット履歴を見ない（見なかったことは必ず表示する）

見るもの:
  1. フォルダの中に個人データ（記憶・索引・台帳・__pycache__）が残っていないか
  2. フォルダ内の**全ファイル**（.gitignore で無視されるものも含む）の秘密情報・メールアドレス・実ユーザー名入りのパス
     … ZIP・Webアップロード・コピーでの共有では .gitignore が効かないため、git が見るファイルだけでは足りない
  3. コミット履歴: 作成者・コミッター・メッセージ（Co-authored-by 等）・注釈タグ・過去の版の中身
  4. .git フォルダ: config（リモートURLの認証情報）と reflog（履歴を書き換えても旧メールが残る）
     … .git ごと共有（ZIP 等）すると出ていく。共有は `git push` か `.git` を含めない形で行う
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

sys.dont_write_bytecode = True  # .pyc には実ユーザー名入りの絶対パスが埋め込まれる
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import HARNESS_ROOT, LEGACY_MEMORY_DIR, LEGACY_STATE_DIR, inside_harness, \
    load_config, memory_dir, state_dir_path  # noqa: E402
from sensitive import find_emails, find_secrets, find_user_paths, mask  # noqa: E402

GIT_TIMEOUT = 120
# 中身を読まずに名前だけで止めるもの（正規表現で追いきれない秘密の入れ物）
SECRET_FILE_RE = re.compile(r"\.env(?:\.(?!example$|sample$|template$).+)?|id_(?:rsa|dsa|ecdsa|ed25519)(?:\.pub)?|.+\.(?:pem|key|p12|pfx|kdbx)"
                            r"|credentials(?:\..+)?|\.netrc|\.npmrc|\.pypirc", re.I)
MAX_FILE = 50 * 1024 * 1024  # これより大きいファイルは読まずに「未検査」として報告する


def git(*args: str, timeout: int = GIT_TIMEOUT) -> str | None:
    """失敗・タイムアウト・git 未導入は None。呼び出し側は None を「未検査」として扱うこと（合格にしない）。"""
    try:
        r = subprocess.run(["git", "-C", str(HARNESS_ROOT), *args], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", check=False, timeout=timeout)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode == 0 else None


def rel(p: Path) -> str:
    try:
        return p.relative_to(HARNESS_ROOT).as_posix()
    except ValueError:
        return str(p)


def read_text(p: Path) -> str | None:
    """テキストとして読む。UTF-16（PowerShell 5 の既定出力）も解釈する。バイナリは None。"""
    raw = p.read_bytes()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", errors="replace")
    if b"\x00" in raw[:8192]:
        return None
    return raw.decode("utf-8", errors="replace")


def scan_text(label: str, text: str) -> list:
    out = [f"{label}: {s}らしき文字列" for s in find_secrets(text)]
    out += [f"{label}: メールアドレス {mask(e)}" for e in find_emails(text)]
    out += [f"{label}: ユーザー名入りのパス {mask(p)}" for p in find_user_paths(text)]
    return out


def check_folder() -> list:
    ng = []
    cfg = load_config()
    for name, p in (("記憶", memory_dir(cfg)), ("索引・台帳", state_dir_path(cfg))):
        if inside_harness(p) and p.exists() and any(p.rglob("*")):
            ng.append(f"{name}の置き場がハーネスのフォルダ内にある: {rel(p)}（`py bin/install.py` で外へ移す）")
    for p in (LEGACY_MEMORY_DIR, LEGACY_STATE_DIR):
        if p.exists() and any(p.rglob("*")):
            ng.append(f"フォルダ内に個人データが残っている: {rel(p)}（`py bin/install.py` で外へ移す）")
    for p in sorted(HARNESS_ROOT.rglob("*")):
        parts = p.relative_to(HARNESS_ROOT).parts
        if ".git" not in parts and p.is_file() and SECRET_FILE_RE.fullmatch(p.name):
            ng.append(f"{rel(p)}: 秘密情報を入れる種類のファイル（.env・鍵・認証情報）。フォルダに置かない")
    for p in HARNESS_ROOT.rglob("__pycache__"):
        if ".git" not in p.relative_to(HARNESS_ROOT).parts:
            ng.append(f"{rel(p)} がある（.pyc に実ユーザー名入りのパスが入る。フォルダごと削除してよい）")
    return ng


def check_files(stats: dict) -> list:
    """フォルダ内の全ファイル（.git を除く）を見る。飛ばしたものは数えて報告する。"""
    ng = []
    ignored = set((git("ls-files", "-z", "--others", "--ignored", "--exclude-standard") or "").split("\0"))
    for p in sorted(HARNESS_ROOT.rglob("*")):
        parts = p.relative_to(HARNESS_ROOT).parts
        if not p.is_file() or ".git" in parts or "__pycache__" in parts:
            continue
        try:
            if p.stat().st_size > MAX_FILE:
                stats["too_big"].append(rel(p)); continue
            text = read_text(p)
        except OSError:
            stats["unreadable"].append(rel(p)); continue
        if text is None:
            stats["binary"].append(rel(p)); continue
        stats["scanned"] += 1
        label = rel(p) + ("（.gitignore 対象。git push では出ないが ZIP 等では出る）" if rel(p) in ignored else "")
        ng += scan_text(label, text)
    return ng


def check_history() -> list:
    ng = []
    unread = "を読めなかった（git の失敗・未導入・タイムアウト）。未検査のまま公開しない"
    if not (HARNESS_ROOT / ".git").exists():
        top = git("rev-parse", "--show-toplevel")
        if top:
            return [f"ハーネスが別のリポジトリ（{mask(top.strip())}）の中にある。履歴は未検査。単独のリポジトリにして公開する"]
        return []  # git 管理外（履歴は出ていかない）
    if git("rev-parse", "--git-dir") is None:
        return ["コミット履歴" + unread]
    if git("rev-parse", "--verify", "-q", "HEAD") is None and not (git("for-each-ref") or "").strip():
        return []  # コミット0件
    log = git("log", "--all", "--format=%an <%ae>%n%cn <%ce>%n%B")
    tags = git("for-each-ref", "--format=%(taggername) %(taggeremail)%0a%(contents)", "refs/tags")
    patch = git("log", "--all", "-p", "--format=", "--no-color", "--no-ext-diff", timeout=GIT_TIMEOUT * 5)
    for label, out in (("コミット履歴（作成者・メッセージ）", log), ("注釈タグ", tags), ("コミット履歴の過去の版", patch)):
        if out is None:
            ng.append(label + unread)
            continue
        if label.endswith("過去の版"):
            out = "\n".join(ln[1:] for ln in out.splitlines() if ln.startswith("+") and not ln.startswith("+++"))
        ng += scan_text(label, out)
    me = (git("config", "user.email") or "").strip()
    if me and find_emails(me):
        ng.append(f"git の user.email が {mask(me)}。次のコミットにも載る（README「公開・共有する前に」）")
    return ng


def check_git_dir() -> list:
    """.git ごと共有したときに出ていくもの。git push では出ない。"""
    gd = HARNESS_ROOT / ".git"
    if not gd.is_dir():
        return []
    ng = []
    note = "（.git ごと共有すると出る。git push では出ない）"
    for name in ("config", "FETCH_HEAD", "COMMIT_EDITMSG", "ORIG_HEAD", "packed-refs", "description"):
        if (gd / name).is_file():
            ng += [x + note for x in scan_text(f".git/{name}", (gd / name).read_text(encoding="utf-8", errors="replace"))]
    for p in sorted((gd / "logs").rglob("*")) if (gd / "logs").is_dir() else []:
        if p.is_file():
            ng += [x + note for x in scan_text(f".git/{p.relative_to(gd).as_posix()}",
                                               p.read_text(encoding="utf-8", errors="replace"))]
    # 履歴を書き換えても、gc するまで旧コミットは .git/objects に残り `git cat-file` で読める（中身は走査しない）
    fsck = git("fsck", "--unreachable", "--no-reflogs", "--no-progress")
    if fsck is None:
        ng.append(".git の未到達オブジェクトを確認できなかった（git の失敗）。.git ごと共有しない")
    elif fsck.strip():
        n = len(fsck.strip().splitlines())
        ng.append(f".git に参照されない古いオブジェクトが {n} 件残っている（書き換え前の履歴など）{note}。"
                  "`git reflog expire --expire=now --all` と `git gc --prune=now` で消える")
    return ng


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-history", action="store_true")
    a = ap.parse_args(argv)
    stats = {"scanned": 0, "binary": [], "unreadable": [], "too_big": []}
    ng = check_folder() + check_files(stats) + check_git_dir() + ([] if a.no_history else check_history())
    ng = list(dict.fromkeys(ng))
    for x in ng:
        print("NG", x)
    print(f"検査したファイル: {stats['scanned']} 件")
    if stats["binary"]:
        print(f"⚠ 中身を見ていない（バイナリ）: {len(stats['binary'])} 件 — " + ", ".join(stats["binary"][:10]))
    for key, why in (("unreadable", "読めなかった"), ("too_big", f"{MAX_FILE // 1024 // 1024}MB超で未検査")):
        if stats[key]:
            ng.append(f"{why}ファイルがある")
            print(f"NG {why}: {', '.join(stats[key][:10])}")
    if a.no_history:
        print("⚠ コミット履歴は未検査（--no-history）。git で公開するなら付けずに実行すること")
    print(f"publish_check: 指摘 {len(ng)} 件（検出は正規表現。0件でも最後は目で確認する）")
    return 1 if ng else 0


if __name__ == "__main__":
    sys.exit(main())
