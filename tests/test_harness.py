# -*- coding: utf-8 -*-
"""ハーネスの回帰テスト。実PCの ~/.claude・記憶・旧置き場には触れない（一時ディレクトリで完結）。"""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))
# 検出対象の見本は組み立てて作る（このファイル自体が publish_check に引っかからないように）
FAKE_MAIL = "taro.yamada" + "@" + "gmail.com"
FAKE_WIN_PATH = "C:\\" + "Users\\taro\\Claude"
FAKE_JA_PATH = "C:\\" + "Users\\山田\\Claude"
FAKE_NIX_PATH = "/ho" + "me/taro/x"
FAKE_KEY = "sk-ant-" + "a1B2c3D4e5F6g7H8i9J0k1L2"
FAKE_GHP = "gh" + "p_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"

_SANDBOX = tempfile.TemporaryDirectory()


def setUpModule():
    """全テストの既定の置き場と旧置き場を一時フォルダへ向ける（実データを移動・消去しない）。"""
    sb = Path(_SANDBOX.name)
    os.environ["HARNESS_MEMORY_DIR"] = str(sb / "mem-default")
    os.environ["HARNESS_STATE_DIR"] = str(sb / "state-default")
    import _common
    import install
    import publish_check
    for mod in (_common, install, publish_check):
        mod.LEGACY_MEMORY_DIR = sb / "legacy-mem-absent"
        mod.LEGACY_STATE_DIR = sb / "legacy-state-absent"


def tearDownModule():
    os.environ.pop("HARNESS_MEMORY_DIR", None)
    os.environ.pop("HARNESS_STATE_DIR", None)
    _SANDBOX.cleanup()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.t = Path(self.tmp.name)
        self.mem = self.t / "mem"
        self._env = {k: os.environ.get(k) for k in ("HARNESS_MEMORY_DIR", "HARNESS_STATE_DIR")}
        os.environ["HARNESS_MEMORY_DIR"] = str(self.mem)
        os.environ["HARNESS_STATE_DIR"] = str(self.t / "state")
        import memory
        self.m = memory

    def tearDown(self):
        for k, v in self._env.items():
            os.environ[k] = v
        self.tmp.cleanup()

    def run_cli(self, *args) -> tuple:
        buf = io.StringIO()
        with redirect_stdout(buf):
            try:
                rc = self.m.main(list(args))
            except SystemExit as e:
                rc = e.code if isinstance(e.code, int) else 1
        return rc, buf.getvalue()


class MemoryTest(Base):
    def test_add_search_lint_japanese(self):
        self.run_cli("init")
        rc, _ = self.run_cli("add", "prefer-japanese", "--type", "feedback",
                             "--description", "全出力を日本語にする", "--global", "--body", "コミットも日本語で書く")
        self.assertEqual(rc, 0)
        self.assertIn("prefer-japanese.md", (self.mem / "MEMORY.md").read_text(encoding="utf-8"))
        for q in ("日本語", "コミットも日本語"):  # 2文字（LIKE経路）と3文字以上（FTS経路）
            rc, out = self.run_cli("search", q, "--type", "feedback")
            self.assertIn("prefer-japanese", out, q)
            self.assertIn("データ", out)
        rc, out = self.run_cli("search", "日本語", "--type", "project")
        self.assertIn("未発見", out)
        self.assertEqual(self.run_cli("lint")[0], 0)

    def test_agent_layer_filter(self):
        self.run_cli("add", "qa-tip", "--type", "feedback", "--description", "境界値を先に試す", "--agent", "tester")
        _, out = self.run_cli("search", "境界値", "--agent", "tester")
        self.assertIn("qa-tip", out)
        _, out = self.run_cli("search", "境界値", "--agent", "other")
        self.assertNotIn("qa-tip", out)
        self.assertEqual(self.run_cli("lint")[0], 0)

    def test_agent_is_taken_from_path_not_frontmatter(self):
        self.mem.mkdir(parents=True)
        (self.mem / "fake-agent.md").write_text(
            "---\nname: fake-agent\ndescription: 偽装された役の記憶\nmetadata:\n  type: feedback\n  agent: tester\n---\nx\n",
            encoding="utf-8")
        _, out = self.run_cli("search", "偽装された役", "--agent", "tester")
        self.assertNotIn("fake-agent", out)
        rc, out = self.run_cli("lint")
        self.assertEqual(rc, 1)
        self.assertIn("フォルダと一致", out)

    def test_search_sees_new_note_without_manual_build(self):
        self.run_cli("add", "a-note", "--type", "project", "--description", "最初の記憶")
        self.run_cli("search", "最初の記憶")
        self.run_cli("add", "b-note", "--type", "project", "--description", "二番目の記憶です")
        _, out = self.run_cli("search", "二番目の記憶")
        self.assertIn("b-note", out)

    def test_deleted_note_disappears_from_search(self):
        self.run_cli("add", "alpha-note", "--type", "project", "--description", "アルファ記録の検証用")
        self.assertIn("alpha-note", self.run_cli("search", "アルファ記録")[1])
        (self.mem / "alpha-note.md").unlink()
        _, out = self.run_cli("search", "アルファ記録")
        self.assertNotIn("alpha-note", out)
        self.assertIn("未発見", out)

    def test_synced_note_with_old_mtime_is_indexed(self):
        """Drive等の同期は元の更新時刻を保つ。索引より古い時刻で届いても引けること。"""
        self.run_cli("add", "first-note", "--type", "project", "--description", "先に作った記憶")
        self.run_cli("search", "先に作った記憶")  # 索引を作る
        p = self.mem / "beta-note.md"
        p.write_text("---\nname: beta-note\ndescription: ベータ同期由来の記憶\nmetadata:\n  type: project\n---\nx\n",
                     encoding="utf-8")
        os.utime(p, (946684800, 946684800))  # 2000-01-01
        _, out = self.run_cli("search", "ベータ同期")
        self.assertIn("beta-note", out)

    def test_edited_note_is_reindexed(self):
        self.run_cli("add", "edit-note", "--type", "project", "--description", "編集前の説明文")
        self.run_cli("search", "編集前の説明文")
        p = self.mem / "edit-note.md"
        st = p.stat()
        p.write_text(p.read_text(encoding="utf-8").replace("編集前の説明文", "編集後の説明文です"), encoding="utf-8")
        os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))  # 同じ時刻のまま中身だけ変わる場合もサイズで気づく
        self.assertIn("edit-note", self.run_cli("search", "編集後の説明文")[1])

    def test_lint_catches_bom_and_leading_blank(self):
        self.mem.mkdir(parents=True)
        (self.mem / "bom-note.md").write_bytes(b"\xef\xbb\xbf---\nname: bom-note\n---\nx")
        (self.mem / "blank-note.md").write_text("\n---\nname: blank-note\n---\nx", encoding="utf-8")
        rc, out = self.run_cli("lint")
        self.assertEqual(rc, 1)
        self.assertIn("BOM", out)
        self.assertIn("blank-note", out)

    def test_like_search_treats_percent_literally(self):
        self.run_cli("add", "pct-note", "--type", "project", "--description", "達成率 50%")
        self.run_cli("add", "other-note", "--type", "project", "--description", "関係ない記憶")
        _, out = self.run_cli("search", "%")
        self.assertIn("pct-note", out)
        self.assertNotIn("other-note", out)

    def test_global_add_after_index_without_trailing_newline(self):
        self.mem.mkdir(parents=True)
        (self.mem / "MEMORY.md").write_text("# MEMORY\n- [x](x.md) — 末尾改行なし", encoding="utf-8")
        self.run_cli("add", "next-note", "--type", "user", "--description", "次の行", "--global")
        lines = (self.mem / "MEMORY.md").read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[-1], "- [next-note](next-note.md) — 次の行")


class MemorySafetyTest(Base):
    def test_agent_name_cannot_escape_memory_dir(self):
        for bad in ("../../evil", "abc\n"):
            rc, _ = self.run_cli("add", "x-note", "--type", "feedback", "--description", "d", "--agent", bad)
            self.assertNotEqual(rc, 0, repr(bad))
        self.assertFalse((self.t / "evil").exists())

    def test_name_with_trailing_newline_rejected(self):
        rc, _ = self.run_cli("add", "abc\n", "--type", "feedback", "--description", "d")
        self.assertNotEqual(rc, 0)

    def test_description_line_breaks_rejected(self):
        for i, sep in enumerate(("\n", "\r", "\u2028", "\u0085", "\x0b")):
            name = f"nl-note-{i}"
            rc, _ = self.run_cli("add", name, "--type", "feedback", "--description", f"a{sep}agent: tester", "--global")
            self.assertNotEqual(rc, 0, repr(sep))
            self.assertFalse((self.mem / f"{name}.md").exists())

    def test_description_length_limited(self):
        rc, _ = self.run_cli("add", "long-note", "--type", "feedback", "--description", "あ" * 201)
        self.assertNotEqual(rc, 0)

    def test_secret_rejected_on_add_and_flagged_by_lint_even_in_broken_files(self):
        rc, _ = self.run_cli("add", "key-note", "--type", "reference", "--description", "鍵", "--body", FAKE_KEY)
        self.assertNotEqual(rc, 0)
        self.mem.mkdir(parents=True, exist_ok=True)
        (self.mem / "hand-note.md").write_text(
            f"---\nname: hand-note\ndescription: 手書き\nmetadata:\n  type: reference\n---\n{FAKE_KEY}\n", encoding="utf-8")
        (self.mem / "bom-key.md").write_bytes(b"\xef\xbb\xbf" + FAKE_GHP.encode())
        (self.mem / "MEMORY.md").write_text(f"- 索引に直書き {FAKE_GHP}\n", encoding="utf-8")
        rc, out = self.run_cli("lint")
        self.assertEqual(rc, 1)
        self.assertIn("hand-note.md: Anthropic", out)
        self.assertIn("bom-key.md: GitHub", out)
        self.assertIn("MEMORY.md: GitHub", out)
        self.assertNotIn(FAKE_KEY, out)

    def test_review_by_and_superseded_by_are_shown(self):
        self.mem.mkdir(parents=True, exist_ok=True)
        (self.mem / "old-rule.md").write_text(
            "---\nname: old-rule\ndescription: 期限付きの決定ルール\nmetadata:\n  type: project\n"
            "review_by: 2000-01-01\n---\nx\n", encoding="utf-8")
        (self.mem / "null-sup.md").write_text(
            "---\nname: null-sup\ndescription: 置換先が空の決定ルール\nmetadata:\n  type: project\n"
            "superseded_by: null\nreview_by: 2000-13-45\n---\nx\n", encoding="utf-8")
        _, out = self.run_cli("search", "期限付きの決定")
        self.assertIn("期限 2000-01-01 超過", out)
        _, out = self.run_cli("search", "置換先が空の決定")
        self.assertNotIn("置換済み", out)
        self.assertNotIn("超過", out)


class SensitiveTest(unittest.TestCase):
    def test_detects_email_and_user_paths(self):
        from sensitive import find_emails, find_user_paths
        self.assertEqual(find_emails("連絡 " + FAKE_MAIL), [FAKE_MAIL])
        for p in (FAKE_WIN_PATH, FAKE_JA_PATH, FAKE_NIX_PATH, f'"{FAKE_WIN_PATH}"'):
            self.assertTrue(find_user_paths(p), p)

    def test_no_false_positives_on_common_text(self):
        from sensitive import find_emails, find_secrets, find_user_paths
        self.assertEqual(find_emails("1+me@users.noreply.github.com noreply@github.com noreply@anthropic.com"), [])
        self.assertTrue(find_emails("alice" + "noreply@anthropic.com"))  # 許可リストは完全一致
        self.assertEqual(find_user_paths(r"C:\Users\<あなた>\.claude C:\Users\Public\x $HOME\Claude"), [])
        self.assertEqual(find_user_paths("https://api.github.com/users/octocat https://example.com/home/pricing"), [])
        self.assertEqual(find_secrets("sk-learn-pipeline-compose-transformer-notes"), [])
        self.assertEqual(find_secrets('password = os.environ["APP_PASSWORD"]'), [])

    def test_detects_more_secret_kinds(self):
        from sensitive import find_secrets
        samples = {
            "JWT": "eyJ" + "hbGciOiJIUzI1NiJ9" + ".eyJ" + "zdWIiOiIxMjM0NTY3ODkwIn0" + ".dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
            "秘密鍵": "-----BEGIN " + "PGP PRIVATE KEY BLOCK-----",
            "URL埋め込みの認証情報": "https://user:" + "s3cret" + "@github.com/x.git",
            "パスワード等の代入": 'password = "' + 'hunter2hunter2"',
        }
        for label, text in samples.items():
            self.assertIn(label, find_secrets(text), label)

    def test_paths_and_tokens_glued_to_japanese_or_urls(self):
        from sensitive import find_secrets, find_user_paths
        win = FAKE_WIN_PATH
        for text in ("場所は" + win, "file:///" + win.replace("\\", "/"), "cd /mnt/c/" + "Users/taro/x",
                     "path:" + win, "a," + win, "<" + win + ">", "パス" + FAKE_NIX_PATH):
            self.assertTrue(find_user_paths(text), text)
        self.assertIn("GitHubトークン", find_secrets("キーは" + FAKE_GHP))

    def test_long_lines_do_not_blow_up(self):
        from sensitive import find_emails, find_secrets, find_user_paths
        blob = "A1b2" * 25_000  # 100KB の英数字連続（base64 等）
        t0 = time.perf_counter()
        find_emails(blob); find_secrets(blob); find_user_paths(blob)
        find_emails("a." * 50_000)
        self.assertLess(time.perf_counter() - t0, 3.0)
        self.assertEqual(find_emails("x" * 9000 + " " + FAKE_MAIL), [FAKE_MAIL])  # 長い行の後ろでも見つける


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


class PublishCheckTest(unittest.TestCase):
    """一時フォルダをハーネスの置き場に見立てて publish_check を実行する。"""

    def setUp(self):
        import publish_check
        self.pc = publish_check
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "h"
        self.root.mkdir()
        self.saved = (publish_check.HARNESS_ROOT, publish_check.LEGACY_MEMORY_DIR, publish_check.LEGACY_STATE_DIR)
        publish_check.HARNESS_ROOT = self.root
        publish_check.LEGACY_MEMORY_DIR = self.root / "data" / "memory"
        publish_check.LEGACY_STATE_DIR = self.root / ".state"

    def tearDown(self):
        self.pc.HARNESS_ROOT, self.pc.LEGACY_MEMORY_DIR, self.pc.LEGACY_STATE_DIR = self.saved
        shutil.rmtree(self.tmp.name, ignore_errors=True)  # .git の読み取り専用ファイルも消す
        self.tmp.cleanup()

    def run_pc(self, *args) -> tuple:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = self.pc.main(list(args))
        return rc, buf.getvalue()

    def test_clean_folder_passes(self):
        (self.root / "README.md").write_text("ok", encoding="utf-8")
        rc, out = self.run_pc("--no-history")
        self.assertEqual(rc, 0, out)
        self.assertIn("履歴は未検査", out)  # 飛ばした検査は必ず表示する

    def test_flags_files_git_would_not_see(self):
        (self.root / "data" / "memory").mkdir(parents=True)
        (self.root / "data" / "memory" / "me.md").write_text("学習記録", encoding="utf-8")
        (self.root / "script.ps1").write_text(FAKE_KEY, encoding="utf-8")
        (self.root / "log.jsonl").write_text(json.dumps({"to": FAKE_MAIL}), encoding="utf-8")
        (self.root / "utf16.md").write_text("連絡 " + FAKE_MAIL, encoding="utf-16")
        (self.root / "big.md").write_text("x\n" * 1_200_000 + FAKE_WIN_PATH, encoding="utf-8")
        (self.root / "bin" / "__pycache__").mkdir(parents=True)
        rc, out = self.run_pc("--no-history")
        self.assertEqual(rc, 1)
        for needle in ("個人データが残っている", "script.ps1: Anthropic", "log.jsonl: メール", "utf16.md: メール",
                       "big.md: ユーザー名", "__pycache__"):
            self.assertIn(needle, out, needle)
        self.assertNotIn(FAKE_MAIL, out)  # 報告に原文を出さない

    @unittest.skipIf(shutil.which("git") is None, "git が無い")
    def test_history_message_tag_config_and_reflog(self):
        r = self.root
        git(r, "init", "-q")
        git(r, "config", "user.name", "t"); git(r, "config", "user.email", FAKE_MAIL)  # 事故と同じ状態
        (r / "a.md").write_text(FAKE_GHP, encoding="utf-8")  # 一度コミットして消した秘密
        git(r, "add", "."); git(r, "commit", "-qm", "first\n\nCo-authored-by: x <" + FAKE_MAIL + ">")
        git(r, "config", "user.email", "1+t@users.noreply.github.com")
        (r / "a.md").write_text("clean", encoding="utf-8")
        git(r, "commit", "-qam", "second")
        git(r, "tag", "-a", "v1", "-m", "tag")
        git(r, "remote", "add", "origin", "https://u:" + FAKE_GHP + "@github.com/x/y.git")
        rc, out = self.run_pc()
        self.assertEqual(rc, 1)
        for needle in ("コミット履歴（作成者・メッセージ）: メール", "過去の版: GitHub", ".git/config: GitHub"):
            self.assertIn(needle, out, needle)
        # 到達可能な履歴から消しても reflog に残る
        git(r, "checkout", "-q", "--orphan", "fresh"); git(r, "commit", "-qm", "clean")
        git(r, "branch", "-D", "master" if "master" in subprocess.run(
            ["git", "branch"], cwd=r, capture_output=True, text=True).stdout else "main")
        git(r, "tag", "-d", "v1"); git(r, "remote", "remove", "origin")
        rc, out = self.run_pc()
        self.assertEqual(rc, 1)
        self.assertIn(".git/logs", out)
        self.assertNotIn("コミット履歴（作成者・メッセージ）", out)
        # reflog だけ消しても、gc するまで旧コミットは .git/objects に残る
        git(r, "reflog", "expire", "--expire=now", "--all")
        rc, out = self.run_pc()
        self.assertEqual(rc, 1)
        self.assertIn("参照されない古いオブジェクト", out)
        git(r, "gc", "-q", "--prune=now")
        rc, out = self.run_pc()
        self.assertEqual(rc, 0, out)

    def test_git_failure_is_not_a_pass(self):
        (self.root / ".git").mkdir()
        saved = self.pc.git
        self.pc.git = lambda *a, **k: None  # git 未導入・dubious ownership・タイムアウト
        try:
            rc, out = self.run_pc()
        finally:
            self.pc.git = saved
        self.assertEqual(rc, 1)
        self.assertIn("未検査のまま公開しない", out)

    def test_secret_file_names_are_flagged(self):
        for name in (".env", "id_rsa", "server.pem", "credentials.json"):
            (self.root / name).write_text("x", encoding="utf-8")
        (self.root / ".env.example").write_text("KEY=", encoding="utf-8")
        rc, out = self.run_pc("--no-history")
        self.assertEqual(rc, 1)
        for name in (".env:", "id_rsa:", "server.pem:", "credentials.json:"):
            self.assertIn(name, out)
        self.assertNotIn(".env.example:", out)


class TokenBudgetTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.proj = Path(self.tmp.name) / "proj"
        (self.proj / ".claude" / "rules").mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def measure(self) -> dict:
        """HOME と一時フォルダの上位（実PCの CLAUDE.md）を見ないよう、両方を一時フォルダに閉じ込めて測る。"""
        import token_budget
        from unittest import mock
        cfg = dict(token_budget.load_config(), always_loaded=[])
        with mock.patch("pathlib.Path.home", return_value=Path(self.tmp.name) / "home"):
            files = token_budget.measure(cfg, self.proj, ceiling=self.proj)["files"]
        return {Path(f["path"]).name: f for f in files if Path(f["path"]).resolve().is_relative_to(self.proj.resolve())}

    def test_follows_imports_strips_comments_and_skips_agents_when_claude_md(self):
        p = self.proj
        (p / "CLAUDE.md").write_text("本文\n@docs/extra.md\n`@docs/code.md`\n<!-- @docs/hidden.md " + "x" * 500 + " -->\n",
                                     encoding="utf-8")
        (p / "docs").mkdir()
        for n in ("extra", "code", "hidden"):
            (p / "docs" / f"{n}.md").write_text(n, encoding="utf-8")
        (p / "AGENTS.md").write_text("y" * 1000, encoding="utf-8")
        (p / ".claude" / "rules" / "always.md").write_text("常時\n", encoding="utf-8")
        (p / ".claude" / "rules" / "lazy.md").write_text("---\npaths: src/**\n---\n遅延\n", encoding="utf-8")
        (p / ".claude" / "rules" / "dir.md").mkdir()  # .md という名前のフォルダで落ちない
        names = self.measure()
        self.assertEqual(names["extra.md"]["hop"], 1)
        self.assertNotIn("code.md", names)  # コードスパン内は取込ではない
        self.assertNotIn("hidden.md", names)  # HTML コメント内も取込ではない
        self.assertIn("always.md", names)
        self.assertNotIn("lazy.md", names)
        self.assertNotIn("AGENTS.md", names)
        self.assertLess(names["CLAUDE.md"]["bytes"], 100)

    def test_agents_md_counts_when_no_claude_md_and_lines_are_exact(self):
        (self.proj / "AGENTS.md").write_text("行\n" * 200, encoding="utf-8")
        names = self.measure()
        self.assertEqual(names["AGENTS.md"]["lines"], 200)

    def test_import_depth_is_four_hops(self):
        p = self.proj
        (p / "CLAUDE.md").write_text("@h1.md", encoding="utf-8")
        for i in range(1, 6):
            (p / f"h{i}.md").write_text(f"@h{i + 1}.md", encoding="utf-8")
        names = self.measure()
        self.assertIn("h4.md", names)
        self.assertNotIn("h5.md", names)


class InstallTest(unittest.TestCase):
    def test_install_idempotent_keeps_foreign_hooks_and_uninstalls(self):
        import install
        with tempfile.TemporaryDirectory() as d:
            cdir = Path(d)
            foreign = {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "echo mine"}]}]}}
            (cdir / "settings.json").write_text(json.dumps(foreign), encoding="utf-8")
            (cdir / "CLAUDE.md").write_text("既存の行", encoding="utf-8")
            with redirect_stdout(io.StringIO()):
                install.main(["--claude-dir", d]); install.main(["--claude-dir", d])
            s = json.loads((cdir / "settings.json").read_text(encoding="utf-8"))
            cmds = [h["command"] for g in s["hooks"]["SessionStart"] for h in g["hooks"]]
            self.assertEqual(sum("session_start.py" in c for c in cmds), 1)
            self.assertIn("echo mine", cmds)
            md = (cdir / "CLAUDE.md").read_text(encoding="utf-8")
            self.assertEqual(md.count("ENTRY.md"), 1)
            self.assertTrue(md.startswith("既存の行\n"))
            self.assertTrue((cdir / "settings.json.bak.orig").exists())
            with redirect_stdout(io.StringIO()):
                install.main(["--claude-dir", d, "--uninstall"])
            s = json.loads((cdir / "settings.json").read_text(encoding="utf-8"))
            self.assertEqual(s, foreign)
            self.assertEqual((cdir / "CLAUDE.md").read_text(encoding="utf-8"), "既存の行\n")

    def test_uninstall_keeps_next_line_if_import_line_was_removed(self):
        import install
        text = "前\n" + install.CLAUDE_MD_MARK + "\n自分の行\n"
        self.assertEqual(install.update_claude_md(text, uninstall=True), "前\n自分の行\n")

    def test_install_accepts_bom_settings_and_writes_without_bom(self):
        import install
        with tempfile.TemporaryDirectory() as d:
            cdir = Path(d)
            (cdir / "settings.json").write_bytes(b'\xef\xbb\xbf{"theme": "dark"}')
            with redirect_stdout(io.StringIO()):
                rc = install.main(["--claude-dir", d])
            self.assertEqual(rc, 0)
            raw = (cdir / "settings.json").read_bytes()
            self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
            s = json.loads(raw.decode("utf-8"))
            self.assertEqual(s["theme"], "dark")
            self.assertIn("SessionStart", s["hooks"])


class MigrateTest(unittest.TestCase):
    def setUp(self):
        import install
        self.install = install
        self.tmp = tempfile.TemporaryDirectory()
        self.t = Path(self.tmp.name)
        self.old = self.t / "repo" / "data" / "memory"
        self.old.mkdir(parents=True)
        (self.old / "a-b.md").write_text("記憶", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def run_mig(self, new: Path, kind: str) -> bool:
        with redirect_stdout(io.StringIO()):
            return self.install.migrate(self.old, new, kind, dry=False)

    def test_moves_memory_out(self):
        new = self.t / "home" / "memory"
        self.assertTrue(self.run_mig(new, "記憶"))
        self.assertEqual((new / "a-b.md").read_text(encoding="utf-8"), "記憶")
        self.assertFalse(self.old.exists())

    def test_memory_conflict_stops_and_keeps_both(self):
        new = self.t / "home" / "memory"
        new.mkdir(parents=True)
        (new / "MEMORY.md").write_text("新", encoding="utf-8")
        self.assertFalse(self.run_mig(new, "記憶"))
        self.assertTrue((self.old / "a-b.md").exists())
        self.assertTrue((new / "MEMORY.md").exists())

    def test_state_conflict_is_kept_under_legacy_folder(self):
        new = self.t / "home" / "state"
        new.mkdir(parents=True)
        (new / "log.jsonl").write_text("新", encoding="utf-8")
        self.assertTrue(self.run_mig(new, "索引・台帳"))
        self.assertFalse(self.old.exists())
        moved = [p for p in new.glob("legacy-*/a-b.md")]
        self.assertEqual(len(moved), 1)

    def test_move_failure_reports_and_keeps_old(self):
        from unittest import mock
        new = self.t / "home" / "memory"
        buf = io.StringIO()
        with mock.patch("shutil.move", side_effect=PermissionError("denied")), redirect_stdout(buf):
            ok = self.install.migrate(self.old, new, "記憶", dry=False)
        self.assertFalse(ok)
        self.assertIn("途中で失敗", buf.getvalue())
        self.assertTrue((self.old / "a-b.md").exists())

    def test_dry_run_changes_nothing(self):
        new = self.t / "home" / "memory"
        with redirect_stdout(io.StringIO()):
            self.assertTrue(self.install.migrate(self.old, new, "記憶", dry=True))
        self.assertTrue((self.old / "a-b.md").exists())
        self.assertFalse(new.exists())


class HookTest(unittest.TestCase):
    def test_session_start_never_fails_and_injects_index_inside_markers(self):
        with tempfile.TemporaryDirectory() as d:
            mem = Path(d) / "mem"; mem.mkdir()
            (mem / "MEMORY.md").write_text("# MEMORY\n- [x](x.md) — テスト索引\n", encoding="utf-8")
            env = dict(os.environ, HARNESS_MEMORY_DIR=str(mem), HARNESS_STATE_DIR=str(Path(d) / "state"),
                       HOME=d, USERPROFILE=d, PYTHONIOENCODING="utf-8")
            r = subprocess.run([sys.executable, str(ROOT / "hooks" / "session_start.py")],
                               capture_output=True, text=True, encoding="utf-8", env=env, cwd=d)
            self.assertEqual(r.returncode, 0)
            self.assertNotIn("失敗", r.stdout)
            start = r.stdout.index("[データ開始")
            end = r.stdout.index("[データ終了")
            self.assertIn("テスト索引", r.stdout[start:end])
            self.assertLess(len(r.stdout), 10_000)


if __name__ == "__main__":
    unittest.main()
