"""agent.errors：错误完整落盘。"""
from __future__ import annotations

import json
from pathlib import Path

from agent.errors import (
    format_error_for_progress,
    is_error_text,
    persist_error,
)


class TestPersistError:
    def test_writes_last_and_step_file(self, tmp_path: Path):
        text = "ERROR: gen.cpp 编译失败 (rc=1):\ngen.cpp:1:1: error: 'foo' was not declared"
        rel = persist_error(tmp_path, text, tool="write_gen", step=3)
        assert rel == "errors/step003_write_gen.txt"
        assert (tmp_path / "errors" / "last_error.txt").read_text(encoding="utf-8") == text
        assert (tmp_path / rel).read_text(encoding="utf-8") == text
        lines = (tmp_path / "errors" / "errors.jsonl").read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        row = json.loads(lines[0])
        assert row["text"] == text
        assert row["tool"] == "write_gen"

    def test_self_check_compat_path(self, tmp_path: Path):
        text = "ERROR: self_check failed\nFAIL case1"
        persist_error(tmp_path, text, tool="run_self_check", kind="self_check")
        assert (tmp_path / "self_check_last_fail.txt").read_text(encoding="utf-8") == text

    def test_is_error_text(self):
        assert is_error_text("ERROR: x")
        assert is_error_text("ok\nFAIL bar")
        assert not is_error_text("OK: wrote")

    def test_format_keeps_short(self):
        s = "ERROR: short"
        assert format_error_for_progress(s) == s
