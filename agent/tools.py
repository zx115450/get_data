"""工具函数集合 + 对应的 schema（喂给 LLM 的工具定义）。

分两类工具：
- 通用：write_file / read_file / finish（hello demo 用）
- 数据生成专用：write_gen / write_validate / run_gen / run_validate / run_std / read_range

数据生成工具需要一个工作目录（work_dir）和标程命令（std_cmd），
由 runner / main.py 在跑 Agent 前通过 set_context() 注入（基于 contextvars，线程隔离）。
"""
import hashlib
import json
import os
import re
import shutil
import sys
from contextvars import ContextVar, Token
from dataclasses import dataclass
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))

from sandbox.run import EXIT_MEMORY, is_stack_overflow, parse_memory_limit_mb, safe_run

# testlib.h / generator.h / 内置 checker 源码（随项目分发）
# 编译时用 -I sandbox，job 目录不再落盘头文件；打包源码时从 sandbox 取头文件。
_SANDBOX = Path(__file__).resolve().parent.parent / "sandbox"
TESTLIB_H = _SANDBOX / "testlib.h"
GENERATOR_H = _SANDBOX / "generator.h"
CHECKER_SRC_DIR = _SANDBOX / "checker"
CHECKER_TEMPLATE_DIR = _SANDBOX / "checker" / "templates"

# 允许的内置 checker 名（对应 sandbox/checker/<name>.cpp）
BUILTIN_CHECKERS = ("lcmp", "wcmp", "rcmp4", "rcmp6", "rcmp9", "yesno")

# 可用的 checker 模板（对应 sandbox/checker/templates/<name>.cpp）
CHECKER_TEMPLATES = (
    "construct_verify",    # 通用构造/方案验证
    "any_of_answers",      # 多解但可推导正确答案条件
    "graph_path",          # 路径/环/walk 验证
    "permutation",         # 排列验证
    "subset",              # 子集/选择验证
    "sequence_property",     # 序列/数组性质验证
    "point_set",           # 点集/几何构造验证
    "matching",            # 匹配/配对方案验证
    "tree_parent",         # 树父节点/边集验证
)

# 题型里通常会用到 generator.h 的集合（供 runner / 提示词参考）
GENERATOR_PROBLEM_TYPES = frozenset({
    "tree", "graph", "geometry", "weighted_tree", "weighted_graph",
    "permutation", "array", "string", "matrix", "number_theory",
})


def _exe(base: str) -> str:
    """Windows 下可执行文件带 .exe，Linux/macOS 不带。"""
    return base + (".exe" if os.name == "nt" else "")


def _pick_gen_exe(typ: str) -> str:
    """根据类型选择生成器二进制：特殊样例用 gen_special（若存在），否则用 gen。"""
    t = (typ or "").strip()
    if (t == "special_samples" or t.startswith("special:")) and (
        _wd() / _exe("gen_special")
    ).exists():
        return "gen_special"
    return "gen"


def _include_flags() -> str:
    """g++ -I 指向公共 sandbox，使 #include \"testlib.h\" / \"generator.h\" 无需拷贝到 job。"""
    inc = _SANDBOX.resolve().as_posix()
    return f'-I"{inc}"'


# Finder：适度加大栈（勿用默认 ~1MB）。不宜过大——Windows Job Object 的
# ProcessMemoryLimit 会计入已提交栈，256MB 栈会严重挤占堆预算导致假 OOM。
FINDER_STACK_BYTES = 32 * 1024 * 1024  # 32MB，迭代搜索足够
FINDER_STACK_MB = FINDER_STACK_BYTES // (1024 * 1024)
FINDER_DEFAULT_TIMEOUT_SEC = 5
FINDER_DEFAULT_MEMORY_MB = 1024
FINDER_MAX_TIMEOUT_SEC = 15
FINDER_MAX_MEMORY_MB = 2048
FINDER_MAX_HITS = 1
FINDER_DIR_NAME = "special_findings"


def _stack_link_flags(stack_bytes: int | None) -> str:
    """链接期扩大栈；Windows 用 --stack，POSIX 尽量用 -z stack-size（失败可降级）。"""
    if not stack_bytes or stack_bytes <= 0:
        return ""
    n = int(stack_bytes)
    if os.name == "nt":
        return f" -Wl,--stack,{n}"
    # GNU gold/lld 支持；GNU ld 可能失败，调用方应准备重试
    return f" -Wl,-z,stack-size={n}"


def _compile_cpp(
    src_name: str,
    out_base: str,
    *,
    timeout: int = 60,
    stack_bytes: int | None = None,
    cwd: Path | None = None,
) -> tuple[int, str, str]:
    """在当前 job 目录（或指定 cwd）下编译单个 C++ 源文件，自动带 sandbox include。"""
    work = cwd if cwd is not None else _wd()
    flags = _stack_link_flags(stack_bytes)
    cmd = (
        f'g++ -O2 -std=c++17 {_include_flags()}{flags} '
        f'{src_name} -o {_exe(out_base)}'
    )
    rc, out, err = safe_run(cmd, timeout=timeout, cwd=str(work))
    # POSIX 上部分链接器不认 stack-size：去掉栈参数重试一次
    if rc != 0 and flags and os.name != "nt" and stack_bytes:
        cmd2 = (
            f'g++ -O2 -std=c++17 {_include_flags()} '
            f'{src_name} -o {_exe(out_base)}'
        )
        rc2, out2, err2 = safe_run(cmd2, timeout=timeout, cwd=str(work))
        if rc2 == 0:
            return rc2, out2, (err2 or "") + "\n(note: stack-size link flag unsupported; runtime RLIMIT_STACK will apply)"
        return rc, out, err
    return rc, out, err


def prewarm_generator_headers() -> None:
    """校验公共头文件存在（兼容旧调用）。不再向 job 目录拷贝。"""
    missing = []
    if not TESTLIB_H.is_file():
        missing.append(str(TESTLIB_H))
    if not GENERATOR_H.is_file():
        missing.append(str(GENERATOR_H))
    if missing:
        raise FileNotFoundError("缺少 sandbox 头文件: " + ", ".join(missing))


def _needs_generator(content: str) -> bool:
    """源码是否引用 ACM-generator。"""
    return "generator.h" in content or "generator::" in content


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


# ---- 运行上下文（contextvars：每线程/协程独立，避免并行 job 串目录）----
@dataclass
class JobContext:
    work_dir: Path
    std_cmd: str = ""
    last_gen_hash: str = ""
    last_val_hash: str = ""
    last_special_gen_hash: str = ""


_job_ctx: ContextVar[JobContext | None] = ContextVar("agent_job_context", default=None)
# 未 set_context 时的兜底（CLI / 单测）；并行 job 必须走 ContextVar
_fallback_ctx = JobContext(work_dir=Path("."))


def get_context() -> JobContext:
    """返回当前线程的 JobContext；未注入时用进程级 fallback。"""
    ctx = _job_ctx.get()
    return ctx if ctx is not None else _fallback_ctx


def _wd() -> Path:
    return get_context().work_dir


def _std() -> str:
    return get_context().std_cmd


def set_context(work_dir: str, std_cmd: str) -> JobContext:
    """注入当前线程的工作目录与标程命令；返回新建的 JobContext。"""
    global _fallback_ctx
    ctx = JobContext(work_dir=Path(work_dir), std_cmd=std_cmd or "")
    ctx.work_dir.mkdir(parents=True, exist_ok=True)
    _job_ctx.set(ctx)
    # 无活跃 ContextVar 的同步路径（如部分脚本）仍可读到最近一次注入
    _fallback_ctx = ctx
    return ctx


def reset_context(token: Token | None = None) -> None:
    """可选：恢复 set 之前的 ContextVar（一般 job 结束不需要）。"""
    if token is not None:
        _job_ctx.reset(token)


def __getattr__(name: str):
    """兼容外部读取 tools.WORK_DIR / tools.STD_CMD。"""
    if name == "WORK_DIR":
        return _wd()
    if name == "STD_CMD":
        return _std()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _resolve(path: str) -> Path:
    """相对路径一律落到当前 job 目录，绝对路径原样。"""
    p = Path(path)
    if p.is_absolute():
        return p
    return _wd() / p


def _compile_err(label: str, rc: int, out: str, err: str) -> str:
    """把编译失败信息整理成可读字符串（避免出现 'None'）。"""
    out = out or ""
    err = err or ""
    detail = (err.strip() or out.strip() or "(编译器无输出，可能是命令未找到或路径错误)")
    return f"ERROR: {label} 编译失败 (rc={rc}):\n{detail}"


def _looks_like_omitted_stub(content: str) -> bool:
    """历史压缩摘要 / 残缺片段，不能当真正源码写入。

    不再按长度拦截（短但完整的 validator/gen 易误判）；
    仅拒绝空内容，或带有对话压缩 stub 标记的文本。
    """
    if not content or not content.strip():
        return True
    markers = (
        "已写入",
        "__OMITTED_SOURCE__",
        "omitted",
        "truncated for context",
        "需要时用 read_file",
        "Do NOT call write",
    )
    return any(m in content for m in markers)


# ---- 通用工具 ----
def write_file(path: str, content: str) -> str:
    """把 content 写入 job 目录下的 path。gen/validator/range 请改用专用工具。"""
    name = Path(path).name.lower()
    if name in ("gen.cpp", "gen.py"):
        return write_gen(content)
    if name == "gen_special.cpp":
        return write_special_gen(content)
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
        json.loads(content)
    except json.JSONDecodeError as e:
        return f"ERROR: range.json 不是合法 JSON: {e}"
    p = _wd() / "range.json"
    p.write_text(content, encoding="utf-8")
    return f"OK: wrote range.json ({len(content)} chars)"


def write_gen(content: str) -> str:
    """把生成器 C++ 源码写到 work_dir/gen.cpp，用 -I sandbox 编译成 gen(.exe)。"""
    ctx = get_context()
    if _looks_like_omitted_stub(content):
        return (
            "ERROR: content 像是历史摘要，不是完整 gen.cpp。"
            "请重新输出完整 C++ 源码（#include \"testlib.h\" 或 \"generator.h\" + registerGen）。"
            "若需查看上一版，先 read_file(\"gen.cpp\")。"
        )
    # 不再做代码层面的静态检查：缺失 registerGen / 错误 API 由编译器报错。
    h = _content_hash(content)
    exe_path = ctx.work_dir / _exe("gen")
    if h == ctx.last_gen_hash and exe_path.exists():
        (ctx.work_dir / "gen.cpp").write_text(content, encoding="utf-8")
        return f"OK: gen unchanged (hash={h[:12]}…), skipped recompile"

    (ctx.work_dir / "gen.cpp").write_text(content, encoding="utf-8")
    compile_timeout = 120 if _needs_generator(content) else 60
    rc, out, err = _compile_cpp("gen.cpp", "gen", timeout=compile_timeout)
    if rc != 0:
        return _compile_err("gen.cpp", rc, out, err)
    ctx.last_gen_hash = h
    return f"OK: wrote & compiled gen ({len(content)} chars)"


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
    rc, out, err = _compile_cpp("gen_special.cpp", "gen_special", timeout=compile_timeout)
    if rc != 0:
        return _compile_err("gen_special.cpp", rc, out, err)
    ctx.last_special_gen_hash = h
    return f"OK: wrote & compiled gen_special ({len(content)} chars)"


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
    rc, out, err = _compile_cpp("validator.cpp", "validator", timeout=60)
    if rc != 0:
        return _compile_err("validator.cpp", rc, out, err)
    ctx.last_val_hash = h
    return f"OK: wrote & compiled validator ({len(content)} chars)"


def ensure_self_check_prereqs(
    skip_recompile: set[str] | frozenset[str] | None = None,
    require_special: bool | None = None,
) -> tuple[bool, str]:
    """自检前确保 gen / validator 可执行文件就绪。

    - 若 exe 已存在：直接通过
    - 若 exe 缺失但对应 .cpp 在磁盘上：尝试用现有源码重编译
      （除非 skip_recompile 含该名字——本步 write_* 刚编译失败时不要用同一份坏源码再编一次）
    - 若源码也不存在 / 跳过重编译：返回缺失项，由调用方提示 Agent 先 write_*

    skip_recompile: 可选 {"gen", "validator", "gen_special"}，表示本步已确认编译失败，禁止重试。
    require_special:
      - None：按 range.json 是否启用特殊样例自动决定
      - False：不要求 gen_special（普通 Gen 阶段）
      - True：若启用特殊样例则必须有 gen_special（SpecialCoder 阶段）

    返回 (ready, message)。
    """
    skip = set(skip_recompile or ())
    notes: list[str] = []
    missing: list[str] = []
    just_failed: list[str] = []

    gen_exe = _wd() / _exe("gen")
    val_exe = _wd() / _exe("validator")
    gen_src = _wd() / "gen.cpp"
    val_src = _wd() / "validator.cpp"

    # 是否启用特殊样例
    rj = _load_range_json()
    special_count = int(rj.get("special_samples_count") or 0)
    range_has_special = special_count > 0 and bool((rj.get("special_samples_desc") or "").strip())
    if require_special is None:
        has_special = range_has_special
    else:
        has_special = bool(require_special) and range_has_special

    if not gen_exe.exists():
        if "gen" in skip:
            missing.append("gen")
            just_failed.append("gen")
        elif gen_src.is_file():
            try:
                content = gen_src.read_text(encoding="utf-8")
            except Exception as e:
                return False, f"读取 gen.cpp 失败: {type(e).__name__}: {e}"
            msg = write_gen(content)
            if not str(msg).startswith("OK"):
                return False, f"尝试重编译 gen.cpp 失败: {msg}"
            notes.append("已从磁盘重编译 gen")
        else:
            missing.append("gen")

    if has_special:
        special_exe = _wd() / _exe("gen_special")
        special_src = _wd() / "gen_special.cpp"
        if not special_exe.exists():
            if "gen_special" in skip:
                missing.append("gen_special")
                just_failed.append("gen_special")
            elif special_src.is_file():
                try:
                    content = special_src.read_text(encoding="utf-8")
                except Exception as e:
                    return False, f"读取 gen_special.cpp 失败: {type(e).__name__}: {e}"
                msg = write_special_gen(content)
                if not str(msg).startswith("OK"):
                    return False, f"尝试重编译 gen_special.cpp 失败: {msg}"
                notes.append("已从磁盘重编译 gen_special")
            else:
                missing.append("gen_special")

    if not val_exe.exists():
        if "validator" in skip:
            missing.append("validator")
            just_failed.append("validator")
        elif val_src.is_file():
            try:
                content = val_src.read_text(encoding="utf-8")
            except Exception as e:
                return False, f"读取 validator.cpp 失败: {type(e).__name__}: {e}"
            msg = write_validate(content)
            if not str(msg).startswith("OK"):
                return False, f"尝试重编译 validator.cpp 失败: {msg}"
            notes.append("已从磁盘重编译 validator")
        else:
            missing.append("validator")

    if missing:
        hints = []
        if "gen" in missing:
            if "gen" in just_failed:
                hints.append("本步 write_gen 已编译失败，请修复 gen.cpp 后重新 write_gen（勿用同一份坏源码空转）")
            else:
                hints.append("请先 write_gen 写出完整可编译的 gen.cpp")
        if "gen_special" in missing:
            if "gen_special" in just_failed:
                hints.append("本步 write_special_gen 已编译失败，请修复 gen_special.cpp 后重新 write_special_gen（勿用同一份坏源码空转）")
            else:
                hints.append("range.json 启用了特殊样例，请先 write_special_gen 写出完整可编译的 gen_special.cpp")
        if "validator" in missing:
            if "validator" in just_failed:
                hints.append(
                    "本步 write_validate 已失败，请修复 validator.cpp 后重新 write_validate"
                    "（以编译/运行报错为准）"
                )
            else:
                hints.append("请先 write_validate 写出完整可编译的 validator.cpp")
        return False, "缺少已编译产物: " + "、".join(missing) + "。" + "；".join(hints)

    return True, ("；".join(notes) if notes else "gen/validator 已就绪")


def write_checker(content: str) -> str:
    """把 special judge 的 C++ 源码写到 work_dir/checker.cpp，用 -I sandbox 编译成 checker(.exe)。"""
    if _looks_like_omitted_stub(content):
        return (
            "ERROR: content 像是历史摘要，不是完整 checker.cpp。"
            "请重新输出完整 C++ 源码（#include \"testlib.h\" + registerTestlibCmd）。"
            "若需查看上一版，先 read_file(\"checker.cpp\")。"
        )
    if "registerTestlibCmd" not in content and "testlib.h" not in content:
        return "ERROR: checker.cpp 必须 #include \"testlib.h\" 并调用 registerTestlibCmd(...)"
    (_wd() / "checker.cpp").write_text(content, encoding="utf-8")
    rc, out, err = _compile_cpp("checker.cpp", "checker", timeout=60)
    if rc != 0:
        return _compile_err("checker.cpp", rc, out, err)
    return f"OK: wrote & compiled checker ({len(content)} chars)"


def use_builtin_checker(name: str) -> str:
    """使用内置 testlib checker（lcmp/wcmp/rcmp4/rcmp6/rcmp9/yesno），编译为 checker(.exe)。

    适用于答案唯一、只需按行/词/浮点/YesNo 比较的题目；答案不唯一时请改用 write_checker。
    """
    name = (name or "").strip().lower()
    if name not in BUILTIN_CHECKERS:
        return (
            f"ERROR: 未知内置 checker {name!r}，"
            f"可选: {', '.join(BUILTIN_CHECKERS)}"
        )
    src = CHECKER_SRC_DIR / f"{name}.cpp"
    if not src.exists():
        return f"ERROR: 缺少内置 checker 源码 {src}"
    dst_src = _wd() / "checker.cpp"
    shutil.copyfile(src, dst_src)
    # 在源码顶部标注来源，方便打包识别
    note = f"// builtin checker: {name} (from sandbox/checker/{name}.cpp)\n"
    body = dst_src.read_text(encoding="utf-8")
    if not body.lstrip().startswith("// builtin checker:"):
        dst_src.write_text(note + body, encoding="utf-8")
    rc, out, err = _compile_cpp("checker.cpp", "checker", timeout=60)
    if rc != 0:
        return _compile_err(f"builtin checker {name}", rc, out, err)
    return f"OK: installed builtin checker {name} -> checker"


def use_checker_template(name: str) -> str:
    """使用 checker 模板（目前支持 construct_verify），生成骨架 checker.cpp 并编译。

    模板会提供一个可编译通过的骨架，模型后续用 read_file + write_checker 替换 TODO 部分。
    """
    name = (name or "").strip().lower()
    if name not in CHECKER_TEMPLATES:
        return (
            f"ERROR: 未知 checker 模板 {name!r}，"
            f"可选: {', '.join(CHECKER_TEMPLATES)}"
        )
    src = CHECKER_TEMPLATE_DIR / f"{name}.cpp"
    if not src.exists():
        return f"ERROR: 缺少 checker 模板源码 {src}"
    dst = _wd() / "checker.cpp"
    shutil.copyfile(src, dst)
    rc, out, err = _compile_cpp("checker.cpp", "checker", timeout=60)
    if rc != 0:
        return _compile_err(f"checker template {name}", rc, out, err)
    return f"OK: installed checker template {name} -> checker.cpp ({len(src.read_text(encoding='utf-8'))} chars)"


def run_checker(input_text: str, output_text: str, answer_text: str) -> str:
    """运行编译好的 checker，判定 output_text 相对 answer_text 是否正确。

    三个参数分别对应 inf / ouf / ans 的内容。会写临时文件、调用 checker、清理临时文件。
    返回包含退出码、stdout、stderr 的摘要。
    """
    exe = _wd() / _exe("checker")
    if not exe.exists():
        return "ERROR: checker 未编译，请先 write_checker 或 use_builtin_checker / use_checker_template"

    inf = _wd() / "_checker_run_inf.txt"
    ouf = _wd() / "_checker_run_ouf.txt"
    ans = _wd() / "_checker_run_ans.txt"
    try:
        inf.write_text(input_text, encoding="utf-8")
        ouf.write_text(output_text, encoding="utf-8")
        ans.write_text(answer_text, encoding="utf-8")

        rc, out, err = safe_run(
            f"{_exe('checker')} {inf} {ouf} {ans}",
            timeout=10,
            cwd=str(_wd()),
        )
        out_s = (out or "").strip()
        err_s = (err or "").strip()
        return (
            f"checker exit_code={rc}\n"
            f"stdout: {out_s[:500]}{'...' if len(out_s) > 500 else ''}\n"
            f"stderr: {err_s[:1500]}{'...' if len(err_s) > 1500 else ''}"
        )
    finally:
        for p in (inf, ouf, ans):
            try:
                p.unlink(missing_ok=True)
            except Exception:
                pass


def _mutate_output(output: str) -> str:
    """对选手输出做轻微扰动，生成一个应该被判错的负例。"""
    if not output.strip():
        return "wrong\n"
    lines = output.splitlines()
    if not lines:
        return output + "x"
    last = lines[-1]
    if last:
        # 改最后一个 token 的末尾字符
        lines[-1] = last[:-1] if len(last) > 1 else last + "x"
    return "\n".join(lines) + "\n"


def run_checker_self_check(count: int = 3) -> str:
    """用当前 gen + std + checker 做 reactive 自检。

    正例：用 gen 生成输入，std 跑出答案，把答案同时当 ouf/ans 跑 checker，必须返回 _ok。
    负例：把答案轻微扰动后当 ouf 跑 checker，必须返回非 _ok（_wa / _pe 均可）。
    全部通过返回 OK；任一失败返回 ERROR 详情。
    """
    exe = _wd() / _exe("checker")
    if not exe.exists():
        return "ERROR: checker 未编译，请先 write_checker 或 use_builtin_checker / use_checker_template"
    if not (_wd() / _exe("gen")).exists():
        return "ERROR: gen 未编译，无法为 checker 生成测试输入"
    if not (_wd() / _exe("std")).exists() and not _std():
        return "ERROR: 标程不可用，无法为 checker 生成标准答案"

    rj = _load_range_json()
    edge_cases = list(rj.get("edge_cases") or [])
    total_count = int(rj.get("count") or 15)

    checks = []
    # 正例：count 个不同规模 / 边界
    for i in range(count):
        idx = min(i, total_count - 1)
        checks.append(("random", 2000 + i, idx, True))
    if edge_cases:
        checks.append((edge_cases[0], 2002, 0, True))
    # 负例：同样输入，扰动输出
    checks.append(("random", 3000, 0, False))

    lines = [f"checker_self_check start: count={count} planned_checks={len(checks)}"]
    fails = []
    for typ, seed, idx, is_positive in checks:
        gen_out = _run_gen_raw(seed, typ, idx, total_count)
        if gen_out.startswith("ERROR"):
            lines.append(f"FAIL gen seed={seed} type={typ}: {gen_out[:200]}")
            fails.append((typ, seed, "gen failed"))
            continue

        std_out = _run_std_raw(gen_out)
        if std_out.startswith("ERROR"):
            lines.append(f"FAIL std seed={seed} type={typ}: {std_out[:200]}")
            fails.append((typ, seed, "std failed"))
            continue

        ouf = std_out if is_positive else _mutate_output(std_out)
        result = run_checker(gen_out, ouf, std_out)
        ok = result.startswith("checker exit_code=0") or "_ok" in result
        status = "OK" if ok else "NOT_OK"
        expected = "expected _ok" if is_positive else "expected _wa or _pe"
        lines.append(f"{status} type={typ} seed={seed} positive={is_positive} {expected}\n{result[:400]}")
        if is_positive and not ok:
            fails.append((typ, seed, "positive case rejected by checker"))
        elif not is_positive and ok:
            fails.append((typ, seed, "negative case accepted by checker"))

    if fails:
        return (
            "ERROR: checker_self_check failed\n"
            + "\n".join(lines)
            + "\n请根据失败信息修复 checker.cpp 后重新 write_checker，再 run_checker_self_check。"
        )
    return "OK: checker_self_check passed\n" + "\n".join(lines)


def _run_gen_raw(seed: int, typ: str, idx: int, count: int) -> str:
    """内部：跑 gen 返回原始输出，失败返回 ERROR 开头字符串。"""
    exe_name = _pick_gen_exe(typ)
    if not (_wd() / _exe(exe_name)).exists():
        return f"ERROR: {exe_name} 未编译"
    rc, out, err = safe_run(
        f"{_exe(exe_name)} --seed {seed} --type {typ} --index {idx} --count {count}",
        timeout=10,
        cwd=str(_wd()),
    )
    if rc != 0:
        return f"ERROR: {exe_name} failed (rc={rc}): {err or out}"
    return out or ""


def _run_std_raw(input_text: str) -> str:
    """内部：跑标程返回原始输出，失败返回 ERROR 开头字符串。"""
    rc, out, err = safe_run(
        _std(),
        stdin=input_text,
        timeout=30,
        cwd=str(_wd()),
    )
    if rc != 0:
        return f"ERROR: std failed (rc={rc}): {err or out}"
    return out or ""


def _load_range_json() -> dict:
    """读取工作目录 range.json；失败返回空 dict。"""
    p = _wd() / "range.json"
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _std_timeout_s(rj: dict | None = None) -> int:
    """标程超时（秒）：time_limit_ms + 1s 余量，默认 10。"""
    rj = rj if rj is not None else _load_range_json()
    tl = rj.get("time_limit_ms")
    if isinstance(tl, int) and not isinstance(tl, bool) and tl > 0:
        return max(1, (tl + 999) // 1000) + 1
    return 10


def _memory_limit_mb(rj: dict | None = None):
    """从 range.json 取 memory_limit_mb；未设置返回 None。"""
    return parse_memory_limit_mb(rj if rj is not None else _load_range_json())


def run_gen(seed: int, type: str = "random", index: int = -1, count: int = 15) -> str:
    """跑编译好的 gen 二进制：`./gen --seed N --type T [--index i --count C]`，返回 stdout。

    若 type == "special_samples" 且存在 gen_special 二进制，则自动转调 gen_special。
    超时硬上限 5 秒。超时直接判定 gen 算法不达标（通常是 O(n^2) 枚举），
    返回明确的 TIMEOUT 错误，提示 Agent 重写 gen.cpp。
    若 range.json 含 memory_limit_mb，则同步限制生成器内存。
    """
    if index < 0:
        index = seed
    mem_mb = _memory_limit_mb()
    exe_name = _pick_gen_exe(type)
    rc, out, err = safe_run(
        f"{_exe(exe_name)} --seed {seed} --type {type} --index {index} --count {count}",
        timeout=5,
        cwd=str(_wd()),
        memory_limit_mb=mem_mb,
    )
    if rc != 0:
        if rc == 124:
            return (
                f"ERROR gen TIMEOUT after 5s type={type} seed={seed}: "
                f"gen 算法太慢（很可能用了 O(n^2) 枚举/预建大池子）。"
                f"请 read_file(\"gen.cpp\") 找到对应分支，改用 unordered_set 随机采样，"
                f"重新 write_gen，再继续自检。不要重试同一段代码。"
            )
        if rc == EXIT_MEMORY:
            return (
                f"ERROR gen MEMORY_LIMIT ({mem_mb} MB) type={type} seed={seed}: "
                f"生成器内存超限。请降低单组规模，或避免 O(n^2) 大数组/边池；"
                f"必要时调高 range.json 的 memory_limit_mb。"
            )
        return f"ERROR gen rc={rc}: {(err or '').strip()}"
    return out if out else f"ERROR gen: empty output (stderr={(err or '').strip()})"


def run_validate(input_text: str) -> str:
    """把 input_text 喂给编译好的 validator 二进制，合法返回 OK，非法返回 stderr。"""
    mem_mb = _memory_limit_mb()
    rc, out, err = safe_run(
        _exe("validator"),
        stdin=input_text,
        timeout=10,
        cwd=str(_wd()),
        memory_limit_mb=mem_mb,
    )
    if rc == 0:
        return "OK: valid"
    if rc == EXIT_MEMORY:
        return f"ERROR validate MEMORY_LIMIT ({mem_mb} MB): {(err or '').strip()}"
    return f"ERROR validate rc={rc}: {err.strip()}"


def run_std(input_text: str) -> str:
    """把 input_text 喂给标程，返回 stdout（即答案）。std_cmd 相对项目根，在根目录跑。"""
    rj = _load_range_json()
    timeout = _std_timeout_s(rj)
    mem_mb = _memory_limit_mb(rj)
    rc, out, err = safe_run(
        _std(), stdin=input_text, timeout=timeout, memory_limit_mb=mem_mb
    )
    if rc != 0:
        if rc == 124:
            return f"ERROR std TIMEOUT after {timeout}s: 标程超时（检查数据规模或标程复杂度）"
        if rc == EXIT_MEMORY:
            return (
                f"ERROR std MEMORY_LIMIT ({mem_mb} MB): "
                f"标程超内存（检查数据规模/复杂度，或调高 memory_limit_mb）"
            )
        if is_stack_overflow(rc):
            return (
                "ERROR std STACK_OVERFLOW: 递归过深导致栈溢出；"
                "请确认标程已用加大栈编译，或降低链/深递归数据规模"
            )
        return f"ERROR std rc={rc}: {err.strip()}"
    # rc==0 时空 stdout 也是合法答案（如仅更新、无查询的操作题）
    return out or ""


def read_range() -> str:
    """读取 work_dir/range.json 的内容，返回 JSON 字符串。"""
    p = _wd() / "range.json"
    if not p.exists():
        return f"ERROR: {p} not found"
    return p.read_text(encoding="utf-8")


def _triple_check(
    seed: int,
    typ: str,
    index: int,
    count: int,
    std_timeout: int,
    mem_mb=None,
) -> tuple[str, str, str]:
    """单组 gen→validate→std。

    返回 (msg, input_text, output_text)。
    成功时 msg 以 OK 开头，input/output 为可落盘内容；失败时后两者为空。
    """
    gen_out = run_gen(seed=seed, type=typ, index=index, count=count)
    if isinstance(gen_out, str) and gen_out.startswith("ERROR"):
        return f"FAIL type={typ} seed={seed}: {gen_out}", "", ""
    val = run_validate(gen_out)
    if not val.startswith("OK"):
        return f"FAIL type={typ} seed={seed} validate: {val}", "", ""
    # 直接调 safe_run 以便用 std_timeout（run_std 会再读 range，此处统一）
    rc, out, err = safe_run(
        _std(), stdin=gen_out, timeout=std_timeout, memory_limit_mb=mem_mb
    )
    if rc != 0:
        if rc == 124:
            return f"FAIL type={typ} seed={seed}: std TIMEOUT after {std_timeout}s", "", ""
        if rc == EXIT_MEMORY:
            return f"FAIL type={typ} seed={seed}: std MEMORY_LIMIT ({mem_mb} MB)", "", ""
        if is_stack_overflow(rc):
            return (
                f"FAIL type={typ} seed={seed}: std STACK_OVERFLOW "
                f"(递归过深；请确认标程已用加大栈编译，或降低链深度)",
                "",
                "",
            )
        return f"FAIL type={typ} seed={seed}: std rc={rc} {(err or '').strip()}", "", ""
    # rc==0 时允许空 stdout（合法 .out）；套件级再检查是否「全部」为空
    inp = (gen_out or "").rstrip("\n") + "\n"
    raw = out or ""
    ans = raw if raw.endswith("\n") else (raw + "\n")
    msg = (
        f"OK type={typ} seed={seed} index={index} "
        f"in_chars={len(inp)} out_chars={len(ans)}"
    )
    return msg, inp, ans


def _clear_out_pairs(out_dir: Path, count: int) -> None:
    """清空 out/ 下 1..count 的成对测例，避免复用过期数据。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    for i in range(1, count + 1):
        for suffix in (".in", ".out"):
            p = out_dir / f"{i}{suffix}"
            if p.is_file():
                try:
                    p.unlink()
                except Exception:
                    pass


def run_self_check(
    fast_mode: bool = False,
    tiny_mode: bool = False,
    skip_special: bool = False,
    special_only: bool = False,
) -> str:
    """按 range.json 做强化自检。

    默认完整模式（交付对齐）：
      1) 按批量同一调度跑 i=0..count-1（seed=i, type=pick_type(i), index=i），
         成功则写入 out/{i+1}.in/.out，供阶段 4 批量 reuse_existing 复用；
      2) 再对每个 edge_case 补一次 index=count-1 的最大档压测（不落盘）。
    fast_mode=True：减少组数并使用中等规模，用于修复循环中间轮次；不写 out/。
    tiny_mode=True：只跑最小档，用于完整自检失败后的返工轮；不写 out/。
    skip_special=True：跳过特殊样例（普通 Gen 阶段；不要求 gen_special）。
    special_only=True：只测 special_samples（SpecialCoder 阶段）。

    通过返回以 OK 开头的摘要；任一失败返回 ERROR/... 详情，供 Agent 修改后重试。
    """
    from pipeline.gen_data import (
        _selected_scheme_schedule,
        normalize_range_json,
        pick_type,
        special_enabled,
    )

    if skip_special and special_only:
        return "ERROR: skip_special 与 special_only 不能同时为 True"

    p = _wd() / "range.json"
    if not p.exists():
        return "ERROR: range.json not found，请先 write_range"
    try:
        rj = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return f"ERROR: range.json 非法: {e}"

    rj = normalize_range_json(dict(rj))
    count = int(rj.get("count") or 15)
    edge_cases = list(rj.get("edge_cases") or [])
    scheme_schedule = _selected_scheme_schedule(rj)
    special_count = sum(n for _, n in scheme_schedule) if scheme_schedule else int(
        rj.get("special_samples_count") or 0
    )
    range_has_special = special_enabled(rj)
    has_special = range_has_special and not skip_special
    if special_only:
        if not range_has_special:
            return "ERROR: special_only=True 但 range.json 未启用特殊样例"
        has_special = True

    if not special_only and not (_wd() / _exe("gen")).exists():
        return "ERROR: gen 未编译，请先 write_gen"
    if has_special and not (_wd() / _exe("gen_special")).exists():
        return "ERROR: range.json 启用了特殊样例，但 gen_special 未编译，请先 write_special_gen"
    if not (_wd() / _exe("validator")).exists():
        return "ERROR: validator 未编译，请先 write_validate"

    constraints = rj.get("constraints") or {}
    std_timeout = _std_timeout_s(rj)
    mem_mb = _memory_limit_mb(rj)

    multi_hints = [
        "big_T_small_n", "small_T_big_n", "edge_Tmax", "edge_T1",
        "sum_full", "single_max_case",
    ]

    # checks: (type, seed, index, persist_slot|None)
    # persist_slot 为 1-based 文件号；None 表示仅压测不落盘
    checks: list[tuple[str, int, int, int | None]] = []

    if special_only:
        mid_index = max(1, count // 2)
        # 每个选中方案至少测一组；完整模式按调度落盘
        scheme_types = [
            (f"special:{sid}" if sid else "special_samples")
            for sid, _ in scheme_schedule
        ] or ["special_samples"]
        if tiny_mode:
            for j, st in enumerate(scheme_types):
                checks.append((st, 2500 + j, 0, None))
        elif fast_mode:
            for j, st in enumerate(scheme_types):
                checks.append((st, 2500 + j, mid_index, None))
        else:
            out_dir = _wd() / "out"
            for i in range(max(0, count - special_count), count):
                slot = i + 1
                for suffix in (".in", ".out"):
                    pth = out_dir / f"{slot}{suffix}"
                    if pth.is_file():
                        try:
                            pth.unlink()
                        except Exception:
                            pass
                st = pick_type(i, count, edge_cases, special_count, scheme_schedule)
                checks.append((st, i, i, slot))
    elif tiny_mode:
        for i, typ in enumerate(edge_cases):
            checks.append((typ, 1000 + i, 0, None))
        checks.append(("random", 2000, 0, None))
        if has_special:
            checks.append(("special_samples", 2500, 0, None))
        for name in multi_hints:
            if name in edge_cases:
                checks.append((name, 4000 + hash(name) % 100, 0, None))
    elif fast_mode:
        mid_index = max(1, count // 2)
        for i, typ in enumerate(edge_cases):
            checks.append((typ, 1000 + i, min(i, mid_index), None))
        checks.append(("random", 2000, 0, None))
        checks.append(("random", 2001, mid_index, None))
        if has_special:
            checks.append(("special_samples", 2500, mid_index, None))
        for name in multi_hints:
            if name in edge_cases:
                checks.append((name, 4000 + hash(name) % 100, mid_index, None))
    else:
        # 完整模式：与批量生成同一调度，成功则写入 out/ 供后续复用
        out_dir = _wd() / "out"
        if skip_special and special_count > 0:
            # 只清常规组，保留特殊组槽位给后续 SpecialCoder
            for i in range(count - special_count):
                for suffix in (".in", ".out"):
                    pth = out_dir / f"{i + 1}{suffix}"
                    if pth.is_file():
                        try:
                            pth.unlink()
                        except Exception:
                            pass
            for i in range(count - special_count):
                typ = pick_type(i, count, edge_cases, special_count, scheme_schedule)
                checks.append((typ, i, i, i + 1))
        else:
            _clear_out_pairs(out_dir, count)
            for i in range(count):
                typ = pick_type(i, count, edge_cases, special_count, scheme_schedule)
                checks.append((typ, i, i, i + 1))
        # 额外最大档压测：每个 edge（及必要的 random max）再跑一次，不落盘
        max_idx = max(0, count - 1 - (special_count if skip_special else 0))
        for i, typ in enumerate(edge_cases):
            checks.append((typ, 10000 + i, max_idx, None))
        has_max_case = any(
            name in edge_cases for name in ("edge_nmax", "nmax", "edge_n_max", "max_n")
        )
        if not has_max_case:
            checks.append(("random", 10000 + len(edge_cases), max_idx, None))
        for name in multi_hints:
            if name in edge_cases:
                checks.append((name, 11000 + hash(name) % 100, max_idx, None))

    # 去重保序：交付组按 persist_slot 优先；压测按 (type, index, seed)
    seen = set()
    uniq: list[tuple[str, int, int, int | None]] = []
    for typ, seed, idx, slot in checks:
        key = ("slot", slot) if slot is not None else ("stress", typ, idx, seed)
        if key in seen:
            continue
        seen.add(key)
        uniq.append((typ, seed, idx, slot))

    limit_note = f"std_timeout={std_timeout}s"
    if mem_mb:
        limit_note += f" memory_limit_mb={mem_mb}"
    mode_note = "tiny" if tiny_mode else ("fast" if fast_mode else "full")
    if special_only:
        mode_note += "+special_only"
    elif skip_special:
        mode_note += "+skip_special"
    lines = [f"self_check start: mode={mode_note} count={count} edges={edge_cases} {limit_note}"]
    if not fast_mode and not tiny_mode:
        lines.append("full: aligned with batch schedule; OK cases written to out/ for reuse")
    if isinstance(constraints, dict) and constraints:
        lines.append(f"constraints_keys={list(constraints.keys())}")
    fails = []
    written = 0
    nonempty_out = 0
    out_dir = _wd() / "out"
    for typ, seed, idx, slot in uniq:
        msg, inp, ans = _triple_check(seed, typ, idx, count, std_timeout, mem_mb=mem_mb)
        if slot is not None:
            tag = f" [out/{slot}.in]"
        else:
            tag = " [stress]"
        lines.append(msg + tag)
        if msg.startswith("FAIL") or msg.startswith("ERROR"):
            fails.append(msg)
            continue
        if (ans or "").strip():
            nonempty_out += 1
        if slot is not None and inp:
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / f"{slot}.in").write_text(inp, encoding="utf-8")
            (out_dir / f"{slot}.out").write_text(ans if ans is not None else "", encoding="utf-8")
            written += 1

    if fails:
        fix_hint = (
            "请根据 FAIL 修复 gen_special.cpp 后重新 write_special_gen 再 run_self_check。"
            if special_only
            else "请根据 FAIL 修复 gen/validator 后重新 write_* 再 run_self_check。"
        )
        return (
            "ERROR: self_check failed\n"
            + "\n".join(lines)
            + "\n" + fix_hint
        )
    if nonempty_out == 0:
        return (
            "ERROR: self_check failed: 全部测例 stdout 为空"
            "（标程可能未输出，或 gen 从未生成查询类操作）\n"
            + "\n".join(lines)
            + "\n请保证至少部分测例含会触发输出的操作/查询，或检查标程是否写了输出。"
        )
    if not fast_mode and not tiny_mode:
        lines.append(f"persisted {written}/{count} pairs to out/ for batch reuse")
    return "OK: self_check passed\n" + "\n".join(lines)


# ---- Finder：搜索特殊样例并留痕 ----
_FINDER_BANNED_RE = re.compile(
    r"\b(system|popen|_popen|execve|execl|fork|CreateProcess|ShellExecute|"
    r"std::system)\s*\(",
    re.I,
)
_FINDER_HUGE_ARRAY_RE = re.compile(
    r"(?:int|long|char|double|float|bool|auto)\s+\w+\s*"
    r"\[\s*(\d{8,}|\d+\s*\*\s*\d+\s*\*\s*\d+)\s*\]"
)


def _sanitize_scheme_id(scheme_id: str) -> str:
    sid = re.sub(r"[^a-zA-Z0-9_]", "_", (scheme_id or "special").strip()).strip("_")
    return sid or "special"


def finder_dir(scheme_id: str) -> Path:
    """special_findings/<scheme_id>/ 目录。"""
    return _wd() / FINDER_DIR_NAME / _sanitize_scheme_id(scheme_id)


_FINDER_HUGE_ALLOC_RE = re.compile(
    r"(?:"
    # 显式 1e6/1e7 级分配
    r"vector\s*<[^;]{0,80}>\s*\w+\s*\([^;]{0,60}(?:1e[6-9]|[1-9]\d{6,})"
    r"|\[\s*(?:1e[6-9]|[1-9]\d{6,})\s*\]"
    r"|(?:vis|sieve|minp|prime|is_prime|lp|phi|mu)\w*\s*\(\s*(?:N\s*\+\s*)?(?:1e[6-9]|[1-9]\d{6,})"
    r"|(?:const\s+)?(?:int|long|long\s+long|size_t)\s+N\s*=\s*(?:1e[6-9]|[1-9]\d{6,})\b"
    r"|#define\s+N\s+(?:1e[6-9]|[1-9]\d{6,})\b"
    r"|new\s+(?:int|long|bool|char|long\s+long)\s*\[\s*(?:1e[6-9]|[1-9]\d{6,})"
    r")",
    re.I,
)


def _finder_static_check(content: str) -> str | None:
    """控制 finder 源码：禁危险调用、禁超大静态/动态数组、要求复杂度声明。"""
    if _FINDER_BANNED_RE.search(content or ""):
        return (
            "ERROR: finder 禁止 system/popen/fork/exec/CreateProcess 等进程调用；"
            "只允许纯计算搜索。"
        )
    if _FINDER_HUGE_ARRAY_RE.search(content or "") or _FINDER_HUGE_ALLOC_RE.search(content or ""):
        return (
            "ERROR: 检测到 1e7 量级大数组/筛（静态或 vector）。"
            "请改用小窗口搜索与局部判定（避免 1e7 级全表），不要为 Finder 开全量预处理表。"
        )
    # 要求源码注释里写清复杂度，便于审查
    low = (content or "").lower()
    if "time" not in low and "o(" not in low and "复杂度" not in (content or ""):
        return (
            "ERROR: finder.cpp 顶部必须用注释写明时间/空间复杂度上界"
            "（例如 // time: O(n*tries) space: O(n)，n<=...）。"
        )
    if "main" not in (content or ""):
        return "ERROR: finder.cpp 必须包含 main"
    return None


def _parse_finder_hits(stdout: str, max_hits: int) -> list[str]:
    """从 finder stdout 解析 ---BEGIN--- ... ---END--- 块。"""
    text = stdout or ""
    hits: list[str] = []
    pattern = re.compile(
        r"---BEGIN---\s*\n(.*?)---END---",
        re.S,
    )
    for m in pattern.finditer(text):
        body = (m.group(1) or "").strip("\n")
        if body.strip():
            hits.append(body if body.endswith("\n") else body + "\n")
        if len(hits) >= max_hits:
            break
    return hits


def write_finder(content: str, scheme_id: str = "special") -> str:
    """写入 special_findings/<id>/finder.cpp 并带大栈编译。"""
    if _looks_like_omitted_stub(content):
        return (
            "ERROR: content 像是摘要不是完整 finder.cpp。"
            "请输出完整 C++ 源码（含复杂度注释与 ---BEGIN---/---END--- 输出协议）。"
        )
    static_err = _finder_static_check(content)
    if static_err:
        return static_err

    sid = _sanitize_scheme_id(scheme_id)
    fdir = finder_dir(sid)
    hits_dir = fdir / "hits"
    fdir.mkdir(parents=True, exist_ok=True)
    hits_dir.mkdir(parents=True, exist_ok=True)
    src = fdir / "finder.cpp"
    src.write_text(content, encoding="utf-8")

    rc, out, err = _compile_cpp(
        "finder.cpp",
        "finder",
        timeout=60,
        stack_bytes=FINDER_STACK_BYTES,
        cwd=fdir,
    )
    if rc != 0:
        return _compile_err(f"finder[{sid}]", rc, out, err)

    meta = {
        "scheme_id": sid,
        "stack_bytes": FINDER_STACK_BYTES,
        "stack_mb": FINDER_STACK_MB,
        "compiled": True,
        "source_chars": len(content),
    }
    (fdir / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    note = (
        f"OK: wrote & compiled finder for {sid} "
        f"(stack={FINDER_STACK_MB}MB link/runtime, {len(content)} chars)"
    )
    if err and "RLIMIT_STACK" in err:
        note += f"\n{err.strip()}"
    return note


def run_finder(
    scheme_id: str = "special",
    seed: int = 1,
    max_hits: int = FINDER_MAX_HITS,
    timeout_sec: int = FINDER_DEFAULT_TIMEOUT_SEC,
    memory_limit_mb: int = FINDER_DEFAULT_MEMORY_MB,
) -> str:
    """运行已编译 finder，解析命中输入并落盘 hits/ + meta.json。"""
    sid = _sanitize_scheme_id(scheme_id)
    fdir = finder_dir(sid)
    exe = fdir / _exe("finder")
    if not exe.is_file():
        return f"ERROR: 未找到 {exe}，请先 write_finder"
    try:
        max_hits = max(1, min(int(max_hits or FINDER_MAX_HITS), FINDER_MAX_HITS))
    except (TypeError, ValueError):
        max_hits = FINDER_MAX_HITS
    try:
        timeout_sec = max(
            1,
            min(int(timeout_sec or FINDER_DEFAULT_TIMEOUT_SEC), FINDER_MAX_TIMEOUT_SEC),
        )
    except (TypeError, ValueError):
        timeout_sec = FINDER_DEFAULT_TIMEOUT_SEC
    try:
        memory_limit_mb = max(
            64,
            min(int(memory_limit_mb or FINDER_DEFAULT_MEMORY_MB), FINDER_MAX_MEMORY_MB),
        )
    except (TypeError, ValueError):
        memory_limit_mb = FINDER_DEFAULT_MEMORY_MB

    hits_dir = fdir / "hits"
    hits_dir.mkdir(parents=True, exist_ok=True)
    # 清旧 hits
    for old in hits_dir.glob("*.in"):
        try:
            old.unlink()
        except OSError:
            pass

    cmd = f'{_exe("finder")} --seed {int(seed)} --max-hits {max_hits}'
    rc, out, err = safe_run(
        cmd,
        timeout=timeout_sec,
        cwd=str(fdir),
        memory_limit_mb=memory_limit_mb,
        stack_limit_mb=FINDER_STACK_MB if os.name != "nt" else None,
    )
    (fdir / "run_log.txt").write_text(
        f"cmd={cmd}\nrc={rc}\ntimeout_sec={timeout_sec}\n"
        f"memory_limit_mb={memory_limit_mb}\nstack_mb={FINDER_STACK_MB}\n\n"
        f"=== stdout ===\n{out or ''}\n\n=== stderr ===\n{err or ''}\n",
        encoding="utf-8",
    )

    if rc == 124:
        status = "timeout"
        hits: list[str] = _parse_finder_hits(out or "", max_hits)
    elif rc == EXIT_MEMORY:
        status = "oom"
        hits = []
    elif is_stack_overflow(rc):
        status = "stack_overflow"
        hits = []
    elif rc not in (0, 1):
        # 约定：0=有命中，1=无命中但正常结束；其它为运行错误
        status = "error"
        hits = _parse_finder_hits(out or "", max_hits)
    else:
        hits = _parse_finder_hits(out or "", max_hits)
        status = "ok" if hits else "none"

    saved_paths: list[str] = []
    for i, body in enumerate(hits, 1):
        rel = f"hits/{i}.in"
        (fdir / rel).write_text(body, encoding="utf-8")
        saved_paths.append(f"{FINDER_DIR_NAME}/{sid}/{rel}")

    # 可选：对命中做 validator 快筛（不阻断，结果写入 meta）
    validated: list[dict] = []
    for p in saved_paths:
        text = (_wd() / p).read_text(encoding="utf-8")
        v = run_validate(text)
        validated.append({
            "path": p,
            "validate": "OK" if isinstance(v, str) and v.startswith("OK") else str(v)[:200],
        })

    meta_path = fdir / "meta.json"
    meta = {}
    if meta_path.is_file():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            meta = {}
    meta.update({
        "scheme_id": sid,
        "status": status,
        "rc": rc,
        "seed": int(seed),
        "max_hits": max_hits,
        "timeout_sec": timeout_sec,
        "memory_limit_mb": memory_limit_mb,
        "stack_bytes": FINDER_STACK_BYTES,
        "stack_mb": FINDER_STACK_MB,
        "hits": saved_paths,
        "validated": validated,
        "stderr_tail": (err or "")[-500:],
    })
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    if status == "timeout":
        return (
            f"ERROR: finder TIMEOUT after {timeout_sec}s "
            f"(partial_hits={len(saved_paths)}). "
            f"允许 O(n^2)，但请缩小搜索上界/分层枚举，或提高 timeout_sec（≤{FINDER_MAX_TIMEOUT_SEC}）后重写。"
            + (f"\nhits: {saved_paths}" if saved_paths else "")
        )
    if status == "oom":
        # 已在 GB 级仍 OOM：几乎一定是代码开了全量大正表，再加 memory 无用
        bump_hint = (
            f"可试 memory_limit_mb≤{FINDER_MAX_MEMORY_MB}；"
            if memory_limit_mb < FINDER_MAX_MEMORY_MB
            else "已达内存上限，禁止再加 memory_limit_mb；"
        )
        return (
            f"ERROR: finder MEMORY_LIMIT ({memory_limit_mb}MB，栈保留约 {FINDER_STACK_MB}MB)。"
            f"{bump_hint}"
            "必须重写为小内存搜索：禁止 1e7 级 vector/筛/全局表；"
            "只用 O(窗口) 或 O(1) 局部变量 + 试除/枚举；不要递归爆栈。"
        )
    if status == "stack_overflow":
        return (
            "ERROR: finder STACK_OVERFLOW（已扩栈仍溢出）。"
            "请改为迭代/显式栈，或减小递归深度。"
        )
    if status == "error":
        return (
            f"ERROR: finder 运行失败 rc={rc}\n{(err or out or '')[:800]}\n"
            f"已保存 hits={len(saved_paths)}"
        )
    if not saved_paths:
        return (
            "OK: finder 正常结束但未找到命中（status=none）。"
            "可调整策略再 run_finder，或放弃 Finder 走纯构造。"
        )
    lines = [f"OK: finder 找到 {len(saved_paths)} 组命中（已留痕）"]
    for item in validated:
        lines.append(f"  - {item['path']}: validate={item['validate']}")
    lines.append(f"meta: {FINDER_DIR_NAME}/{sid}/meta.json")
    return "\n".join(lines)


def list_finder_hits(scheme_id: str = "special") -> str:
    """列出某方案已保存的 finder 命中路径与预览。"""
    sid = _sanitize_scheme_id(scheme_id)
    fdir = finder_dir(sid)
    meta_path = fdir / "meta.json"
    if not meta_path.is_file():
        return f"OK: no finder meta for {sid}"
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return f"ERROR: meta.json 损坏: {e}"
    hits = meta.get("hits") or []
    if not hits:
        return f"OK: finder status={meta.get('status')} hits=0"
    parts = [f"OK: status={meta.get('status')} hits={len(hits)}"]
    for p in hits:
        fp = _wd() / p
        preview = ""
        if fp.is_file():
            preview = fp.read_text(encoding="utf-8")[:400]
        parts.append(f"--- {p} ---\n{preview}")
    return "\n".join(parts)


# ---- 函数表 ----
FUNCTIONS = {
    "write_file": write_file,
    "read_file": read_file,
    "finish": finish,
    "write_range": write_range,
    "write_gen": write_gen,
    "write_special_gen": write_special_gen,
    "write_validate": write_validate,
    "write_checker": write_checker,
    "use_builtin_checker": use_builtin_checker,
    "use_checker_template": use_checker_template,
    "write_finder": write_finder,
    "run_finder": run_finder,
    "list_finder_hits": list_finder_hits,
    "run_gen": run_gen,
    "run_validate": run_validate,
    "run_std": run_std,
    "run_self_check": run_self_check,
    "run_checker": run_checker,
    "run_checker_self_check": run_checker_self_check,
    "read_range": read_range,
}


def _schema(name: str, description: str, props: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": props,
                "required": required,
            },
        },
    }


TOOL_SCHEMAS = [
    _schema(
        "write_file",
        "把文本写入工作目录下的相对路径。若 path 是 gen.cpp/validator.cpp/range.json，会自动转调 write_gen/write_validate/write_range。",
        {
            "path": {"type": "string", "description": "相对工作目录的路径，如 gen.cpp"},
            "content": {"type": "string", "description": "文件内容"},
        },
        ["path", "content"],
    ),
    _schema(
        "read_file",
        "读取工作目录下的文件。查上一版源码请用 path=gen.cpp 或 validator.cpp（不要用项目根路径）。",
        {"path": {"type": "string", "description": "相对工作目录，如 gen.cpp / validator.cpp / range.json"}},
        ["path"],
    ),
    _schema(
        "write_range",
        "把数据范围写到 range.json。content 为合法 JSON：count 默认 15；constraints；edge_cases（不要含 random；含 edge_n1/edge_nmax 等最小最大边界）。",
        {"content": {"type": "string", "description": "range.json 的完整 JSON 字符串"}},
        ["content"],
    ),
    _schema(
        "write_gen",
        "把【完整】生成器 C++ 源码写到工作目录 gen.cpp 并 g++ 编译。"
        "必填 content=完整源码字符串；禁止空调用、禁止省略 content。"
        "树/图/几何题优先 #include \"generator.h\" + using namespace generator::all；"
        "树/图必须先 t.gen()，再 cout << t，或 for (auto &e : t.edges())；"
        "get_edges() / Tree::shuffle() / 访问 _edges 不存在，写错会编译失败。"
        "仍须 registerGen + --seed/--type/--index/--count。random 分支必须用 --index/--count 分层取规模。"
        "禁止 std::shuffle(...,rnd)；禁止枚举 O(n^2) 边池。",
        {
            "content": {
                "type": "string",
                "description": (
                    "【必填】完整 gen.cpp 源码全文，不能为空、不能是摘要。"
                    "树/图：t.gen(); cout << t 或 t.edges()；禁止 get_edges/shuffle。"
                ),
            }
        },
        ["content"],
    ),
    _schema(
        "write_special_gen",
        "把【完整】特殊样例生成器 C++ 源码写到工作目录 gen_special.cpp 并 g++ 编译成 gen_special(.exe)。"
        "仅当 range.json 含 special_samples_desc 时使用。"
        "与 gen.cpp 一样须 registerGen + --seed/--type/--index/--count，输出格式须与 gen.cpp 完全一致。"
        "按 special_samples_desc 严格生成数据，通常用 seed 做随机化、index/count 做规模分层。",
        {
            "content": {
                "type": "string",
                "description": (
                    "完整 gen_special.cpp（testlib.h 或 generator.h + registerGen）。"
                    "输出格式与 gen.cpp 完全一致，能被同一 validator/std 处理。"
                ),
            }
        },
        ["content"],
    ),
    _schema(
        "write_finder",
        "把【完整】特殊样例搜索器 C++ 写到 special_findings/<scheme_id>/finder.cpp 并编译。"
        f"系统强制栈约 {FINDER_STACK_MB}MB（非默认栈）；禁止 system/popen/fork；允许 O(n^2)，"
        f"默认时限 {FINDER_DEFAULT_TIMEOUT_SEC}s / 内存 {FINDER_DEFAULT_MEMORY_MB}MB；"
        "内存必须 O(窗口)/O(1)，禁止全量筛表；顶部注释须写复杂度与搜索上界。"
        "命中输出协议：每组用 ---BEGIN--- / ---END--- 包裹完整输入；支持 --seed/--max-hits。",
        {
            "content": {
                "type": "string",
                "description": "完整 finder.cpp（含复杂度注释与 BEGIN/END 输出协议）",
            },
            "scheme_id": {
                "type": "string",
                "description": "方案 id，对应 special_findings 子目录",
            },
        },
        ["content", "scheme_id"],
    ),
    _schema(
        "run_finder",
        "运行已编译的 finder（限时/限内存/大栈），解析命中并落盘 hits/*.in + meta.json。"
        f"默认 timeout {FINDER_DEFAULT_TIMEOUT_SEC}s、memory {FINDER_DEFAULT_MEMORY_MB}MB"
        f"（上限 {FINDER_MAX_TIMEOUT_SEC}s / {FINDER_MAX_MEMORY_MB}MB）。"
        "允许 O(n^2)；超时/OOM 可缩小规模或在上限内调高时限/内存后重试。",
        {
            "scheme_id": {"type": "string", "description": "方案 id"},
            "seed": {"type": "integer", "description": "随机种子，默认 1"},
            "max_hits": {"type": "integer", "description": "最多保存命中数，默认 1，上限 1"},
            "timeout_sec": {
                "type": "integer",
                "description": (
                    f"运行时限秒，默认 {FINDER_DEFAULT_TIMEOUT_SEC}，"
                    f"上限 {FINDER_MAX_TIMEOUT_SEC}"
                ),
            },
            "memory_limit_mb": {
                "type": "integer",
                "description": (
                    f"内存上限 MB，默认 {FINDER_DEFAULT_MEMORY_MB}，"
                    f"上限 {FINDER_MAX_MEMORY_MB}"
                ),
            },
        },
        ["scheme_id"],
    ),
    _schema(
        "list_finder_hits",
        "列出 special_findings/<scheme_id> 下已保存的命中输入预览，供 SpecialCoder 参考。",
        {"scheme_id": {"type": "string", "description": "方案 id"}},
        ["scheme_id"],
    ),
    _schema(
        "write_validate",
        "把【完整】校验器 C++ 源码写到工作目录 validator.cpp 并 g++ 编译。"
        "content 必须是完整源码，禁止摘要。建议 registerValidation + readEof。"
        "结构性质可用 ensuref 校验；若只有范围/格式约束，也可不加 ensuref，"
        "以编译通过、运行 validate 不报错为准。",
        {
            "content": {
                "type": "string",
                "description": (
                    "完整 validator.cpp。"
                    "需 registerValidation + readEof；"
                    "有结构约束时加 ensuref，否则按范围/格式校验即可。"
                ),
            }
        },
        ["content"],
    ),
    _schema(
        "write_checker",
        "把【完整】special judge / checker C++ 源码写到工作目录 checker.cpp 并 g++ 编译。"
        "仅当答案不唯一或需额外判定时使用；若只需按行/词/浮点/YesNo 比较，请改用 use_builtin_checker。",
        {"content": {"type": "string", "description": "完整 checker.cpp（#include \"testlib.h\"，registerTestlibCmd(...)）"}},
        ["content"],
    ),
    _schema(
        "use_builtin_checker",
        "安装并编译内置 testlib checker 为 checker(.exe)。"
        "name=lcmp(按行比 token) / wcmp(按词) / rcmp4|rcmp6|rcmp9(浮点精度) / yesno(Yes/No)。"
        "答案唯一的常规题优先用这个，不要手写 checker。",
        {
            "name": {
                "type": "string",
                "description": "lcmp | wcmp | rcmp4 | rcmp6 | rcmp9 | yesno",
            },
        },
        ["name"],
    ),
    _schema(
        "use_checker_template",
        "安装 checker 模板，生成可编译的骨架 checker.cpp。"
        "可选模板：\n"
        "- construct_verify: 通用构造/方案验证\n"
        "- any_of_answers: 多解但可推导正确答案条件\n"
        "- graph_path: 路径/环/walk 验证\n"
        "- permutation: 排列验证\n"
        "- subset: 子集/选择验证\n"
        "- sequence_property: 序列/数组性质验证\n"
        "- point_set: 点集/几何构造验证\n"
        "- matching: 匹配/配对方案验证\n"
        "- tree_parent: 树父节点/边集验证\n"
        "安装后应 read_file(\"checker.cpp\") 查看并用 write_checker 替换 TODO 部分。",
        {
            "name": {
                "type": "string",
                "description": "construct_verify | any_of_answers | graph_path | permutation | subset | sequence_property | point_set | matching | tree_parent",
            },
        },
        ["name"],
    ),
    _schema(
        "run_gen",
        "运行编译好的 gen：`gen --seed N --type T --index i --count C`。返回生成的输入文本。",
        {
            "seed": {"type": "integer", "description": "随机种子"},
            "type": {"type": "string", "description": "edge_cases 中的类型名，或 random"},
            "index": {"type": "integer", "description": "组号 0..count-1，用于规模分层；默认=seed"},
            "count": {"type": "integer", "description": "总组数，默认 15"},
        },
        ["seed"],
    ),
    _schema(
        "run_validate",
        "把一段输入文本喂给编译好的 validator 二进制校验合法性。合法返回 OK，非法返回 stderr 里的错误原因。",
        {"input_text": {"type": "string", "description": "待校验的输入内容"}},
        ["input_text"],
    ),
    _schema(
        "run_std",
        "把一段输入文本喂给标程，返回标程的输出（即答案）。"
        "超时参考 range.json 的 time_limit_ms；内存参考 memory_limit_mb。",
        {"input_text": {"type": "string", "description": "标程的 stdin 输入"}},
        ["input_text"],
    ),
    _schema(
        "run_self_check",
        "强化自检：执行 gen→validate→std。"
        "完整模式按批量同一调度生成 count 组并写入 out/（供后续打包复用），"
        "另对每个 edge 补最大档压测；快速/微小模式不落盘。"
        "全部通过才返回 OK；失败返回 ERROR 详情。finish 前必须调用且通过。",
        {},
        [],
    ),
    _schema(
        "run_checker",
        "运行编译好的 checker，用一组 (input, output, answer) 测试其判定行为。"
        "input_text 对应 inf，output_text 对应 ouf，answer_text 对应 ans。"
        "返回退出码与 stdout/stderr 摘要。",
        {
            "input_text": {"type": "string", "description": "题目输入内容（inf）"},
            "output_text": {"type": "string", "description": "选手输出内容（ouf）"},
            "answer_text": {"type": "string", "description": "标程答案内容（ans）"},
        },
        ["input_text", "output_text", "answer_text"],
    ),
    _schema(
        "run_checker_self_check",
        "用当前 gen + std + checker 做 reactive 自检："
        "正例（标程输出当 ouf/ans）必须返回 _ok；负例（扰动输出当 ouf）必须返回 _wa 或 _pe。"
        "全部通过才返回 OK；失败返回 ERROR 详情。write_checker 后建议调用。",
        {},
        [],
    ),
    _schema(
        "read_range",
        "读取当前工作目录的 range.json，返回数据范围约束的 JSON 字符串。",
        {},
        [],
    ),
    _schema(
        "finish",
        "任务完成后调用此工具结束循环，summary 简述结果。",
        {"summary": {"type": "string", "description": "对完成情况的简述"}},
        [],
    ),
]


_WRITE_CONTENT_TOOLS = frozenset({
    "write_gen", "write_special_gen", "write_validate", "write_checker",
    "write_range", "write_finder", "write_file",
})

# 模型常把源码塞进这些键名而不是 content
_CONTENT_ALIASES = (
    "content", "code", "source", "source_code", "src", "text", "body",
    "gen_cpp", "cpp", "file_content", "program",
)


def _extract_content_arg(args: dict) -> str | None:
    """从 args 中取出源码/正文；支持常见别名。"""
    if not isinstance(args, dict):
        return None
    for key in _CONTENT_ALIASES:
        val = args.get(key)
        if isinstance(val, str) and val.strip():
            return val
    return None


def normalize_tool_args(name: str, args: dict | None) -> tuple[dict, str | None]:
    """规范化工具参数。返回 (clean_args, error_or_None)。

    处理：缺 content、别名、空串、参数 JSON 解析失败标记。
    """
    raw = dict(args or {})
    # 内部标记不传给函数
    parse_failed = bool(raw.pop("_args_parse_failed", False))
    recovered = bool(raw.pop("_args_recovered", False))
    raw_preview = raw.pop("_raw_args_preview", None)

    if name not in _WRITE_CONTENT_TOOLS:
        return raw, None

    content = _extract_content_arg(raw)
    if content is None:
        # 把别名空值清掉，避免 TypeError 难读
        for key in _CONTENT_ALIASES:
            raw.pop(key, None)
        hint = (
            f"ERROR: {name} 缺少必填参数 content（完整源码/正文）。"
            "禁止空调用；禁止只传函数名。"
            "请立即重新调用，arguments 形如 "
            '{"content":"#include ... 完整源码 ..."}。'
            "若上一版已在磁盘，先 read_file 读出再整份 write；"
            "禁止把 __OMITTED_SOURCE__ 摘要写回。"
        )
        if parse_failed:
            hint += (
                f" 另：工具参数 JSON 解析失败（可能被截断），预览={raw_preview!r}。"
                "请重新提交完整 content（一次写全，勿截断）。"
            )
        if name == "write_file" and not raw.get("path"):
            hint += " write_file 还需要 path。"
        return raw, hint

    raw["content"] = content
    # 去掉其它别名，避免 unexpected keyword
    for key in _CONTENT_ALIASES:
        if key != "content":
            raw.pop(key, None)

    if recovered and len(content) < 40:
        return raw, (
            f"ERROR: {name} 参数疑似截断恢复后过短（{len(content)} chars），"
            "请重新提交完整 content，勿空调用。"
        )
    return raw, None


def dispatch(name: str, args: dict) -> str:
    """按名字执行工具，返回字符串结果（喂回 LLM）。"""
    fn = FUNCTIONS.get(name)
    if fn is None:
        return f"ERROR: unknown tool {name!r}"
    clean, err = normalize_tool_args(name, args)
    if err:
        return err
    try:
        return str(fn(**clean))
    except TypeError as e:
        msg = str(e)
        if "content" in msg or "required positional" in msg:
            return (
                f"ERROR: {name} 参数不完整: {e}。"
                "必须传 content=完整源码字符串；"
                "请 read_file 后重新 write_*，不要空参数重试。"
            )
        return f"ERROR: bad args for {name}: {e}"
    except Exception as e:
        return f"ERROR: {name} raised {type(e).__name__}: {e}"
