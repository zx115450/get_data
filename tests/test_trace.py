"""agent/trace JSONL 追踪。"""
from __future__ import annotations

import json
from pathlib import Path

from agent import trace
from agent.llm import reset_token_usage


def test_log_event_writes_jsonl(tmp_path: Path):
    reset_token_usage()
    path = tmp_path / "agent_trace.jsonl"
    trace.set_trace_context(job_id="job1", path=path)
    try:
        trace.log_event(
            step=1,
            tool="write_gen",
            result="OK: wrote",
            args_size=100,
        )
        trace.log_event(step=2, tool="nudge", result="please call tools")
    finally:
        trace.clear_trace_context()

    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    row = json.loads(lines[0])
    assert row["job"] == "job1"
    assert row["tool"] == "write_gen"
    assert row["result"] == "OK"
    assert row["args_size"] == 100
    assert "tokens_total" in row


def test_no_path_is_noop():
    trace.clear_trace_context()
    trace.log_event(step=1, tool="x", result="y")  # 不应抛错
