"""工具 schema、FUNCTIONS 表与 dispatch。"""
from .checker import (
    run_checker,
    run_checker_self_check,
    use_builtin_checker,
    use_checker_template,
    write_checker,
)
from .context import CHECKER_TIMEOUT_S
from .run import (
    read_range,
    run_gen,
    run_property_check,
    run_std,
    run_validate,
)
from .self_check import run_self_check
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

# ---- 函数表 ----
FUNCTIONS = {
    "write_file": write_file,
    "read_file": read_file,
    "finish": finish,
    "write_range": write_range,
    "write_gen": write_gen,
    "write_special_gen": write_special_gen,
    "write_special_check": write_special_check,
    "write_validate": write_validate,
    "write_checker": write_checker,
    "use_builtin_checker": use_builtin_checker,
    "use_checker_template": use_checker_template,
    "run_gen": run_gen,
    "run_validate": run_validate,
    "run_property_check": run_property_check,
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
        "把数据范围写到 range.json。content 为合法 JSON：count 由你自定且不得小于 15；constraints；edge_cases（不要含 random；含 edge_n1/edge_nmax 等最小最大边界）。",
        {"content": {"type": "string", "description": "range.json 的完整 JSON 字符串"}},
        ["content"],
    ),
    _schema(
        "write_gen",
        "把【完整】生成器 C++ 源码写到工作目录 gen.cpp 并 g++ 编译。"
        "【content 硬约束】arguments 必须含 content=从 #include 到 main 结尾 } 的完整源码；"
        "禁止空调用、省略 content、半截文件、__OMITTED_SOURCE__；宜短而全，避免 JSON 截断（recovered/missing_content）。"
        "树/图/几何题优先 #include \"generator.h\" + using namespace generator::all；"
        "树/图必须先 t.gen()，再 cout << t，或 for (auto &e : t.edges())；"
        "get_edges() / Tree::shuffle() / 访问 _edges 不存在，写错会编译失败。"
        "仍须 registerGen；seed/type/index/count 必须在 type 分支前全部 opt<>() 消费"
        "（type 必须 string type=opt<string>(\"type\",\"random\")，禁止 opt<int>(\"type\")/type==0；"
        "禁止只在 random 里读，否则 edge_* 报 unused key）。"
        "random 分支用 --index/--count 分层取规模。"
        "禁止 std::shuffle(...,rnd)；禁止枚举 O(n^2) 边池。"
        "实现须对照题面+标程+range（多测 T、edge_cases 分支、约束变量全部 opt）。",
        {
            "content": {
                "type": "string",
                "description": (
                    "【必填·完整上下文】完整 gen.cpp 源码全文（含全部函数与 main 结尾 }），"
                    "不能为空、不能截断、不能是摘要。宜短而全。"
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
        "write_special_check",
        "把【完整】特殊样例性质检查器写到 check_special.cpp 并编译。"
        "从 stdin 读入与 gen 同格式输入；must_hold 全部成立则 exit 0，否则 exit 1 并在 stderr 写原因。"
        "禁止搜索/大表；只做 O(输入) 判定。空区间/端点平凡真必须判 FAIL。",
        {
            "content": {
                "type": "string",
                "description": "完整 check_special.cpp（读 stdin，exit 0/1）",
            }
        },
        ["content"],
    ),
    _schema(
        "run_property_check",
        "把 input_text 喂给已编译的 check_special，判定 must_hold 是否成立。",
        {
            "input_text": {
                "type": "string",
                "description": "待检查的完整输入（通常来自 run_gen）",
            }
        },
        ["input_text"],
    ),
    _schema(
        "write_validate",
        "把【完整】校验器 C++ 源码写到工作目录 validator.cpp 并 g++ 编译。"
        "【content 硬约束】必须传完整源码字符串；禁止空调用、半截、摘要；宜短而全，避免工具参数截断。"
        "读入顺序须与标程一致。registerValidation 后 inf.strict=false；连续 read*；"
        "收尾必须 skipBlanks()+readEof()（禁止裸 readEof）；"
        "只验范围与结构（ensuref），不验空格/换行格式；以编译/运行通过为准。",
        {
            "content": {
                "type": "string",
                "description": (
                    "【必填·完整上下文】完整 validator.cpp 全文（含 main 结尾 }），"
                    "不能为空/截断/摘要。需 registerValidation + inf.strict=false + read*；"
                    "结尾 inf.skipBlanks(); inf.readEof(); 有结构约束时加 ensuref；"
                    "禁止为格式写 readSpace/readEoln。"
                ),
            }
        },
        ["content"],
    ),
    _schema(
        "write_checker",
        "把【完整】special judge / checker C++ 源码写到工作目录 checker.cpp 并 g++ 编译。"
        "仅当答案不唯一或需额外判定时使用；若只需按行/词/浮点/YesNo 比较，请改用 use_builtin_checker。"
        "SPJ 只验答案合法性（_ok/_wa），不验输出格式；读完后 seekEof 排空再 quit。"
        "复杂度：设计 ≤1s，运行硬超时 2s；须符合 plan 第 6 节预算。"
        "只用 testlib 真实 API；禁止 isNumber；编译失败会删除旧 exe 且不计写次数额度。"
        "带空格整句用 readLine，禁止 readToken(\"s\")。",
        {
            "content": {
                "type": "string",
                "description": (
                    "完整 checker.cpp（#include \"testlib.h\"，registerTestlibCmd(...)；"
                    "只判合法性；quit 前排空 ouf；禁止 isNumber / 严格格式 _pe）"
                ),
            }
        },
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
        "安装 checker 模板，生成可编译的骨架 checker.cpp（占位 quitf(_fail)，不可直接当正式 checker）。"
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
        "安装后必须 write_checker 替换 TODO；未替换模板跑自检会判 [SYSTEM]。",
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
            "count": {"type": "integer", "description": "总组数，默认 30"},
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
        f"单测硬超时 {CHECKER_TIMEOUT_S}s（SPJ 设计目标 ≤1s）；超时视为复杂度超规。"
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
        "用当前 gen + std + checker 做 reactive 自检（仅一次正例）："
        "标程输出同时当 ouf/ans，必须返回 _ok；不跑负例扰动。"
        "未替换模板 / 源码新于 exe / 未编译 → [SYSTEM]；判定错 → [LOGIC]。"
        "通过返回 OK；失败返回 ERROR 详情。write_checker 编译成功后建议调用。",
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
    "write_gen", "write_special_gen", "write_special_check", "write_validate",
    "write_checker", "write_range", "write_file",
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
            "这是 content 书写错误：禁止空调用、禁止只传函数名、禁止省略 content。"
            "请立即重新调用，arguments 只能是 "
            '{"content":"#include ... 从首行到 main 结尾 } 的完整源码"}。'
            "宜短而全，避免再次被 JSON 截断。"
            "若上一版已在磁盘，先 read_file 读出再整份 write；"
            "禁止把 __OMITTED_SOURCE__ 摘要写回。"
        )
        if parse_failed:
            hint += (
                f" 另：工具参数 JSON 解析失败（常见于 content 过长被截断），预览={raw_preview!r}。"
                "请缩短实现后重新提交【完整】content（一次写全，勿半截）。"
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
    recovered = bool((args or {}).get("_args_recovered"))
    clean, err = normalize_tool_args(name, args)
    if err:
        return err
    try:
        result = str(fn(**clean))
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
    # 截断恢复出的正文常不完整：即使编译碰巧过，也强制提醒整份重写 content
    if recovered and name in _WRITE_CONTENT_TOOLS:
        clen = len(str(clean.get("content") or ""))
        warn = (
            f"\nWARN: {name} 的 content 来自截断 JSON 恢复（约 {clen} chars），"
            "很可能不完整。请立即用【完整】content 重新调用同一 write_*（宜短而全），"
            "禁止空调用；对照题面+标程+range 写全上下文。"
        )
        result = result + warn
    return result
