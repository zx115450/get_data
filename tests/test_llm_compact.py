"""agent/llm.py：compact_messages 压缩行为。"""
from __future__ import annotations

import json

from agent.llm import compact_messages


def _write_tc(name: str, content: str, tc_id: str = "1"):
    return {
        "id": tc_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps({"content": content}, ensure_ascii=False),
        },
    }


def test_compact_stubs_large_write_gen_args():
    big = "int main(){" + ("x" * 800) + "}"
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "task"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [_write_tc("write_gen", big)],
        },
        {"role": "tool", "tool_call_id": "1", "content": "OK: wrote gen.cpp"},
    ]
    compact_messages(messages)
    args = json.loads(messages[2]["tool_calls"][0]["function"]["arguments"])
    assert "__OMITTED_SOURCE__" in args["content"]
    assert "gen.cpp" in args["content"]


def test_compact_truncates_tool_result_by_limit():
    messages = [
        {"role": "system", "content": "sys"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "r1",
                    "type": "function",
                    "function": {"name": "run_gen", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "r1", "content": "line\n" * 500},
    ]
    compact_messages(messages)
    # run_gen limit=400
    assert len(messages[2]["content"]) <= 450


def test_compact_write_tool_result_to_stub():
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [_write_tc("write_validate", "code", "v1")]},
        {
            "role": "tool",
            "tool_call_id": "v1",
            "content": "OK: wrote validator.cpp\n" + ("detail\n" * 50),
        },
    ]
    compact_messages(messages)
    assert messages[1]["content"].startswith("[write_validate]")
