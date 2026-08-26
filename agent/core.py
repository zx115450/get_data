"""Agent 主循环：消息 + 工具 + 循环。

支持按阶段切换 System Prompt（stage_prompts）：例如先让 Agent 只写 range.json，
写完后自动切换为 gen/validator 阶段的完整 prompt，避免一个巨型 system 从头用到尾。

支持 write_check_discipline：成功 write_gen/write_validate 后自动跑快速自检，
禁止未自检连续改写。

支持 checker_write_discipline：成功 write_checker 后自动 run_checker_self_check，
每步最多一次 write_checker，自检通过后禁止再写。
"""
import concurrent.futures

from . import llm, prompts, tools
from . import trace as agent_trace

# 默认完整版：兼容旧代码，仍可用 system_prompt 覆盖。
SYSTEM_PROMPT = prompts.default_full_prompt

# 写代码类工具：门禁针对这些（不含 write_range）
_WRITE_CODE_TOOLS = frozenset({
    "write_gen", "write_special_gen", "write_special_check",
    "write_validate", "write_file",
})

_CHECKER_WRITE_TOOLS = frozenset({"write_checker"})


# 阶段切换提示词
_STAGE_SWITCH = {
    "range_to_gen": "【阶段切换】range 已规划完成，现在进入 gen/validator 阶段。请按新的 system 规则写 C++ 生成器/校验器，并完成自检。",
    "gen_to_check": "【阶段切换】gen/validator 已就绪，现在进入强化自检阶段。请调用 run_self_check()，通过后调 finish。",
}


def _switch_stage(
    messages: list,
    current_stage: str,
    target_stage: str,
    stage_prompts: dict[str, str],
    *,
    stage_tool_schemas: dict[str, list] | None = None,
    current_schemas: list | None = None,
    verbose: bool = True,
    step: int = 0,
) -> tuple[str, list]:
    """替换 messages[0] 的 system prompt，并追加阶段切换提示。

    若提供了 stage_tool_schemas，会同步切换当前可用工具列表。
    返回 (new_stage, new_schemas)。
    """
    new_prompt = stage_prompts.get(target_stage)
    if not new_prompt or target_stage == current_stage:
        return current_stage, current_schemas or []

    messages[0]["content"] = new_prompt
    switch_msg = _STAGE_SWITCH.get(f"{current_stage}_to_{target_stage}",
                                   f"【阶段切换】进入 {target_stage} 阶段，请按新的 system 规则继续。")
    messages.append({"role": "user", "content": switch_msg})
    if verbose:
        print(f"[step {step}] stage switch: {current_stage} -> {target_stage}")

    new_schemas = current_schemas
    if stage_tool_schemas is not None and target_stage in stage_tool_schemas:
        new_schemas = stage_tool_schemas[target_stage]
    return target_stage, new_schemas or []


def _filter_schemas(schemas: list, deny_names: set[str] | frozenset[str]) -> list:
    """从 tool schemas 中去掉指定工具。"""
    out = []
    for s in schemas:
        name = (s.get("function") or {}).get("name", "")
        if name not in deny_names:
            out.append(s)
    return out if out else schemas


def _schema_tool_names(schemas: list | None) -> set[str]:
    names: set[str] = set()
    for s in schemas or []:
        name = (s.get("function") or {}).get("name", "")
        if name:
            names.add(name)
    return names


def _is_range_only_schemas(schemas: list | None) -> bool:
    """当前可用工具是否仅为 write_range + finish（range-only 阶段）。"""
    names = _schema_tool_names(schemas)
    return bool(names) and names <= {"write_range", "finish"}


def _norm_read_path(path: str) -> str:
    """归一化 read_file 路径，用于去重（相对路径、统一斜杠与大小写）。"""
    p = (path or "").strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p.lower()


def _preview(result: str, limit: int = 120) -> str:
    """进度预览：编译失败时保留完整首条 error 行，避免截成 error: cal…。"""
    text = result or ""
    if text.startswith("ERROR:") and ("编译失败" in text or "compile" in text.lower()):
        lines = text.splitlines()
        head: list[str] = []
        err_line = ""
        for ln in lines:
            head.append(ln)
            if (not err_line) and ("error:" in ln.lower()):
                err_line = ln.strip()
                break
            if len("\n".join(head)) > 280:
                break
        preview = "\n".join(head)
        if err_line and err_line not in preview:
            preview = preview + "\n" + err_line
        if len(preview) <= 600:
            return preview
        # 仍过长：前缀 + 完整 error 行
        prefix = preview[:400].rstrip()
        if err_line:
            return f"{prefix}\n...\n{err_line}"
        return prefix + "..."
    return text if len(text) <= limit else text[:limit] + "..."


def _truncate_for_messages(name: str, result: str) -> str:
    """把工具结果压到适合塞回 LLM 上下文的长度。"""
    if name == "run_gen" and len(result) > 2000 and not result.startswith("ERROR"):
        lines = result.splitlines()
        first_line = lines[0] if lines else ""
        last_line = lines[-1] if lines else ""
        return (
            f"[run_gen] generated {len(result)} chars, {len(lines)} lines. "
            f"first line: {first_line[:80]}... last line: {last_line[:80]}..."
        )
    if name == "run_validate" and len(result) > 1200 and result.startswith("OK"):
        return "OK: valid"
    if name == "run_std" and len(result) > 1200 and not result.startswith("ERROR"):
        lines = result.splitlines()
        return (
            f"[run_std] produced {len(result)} chars, {len(lines)} lines. "
            f"first line: {lines[0][:80]}... last line: {lines[-1][:80]}..."
        )
    if name == "run_self_check" and len(result) > 3000:
        if result.startswith("OK"):
            return "OK: self_check passed (details truncated)"
        return result[:2500] + f"\n...[{len(result)} chars truncated]...\n" + result[-500:]
    if len(result) > 4000:
        return result[:2000] + f"\n...[{len(result)} chars truncated]...\n" + result[-800:]
    return result


def run(
    task: str,
    max_steps: int = 30,
    verbose: bool = True,
    system_prompt: str | None = None,
    stage_prompts: dict[str, str] | None = None,
    stage_tool_schemas: dict[str, list] | None = None,
    on_event=None,
    tool_schemas=None,
    write_check_discipline: bool = False,
    checker_write_discipline: bool = False,
    self_check_fast: bool = False,
    self_check_args: dict | None = None,
    tool_limits: dict[str, int] | None = None,
) -> str:
    """跑一轮 Agent。

    task:           给 Agent 的任务描述
    max_steps:      最多循环多少轮，防止空转
    verbose:        是否打印每步动作
    system_prompt:  单阶段系统提示词；与 stage_prompts 二选一，默认用 prompts.default_full_prompt
    stage_prompts:  阶段化 system 字典，如 {"range": "...", "gen": "...", "check": "..."}
                    Agent 在调用 write_range 后自动切到 gen 阶段的 prompt；
                    在 run_self_check 之后可切到 check 阶段的 prompt（若提供）。
    stage_tool_schemas: 阶段化工具列表，与 stage_prompts 的键对齐；
                    切换阶段时同步换可用工具，例如 gen 阶段不暴露 write_checker。
    on_event:       可选回调 on_event(step, name, args, preview)，用于外部追踪进度
    tool_schemas:   可选，覆盖默认 TOOL_SCHEMAS（例如只允许 write_range）；
                    若同时用 stage_tool_schemas，base_schemas 默认不会被使用。
    write_check_discipline:
                    True 时启用「写一次 → 自动快速自检」硬门禁：
                    - 同一步可并行 write_gen + write_validate（算一次 fix）
                    - 写入成功且本步未自检 → 先 ensure_self_check_prereqs
                      （缺 exe 时尝试用磁盘 .cpp 重编译；本步刚编译失败的项跳过重编；仍缺则提示修复，不跑自检）
                    - 双产物就绪后系统自动 run_self_check(fast)
                    - 自检通过后禁止再 write_*，只能 finish / 只读工具
                    - 自检失败后允许再写一轮，写完再次自动自检
    checker_write_discipline:
                    True 时启用 checker「写一次 → 自动自检」：
                    - 每步最多一次 write_checker
                    - 写入并编译成功且本步未自检 → 自动 run_checker_self_check
                    - 编译失败不计入 tool_limits 中的 write_checker 次数
                    - 自检通过后禁止再 write_checker，只能 finish
    self_check_fast:
                    True 时 Agent 主动调用的 run_self_check 强制 fast_mode=True
                    （与门禁的自动自检一致；完整自检由外层 runner 负责）。
    self_check_args:
                    额外传给 run_self_check / ensure_self_check_prereqs 的参数，
                    如 {"skip_special": True} 或 {"special_only": True}。
    tool_limits:      限制单个工具最多被调用次数，例如 {"write_special_check": 3}。
                      超过限制时该工具返回 ERROR，并提示模型改用其他工具或 finish。
    返回:           finish 的 summary，或 "预算用尽"
    """
    _sc_args = dict(self_check_args or {})
    if system_prompt is None and stage_prompts is None:
        system_prompt = SYSTEM_PROMPT

    base_schemas = tool_schemas if tool_schemas is not None else tools.TOOL_SCHEMAS
    schemas = base_schemas

    # 初始化阶段
    if stage_prompts is not None:
        initial_stage = "range" if "range" in stage_prompts else "gen"
        current_system = stage_prompts.get(
            initial_stage,
            stage_prompts.get("gen", system_prompt or SYSTEM_PROMPT),
        )
        current_stage = initial_stage
        if stage_tool_schemas is not None and initial_stage in stage_tool_schemas:
            schemas = stage_tool_schemas[initial_stage]
    else:
        current_system = system_prompt or SYSTEM_PROMPT
        current_stage = "single"

    messages = [
        {"role": "system", "content": current_system},
        {"role": "user", "content": task},
    ]

    # 工具调用次数限制
    tool_counts: dict[str, int] = {name: 0 for name in (tool_limits or {})}

    # LLM 偶尔会只返回文本不调工具（如「解释一下思路」）。此时不直接 finish，
    # 而是发一条 nudge 提醒它调工具。连续 nudge 太多次仍不改，才放弃。
    max_nudges = 3
    nudge_count = 0

    # 门禁状态：快速自检是否已通过（通过后禁止再写）
    check_ok = False
    checker_check_ok = False
    # range-only：write_range 成功后自动结束；失败可在后续步骤重写（每步最多一次）
    range_only = _is_range_only_schemas(schemas)
    range_done = False
    # 同一 Agent 轮次内已成功读过的路径，禁止重复 read_file（如两次 gen_plan.md）
    read_paths_ok: set[str] = set()

    for step in range(1, max_steps + 1):
        # 自检通过后从 schema 里拿掉写代码工具，减少模型违规调用
        step_schemas = schemas
        if write_check_discipline and check_ok:
            step_schemas = _filter_schemas(schemas, _WRITE_CODE_TOOLS)
        if checker_write_discipline and checker_check_ok:
            step_schemas = _filter_schemas(step_schemas, _CHECKER_WRITE_TOOLS)
        # range 已写好：不再暴露 write_range，只留 finish（若模型仍不 finish 则下一步自动结束）
        if range_only and range_done:
            step_schemas = _filter_schemas(step_schemas, {"write_range"})

        try:
            actions = llm.chat(messages, step_schemas)
        except llm.TokenBudgetExceeded as e:
            if verbose:
                print(f"[step {step}] token budget exceeded: {e}")
            if on_event:
                on_event(step, "budget", {"reason": str(e)}, str(e)[:120])
            agent_trace.log_event(
                step=step, tool="budget", result=str(e), args_size=0,
            )
            return f"预算用尽（token）：{e}"

        # LLM 没调任何工具：发 nudge 提醒它调工具，而不是直接 finish。
        # 这避免了「LLM 解释思路 → Agent 立刻终止 → 没产出 gen」的常见失败模式。
        if not actions:
            if range_only and range_done:
                if verbose:
                    print(f"[step {step}] range already written; auto-finish (no tool call)")
                if on_event:
                    on_event(step, "finish", {"auto": True, "reason": "range_written"}, "已写出 range.json")
                agent_trace.log_event(
                    step=step, tool="finish", result="OK",
                    extra={"auto": True, "reason": "range_written"},
                )
                return "已写出 range.json（write_range 成功后自动结束）"
            nudge_count += 1
            if nudge_count > max_nudges:
                if verbose:
                    print(f"[step {step}] LLM 连续 {max_nudges} 次不调工具，放弃")
                if on_event:
                    on_event(step, "give_up", {"reason": "LLM 连续多次不调工具"}, "")
                agent_trace.log_event(
                    step=step, tool="give_up", result="LLM 连续多次不调工具",
                )
                return "预算用尽（LLM 连续多次不调工具）"
            if write_check_discipline and check_ok:
                nudge_msg = (
                    "快速自检已通过。请立即调用 finish(summary) 结束，"
                    "不要再改写 gen/validator，也不要只输出文字。"
                )
            else:
                nudge_msg = (
                    "你上一条回复没有调用任何工具。任务还没完成——"
                    "请直接调用工具（write_range / write_gen / write_validate / run_self_check 等）来执行，"
                    "不要只输出文字解释。如果是想看已有文件，调 read_file；"
                    "如果任务已完成（且 run_self_check 已 OK），调 finish。"
                )
            messages.append({"role": "user", "content": nudge_msg})
            if verbose:
                print(f"[step {step}] nudge #{nudge_count}（LLM 没调工具）")
            if on_event:
                on_event(step, "nudge", {"count": nudge_count}, nudge_msg[:120])
            agent_trace.log_event(
                step=step, tool="nudge", result=nudge_msg[:80],
                extra={"count": nudge_count},
            )
            continue

        # LLM 调了工具，重置 nudge 计数
        nudge_count = 0

        # 如果一轮里出现 write_gen / write_validate / write_checker / use_builtin_checker，并行编译以节省时间
        results = [None] * len(actions)
        writer_indices = {}
        # checker / range：同一步若出现多次写入，只执行第一次
        checker_write_seen = False
        range_write_seen = False
        for idx, act in enumerate(actions):
            # 门禁：自检通过后禁止写代码（即使模型仍塞了 write_*）
            if write_check_discipline and check_ok and act.name in _WRITE_CODE_TOOLS:
                results[idx] = (
                    "ERROR: 快速自检已通过，禁止继续改写 gen/validator。"
                    "请调用 finish(summary) 结束本阶段。"
                )
                continue
            if checker_write_discipline and checker_check_ok and act.name in _CHECKER_WRITE_TOOLS:
                results[idx] = (
                    "ERROR: checker 自检已通过，禁止继续 write_checker。"
                    "请调用 finish(summary) 结束本阶段。"
                )
                continue
            if (
                checker_write_discipline
                and act.name == "write_checker"
                and checker_write_seen
            ):
                results[idx] = (
                    "ERROR: 本步已有一次 write_checker，禁止同轮连写。"
                    "请先 run_checker_self_check()；失败后再写下一轮。"
                )
                continue
            if act.name == "write_checker":
                checker_write_seen = True
            if (
                range_only
                and act.name == "write_range"
                and range_write_seen
            ):
                results[idx] = (
                    "ERROR: 本步已有一次 write_range，禁止同轮连写。"
                    "若本次/上一调用返回 ERROR（缺 content / JSON 非法 / 校验失败），"
                    "请在下一轮再 write_range 整份修正；成功后系统会自动结束。"
                )
                continue
            if act.name == "write_range":
                range_write_seen = True
            if range_only and range_done and act.name == "write_range":
                results[idx] = (
                    "ERROR: range.json 已写好，禁止再次 write_range。"
                    "请调用 finish(summary) 结束本阶段。"
                )
                continue
            if act.name in ("write_gen", "write_validate", "write_checker", "use_builtin_checker"):
                writer_indices[act.name] = idx
        parallel_writers = [name for name in ("write_gen", "write_validate", "write_checker", "use_builtin_checker")
                            if name in writer_indices]
        if len(parallel_writers) > 1:
            if verbose:
                print(f"[step {step}] parallel compile {', '.join(parallel_writers)}")
            with concurrent.futures.ThreadPoolExecutor(max_workers=len(parallel_writers)) as executor:
                futures = {
                    name: executor.submit(
                        tools.dispatch, actions[writer_indices[name]].name, actions[writer_indices[name]].args
                    )
                    for name in parallel_writers
                }
                for name, fut in futures.items():
                    results[writer_indices[name]] = fut.result()

        range_written_ok = False
        had_successful_write = False
        had_self_check = False
        had_successful_checker_write = False
        had_checker_self_check = False
        # 本步 write_gen / write_validate 已失败时，precheck 禁止用同一份坏源码重编
        skip_recompile: set[str] = set()

        for idx, act in enumerate(actions):
            # 工具调用次数限制：超限时直接返回错误，不执行工具
            # 门禁预拒（results 已是 ERROR）不计入次数，避免同轮连写误耗额度
            pre_set = results[idx]
            is_pre_reject = (
                isinstance(pre_set, str)
                and pre_set.startswith("ERROR:")
                and (
                    "禁止继续" in pre_set
                    or "禁止同轮连写" in pre_set
                    or "禁止再次 write_range" in pre_set
                )
            )
            if tool_limits and act.name in tool_limits and not is_pre_reject:
                limit = tool_limits[act.name]
                if tool_counts[act.name] >= limit:
                    if act.name == "write_checker":
                        limit_msg = (
                            f"ERROR: write_checker 已超过调用次数限制"
                            f"（最多 {limit} 次编译成功写入）。"
                            "编译失败不计入次数；请 run_checker_self_check()；"
                            "若已自检则 finish(summary)。禁止继续空转改写。"
                        )
                    else:
                        limit_msg = (
                            f"ERROR: 工具 {act.name} 已超过调用次数限制（最多 {limit} 次）。"
                            "请改用其他工具或调用 finish(summary) 结束本阶段。"
                        )
                    results[idx] = limit_msg
                    messages.append({
                        "role": "tool",
                        "tool_call_id": act.tool_call_id,
                        "content": limit_msg,
                    })
                    if verbose:
                        print(f"[step {step}] {act.name} blocked (limit {limit})")
                    if on_event:
                        on_event(step, act.name, dict(act.args), _preview(limit_msg))
                    continue
                tool_counts[act.name] += 1

            if verbose:
                print(f"[step {step}] {act.name} {act.args}")

            if act.name == "finish":
                summary = act.args.get("summary", "done")
                if verbose:
                    print(f"[done] {summary}")
                agent_trace.log_event(
                    step=step,
                    tool="finish",
                    result="OK",
                    args_size=agent_trace.args_size(act.args),
                )
                return summary

            result = results[idx]
            if result is None:
                # 同一步里若已成功写过 range，后续 write_range 直接拒掉
                if range_only and range_done and act.name == "write_range":
                    result = (
                        "ERROR: range.json 已写好，禁止再次 write_range。"
                        "请调用 finish(summary) 结束本阶段。"
                    )
                # 强制快速自检：Agent 主动调 run_self_check 时注入 fast_mode
                elif act.name == "run_self_check":
                    args = dict(act.args or {})
                    if self_check_fast or write_check_discipline:
                        args["fast_mode"] = True
                    args.update(_sc_args)
                    result = tools.dispatch(act.name, args)
                elif act.name == "read_file":
                    path = str((act.args or {}).get("path") or "")
                    key = _norm_read_path(path)
                    if key and key in read_paths_ok:
                        result = (
                            f"ERROR: 本阶段已读过 {path}，内容已在上文工具结果中。"
                            "禁止重复 read_file。请直接 write_gen / write_validate / finish。"
                        )
                    else:
                        result = tools.dispatch(act.name, act.args)
                        if key and isinstance(result, str) and not result.startswith("ERROR"):
                            read_paths_ok.add(key)
                else:
                    result = tools.dispatch(act.name, act.args)
                results[idx] = result

            result_for_llm = _truncate_for_messages(act.name, result)

            if verbose:
                print(f"        -> {_preview(result)}")
            if on_event:
                on_event(step, act.name, dict(act.args), _preview(result))
            agent_trace.log_event(
                step=step,
                tool=act.name,
                result=result if isinstance(result, str) else str(result),
                args_size=agent_trace.args_size(act.args),
            )

            # 把工具结果作为 tool 消息塞回上下文
            # 必须带 tool_call_id，对应 assistant 的 tool_calls
            messages.append({
                "role": "tool",
                "tool_call_id": act.tool_call_id,
                "content": result_for_llm,
            })

            # 同一步并行多次读同一文件：第一次成功后登记，后续拒掉
            if act.name == "read_file" and isinstance(result, str) and not result.startswith("ERROR"):
                key = _norm_read_path(str((act.args or {}).get("path") or ""))
                if key:
                    read_paths_ok.add(key)

            if act.name == "write_range" and not str(result_for_llm).startswith("ERROR"):
                range_written_ok = True
                range_done = True

            if act.name in _WRITE_CODE_TOOLS and isinstance(result, str) and result.startswith("OK"):
                had_successful_write = True
            elif act.name == "write_checker" and isinstance(result, str) and result.startswith("OK"):
                had_successful_checker_write = True
            elif act.name == "write_checker" and isinstance(result, str) and result.startswith("ERROR"):
                # 编译失败 / 摘要拒写：不计入 write_checker 成功额度，便于修编译
                if tool_limits and "write_checker" in tool_counts:
                    tool_counts["write_checker"] = max(
                        0, tool_counts.get("write_checker", 1) - 1
                    )
            elif act.name == "write_range" and isinstance(result, str) and result.startswith("ERROR"):
                # 缺 content / JSON 非法 / 校验失败：不占额度，下一轮可再写
                if tool_limits and "write_range" in tool_counts:
                    tool_counts["write_range"] = max(
                        0, tool_counts.get("write_range", 1) - 1
                    )
            elif act.name == "write_gen" and isinstance(result, str) and result.startswith("ERROR"):
                skip_recompile.add("gen")
            elif act.name == "write_special_gen" and isinstance(result, str) and result.startswith("ERROR"):
                skip_recompile.add("gen_special")
            elif act.name == "write_special_check" and isinstance(result, str) and result.startswith("ERROR"):
                skip_recompile.add("check_special")
            elif act.name == "write_validate" and isinstance(result, str) and result.startswith("ERROR"):
                skip_recompile.add("validator")

            if act.name == "run_self_check":
                had_self_check = True
                if isinstance(result, str) and result.startswith("OK"):
                    check_ok = True
                else:
                    check_ok = False

            if act.name == "run_checker_self_check":
                had_checker_self_check = True
                if isinstance(result, str) and result.startswith("OK"):
                    checker_check_ok = True
                else:
                    checker_check_ok = False

        # range-only：本步 write_range 已成功 → 立即结束，杜绝反复改写空转
        if range_only and range_written_ok:
            if verbose:
                print(f"[step {step}] write_range OK; auto-finish (range-only)")
            if on_event:
                on_event(
                    step, "finish",
                    {"auto": True, "reason": "write_range_ok"},
                    "已写出 range.json",
                )
            return "已写出 range.json（write_range 成功后自动结束）"

        # write_* 缺 content / 参数截断 / 截断恢复：立刻强提醒，避免空转
        missing_content = any(
            act.name in _WRITE_CODE_TOOLS | {"write_range"}
            and isinstance(results[i], str)
            and (
                "缺少必填参数 content" in results[i]
                or "参数不完整" in results[i]
                or "参数疑似截断" in results[i]
                or "JSON 解析失败" in results[i]
                or "不是合法 JSON" in results[i]
                or "range.json 校验失败" in results[i]
                or "来自截断 JSON 恢复" in results[i]
            )
            for i, act in enumerate(actions)
        )
        if missing_content:
            follow = (
                "【硬错误 · 上一轮写入失败】请在下一轮再调用一次 write_* 整份修正（本步禁止连写）。"
            )
            if any(
                act.name == "write_range"
                and isinstance(results[i], str)
                and results[i].startswith("ERROR")
                for i, act in enumerate(actions)
            ):
                follow = (
                    "【硬错误 · write_range 失败】content 必须是完整合法 range.json。"
                    "下一轮再调用一次 write_range（arguments 只能是 "
                    '{"content":"{\\"count\\":15,\\"constraints\\":...,\\"edge_cases\\":[...]}"}'
                    "）；禁止空调用、禁止同轮连写。"
                    "校验失败则按 ERROR 逐条改完再写。无已有 range 时禁止 finish「无需重写」。"
                )
            else:
                follow = (
                    "【硬错误 · content 书写】上一轮 write_* 的 content 不完整"
                    "（空参数、工具 JSON 被截断、或 recovered 残缺源码）。"
                    "下一轮必须重新调用同一个 write_*，arguments 形如 "
                    '{"content":"#include ... 完整可编译源码到 main 结尾 }"}；'
                    "宜短而全，避免再次截断。"
                    "若磁盘已有旧版：先 read_file(\"gen.cpp\") / validator.cpp，再整份写出。"
                    "禁止再次空调用；禁止 __OMITTED_SOURCE__ /「其余不变」摘要。"
                    "同时对照题面+标程+range 写全 edge_cases 分支与 opt 参数。"
                )
            messages.append({"role": "user", "content": follow})
            if on_event:
                on_event(step, "nudge", {"reason": "missing_write_content"}, follow[:120])

        # 门禁：成功写入且本步未自检 → 先确保双产物就绪，再自动快速自检
        if write_check_discipline and had_successful_write and not had_self_check:
            require_special = None
            if _sc_args.get("skip_special"):
                require_special = False
            elif _sc_args.get("special_only"):
                require_special = True
            ready, prep_msg = tools.ensure_self_check_prereqs(
                skip_recompile=skip_recompile,
                require_special=require_special,
            )
            if on_event:
                on_event(
                    step, "precheck",
                    {"ready": ready, "auto": True, "skip_recompile": sorted(skip_recompile)},
                    _preview(prep_msg, 200),
                )
            if not ready:
                # gen/validator 尚未齐备：不跑自检，提示补写 / 修复编译失败项
                check_ok = False
                if _sc_args.get("special_only"):
                    follow = (
                        "【系统自检前置检查未通过】尚未自动跑 run_self_check。\n"
                        f"{prep_msg}\n\n"
                        "本阶段需写出可编译的 gen_special.cpp（write_special_gen）"
                        "与 check_special.cpp（write_special_check）；"
                        "不要改 gen.cpp / validator.cpp。"
                    )
                elif skip_recompile:
                    follow = (
                        "【系统自检前置检查未通过】尚未自动跑 run_self_check。\n"
                        f"{prep_msg}\n\n"
                        "本步部分 write_* 已失败，系统不会用同一份坏源码再重编译。"
                        "请根据上方 ERROR 修复后重新写出完整源码；"
                        "gen 与 validator 都编译成功后才会自动快速自检。"
                    )
                else:
                    # 按磁盘产物点名：避免「补齐 write_gen/validate」被理解成再刷已成功侧
                    from agent.tools.context import _exe, _wd

                    gen_ok = (_wd() / _exe("gen")).is_file()
                    val_ok = (_wd() / _exe("validator")).is_file()
                    if gen_ok and not val_ok:
                        follow = (
                            "【系统自检前置检查未通过】尚未自动跑 run_self_check。\n"
                            f"{prep_msg}\n\n"
                            "【硬】gen 已编译通过，只缺 validator。\n"
                            "下一轮必须调用 write_validate(完整源码)；"
                            "禁止再 write_gen / 微调 gen / read_file(gen.cpp)。\n"
                            "validator 须含 #include \"testlib.h\" + registerValidation + main，"
                            "禁止摘要/「已写入」冒充源码。"
                        )
                    elif val_ok and not gen_ok:
                        follow = (
                            "【系统自检前置检查未通过】尚未自动跑 run_self_check。\n"
                            f"{prep_msg}\n\n"
                            "【硬】validator 已编译通过，只缺 gen。\n"
                            "下一轮必须调用 write_gen(完整源码)；禁止再 write_validate。"
                        )
                    else:
                        follow = (
                            "【系统自检前置检查未通过】尚未自动跑 run_self_check。\n"
                            f"{prep_msg}\n\n"
                            "请先补齐缺失侧：缺 gen 则 write_gen，缺 validator 则 write_validate"
                            "（可同轮并行）；两者都编译成功后系统才会自动快速自检。"
                            "禁止在已成功的一侧反复空写。"
                        )
                messages.append({"role": "user", "content": follow})
                if verbose:
                    print(f"[step {step}] skip auto self_check: {prep_msg}")
            else:
                if verbose:
                    print(
                        f"[step {step}] auto run_self_check(fast_mode=True) "
                        f"after successful write ({prep_msg})"
                    )
                auto_kwargs = {"fast_mode": True, **_sc_args}
                auto_result = tools.run_self_check(**auto_kwargs)
                # 失败时保留足够长的 FAIL 行供日志复盘（成功仍短预览）
                if isinstance(auto_result, str) and auto_result.startswith("ERROR"):
                    auto_preview = auto_result if len(auto_result) <= 4000 else (
                        auto_result[:3200] + f"\n...[{len(auto_result)} chars]...\n" + auto_result[-600:]
                    )
                    try:
                        fail_path = tools._wd() / "self_check_last_fail.txt"
                        fail_path.write_text(auto_result, encoding="utf-8")
                    except Exception:
                        pass
                else:
                    auto_preview = _preview(auto_result, 200)
                if on_event:
                    on_event(
                        step, "run_self_check",
                        {**auto_kwargs, "auto": True},
                        auto_preview,
                    )

                if isinstance(auto_result, str) and auto_result.startswith("OK"):
                    check_ok = True
                    follow = (
                        "【系统自动快速自检】写入成功后已自动执行 run_self_check(fast_mode=True)，结果 OK。\n"
                        f"{_truncate_for_messages('run_self_check', auto_result)}\n\n"
                        "请立即调用 finish(summary) 结束。"
                        "禁止再 write_gen / write_validate / write_special_gen / write_special_check。"
                    )
                else:
                    check_ok = False
                    if _sc_args.get("special_only"):
                        fix_hint = (
                            "请根据 FAIL 再修改一轮（write_special_gen 和/或 write_special_check），"
                        )
                    else:
                        fix_hint = "请根据 FAIL 再修改一轮（可同轮 write_gen + write_validate），"
                    follow = (
                        "【系统自动快速自检】写入成功后已自动执行 run_self_check(fast_mode=True)，未通过。\n"
                        f"{_truncate_for_messages('run_self_check', auto_result)}\n\n"
                        f"{fix_hint}"
                        "写完后系统会再次自动快速自检。禁止未自检连续多次改写。"
                    )
                messages.append({"role": "user", "content": follow})
                if verbose:
                    print(f"        -> auto_check {_preview(auto_result)}")

        # checker 门禁：成功 write_checker 且本步未自检 → 自动 run_checker_self_check
        if (
            checker_write_discipline
            and had_successful_checker_write
            and not had_checker_self_check
        ):
            if verbose:
                print(f"[step {step}] auto run_checker_self_check after write_checker")
            auto_result = tools.run_checker_self_check()
            if isinstance(auto_result, str) and auto_result.startswith("ERROR"):
                auto_preview = auto_result if len(auto_result) <= 4000 else (
                    auto_result[:3200] + f"\n...[{len(auto_result)} chars]...\n" + auto_result[-600:]
                )
            else:
                auto_preview = _preview(auto_result, 200)
            if on_event:
                on_event(
                    step, "run_checker_self_check",
                    {"auto": True},
                    auto_preview,
                )
            if isinstance(auto_result, str) and auto_result.startswith("OK"):
                checker_check_ok = True
                follow = (
                    "【系统自动 checker 自检】write_checker 成功后已自动执行 "
                    "run_checker_self_check()，结果 OK。\n"
                    f"{_truncate_for_messages('run_checker_self_check', auto_result)}\n\n"
                    "请立即调用 finish(summary) 结束。禁止再 write_checker。"
                )
            else:
                checker_check_ok = False
                is_system = isinstance(auto_result, str) and "[SYSTEM]" in auto_result
                if is_system and tool_limits and "write_checker" in tool_counts:
                    # 环境失败不计入 write 额度，退回本轮成功写入消耗的 1 次
                    tool_counts["write_checker"] = max(
                        0, tool_counts.get("write_checker", 1) - 1
                    )
                if is_system:
                    follow = (
                        "【系统自动 checker 自检】失败原因标注为 [SYSTEM]（环境/标程/生成器），"
                        "不是 checker 逻辑问题；本轮 write_checker 不计入次数。\n"
                        f"{_truncate_for_messages('run_checker_self_check', auto_result)}\n\n"
                        "请直接再调用 run_checker_self_check()，不要 rewrite checker。"
                        "若仍 SYSTEM，finish 说明环境失败。"
                    )
                else:
                    follow = (
                        "【系统自动 checker 自检】失败原因标注为 [LOGIC]。\n"
                        f"{_truncate_for_messages('run_checker_self_check', auto_result)}\n\n"
                        "请根据 FAIL 再 write_checker 一轮"
                        "（本阶段最多 2 次编译成功的 write_checker；编译失败不计次）；"
                        "写完后系统会再次自动自检。禁止未自检连续改写。"
                    )
            messages.append({"role": "user", "content": follow})
            if verbose:
                print(f"        -> auto_checker_check {_preview(auto_result)}")

        # 阶段切换：write_range 成功后再从 range 阶段切到 gen 阶段，避免 range 还没写好就进入 coding。
        if stage_prompts is not None and current_stage == "range" and range_written_ok:
            current_stage, schemas = _switch_stage(
                messages,
                current_stage,
                "gen",
                stage_prompts,
                stage_tool_schemas=stage_tool_schemas,
                current_schemas=schemas,
                verbose=verbose,
                step=step,
            )

    return "预算用尽"
