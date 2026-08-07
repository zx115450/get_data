"""运行类工具：run_gen / run_validate / run_std / read_range / property_check。"""
from sandbox.run import EXIT_MEMORY, is_stack_overflow, safe_run
from pipeline.gen_data import DEFAULT_REGULAR_COUNT

from .context import (
    DEFAULT_STACK_MB,
    _exe,
    _load_range_json,
    _memory_limit_mb,
    _pick_gen_exe,
    _resolve_std_cmd,
    _runtime_stack_limit_mb,
    _std,
    _std_timeout_s,
    _wd,
)

def run_property_check(input_text: str) -> str:
    """把 input 喂给 check_special；exit 0 → OK，否则 ERROR。"""
    exe = _wd() / _exe("check_special")
    if not exe.is_file():
        return "ERROR: check_special 未编译，请先 write_special_check"
    mem_mb = _memory_limit_mb()
    rc, out, err = safe_run(
        _exe("check_special"),
        stdin=input_text if input_text is not None else "",
        timeout=10,
        cwd=str(_wd()),
        memory_limit_mb=mem_mb,
    )
    if rc == 0:
        return "OK: property_check passed"
    if rc == 124:
        return "ERROR: property_check TIMEOUT"
    if rc == EXIT_MEMORY:
        return f"ERROR: property_check MEMORY_LIMIT ({mem_mb} MB)"
    detail = (err or out or "").strip()[:500]
    return f"ERROR: property_check failed rc={rc}: {detail or '(no stderr)'}"


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
    """内部：跑标程返回原始输出，失败返回 ERROR 开头字符串。

    使用绝对路径标程，不强制 cwd=job_dir（避免相对 std_cmd 找不到）。
    """
    cmd = _resolve_std_cmd()
    if not cmd:
        return "ERROR: std failed (rc=-1): 标程路径不可用"
    timeout = _std_timeout_s()
    mem_mb = _memory_limit_mb()
    rc, out, err = safe_run(
        cmd,
        stdin=input_text,
        timeout=timeout,
        memory_limit_mb=mem_mb,
    )
    if rc != 0:
        return f"ERROR: std failed (rc={rc}): {err or out}"
    return out or ""


def run_gen(seed: int, type: str = "random", index: int = -1, count: int = -1) -> str:
    """跑编译好的 gen 二进制：`./gen --seed N --type T [--index i --count C]`，返回 stdout。

    若 type == "special_samples" 且存在 gen_special 二进制，则自动转调 gen_special。
    超时硬上限 5 秒。超时直接判定 gen 算法不达标（通常是 O(n^2) 枚举），
    返回明确的 TIMEOUT 错误，提示 Agent 重写 gen.cpp。
    若 range.json 含 memory_limit_mb，则同步限制生成器内存。
    允许空 stdout（EOF 空输入合法时）；是否合法由 validator/标程判定。
    """
    if index < 0:
        index = seed
    if count is None or int(count) < 0:
        try:
            count = int((_load_range_json() or {}).get("count") or DEFAULT_REGULAR_COUNT)
        except Exception:
            count = DEFAULT_REGULAR_COUNT
    mem_mb = _memory_limit_mb()
    exe_name = _pick_gen_exe(type)
    rc, out, err = safe_run(
        f"{_exe(exe_name)} --seed {seed} --type {type} --index {index} --count {count}",
        timeout=5,
        cwd=str(_wd()),
        memory_limit_mb=mem_mb,
        stack_limit_mb=_runtime_stack_limit_mb(),
    )
    if rc != 0:
        if rc == 124:
            return (
                f"ERROR gen TIMEOUT after 5s type={type} seed={seed}: "
                f"gen 算法太慢或死循环。"
                f"先查：若 while/set 凑唯一值，uni 是否 > 域基数（hi-lo+1 / 候选 size）"
                f"→ 改为 uni=min(目标,域大小)；"
                f"否则避免 O(n^2) 枚举/预建大池，改有上限采样或 generator.h。"
                f"请 read_file(\"gen.cpp\") 找到对应分支，重新 write_gen，再继续自检。"
                f"不要重试同一段代码。"
            )
        if rc == EXIT_MEMORY:
            return (
                f"ERROR gen MEMORY_LIMIT ({mem_mb} MB) type={type} seed={seed}: "
                f"生成器内存超限。请降低单组规模，或避免 O(n^2) 大数组/边池；"
                f"必要时调高 range.json 的 memory_limit_mb。"
            )
        if is_stack_overflow(rc):
            return (
                f"ERROR gen STACK_OVERFLOW type={type} seed={seed}: "
                f"递归过深导致栈溢出（已链接 {DEFAULT_STACK_MB}MB 栈）；"
                f"请改为迭代/显式栈，或降低链深度"
            )
        return f"ERROR gen rc={rc}: {(err or '').strip()}"
    # 允许空 stdout：部分题（EOF 读入、m=0）合法输入就是空文件；
    # 是否合法由后续 validator / 标程判定，不再在此一律 ERROR。
    return out if out is not None else ""


def run_validate(input_text: str) -> str:
    """把 input_text 喂给编译好的 validator 二进制，合法返回 OK，非法返回 stderr。"""
    mem_mb = _memory_limit_mb()
    rc, out, err = safe_run(
        _exe("validator"),
        stdin=input_text,
        timeout=10,
        cwd=str(_wd()),
        memory_limit_mb=mem_mb,
        stack_limit_mb=_runtime_stack_limit_mb(),
    )
    if rc == 0:
        return "OK: valid"
    if rc == EXIT_MEMORY:
        return f"ERROR validate MEMORY_LIMIT ({mem_mb} MB): {(err or '').strip()}"
    if is_stack_overflow(rc):
        return (
            "ERROR validate STACK_OVERFLOW: 递归过深导致栈溢出"
            f"（已链接 {DEFAULT_STACK_MB}MB 栈）；"
            "请将连通性等检查改为并查集/BFS/显式栈，禁止深递归 DFS"
        )
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


def _is_special_type(typ: str) -> bool:
    t = (typ or "").strip()
    return t == "special_samples" or t.startswith("special:")

