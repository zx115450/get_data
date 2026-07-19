"""工具函数集合 + 对应的 schema（喂给 LLM 的工具定义）。

分两类工具：
- 通用：write_file / read_file / finish（hello demo 用）
- 数据生成专用：write_gen / write_validate / run_gen / run_validate / run_std / read_range

数据生成工具需要一个工作目录（work_dir）和标程命令（std_cmd），
由 main.py 在跑 Agent 前通过 set_context() 注入。
"""
import json
import os
import shutil
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))

from sandbox.run import safe_run

# testlib.h 单头文件库的位置（随项目分发，每个 job 目录拷一份）
TESTLIB_H = Path(__file__).resolve().parent.parent / "sandbox" / "testlib.h"


def _exe(base: str) -> str:
    """Windows 下可执行文件带 .exe，Linux/macOS 不带。"""
    return base + (".exe" if os.name == "nt" else "")


def _copy_testlib() -> None:
    """把 testlib.h 拷到工作目录，供 gen.cpp / validator.cpp #include。"""
    dst = WORK_DIR / "testlib.h"
    if TESTLIB_H.exists() and not dst.exists():
        shutil.copyfile(TESTLIB_H, dst)


# ---- 运行上下文（由 main.py 注入）----
WORK_DIR = Path(".")
STD_CMD = ""


def set_context(work_dir: str, std_cmd: str) -> None:
    global WORK_DIR, STD_CMD
    WORK_DIR = Path(work_dir)
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    STD_CMD = std_cmd


def _resolve(path: str) -> Path:
    """相对路径一律落到 WORK_DIR（job 目录），绝对路径原样。"""
    p = Path(path)
    if p.is_absolute():
        return p
    return WORK_DIR / p


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


def _validator_static_check(content: str) -> str | None:
    """编译前对 validator.cpp 做静态检查：必须包含 readEof 和 ensuref。"""
    if "readEof" not in content:
        return "ERROR: validator.cpp 必须调用 inf.readEof() 确认读到文件末尾，否则可能漏检 trailing 数据"
    if "ensuref" not in content:
        return "ERROR: validator.cpp 必须至少使用一次 ensuref(...) 对题意结构性质做显式校验"
    return None


# ---- 通用工具 ----
def write_file(path: str, content: str) -> str:
    """把 content 写入 WORK_DIR 下的 path。gen/validator/range 请改用专用工具。"""
    name = Path(path).name.lower()
    if name in ("gen.cpp", "gen.py"):
        return write_gen(content)
    if name in ("validator.cpp", "validate.py"):
        return write_validate(content)
    if name == "range.json":
        return write_range(content)
    p = _resolve(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return f"OK: wrote {len(content)} chars to {p}"


def read_file(path: str) -> str:
    """读取 WORK_DIR 下的文件（如 gen.cpp / validator.cpp）。"""
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
    p = WORK_DIR / "range.json"
    p.write_text(content, encoding="utf-8")
    return f"OK: wrote range.json ({len(content)} chars)"


def write_gen(content: str) -> str:
    """把生成器 C++ 源码写到 work_dir/gen.cpp，拷 testlib.h，编译成 gen(.exe)。"""
    if _looks_like_omitted_stub(content):
        return (
            "ERROR: content 像是历史摘要，不是完整 gen.cpp。"
            "请重新输出完整 C++ 源码（#include \"testlib.h\" + registerGen）。"
            "若需查看上一版，先 read_file(\"gen.cpp\")。"
        )
    if "registerGen" not in content and "testlib.h" not in content:
        return "ERROR: gen.cpp 必须 #include \"testlib.h\" 并调用 registerGen(argc, argv, 1)"
    (WORK_DIR / "gen.cpp").write_text(content, encoding="utf-8")
    _copy_testlib()
    rc, out, err = safe_run(
        "g++ -O2 -std=c++17 gen.cpp -o " + _exe("gen"),
        timeout=60, cwd=str(WORK_DIR),
    )
    if rc != 0:
        return _compile_err("gen.cpp", rc, out, err)
    return f"OK: wrote & compiled gen ({len(content)} chars)"


def write_validate(content: str) -> str:
    """把校验器 C++ 源码写到 work_dir/validator.cpp，拷 testlib.h，编译成 validator(.exe)。"""
    if _looks_like_omitted_stub(content):
        return (
            "ERROR: content 像是历史摘要，不是完整 validator.cpp。"
            "请重新输出完整 C++ 源码（#include \"testlib.h\" + registerValidation）。"
            "若需查看上一版，先 read_file(\"validator.cpp\")。"
        )
    if "registerValidation" not in content and "testlib.h" not in content:
        return "ERROR: validator.cpp 必须 #include \"testlib.h\" 并调用 registerValidation()"
    static_err = _validator_static_check(content)
    if static_err:
        return static_err
    (WORK_DIR / "validator.cpp").write_text(content, encoding="utf-8")
    _copy_testlib()
    rc, out, err = safe_run(
        "g++ -O2 -std=c++17 validator.cpp -o " + _exe("validator"),
        timeout=60, cwd=str(WORK_DIR),
    )
    if rc != 0:
        return _compile_err("validator.cpp", rc, out, err)
    return f"OK: wrote & compiled validator ({len(content)} chars)"


def write_checker(content: str) -> str:
    """把 special judge 的 C++ 源码写到 work_dir/checker.cpp，拷 testlib.h，编译成 checker(.exe)。"""
    if _looks_like_omitted_stub(content):
        return (
            "ERROR: content 像是历史摘要，不是完整 checker.cpp。"
            "请重新输出完整 C++ 源码（#include \"testlib.h\" + registerTestlibCmd）。"
            "若需查看上一版，先 read_file(\"checker.cpp\")。"
        )
    if "registerTestlibCmd" not in content and "testlib.h" not in content:
        return "ERROR: checker.cpp 必须 #include \"testlib.h\" 并调用 registerTestlibCmd(...)"
    (WORK_DIR / "checker.cpp").write_text(content, encoding="utf-8")
    _copy_testlib()
    rc, out, err = safe_run(
        "g++ -O2 -std=c++17 checker.cpp -o " + _exe("checker"),
        timeout=60, cwd=str(WORK_DIR),
    )
    if rc != 0:
        return _compile_err("checker.cpp", rc, out, err)
    return f"OK: wrote & compiled checker ({len(content)} chars)"


def run_gen(seed: int, type: str = "random", index: int = -1, count: int = 15) -> str:
    """跑编译好的 gen 二进制：`./gen --seed N --type T [--index i --count C]`，返回 stdout。

    超时硬上限 5 秒。超时直接判定 gen 算法不达标（通常是 O(n^2) 枚举），
    返回明确的 TIMEOUT 错误，提示 Agent 重写 gen.cpp。
    """
    if index < 0:
        index = seed
    rc, out, err = safe_run(
        f"{_exe('gen')} --seed {seed} --type {type} --index {index} --count {count}",
        timeout=5,
        cwd=str(WORK_DIR),
    )
    if rc != 0:
        if rc == 124:
            return (
                f"ERROR gen TIMEOUT after 5s type={type} seed={seed}: "
                f"gen 算法太慢（很可能用了 O(n^2) 枚举/预建大池子）。"
                f"请 read_file(\"gen.cpp\") 找到对应分支，改用 unordered_set 随机采样，"
                f"重新 write_gen，再继续自检。不要重试同一段代码。"
            )
        return f"ERROR gen rc={rc}: {(err or '').strip()}"
    return out if out else f"ERROR gen: empty output (stderr={(err or '').strip()})"


def run_validate(input_text: str) -> str:
    """把 input_text 喂给编译好的 validator 二进制，合法返回 OK，非法返回 stderr。"""
    rc, out, err = safe_run(_exe("validator"), stdin=input_text, timeout=10, cwd=str(WORK_DIR))
    if rc == 0:
        return "OK: valid"
    return f"ERROR validate rc={rc}: {err.strip()}"


def run_std(input_text: str) -> str:
    """把 input_text 喂给标程，返回 stdout（即答案）。std_cmd 相对项目根，在根目录跑。"""
    rc, out, err = safe_run(STD_CMD, stdin=input_text, timeout=10)
    if rc != 0:
        return f"ERROR std rc={rc}: {err.strip()}"
    return out if out else f"ERROR std: empty output (stderr={err.strip()})"


def read_range() -> str:
    """读取 work_dir/range.json 的内容，返回 JSON 字符串。"""
    p = WORK_DIR / "range.json"
    if not p.exists():
        return f"ERROR: {p} not found"
    return p.read_text(encoding="utf-8")


# ---- 函数表 ----
FUNCTIONS = {
    "write_file": write_file,
    "read_file": read_file,
    "finish": finish,
    "write_range": write_range,
    "write_gen": write_gen,
    "write_validate": write_validate,
    "write_checker": write_checker,
    "run_gen": run_gen,
    "run_validate": run_validate,
    "run_std": run_std,
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
        "把【完整】生成器 C++ 源码写到工作目录 gen.cpp 并 g++ 编译。random 分支必须用 --index/--count 在 [L,R] 分层取规模，禁止只抽 min(100,R)。禁止 std::shuffle(...,rnd)。",
        {"content": {"type": "string", "description": "完整 gen.cpp（#include \"testlib.h\"，registerGen(argc,argv,1)）"}},
        ["content"],
    ),
    _schema(
        "write_validate",
        "把【完整】校验器 C++ 源码写到工作目录 validator.cpp 并 g++ 编译。content 必须是完整源码，禁止摘要。用 readInt/readSpace/readEoln/readEof 严格校验。",
        {"content": {"type": "string", "description": "完整 validator.cpp（#include \"testlib.h\"，registerValidation()）"}},
        ["content"],
    ),
    _schema(
        "write_checker",
        "把【完整】special judge / checker C++ 源码写到工作目录 checker.cpp 并 g++ 编译。special judge 用于答案不唯一或需要额外判定的题目。",
        {"content": {"type": "string", "description": "完整 checker.cpp（#include \"testlib.h\"，registerTestlibCmd(...)）"}},
        ["content"],
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
        "把一段输入文本喂给标程，返回标程的输出（即答案）。",
        {"input_text": {"type": "string", "description": "标程的 stdin 输入"}},
        ["input_text"],
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


def dispatch(name: str, args: dict) -> str:
    """按名字执行工具，返回字符串结果（喂回 LLM）。"""
    fn = FUNCTIONS.get(name)
    if fn is None:
        return f"ERROR: unknown tool {name!r}"
    try:
        return str(fn(**args))
    except TypeError as e:
        return f"ERROR: bad args for {name}: {e}"
    except Exception as e:
        return f"ERROR: {name} raised {type(e).__name__}: {e}"
