"""工具共享上下文：JobContext、编译辅助、常量。"""
import hashlib
import json
import os
import re
from contextvars import ContextVar, Token
from dataclasses import dataclass
from pathlib import Path

from sandbox.run import parse_memory_limit_mb, safe_run

# testlib.h / generator.h / 内置 checker 源码（随项目分发）
# 编译时用 -I sandbox，job 目录不再落盘头文件；打包源码时从 sandbox 取头文件。
_SANDBOX = Path(__file__).resolve().parent.parent.parent / "sandbox"
TESTLIB_H = _SANDBOX / "testlib.h"
GENERATOR_H = _SANDBOX / "generator.h"
CHECKER_SRC_DIR = _SANDBOX / "checker"
CHECKER_TEMPLATE_DIR = _SANDBOX / "checker" / "templates"

# 允许的内置 checker 名（对应 sandbox/checker/<name>.cpp）
BUILTIN_CHECKERS = ("lcmp", "wcmp", "rcmp4", "rcmp6", "rcmp9", "yesno")

# 喂给 LLM 的短释义（工具 schema / Checker Planner 共用）
BUILTIN_CHECKER_HELP = """【内置 checker 含义 · testlib ── 对比表】
┌────────┬──────────┬────────────────────────────────────────────────────────┐
│ 名称   │ 误差/行为 │ 适用场景                                               │
├────────┼──────────┼────────────────────────────────────────────────────────┤
│ lcmp   │ 按行     │ 逐行读取，每行内按空白分隔 token 后逐个比对。          │
│        │ 严格行   │ 行数不同 →_wa；同行 token 数不同 →_wa。                │
│        │          │ 适合：答案按行组织、行结构有意义的题。                  │
├────────┼──────────┼────────────────────────────────────────────────────────┤
│ wcmp   │ 按 token │ 跳过所有空白，按 token 序列顺序逐个比对。              │
│        │ 忽略换行 │ 完全忽略换行位置，只关心 token 内容与顺序。            │
│        │          │ 适合：唯一答案的整数/词序列（绝大部分唯一答案题首选）。│
├────────┼──────────┼────────────────────────────────────────────────────────┤
│ rcmp4  │ EPS=1e-4 │ 浮点数逐个比对，相对/绝对误差 ≤1e-4 即认为相等。      │
│        │          │ 适合：题面标注 1e-4 / 四位小数数量级。                  │
├────────┼──────────┼────────────────────────────────────────────────────────┤
│ rcmp6  │ EPS=1e-6 │ 同上，误差 ≤1e-6。                                     │
│        │          │ 适合：题面标注 1e-6 / 六位小数数量级。                  │
├────────┼──────────┼────────────────────────────────────────────────────────┤
│ rcmp9  │ EPS=1e-9 │ 同上，误差 ≤1e-9。                                     │
│        │          │ 适合：题面标注 1e-9 / 高精度浮点。                      │
├────────┼──────────┼────────────────────────────────────────────────────────┤
│ yesno  │ 大小写   │ 逐 token 读取，upperCase() 后与 "YES"/"NO" 比对。      │
│        │ 不敏感   │ 适合：答案只有 Yes/No（或 YES/NO）二选一的判断题。     │
└────────┴──────────┴────────────────────────────────────────────────────────┘
多解 / 构造 / 需验合法性 → 不要用内置，改 write_checker 或 use_checker_template。
"""

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

# SPJ 单测墙钟硬超时（秒）：与 prompts.CHECKER_COMPLEXITY_RULES 一致（设计 ≤1s，硬超时 2s）
CHECKER_TIMEOUT_S = 2

# 题型里通常会用到 generator.h 的集合（供 runner / 提示词参考）
GENERATOR_PROBLEM_TYPES = frozenset({
    "tree", "graph", "geometry", "weighted_tree", "weighted_graph",
    "permutation", "array", "string", "matrix", "number_theory",
})

SPECIAL_META_DIR_NAME = "special_meta"

# Windows MinGW 默认栈约 1MB，树/图 gen·validator 递归判连通在链上易炸；对齐标程 16MB。
# POSIX 链接 -z stack-size 可能不被支持，运行时再用 RLIMIT_STACK（stack_limit_mb）兜底。
DEFAULT_STACK_BYTES = 16 * 1024 * 1024  # 16MB
DEFAULT_STACK_MB = DEFAULT_STACK_BYTES // (1024 * 1024)

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


SPECIAL_META_DIR_NAME = "special_meta"

# Windows MinGW 默认栈约 1MB，树/图 gen·validator 递归判连通在链上易炸；对齐标程 16MB。
# POSIX 链接 -z stack-size 可能不被支持，运行时再用 RLIMIT_STACK（stack_limit_mb）兜底。
DEFAULT_STACK_BYTES = 16 * 1024 * 1024  # 16MB
DEFAULT_STACK_MB = DEFAULT_STACK_BYTES // (1024 * 1024)


def _stack_link_flags(stack_bytes: int | None) -> str:
    """链接期扩大栈；Windows 用 --stack，POSIX 尽量用 -z stack-size（失败可降级）。"""
    if not stack_bytes or stack_bytes <= 0:
        return ""
    n = int(stack_bytes)
    if os.name == "nt":
        return f" -Wl,--stack,{n}"
    # GNU gold/lld 支持；GNU ld 可能失败，调用方应准备重试
    return f" -Wl,-z,stack-size={n}"


def _runtime_stack_limit_mb() -> int | None:
    """POSIX 运行时抬高 RLIMIT_STACK；Windows 栈靠链接参数，此处返回 None。"""
    if os.name == "nt":
        return None
    return DEFAULT_STACK_MB


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
        f'g++ -std=c++17 -I{_SANDBOX.resolve().as_posix()}{flags} '
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
    last_special_check_hash: str = ""


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


def _resolve_std_cmd() -> str:
    """解析可用的标程命令：优先 job 目录下已编译的 std，返回绝对路径。"""
    local = _wd() / _exe("std")
    if local.is_file():
        return str(local.resolve())
    cmd = (_std() or "").strip()
    if not cmd:
        return ""
    p = Path(cmd)
    if p.is_file():
        return str(p.resolve())
    # 相对项目根的路径：拼成绝对路径（若存在）
    try:
        root = Path(__file__).resolve().parent.parent.parent
        cand = (root / cmd).resolve()
        if cand.is_file():
            return str(cand)
    except Exception:
        pass
    return cmd


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
    msg = f"ERROR: {label} 编译失败 (rc={rc}):\n{detail}"
    try:
        from agent.errors import persist_error

        tool = "write_gen"
        if "validator" in label:
            tool = "write_validate"
        elif "gen_special" in label:
            tool = "write_special_gen"
        elif "check_special" in label:
            tool = "write_special_check"
        elif "checker" in label:
            tool = "write_checker"
        persist_error(_wd(), msg, tool=tool, kind="compile")
    except Exception:
        pass
    return msg


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


# ---- Special 元数据目录（decision.json 等）----
def _sanitize_scheme_id(scheme_id: str) -> str:
    sid = re.sub(r"[^a-zA-Z0-9_]", "_", (scheme_id or "special").strip()).strip("_")
    return sid or "special"


def special_meta_dir(scheme_id: str) -> Path:
    """special_meta/<scheme_id>/ 目录。"""
    return _wd() / SPECIAL_META_DIR_NAME / _sanitize_scheme_id(scheme_id)
