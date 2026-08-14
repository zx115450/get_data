"""写文件类工具：write_* / read_file / finish。"""
import json
import re
from pathlib import Path

from .context import (
    DEFAULT_STACK_BYTES,
    DEFAULT_STACK_MB,
    _compile_cpp,
    _compile_err,
    _content_hash,
    _exe,
    _looks_like_omitted_stub,
    _needs_generator,
    _resolve,
    _wd,
    get_context,
)

# ---- 通用工具 ----
def write_file(path: str, content: str) -> str:
    """把 content 写入 job 目录下的 path。gen/validator/range 请改用专用工具。"""
    name = Path(path).name.lower()
    if name in ("gen.cpp", "gen.py"):
        return write_gen(content)
    if name == "gen_special.cpp":
        return write_special_gen(content)
    if name == "check_special.cpp":
        return write_special_check(content)
    if name in ("validator.cpp", "validate.py"):
        return write_validate(content)
    if name == "range.json":
        return write_range(content)
    p = _resolve(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return f"OK: wrote {len(content)} chars to {p}"


def read_file(path: str) -> str:
    """读取 job 目录下的文件（如 gen.cpp / validator.cpp）。"""
    p = _resolve(path)
    if not p.exists():
        return f"ERROR: {path} not found (resolved: {p})"
    text = p.read_text(encoding="utf-8")
    # 超大文件截断，避免下一轮 413；完整文件仍在磁盘
    if len(text) > 12000:
        return (
            text[:6000]
            + f"\n\n/* ... file has {len(text)} chars, middle omitted ... */\n\n"
            + text[-4000:]
        )
    return text


def finish(summary: str = "") -> str:
    """标记任务完成。"""
    return summary or "done"


# ---- 数据生成专用工具 ----
def write_range(content: str) -> str:
    """把 range.json 写到 work_dir。content 必须是合法 JSON，含 count/constraints/edge_cases。"""
    try:
        data = json.loads(content)
    except json.JSONDecodeError as e:
        return f"ERROR: range.json 不是合法 JSON: {e}"
    if isinstance(data, dict):
        from pipeline.gen_data import normalize_range_json, validate_range_json
        data = normalize_range_json(dict(data))
        errs = validate_range_json(data)
        if errs:
            return "ERROR: range.json 校验失败:\n- " + "\n- ".join(errs)
        content = json.dumps(data, ensure_ascii=False, indent=2)
    p = _wd() / "range.json"
    p.write_text(content, encoding="utf-8")
    return f"OK: wrote range.json ({len(content)} chars)"


_CPP_STRING_LIT_RE = re.compile(
    r'"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\''
)


def _cpp_string_literals(content: str) -> set[str]:
    """提取 C++ 源码中的普通字符串字面量内容（不含引号）。"""
    out: set[str] = set()
    for m in _CPP_STRING_LIT_RE.finditer(content or ""):
        raw = m.group(0)
        if len(raw) < 2:
            continue
        body = raw[1:-1]
        try:
            # 处理常见转义，使 \" 等可与 edge 名比对
            body = (
                body.replace(r"\\", "\0")
                .replace(r"\"", '"')
                .replace(r"\'", "'")
                .replace(r"\n", "\n")
                .replace(r"\t", "\t")
                .replace("\0", "\\")
            )
        except Exception:
            pass
        out.add(body)
    return out


def _check_gen_edge_case_literals(content: str) -> str | None:
    """编译前静态门禁：range.edge_cases 每个名字必须在 gen.cpp 中出现完全一致的字符串字面量。

    返回 ERROR 文案；通过则 None。无 range.json 时跳过。
    """
    p = _wd() / "range.json"
    if not p.is_file():
        return None
    try:
        rj = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    edges = rj.get("edge_cases") or []
    if not isinstance(edges, list) or not edges:
        return None
    names = [e for e in edges if isinstance(e, str) and e.strip()]
    if not names:
        return None
    lits = _cpp_string_literals(content)
    missing: list[str] = []
    hints: list[str] = []
    for name in names:
        if name in lits:
            continue
        missing.append(name)
        if f"edge_{name}" in lits:
            hints.append(
                f'源码有 "edge_{name}"，range 名为 "{name}"——'
                f'请改成 type == "{name}"（禁止自行加 edge_ 前缀）'
            )
        elif name.startswith("edge_") and name[5:] in lits:
            bare = name[5:]
            hints.append(
                f'源码有 "{bare}"，range 名为 "{name}"——'
                f'请改成 type == "{name}"（须与 range 逐字符一致）'
            )
    if not missing:
        return None
    sample = missing[0]
    lines = [
        "ERROR: gen.cpp 未覆盖 range.json 的全部 edge_cases 分支（编译前静态门禁）。",
        f"缺少字面量: {missing}",
        "每个名字必须在 gen 中出现完全一致的字符串，例如：",
        f'  if (type == "{sample}") {{ ... }}',
        "禁止自行加/删 edge_ 前缀；字符串须与 range.json 逐字符相同。",
    ]
    if hints:
        lines.append("可能的误写：")
        lines.extend(f"  - {h}" for h in hints)
    lines.append("请修正后重新 write_gen。")
    return "\n".join(lines)


def write_gen(content: str) -> str:
    """把生成器 C++ 源码写到 work_dir/gen.cpp，用 -I sandbox 编译成 gen(.exe)。"""
    ctx = get_context()
    if _looks_like_omitted_stub(content):
        return (
            "ERROR: content 像是历史摘要，不是完整 gen.cpp。"
            "请重新输出完整 C++ 源码（#include \"testlib.h\" 或 \"generator.h\" + registerGen）。"
            "若需查看上一版，先 read_file(\"gen.cpp\")。"
        )
    gate = _check_gen_edge_case_literals(content)
    if gate:
        return gate
    # 不再做其它代码层面的静态检查：缺失 registerGen / 错误 API 由编译器报错。
    h = _content_hash(content)
    exe_path = ctx.work_dir / _exe("gen")
    if h == ctx.last_gen_hash and exe_path.exists():
        (ctx.work_dir / "gen.cpp").write_text(content, encoding="utf-8")
        return f"OK: gen unchanged (hash={h[:12]}…), skipped recompile"

    (ctx.work_dir / "gen.cpp").write_text(content, encoding="utf-8")
    compile_timeout = 120 if _needs_generator(content) else 60
    rc, out, err = _compile_cpp(
        "gen.cpp", "gen", timeout=compile_timeout, stack_bytes=DEFAULT_STACK_BYTES
    )
    if rc != 0:
        return _compile_err("gen.cpp", rc, out, err)
    ctx.last_gen_hash = h
    return f"OK: wrote & compiled gen ({len(content)} chars, stack={DEFAULT_STACK_MB}MB)"


def write_special_gen(content: str) -> str:
    """把特殊样例生成器 C++ 源码写到 work_dir/gen_special.cpp，编译成 gen_special(.exe)。"""
    ctx = get_context()
    if _looks_like_omitted_stub(content):
        return (
            "ERROR: content 像是历史摘要，不是完整 gen_special.cpp。"
            "请重新输出完整 C++ 源码（#include \"testlib.h\" 或 \"generator.h\" + registerGen）。"
            "若需查看上一版，先 read_file(\"gen_special.cpp\")。"
        )
    # 不再做代码层面的静态检查：缺失 registerGen / 错误 API 由编译器报错。
    h = _content_hash(content)
    exe_path = ctx.work_dir / _exe("gen_special")
    if h == ctx.last_special_gen_hash and exe_path.exists():
        (ctx.work_dir / "gen_special.cpp").write_text(content, encoding="utf-8")
        return f"OK: gen_special unchanged (hash={h[:12]}…), skipped recompile"

    (ctx.work_dir / "gen_special.cpp").write_text(content, encoding="utf-8")
    compile_timeout = 120 if _needs_generator(content) else 60
    rc, out, err = _compile_cpp(
        "gen_special.cpp",
        "gen_special",
        timeout=compile_timeout,
        stack_bytes=DEFAULT_STACK_BYTES,
    )
    if rc != 0:
        return _compile_err("gen_special.cpp", rc, out, err)
    ctx.last_special_gen_hash = h
    return (
        f"OK: wrote & compiled gen_special "
        f"({len(content)} chars, stack={DEFAULT_STACK_MB}MB)"
    )


def write_special_check(content: str) -> str:
    """写入 check_special.cpp：判定特殊样例 must_hold（stdin→exit 0/1）。"""
    ctx = get_context()
    if _looks_like_omitted_stub(content):
        return (
            "ERROR: content 像是摘要，不是完整 check_special.cpp。"
            "请输出完整源码：从 stdin 读入与 gen 同格式的输入，"
            "must_hold 成立 exit 0，否则 exit 1 并在 stderr 写原因。"
        )
    if "main" not in (content or ""):
        return "ERROR: check_special.cpp 必须包含 main"
    banned = re.search(
        r"\b(system|popen|_popen|execve|fork|CreateProcess)\s*\(",
        content or "",
        re.I,
    )
    if banned:
        return "ERROR: check_special 禁止 system/popen/fork 等进程调用"
    h = _content_hash(content)
    exe_path = ctx.work_dir / _exe("check_special")
    if h == ctx.last_special_check_hash and exe_path.exists():
        (ctx.work_dir / "check_special.cpp").write_text(content, encoding="utf-8")
        return f"OK: check_special unchanged (hash={h[:12]}…), skipped recompile"
    (ctx.work_dir / "check_special.cpp").write_text(content, encoding="utf-8")
    rc, out, err = _compile_cpp("check_special.cpp", "check_special", timeout=60)
    if rc != 0:
        return _compile_err("check_special.cpp", rc, out, err)
    ctx.last_special_check_hash = h
    return f"OK: wrote & compiled check_special ({len(content)} chars)"


def write_validate(content: str) -> str:
    """把校验器 C++ 源码写到 work_dir/validator.cpp，用 -I sandbox 编译成 validator(.exe)。"""
    ctx = get_context()
    if _looks_like_omitted_stub(content):
        return (
            "ERROR: content 像是历史摘要，不是完整 validator.cpp。"
            "请重新输出完整 C++ 源码（#include \"testlib.h\" + registerValidation）。"
            "若需查看上一版，先 read_file(\"validator.cpp\")。"
        )
    # 不再做代码层面的静态检查：缺失 registerValidation / 缺 readEof 由编译运行报错。
    h = _content_hash(content)
    exe_path = ctx.work_dir / _exe("validator")
    if h == ctx.last_val_hash and exe_path.exists():
        (ctx.work_dir / "validator.cpp").write_text(content, encoding="utf-8")
        return f"OK: validator unchanged (hash={h[:12]}…), skipped recompile"

    (ctx.work_dir / "validator.cpp").write_text(content, encoding="utf-8")
    rc, out, err = _compile_cpp(
        "validator.cpp", "validator", timeout=60, stack_bytes=DEFAULT_STACK_BYTES
    )
    if rc != 0:
        return _compile_err("validator.cpp", rc, out, err)
    ctx.last_val_hash = h
    return (
        f"OK: wrote & compiled validator "
        f"({len(content)} chars, stack={DEFAULT_STACK_MB}MB)"
    )

