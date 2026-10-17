# -*- coding: utf-8 -*-
"""memory.py — 記憶ハーネスの道具（標準ライブラリのみ）。

  python bin/memory.py init                         記憶フォルダと MEMORY.md を用意
  python bin/memory.py add <name> --type feedback --description "..." [--global] [--agent X] [--body "..."]
  python bin/memory.py search "<語>" [--type T] [--agent X] [--top 5]
  python bin/memory.py build                        検索索引を再構築（search は古ければ自動再構築）
  python bin/memory.py lint                         書式検査（違反があれば終了コード1）
"""
from __future__ import annotations

import argparse
import hashlib
import re
import sqlite3
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import memory_dir, state_dir  # noqa: E402

TYPES = ("user", "feedback", "project", "reference")
NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
INDEX_HEADER = "# MEMORY（global層の索引。毎セッションこれだけ読まれる）\n\n"


# ---------- frontmatter ----------
def parse(text: str) -> tuple[dict, str] | None:
    """1行目が '---' のときだけ frontmatter とみなす（BOM・先頭空行は不正）。"""
    if not text.startswith("---\n") and not text.startswith("---\r\n"):
        return None
    lines = text.splitlines()
    try:
        end = lines.index("---", 1)
    except ValueError:
        return None
    meta: dict = {}
    for ln in lines[1:end]:
        m = re.match(r"^\s*([A-Za-z_]+):\s*(.*)$", ln)
        if m and m.group(2):
            meta[m.group(1)] = m.group(2).strip().strip('"')
    return meta, "\n".join(lines[end + 1:])


def iter_notes(root: Path):
    for p in sorted(root.rglob("*.md")):
        if p.name == "MEMORY.md" or any(part.startswith(".") for part in p.relative_to(root).parts):
            continue
        yield p


# ---------- index ----------
def db_path() -> Path:
    return state_dir() / "memory_index.sqlite"


def fingerprint(root: Path) -> str:
    """記憶フォルダの状態（パス・更新時刻・サイズの全組）。

    「最新の更新時刻 > 索引の時刻」だけでは、削除したノートが残り、元の時刻を保ったまま
    同期で届いたノート（Drive等）が永久に索引へ入らない。集合全体の指紋で比べる。
    """
    h = hashlib.sha256()
    for p in iter_notes(root):
        st = p.stat()
        h.update(f"{p.relative_to(root).as_posix()}\0{st.st_mtime_ns}\0{st.st_size}\n".encode("utf-8"))
    return h.hexdigest()


def stored_fingerprint(dbp: Path) -> str | None:
    try:
        con = sqlite3.connect(dbp)
        try:
            row = con.execute("SELECT v FROM info WHERE k='fingerprint'").fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return None
    return row[0] if row else None


def build(root: Path) -> int:
    dbp = db_path()
    tmp = dbp.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    con = sqlite3.connect(tmp)
    try:
        con.execute("CREATE VIRTUAL TABLE notes USING fts5(path UNINDEXED, name, type UNINDEXED, "
                    "agent UNINDEXED, description, body, tokenize='trigram')")
        fts = True
    except sqlite3.OperationalError:  # 古い SQLite（trigram 非対応）→ LIKE 検索へ縮退
        con.execute("CREATE TABLE notes(path, name, type, agent, description, body)")
        fts = False
    n = 0
    for p in iter_notes(root):
        parsed = parse(p.read_text(encoding="utf-8", errors="replace"))
        if not parsed:
            continue
        meta, body = parsed
        con.execute("INSERT INTO notes VALUES (?,?,?,?,?,?)",
                    (str(p.relative_to(root)), meta.get("name", p.stem), meta.get("type", ""),
                     meta.get("agent", ""), meta.get("description", ""), body))
        n += 1
    con.execute("CREATE TABLE info(k, v)")
    con.execute("INSERT INTO info VALUES ('fts', ?)", (int(fts),))
    con.execute("INSERT INTO info VALUES ('fingerprint', ?)", (fingerprint(root),))
    con.commit()
    con.close()
    tmp.replace(dbp)
    return n


def search(root: Path, query: str, type_: str | None, agent: str | None, top: int) -> list[dict]:
    dbp = db_path()
    if not dbp.exists() or stored_fingerprint(dbp) != fingerprint(root):
        build(root)
    con = sqlite3.connect(dbp)
    con.row_factory = sqlite3.Row
    fts = con.execute("SELECT v FROM info WHERE k='fts'").fetchone()[0]
    where, args = [], []
    if type_:
        where.append("type = ?"); args.append(type_)
    if agent:
        where.append("agent = ?"); args.append(agent)
    terms = [t for t in query.split() if t]
    rows = []
    # trigram は3文字未満の語を引けないので、その場合は LIKE を使う
    if fts and terms and all(len(t) >= 3 for t in terms):
        match = " AND ".join('"' + t.replace('"', '""') + '"' for t in terms)
        sql = ("SELECT path,name,type,agent,description, snippet(notes,5,'[',']','…',12) AS snip "
               "FROM notes WHERE notes MATCH ?" + "".join(f" AND {w}" for w in where) +
               " ORDER BY bm25(notes, 0, 5, 0, 0, 3, 1) LIMIT ?")
        rows = con.execute(sql, [match, *args, top]).fetchall()
    else:
        conds = [f"(name||' '||description||' '||body) LIKE ?" for _ in terms] + where
        sql = ("SELECT path,name,type,agent,description, substr(body,1,80) AS snip FROM notes"
               + (" WHERE " + " AND ".join(conds) if conds else "") + " LIMIT ?")
        rows = con.execute(sql, [*(f"%{t}%" for t in terms), *args, top]).fetchall()
    con.close()
    return [dict(r) for r in rows]


# ---------- lint ----------
def lint(root: Path) -> list[str]:
    errs = []
    for p in iter_notes(root):
        rel = p.relative_to(root)
        raw = p.read_bytes()
        if raw.startswith(b"\xef\xbb\xbf"):
            errs.append(f"{rel}: BOM付き（検索から漏れる）"); continue
        parsed = parse(raw.decode("utf-8", errors="replace"))
        if not parsed:
            errs.append(f"{rel}: 1行目が '---' でない／frontmatter が閉じていない"); continue
        meta, _ = parsed
        if meta.get("name") != p.stem:
            errs.append(f"{rel}: name がファイル名と不一致（{meta.get('name')}）")
        if not NAME_RE.match(p.stem):
            errs.append(f"{rel}: ファイル名が kebab-case でない")
        if meta.get("type") not in TYPES:
            errs.append(f"{rel}: type が {TYPES} のいずれでもない")
        if not meta.get("description"):
            errs.append(f"{rel}: description が空")
        if rel.parts[0] == "agents" and meta.get("agent") != (rel.parts[1] if len(rel.parts) > 2 else None):
            errs.append(f"{rel}: agents/<名前>/ 配下は agent: <名前> が必須")
    idx = root / "MEMORY.md"
    if idx.exists():
        for m in re.finditer(r"\]\(([^)]+\.md)\)", idx.read_text(encoding="utf-8")):
            if not (root / m.group(1)).exists():
                errs.append(f"MEMORY.md: リンク切れ {m.group(1)}")
    return errs


# ---------- add / init ----------
def init(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "agents").mkdir(exist_ok=True)
    idx = root / "MEMORY.md"
    if not idx.exists():
        idx.write_text(INDEX_HEADER, encoding="utf-8")
    print(f"記憶フォルダ: {root}")


def add(root: Path, a) -> Path:
    if not NAME_RE.match(a.name):
        sys.exit(f"エラー: name は kebab-case（英小文字・数字・ハイフン）: {a.name}")
    init_quiet(root)
    folder = root / "agents" / a.agent if a.agent else root
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{a.name}.md"
    if path.exists():
        sys.exit(f"エラー: 既に存在（update なら直接編集）: {path}")
    fm = ["---", f"name: {a.name}", f"description: {a.description}", "metadata:",
          f"  type: {a.type}", f"  created: {date.today().isoformat()}"]
    if a.agent:
        fm += ["  tier: agent", f"  agent: {a.agent}"]
    fm.append("---")
    body = a.body or ("<事実>\n\n**Why:** \n**How to apply:** " if a.type in ("feedback", "project") else "<事実>")
    path.write_text("\n".join(fm) + "\n" + body + "\n", encoding="utf-8")
    if a.is_global:
        if a.agent:
            sys.exit("エラー: agent層の記憶は --global にできない（昇格は別の役割が判定）")
        with (root / "MEMORY.md").open("a", encoding="utf-8") as f:
            f.write(f"- [{a.name}]({a.name}.md) — {a.description}\n")
    return path


def init_quiet(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    if not (root / "MEMORY.md").exists():
        (root / "MEMORY.md").write_text(INDEX_HEADER, encoding="utf-8")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="記憶ハーネス")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init"); sub.add_parser("build"); sub.add_parser("lint")
    s = sub.add_parser("search"); s.add_argument("query")
    s.add_argument("--type", choices=TYPES); s.add_argument("--agent"); s.add_argument("--top", type=int, default=5)
    d = sub.add_parser("add"); d.add_argument("name"); d.add_argument("--type", choices=TYPES, required=True)
    d.add_argument("--description", required=True); d.add_argument("--agent"); d.add_argument("--body")
    d.add_argument("--global", dest="is_global", action="store_true")
    a = ap.parse_args(argv)
    root = memory_dir()

    if a.cmd == "init":
        init(root)
    elif a.cmd == "build":
        print(f"索引を再構築: {build(root)} 件")
    elif a.cmd == "lint":
        errs = lint(root)
        for e in errs:
            print("NG", e)
        print(f"lint: 違反 {len(errs)} 件")
        return 1 if errs else 0
    elif a.cmd == "add":
        print(f"作成: {add(root, a)}（本文を埋めること）")
    elif a.cmd == "search":
        if not root.exists():
            print(f"記憶フォルダが無い: {root}（`python bin/memory.py init`）"); return 1
        hits = search(root, a.query, a.type, a.agent, a.top)
        for i, h in enumerate(hits, 1):
            tag = h["type"] + (f"/{h['agent']}" if h["agent"] else "")
            print(f"#{i} [{tag}] {h['path']}\n   {h['description']}\n   {h['snip'].strip()}")
        if not hits:
            cond = ", ".join(x for x in (a.type and f"type={a.type}", a.agent and f"agent={a.agent}") if x) or "絞込なし"
            print(f"該当なし（条件: {cond}）。結論は「今回の条件では未発見」であって「無い」ではない。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
