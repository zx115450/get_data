"""Agent 主循环：消息 + 工具 + 循环。

支持按阶段切换 System Prompt（stage_prompts）：例如先让 Agent 只写 range.json，
写完后自动切换为 gen/validator 阶段的完整 prompt，避免一个巨型 system 从头用到尾。

支持 write_check_discipline：成功 write_gen/write_validate 后自动跑快速自检，
禁止未自检连续改写。
"""
import concurrent.futures

from . import llm, prompts, tools

# 默认完整版：兼容旧代码，仍可用 system_prompt 覆盖。
SYSTEM_PROMPT = prompts.default_full_prompt

# 写代码类工具：门禁针对这些（不含 write_range）
_WRITE_CODE_TOOLS = frozenset({"write_gen", "write_validate", "write_file"})


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


def _preview(result: str, limit: int = 120) -> str:
    return result if len(result) <= limit else result[:limit] + "..."


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
    self_check_fast: bool = False,
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
    self_check_fast:
                    True 时 Agent 主动调用的 run_self_check 强制 fast_mode=True
                    （与门禁的自动自检一致；完整自检由外层 runner 负责）。
    返回:           finish 的 summary，或 "预算用尽"
    """
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

    # LLM 偶尔会只返回文本不调工具（如「解释一下思路」）。此时不直接 finish，
    # 而是发一条 nudge 提醒它调工具。连续 nudge 太多次仍不改，才放弃。
    max_nudges = 3
    nudge_count = 0

    # 门禁状态：快速自检是否已通过（通过后禁止再写）
    check_ok = False

    for step in range(1, max_steps + 1):
        # 自检通过后从 schema 里拿掉写代码工具，减少模型违规调用
        step_schemas = schemas
        if write_check_discipline and check_ok:
            step_schemas = _filter_schemas(schemas, _WRITE_CODE_TOOLS)

        actions = llm.chat(messages, step_schemas)

        # LLM 没调任何工具：发 nudge 提醒它调工具，而不是直接 finish。
        # 这避免了「LLM 解释思路 → Agent 立刻终止 → 没产出 gen」的常见失败模式。
        if not actions:
            nudge_count += 1
            if nudge_count > max_nudges:
                if verbose:
                    print(f"[step {step}] LLM 连续 {max_nudges} 次不调工具，放弃")
                if on_event:
                    on_event(step, "give_up", {"reason": "LLM 连续多次不调工具"}, "")
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
            continue

        # LLM 调了工具，重置 nudge 计数
        nudge_count = 0

        # 如果一轮里出现 write_gen / write_validate / write_checker / use_builtin_checker，并行编译以节省时间
        results = [None] * len(actions)
        writer_indices = {}
        for idx, act in enumerate(actions):
            # 门禁：自检通过后禁止写代码（即使模型仍塞了 write_*）
            if write_check_discipline and check_ok and act.name in _WRITE_CODE_TOOLS:
                results[idx] = (
                    "ERROR: 快速自检已通过，禁止继续改写 gen/validator。"
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
        # 本步 write_gen / write_validate 已失败时，precheck 禁止用同一份坏源码重编
        skip_recompile: set[str] = set()

        for idx, act in enumerate(actions):
            if verbose:
                print(f"[step {step}] {act.name} {act.args}")

            if act.name == "finish":
                summary = act.args.get("summary", "done")
                if verbose:
                    print(f"[done] {summary}")
                return summary

            result = results[idx]
            if result is None:
                # 强制快速自检：Agent 主动调 run_self_check 时注入 fast_mode
                if act.name == "run_self_check" and (self_check_fast or write_check_discipline):
                    args = dict(act.args or {})
                    args["fast_mode"] = True
                    result = tools.dispatch(act.name, args)
                else:
                    result = tools.dispatch(act.name, act.args)
                results[idx] = result

            result_for_llm = _truncate_for_messages(act.name, result)

            if verbose:
                print(f"        -> {_preview(result)}")
            if on_event:
                on_event(step, act.name, dict(act.args), _preview(result))

            # 把工具结果作为 tool 消息塞回上下文
            # 必须带 tool_call_id，对应 assistant 的 tool_calls
            messages.append({
                "role": "tool",
                "tool_call_id": act.tool_call_id,
                "content": result_for_llm,
            })

            if act.name == "write_range" and not str(result_for_llm).startswith("ERROR"):
                range_written_ok = True

            if act.name in _WRITE_CODE_TOOLS and isinstance(result, str) and result.startswith("OK"):
                had_successful_write = True
            elif act.name == "write_gen" and isinstance(result, str) and result.startswith("ERROR"):
                skip_recompile.add("gen")
            elif act.name == "write_validate" and isinstance(result, str) and result.startswith("ERROR"):
                skip_recompile.add("validator")

            if act.name == "run_self_check":
                had_self_check = True
                if isinstance(result, str) and result.startswith("OK"):
                    check_ok = True
                else:
                    check_ok = False

        # 门禁：成功写入且本步未自检 → 先确保双产物就绪，再自动快速自检
        if write_check_discipline and had_successful_write and not had_self_check:
            ready, prep_msg = tools.ensure_self_check_prereqs(skip_recompile=skip_recompile)
            if on_event:
                on_event(
                    step, "precheck",
                    {"ready": ready, "auto": True, "skip_recompile": sorted(skip_recompile)},
                    _preview(prep_msg, 200),
                )
            if not ready:
                # gen/validator 尚未齐备：不跑自检，提示补写 / 修复编译失败项
                check_ok = False
                if skip_recompile:
                    follow = (
                        "【系统自检前置检查未通过】尚未自动跑 run_self_check。\n"
                        f"{prep_msg}\n\n"
                        "本步部分 write_* 已失败，系统不会用同一份坏源码再重编译。"
                        "请根据上方 ERROR 修复后重新写出完整源码；"
                        "gen 与 validator 都编译成功后才会自动快速自检。"
                    )
                else:
                    follow = (
                        "【系统自检前置检查未通过】尚未自动跑 run_self_check。\n"
                        f"{prep_msg}\n\n"
                        "请先补齐缺失的 write_gen / write_validate（可同轮并行写出），"
                        "两者都编译成功后系统才会自动快速自检。不要在产物不齐时反复空写。"
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
                auto_result = tools.run_self_check(fast_mode=True)
                auto_preview = _preview(auto_result, 200)
                if on_event:
                    on_event(
                        step, "run_self_check",
                        {"fast_mode": True, "auto": True},
                        auto_preview,
                    )

                if isinstance(auto_result, str) and auto_result.startswith("OK"):
                    check_ok = True
                    follow = (
                        "【系统自动快速自检】写入成功后已自动执行 run_self_check(fast_mode=True)，结果 OK。\n"
                        f"{_truncate_for_messages('run_self_check', auto_result)}\n\n"
                        "请立即调用 finish(summary) 结束。禁止再 write_gen / write_validate。"
                    )
                else:
                    check_ok = False
                    follow = (
                        "【系统自动快速自检】写入成功后已自动执行 run_self_check(fast_mode=True)，未通过。\n"
                        f"{_truncate_for_messages('run_self_check', auto_result)}\n\n"
                        "请根据 FAIL 再修改一轮（可同轮 write_gen + write_validate），"
                        "写完后系统会再次自动快速自检。禁止未自检连续多次改写。"
                    )
                messages.append({"role": "user", "content": follow})
                if verbose:
                    print(f"        -> auto_check {_preview(auto_result)}")

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
