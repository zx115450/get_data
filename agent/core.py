"""Agent 主循环：消息 + 工具 + 循环。

支持按阶段切换 System Prompt（stage_prompts）：例如先让 Agent 只写 range.json，
写完后自动切换为 gen/validator 阶段的完整 prompt，避免一个巨型 system 从头用到尾。
"""
import concurrent.futures

from . import llm, prompts, tools

# 默认完整版：兼容旧代码，仍可用 system_prompt 覆盖。
SYSTEM_PROMPT = prompts.default_full_prompt


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


def run(
    task: str,
    max_steps: int = 30,
    verbose: bool = True,
    system_prompt: str | None = None,
    stage_prompts: dict[str, str] | None = None,
    stage_tool_schemas: dict[str, list] | None = None,
    on_event=None,
    tool_schemas=None,
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

    for step in range(1, max_steps + 1):
        actions = llm.chat(messages, schemas)

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
                result = tools.dispatch(act.name, act.args)
                results[idx] = result
            # 大输出（尤其 run_gen 的大数据）立刻截断再进上下文，避免下一轮 413
            if act.name == "run_gen" and len(result) > 2500 and not result.startswith("ERROR"):
                result_for_llm = (
                    result[:800]
                    + f"\n...[generated {len(result)} chars, truncated for context]...\n"
                    + result[-400:]
                )
            elif act.name == "run_self_check" and len(result) > 4000:
                result_for_llm = result[:2500] + f"\n...[{len(result)} chars truncated]...\n" + result[-1000:]
            elif len(result) > 4000:
                result_for_llm = result[:2000] + f"\n...[{len(result)} chars truncated]...\n" + result[-800:]
            else:
                result_for_llm = result

            if verbose:
                preview = result if len(result) <= 120 else result[:120] + "..."
                print(f"        -> {preview}")
            if on_event:
                preview = result if len(result) <= 120 else result[:120] + "..."
                on_event(step, act.name, dict(act.args), preview)

            # 把工具结果作为 tool 消息塞回上下文
            # 必须带 tool_call_id，对应 assistant 的 tool_calls
            messages.append({
                "role": "tool",
                "tool_call_id": act.tool_call_id,
                "content": result_for_llm,
            })

            if act.name == "write_range" and not str(result_for_llm).startswith("ERROR"):
                range_written_ok = True

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
