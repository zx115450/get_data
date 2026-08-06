"""agent_trace.jsonl 失败模式分析。"""
from __future__ import annotations

import json
from pathlib import Path

from agent.trace_analyze import (
    analyze_traces,
    format_report,
    load_events,
    report_to_dict,
)


def _write_trace(job_dir: Path, rows: list[dict]) -> Path:
    job_dir.mkdir(parents=True, exist_ok=True)
    path = job_dir / "agent_trace.jsonl"
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
        encoding="utf-8",
    )
    return path


def test_analyze_tool_fails_and_nudges(tmp_path: Path):
    j1 = tmp_path / "job_a"
    _write_trace(
        j1,
        [
            {
                "ts": "2026-08-06T12:00:00",
                "job": "job_a",
                "step": 1,
                "tool": "write_gen",
                "args_size": 100,
                "result": "ERROR: compile failed",
                "tokens_prompt": 100,
                "tokens_completion": 50,
                "tokens_total": 150,
            },
            {
                "ts": "2026-08-06T12:00:01",
                "job": "job_a",
                "step": 2,
                "tool": "nudge",
                "args_size": 0,
                "result": "please call tools",
                "tokens_prompt": 200,
                "tokens_completion": 60,
                "tokens_total": 260,
            },
            {
                "ts": "2026-08-06T12:00:02",
                "job": "job_a",
                "step": 3,
                "tool": "nudge",
                "args_size": 0,
                "result": "please call tools",
                "tokens_prompt": 300,
                "tokens_completion": 70,
                "tokens_total": 370,
            },
            {
                "ts": "2026-08-06T12:00:03",
                "job": "job_a",
                "step": 4,
                "tool": "write_gen",
                "args_size": 120,
                "result": "OK",
                "tokens_prompt": 400,
                "tokens_completion": 80,
                "tokens_total": 480,
            },
        ],
    )
    (j1 / "meta.json").write_text(
        json.dumps({"problem_type": "tree"}), encoding="utf-8"
    )

    j2 = tmp_path / "job_b"
    _write_trace(
        j2,
        [
            {
                "ts": "2026-08-06T12:10:00",
                "job": "job_b",
                "step": 1,
                "tool": "finish",
                "args_size": 10,
                "result": "OK",
                "tokens_prompt": 50,
                "tokens_completion": 20,
                "tokens_total": 70,
            },
        ],
    )
    (j2 / "meta.json").write_text(
        json.dumps({"problem_type": "array"}), encoding="utf-8"
    )

    report = analyze_traces([tmp_path])
    assert len(report.jobs) == 2
    assert report.tools["write_gen"].calls == 2
    assert report.tools["write_gen"].fails == 1
    assert abs(report.tools["write_gen"].fail_rate - 0.5) < 1e-9

    job_a = next(j for j in report.jobs if j.job_id == "job_a")
    assert job_a.nudges == 2
    assert job_a.max_nudge_streak == 2
    assert job_a.tokens_total == 480
    assert job_a.problem_type == "tree"

    text = format_report(report, top=5)
    assert "write_gen" in text
    assert "Nudge" in text or "nudge" in text.lower()
    assert "tree" in text

    data = report_to_dict(report)
    assert data["job_count"] == 2
    assert data["tools"]["write_gen"]["fails"] == 1


def test_load_events_skips_bad_lines(tmp_path: Path):
    path = tmp_path / "agent_trace.jsonl"
    path.write_text(
        '{"tool":"finish","result":"OK","job":"x","step":1}\n'
        "not-json\n"
        '{"tool":"nudge","result":"hi","job":"x","step":2}\n',
        encoding="utf-8",
    )
    ev = load_events(path)
    assert len(ev) == 2
