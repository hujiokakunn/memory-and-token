# -*- coding: utf-8 -*-
"""ハーネスの回帰テスト。実PCの ~/.claude と記憶には触れない（一時ディレクトリで完結）。"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.t = Path(self.tmp.name)
        self.mem = self.t / "mem"
        os.environ["HARNESS_MEMORY_DIR"] = str(self.mem)
        import _common
        self._state = _common.state_dir
        _common.state_dir = lambda cfg=None: (self.t / "state").mkdir(exist_ok=True) or self.t / "state"
        import memory
        memory.state_dir = _common.state_dir
        self.m = memory

    def tearDown(self):
        import _common
        _common.state_dir = self._state
        os.environ.pop("HARNESS_MEMORY_DIR", None)
        self.tmp.cleanup()

    def run_cli(self, *args) -> tuple[int, str]:
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

    def test_search_sees_new_note_without_manual_build(self):
        self.run_cli("add", "a-note", "--type", "project", "--description", "最初の記憶")
        self.run_cli("search", "最初の記憶")
        self.run_cli("add", "b-note", "--type", "project", "--description", "二番目の記憶です")
        os.utime(self.mem / "b-note.md", (9e9, 9e9))
        _, out = self.run_cli("search", "二番目の記憶")
        self.assertIn("b-note", out)

    def test_lint_catches_bom_and_leading_blank(self):
        self.mem.mkdir(parents=True)
        (self.mem / "bom-note.md").write_bytes(b"\xef\xbb\xbf---\nname: bom-note\n---\nx")
        (self.mem / "blank-note.md").write_text("\n---\nname: blank-note\n---\nx", encoding="utf-8")
        rc, out = self.run_cli("lint")
        self.assertEqual(rc, 1)
        self.assertIn("BOM", out)
        self.assertIn("blank-note", out)


class InstallTest(unittest.TestCase):
    def test_install_idempotent_keeps_foreign_hooks_and_uninstalls(self):
        import install
        with tempfile.TemporaryDirectory() as d:
            cdir = Path(d)
            foreign = {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "echo mine"}]}]}}
            (cdir / "settings.json").write_text(json.dumps(foreign), encoding="utf-8")
            (cdir / "CLAUDE.md").write_text("既存の行", encoding="utf-8")
            os.environ["HARNESS_MEMORY_DIR"] = str(cdir / "mem")
            try:
                with redirect_stdout(io.StringIO()):
                    install.main(["--claude-dir", d]); install.main(["--claude-dir", d])
                s = json.loads((cdir / "settings.json").read_text(encoding="utf-8"))
                cmds = [h["command"] for g in s["hooks"]["SessionStart"] for h in g["hooks"]]
                self.assertEqual(sum("session_start.py" in c for c in cmds), 1)
                self.assertIn("echo mine", cmds)
                md = (cdir / "CLAUDE.md").read_text(encoding="utf-8")
                self.assertEqual(md.count("ENTRY.md"), 1)
                self.assertTrue(md.startswith("既存の行\n"))
                with redirect_stdout(io.StringIO()):
                    install.main(["--claude-dir", d, "--uninstall"])
                s = json.loads((cdir / "settings.json").read_text(encoding="utf-8"))
                self.assertEqual(s, foreign)
                self.assertEqual((cdir / "CLAUDE.md").read_text(encoding="utf-8"), "既存の行\n")
            finally:
                os.environ.pop("HARNESS_MEMORY_DIR", None)


class HookTest(unittest.TestCase):
    def test_session_start_never_fails_and_injects_index(self):
        with tempfile.TemporaryDirectory() as d:
            mem = Path(d) / "mem"; mem.mkdir()
            (mem / "MEMORY.md").write_text("# MEMORY\n- [x](x.md) — テスト索引\n", encoding="utf-8")
            env = dict(os.environ, HARNESS_MEMORY_DIR=str(mem), HOME=d, USERPROFILE=d, PYTHONIOENCODING="utf-8")
            r = subprocess.run([sys.executable, str(ROOT / "hooks" / "session_start.py")],
                               capture_output=True, text=True, encoding="utf-8", env=env, cwd=d)
            self.assertEqual(r.returncode, 0)
            self.assertIn("テスト索引", r.stdout)
            self.assertNotIn("失敗", r.stdout)


if __name__ == "__main__":
    unittest.main()
