"""agent/tools 纯逻辑参数校验（不编译）。"""
from __future__ import annotations

from agent.tools import dispatch


def test_write_gen_missing_content():
    out = dispatch("write_gen", {})
    assert out.startswith("ERROR")
    assert "content" in out.lower() or "参数" in out


def test_write_gen_parse_failed_flag():
    out = dispatch(
        "write_gen",
        {"_args_parse_failed": True, "_raw_args_preview": "{bad"},
    )
    assert out.startswith("ERROR")


def test_unknown_tool():
    out = dispatch("not_a_real_tool", {})
    assert "ERROR" in out or "未知" in out or "unknown" in out.lower()


def test_rnd_next_three_arg_numeric_rejected():
    from agent.tools.write import _check_rnd_next_misuse

    bad = 'double x = rnd.next(0, 100, 1);\ndouble y = rnd.next(1, 10, 1);'
    err = _check_rnd_next_misuse(bad)
    assert err and err.startswith("ERROR")
    assert "三参数" in err or "decimals" in err
    assert "rnd.next(0, 100, 1)" in err or "0, 100, 1" in err


def test_rnd_next_two_arg_and_format_ok():
    from agent.tools.write import _check_rnd_next_misuse

    ok = """
    int a = rnd.next(1, 100);
    double b = rnd.next(0.0, 100.0);
    string s = rnd.next("[a-z]{5}");
    string t = rnd.next("%d", 3);  // format + varargs, first is string
    """
    assert _check_rnd_next_misuse(ok) is None


def test_rnd_next_nested_parens_two_arg_ok():
    from agent.tools.write import _check_rnd_next_misuse

    ok = "int x = rnd.next(0, min(n, 100));"
    assert _check_rnd_next_misuse(ok) is None


def test_pair_artifact_idle_gate_blocks_rewrite_gen(tmp_path, monkeypatch):
    """gen.exe 已在、缺 validator 时禁止再 write_gen。"""
    from agent.tools import write as write_mod

    monkeypatch.setattr(write_mod, "_wd", lambda: tmp_path)
    gen_name = write_mod._exe("gen")
    (tmp_path / gen_name).write_bytes(b"x")
    err = write_mod._pair_artifact_idle_gate("gen")
    assert err and err.startswith("ERROR")
    assert "禁止再 write_gen" in err
    assert write_mod._pair_artifact_idle_gate("validator") is None


def test_pair_artifact_idle_gate_both_missing_ok(tmp_path, monkeypatch):
    from agent.tools import write as write_mod

    monkeypatch.setattr(write_mod, "_wd", lambda: tmp_path)
    assert write_mod._pair_artifact_idle_gate("gen") is None
    assert write_mod._pair_artifact_idle_gate("validator") is None
