"""Mock LLM 的 Agent 循环：nudge / range-only / 阶段切换 / 写代码门禁。"""
from __future__ import annotations

import pytest

from agent import core, llm
from agent.llm import Action


def _act(name: str, args: dict | None = None, tid: str = "tc1") -> Action:
    return Action(name=name, args=args or {}, tool_call_id=tid)


def _finish_schema():
    return {"type": "function", "function": {"name": "finish", "parameters": {}}}


def _tool_schema(name: str):
    return {"type": "function", "function": {"name": name, "parameters": {}}}


@pytest.fixture
def mock_chat(monkeypatch):
    queue: list[list[Action]] = []

    def feed(*batches: list[Action]):
        queue.extend(batches)

    def _chat(messages, schemas):
        if not queue:
            return []
        return queue.pop(0)

    monkeypatch.setattr(llm, "chat", _chat)
    return feed


@pytest.fixture
def mock_dispatch(monkeypatch):
    def _dispatch(name, args):
        return f"OK: {name}"

    monkeypatch.setattr("agent.tools.dispatch", _dispatch)
    monkeypatch.setattr(core.tools, "dispatch", _dispatch)
    return _dispatch


class TestNudge:
    def test_nudge_then_finish(self, mock_chat, mock_dispatch):
        mock_chat([], [_act("finish", {"summary": "done"})])
        events = []
        out = core.run(
            "task",
            max_steps=10,
            verbose=False,
            system_prompt="sys",
            tool_schemas=[_finish_schema(), _tool_schema("write_range")],
            on_event=lambda *a: events.append(a),
        )
        assert out == "done"
        assert any(e[1] == "nudge" for e in events)

    def test_too_many_nudges_gives_up(self, mock_chat):
        mock_chat([], [], [], [])
        out = core.run(
            "task",
            max_steps=10,
            verbose=False,
            system_prompt="sys",
            tool_schemas=[_finish_schema()],
        )
        assert "预算用尽" in out


class TestRangeOnly:
    def test_write_range_auto_finish(self, mock_chat, mock_dispatch):
        mock_chat([_act("write_range", {"content": '{"count":15}'})])
        out = core.run(
            "写 range",
            max_steps=5,
            verbose=False,
            system_prompt="sys",
            tool_schemas=[_tool_schema("write_range"), _finish_schema()],
        )
        assert "range.json" in out


    def test_write_range_retry_after_error(self, mock_chat, monkeypatch):
        n = {"i": 0}

        def _dispatch(name, args):
            n["i"] += 1
            if n["i"] == 1:
                return "ERROR: range.json 校验失败:\n- count too small"
            return "OK: wrote range.json"

        monkeypatch.setattr(core.tools, "dispatch", _dispatch)
        monkeypatch.setattr("agent.tools.dispatch", _dispatch)
        mock_chat(
            [_act("write_range", {"content": "{}"}, "t1")],
            [_act("write_range", {"content": '{"count":15}'}, "t2")],
        )
        out = core.run(
            "写 range",
            max_steps=5,
            verbose=False,
            system_prompt="sys",
            tool_schemas=[_tool_schema("write_range"), _finish_schema()],
        )
        assert "range.json" in out
        assert n["i"] == 2

    def test_write_range_same_step_only_once(self, mock_chat, monkeypatch):
        n = {"i": 0}

        def _dispatch(name, args):
            n["i"] += 1
            return "OK: wrote range.json"

        monkeypatch.setattr(core.tools, "dispatch", _dispatch)
        monkeypatch.setattr("agent.tools.dispatch", _dispatch)
        events = []
        mock_chat([
            _act("write_range", {"content": '{"count":15}'}, "t1"),
            _act("write_range", {"content": '{"count":20}'}, "t2"),
        ])
        out = core.run(
            "写 range",
            max_steps=5,
            verbose=False,
            system_prompt="sys",
            tool_schemas=[_tool_schema("write_range"), _finish_schema()],
            on_event=lambda *a: events.append(a),
        )
        assert "range.json" in out
        assert n["i"] == 1
        blocked = [e for e in events if e[1] == "write_range" and isinstance(e[3], str) and "同轮连写" in e[3]]
        assert blocked


class TestStageSwitch:
    def test_switch_updates_prompt_and_schemas(self):
        messages = [{"role": "system", "content": "RANGE"}]
        stage_prompts = {"range": "RANGE", "gen": "GEN"}
        stage_schemas = {
            "range": [_tool_schema("write_range")],
            "gen": [_tool_schema("write_gen"), _finish_schema()],
        }
        new_stage, new_schemas = core._switch_stage(
            messages,
            "range",
            "gen",
            stage_prompts,
            stage_tool_schemas=stage_schemas,
            current_schemas=stage_schemas["range"],
            verbose=False,
            step=1,
        )
        assert new_stage == "gen"
        assert messages[0]["content"] == "GEN"
        assert any(m.get("role") == "user" for m in messages)
        names = {(s.get("function") or {}).get("name") for s in new_schemas}
        assert "write_gen" in names


class TestWriteCheckDiscipline:
    def test_rejects_write_after_self_check_ok(self, mock_chat, monkeypatch):
        def _dispatch(name, args):
            if name == "run_self_check":
                return "OK: self_check passed"
            return f"OK: {name}"

        monkeypatch.setattr(core.tools, "dispatch", _dispatch)
        monkeypatch.setattr("agent.tools.dispatch", _dispatch)

        mock_chat(
            [_act("run_self_check", {})],
            [_act("write_gen", {"content": "x"})],
            [_act("finish", {"summary": "done"})],
        )
        events = []
        out = core.run(
            "task",
            max_steps=10,
            verbose=False,
            system_prompt="sys",
            write_check_discipline=True,
            tool_schemas=[
                _tool_schema("write_gen"),
                _tool_schema("write_validate"),
                _tool_schema("run_self_check"),
                _finish_schema(),
            ],
            on_event=lambda *a: events.append(a),
        )
        assert out == "done"
        write_events = [e for e in events if e[1] == "write_gen"]
        assert write_events
        assert any(isinstance(e[3], str) and "禁止" in e[3] for e in write_events)


class TestTokenBudgetInLoop:
    def test_budget_stops_loop(self, monkeypatch):
        def _boom(messages, schemas):
            raise llm.TokenBudgetExceeded("已用 100 tokens，达到上限")

        monkeypatch.setattr(llm, "chat", _boom)
        out = core.run(
            "task",
            max_steps=3,
            verbose=False,
            system_prompt="sys",
            tool_schemas=[_finish_schema()],
        )
        assert "预算用尽" in out
