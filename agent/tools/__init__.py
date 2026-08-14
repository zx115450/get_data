"""工具函数集合 + 对应的 schema（喂给 LLM 的工具定义）。

分两类工具：
- 通用：write_file / read_file / finish（hello demo 用）
- 数据生成专用：write_gen / write_validate / run_gen / run_validate / run_std / read_range

数据生成工具需要一个工作目录（work_dir）和标程命令（std_cmd），
由 runner / main.py 在跑 Agent 前通过 set_context() 注入（基于 contextvars，线程隔离）。

实现按模块拆分：
- context: JobContext / 编译辅助 / 常量
- write: write_* / read_file / finish
- run: run_gen / run_validate / run_std / …
- checker: SPJ / builtin / template
- self_check: run_self_check / ensure_self_check_prereqs
- schema: TOOL_SCHEMAS / dispatch
"""
from .checker import (
    run_checker,
    run_checker_self_check,
    use_builtin_checker,
    use_checker_template,
    write_checker,
)
from .context import (
    BUILTIN_CHECKERS,
    BUILTIN_CHECKER_HELP,
    CHECKER_SRC_DIR,
    CHECKER_TEMPLATE_DIR,
    CHECKER_TEMPLATES,
    CHECKER_TIMEOUT_S,
    DEFAULT_STACK_BYTES,
    DEFAULT_STACK_MB,
    GENERATOR_H,
    GENERATOR_PROBLEM_TYPES,
    SPECIAL_META_DIR_NAME,
    TESTLIB_H,
    JobContext,
    _wd,
    get_context,
    prewarm_generator_headers,
    reset_context,
    set_context,
    special_meta_dir,
)
from .run import (
    read_range,
    run_gen,
    run_property_check,
    run_std,
    run_validate,
)
from .schema import (
    FUNCTIONS,
    TOOL_SCHEMAS,
    _schema,
    dispatch,
    normalize_tool_args,
)
from .self_check import (
    ensure_self_check_prereqs,
    format_self_check_for_progress,
    run_self_check,
)
from .write import (
    finish,
    read_file,
    write_file,
    write_gen,
    write_range,
    write_special_check,
    write_special_gen,
    write_validate,
)

__all__ = [
    "BUILTIN_CHECKERS",
    "BUILTIN_CHECKER_HELP",
    "CHECKER_SRC_DIR",
    "CHECKER_TEMPLATE_DIR",
    "CHECKER_TEMPLATES",
    "CHECKER_TIMEOUT_S",
    "DEFAULT_STACK_BYTES",
    "DEFAULT_STACK_MB",
    "FUNCTIONS",
    "GENERATOR_H",
    "GENERATOR_PROBLEM_TYPES",
    "JobContext",
    "SPECIAL_META_DIR_NAME",
    "TESTLIB_H",
    "TOOL_SCHEMAS",
    "_schema",
    "_wd",
    "dispatch",
    "ensure_self_check_prereqs",
    "finish",
    "format_self_check_for_progress",
    "get_context",
    "normalize_tool_args",
    "prewarm_generator_headers",
    "read_file",
    "read_range",
    "reset_context",
    "run_checker",
    "run_checker_self_check",
    "run_gen",
    "run_property_check",
    "run_self_check",
    "run_std",
    "run_validate",
    "set_context",
    "special_meta_dir",
    "use_builtin_checker",
    "use_checker_template",
    "write_checker",
    "write_file",
    "write_gen",
    "write_range",
    "write_special_check",
    "write_special_gen",
    "write_validate",
]


def __getattr__(name: str):
    """兼容外部读取 tools.WORK_DIR / tools.STD_CMD。"""
    if name == "WORK_DIR":
        return _wd()
    if name == "STD_CMD":
        from .context import _std

        return _std()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
