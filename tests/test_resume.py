"""runners/resume：SHA256 指纹与同题复用判定。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from runners.resume import (
    RESUME_LEVEL_EMPTY,
    RESUME_LEVEL_FULL,
    RESUME_LEVEL_GEN_VAL,
    RESUME_LEVEL_RANGE,
    RESUME_VERSION,
    classify_resume_dir,
    find_matching_parent_job,
    load_parent_failure_context,
    text_hash,
)


class TestTextHash:
    def test_stable_sha256(self):
        assert text_hash("abc") == hashlib.sha256(b"abc").hexdigest()

    def test_differs(self):
        assert text_hash("a") != text_hash("b")


class TestClassifyResumeDir:
    def test_empty(self, tmp_path: Path):
        info = classify_resume_dir(tmp_path)
        assert info["resume_level"] == RESUME_LEVEL_EMPTY
        assert info["has_range"] is False

    def test_range_only(self, tmp_path: Path):
        (tmp_path / "range.json").write_text("{}", encoding="utf-8")
        info = classify_resume_dir(tmp_path)
        assert info["resume_level"] == RESUME_LEVEL_RANGE
        assert info["has_range"] is True

    def test_gen_val(self, tmp_path: Path, monkeypatch):
        (tmp_path / "range.json").write_text("{}", encoding="utf-8")
        (tmp_path / "gen.cpp").write_text("x", encoding="utf-8")
        (tmp_path / "validator.cpp").write_text("y", encoding="utf-8")
        # has_gen_val_at_resume 可能还检查 exe；打补丁保证测到 gen_val 级别
        monkeypatch.setattr(
            "runners.resume.has_gen_val_at_resume",
            lambda d: True,
        )
        info = classify_resume_dir(tmp_path)
        assert info["resume_level"] == RESUME_LEVEL_GEN_VAL
        assert info["has_gen_val"] is True

    def test_full_with_out_good(self, tmp_path: Path, monkeypatch):
        (tmp_path / "range.json").write_text("{}", encoding="utf-8")
        monkeypatch.setattr(
            "runners.resume.has_gen_val_at_resume",
            lambda d: True,
        )
        og = tmp_path / "out.good"
        og.mkdir()
        (og / "1.in").write_text("1\n", encoding="utf-8")
        info = classify_resume_dir(tmp_path)
        assert info["resume_level"] == RESUME_LEVEL_FULL


class TestFailureContext:
    def test_load_valid(self, tmp_path: Path):
        ctx = {
            "v": 1,
            "resume_version": RESUME_VERSION,
            "statement_hash": "s",
            "std_hash": "t",
            "lang": "cpp",
        }
        (tmp_path / "failure_context.json").write_text(
            json.dumps(ctx), encoding="utf-8"
        )
        loaded = load_parent_failure_context(tmp_path)
        assert loaded is not None
        assert loaded["statement_hash"] == "s"

    def test_reject_bad_version(self, tmp_path: Path):
        (tmp_path / "failure_context.json").write_text(
            json.dumps({"v": 99}), encoding="utf-8"
        )
        assert load_parent_failure_context(tmp_path) is None

    def test_find_matching_parent(self, tmp_path: Path, monkeypatch):
        jobs = tmp_path / "jobs"
        parent = jobs / "20260806_120000"
        parent.mkdir(parents=True)
        ctx = {
            "v": 1,
            "resume_version": RESUME_VERSION,
            "statement_hash": "stmt",
            "std_hash": "std",
            "lang": "cpp",
        }
        (parent / "failure_context.json").write_text(
            json.dumps(ctx), encoding="utf-8"
        )
        monkeypatch.setattr("runners.resume.JOBS_DIR", jobs)
        hit = find_matching_parent_job("stmt", "std", "cpp", lookback=5)
        assert hit is not None
        d, loaded = hit
        assert d == parent
        assert loaded["lang"] == "cpp"

    def test_find_no_match(self, tmp_path: Path, monkeypatch):
        jobs = tmp_path / "jobs"
        jobs.mkdir()
        monkeypatch.setattr("runners.resume.JOBS_DIR", jobs)
        assert find_matching_parent_job("x", "y", "cpp") is None
