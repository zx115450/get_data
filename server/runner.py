"""单个任务的执行：准备 std -> 跑 Agent 写 gen/validate -> pipeline 出数据 -> 打包 zip。"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import tools
from agent.core import run as agent_run
from pipeline import gen_data, pack
from pipeline.gen_data import validate_range_json, normalize_range_json
from pipeline.pack import pack_checker
from sandbox.run import safe_run
from server import job_store
from server.few_shots import get_few_shot_rag, detected_type
from server.few_shots_rag import add_job_to_corpus
from server.struct_hints import scan_structural_hints
from server.text_agent import simplify_text
from utils.markup import to_plain_for_llm

JOBS_DIR = Path("jobs")


def _compile_std(job_dir: Path, std_code: str, lang: str) -> str:
    """把标程写入 job_dir，必要时编译，返回可执行的 std_cmd（绝对/相对项目根）。"""
    if lang == "python":
        (job_dir / "std.py").write_text(std_code, encoding="utf-8")
        return f"python {job_dir / 'std.py'}"

    if lang == "cpp":
        (job_dir / "std.cpp").write_text(std_code, encoding="utf-8")
        out_name = "std.exe" if os.name == "nt" else "std"
        rc, _, err = safe_run(
            f"g++ -O2 -std=c++17 -o {out_name} std.cpp",
            timeout=30,
            cwd=str(job_dir),
        )
        if rc != 0:
            raise RuntimeError(f"标程编译失败:\n{err}")
        return str(job_dir / out_name)

    raise ValueError(f"不支持的标程语言: {lang}")


def run_job(job: job_store.Job, std_code: str, lang: str,
            problem_statement: str, data_range_desc: str,
            problem_type: str = "",
            output_desc: str = "",
            range_json=None,
            special_judge: bool = False) -> None:
    """在 worker 线程里跑完整流程。

    range_json: 若 GUI 已提供，则跳过 Agent 写 range 的步骤，直接用给定方案写 gen/validator。
    special_judge: 若 True，则额外要求 Agent 写 checker.cpp 并打包成独立的 checker.zip。
    """
    job_dir = JOBS_DIR / job.id
    job_dir.mkdir(parents=True, exist_ok=True)
    out_dir = job_dir / "out"
    zip_path = job_dir / "data.zip"
    checker_zip_path = job_dir / "checker.zip"

    def on_event(step, name, args, preview):
        brief = {}
        if name == "run_gen":
            brief = {"seed": args.get("seed"), "type": args.get("type")}
        elif name in ("write_gen", "write_validate", "write_range", "write_checker"):
            content = args.get("content") or ""
            brief = {"chars": len(content)}
        elif name in ("run_validate", "run_std"):
            t = args.get("input_text") or ""
            brief = {"input_chars": len(t)}
        elif name == "finish":
            brief = {"summary": (args.get("summary") or "")[:80]}
        else:
            brief = {k: (str(v)[:40] if not isinstance(v, (int, float, bool)) else v)
                     for k, v in list(args.items())[:4]}
        pv = preview if len(preview) <= 100 else preview[:100] + "…"
        job_store.add_progress(job, f"[step {step}] {name} {brief} -> {pv}")

    # 1) 准备标程
    job_store.add_progress(job, f"【阶段 1/5】准备标程 (lang={lang})")
    std_cmd = _compile_std(job_dir, std_code, lang)
    job_store.add_progress(job, f"标程就绪: {std_cmd}")

    # 1.5) 出数据前先对题面做一次「简化」（markup + LLM 无符号纯文本），再喂给后续 Agent
    job_store.add_progress(job, "【预处理】简化题面（markup → LLM 总结为纯文本）…")
    try:
        stmt_plain = simplify_text(problem_statement or "", kind="statement")
        job_store.add_progress(
            job,
            f"题面已简化: {len(problem_statement or '')} → {len(stmt_plain)} 字",
        )
        (job_dir / "statement_simplified.txt").write_text(stmt_plain, encoding="utf-8")
    except Exception as e:
        stmt_plain = to_plain_for_llm(problem_statement or "")
        job_store.add_progress(
            job,
            f"题面简化失败，回退为 markup 纯文本: {type(e).__name__}: {e}",
        )

    range_plain = to_plain_for_llm(data_range_desc or "")
    if range_plain != (data_range_desc or "").strip():
        job_store.add_progress(
            job,
            f"范围描述已 markup 清洗: {len(data_range_desc or '')} → {len(range_plain)} 字",
        )

    tools.set_context(str(job_dir), std_cmd)
    eff_type = detected_type(problem_type, stmt_plain, range_plain, std_code)
    job_store.add_progress(
        job,
        f"题型: {eff_type}" + ("" if problem_type else " (自动检测)")
        + f" | 题面 {len(stmt_plain)} 字 | 范围描述 {len(range_plain)} 字 | std {len(std_code)} 字",
    )
    few_shot_block, few_shot_summary = get_few_shot_rag(
        problem_type, stmt_plain, range_plain, std_code, top_k=2
    )
    # 日志精简：只保留关键信息，避免完整召回列表刷屏
    if few_shot_summary.startswith("RAG 召回"):
        short = few_shot_summary.replace("RAG 召回 2 个模板: ", "")
        job_store.add_progress(job, f"few-shot RAG: {short}")
    elif few_shot_summary.startswith("RAG 失败"):
        job_store.add_progress(job, f"few-shot 回退: {few_shot_summary}")
    elif few_shot_summary.startswith("RAG 未召回"):
        job_store.add_progress(job, f"few-shot 回退: {few_shot_summary}")
    else:
        job_store.add_progress(job, f"few-shot: {few_shot_summary}")
    if few_shot_block:
        few_shot_block = (
            f"\n\n{few_shot_block}\n\n"
            f"请参照上面 RAG 召回范例的写法风格（校验严格度、--type 分支方式），"
            f"为本次题目写 gen.cpp 和 validator.cpp。"
        )
    std_for_prompt = std_code
    if len(std_for_prompt) > 12000:
        std_for_prompt = (
            std_code[:6000]
            + f"\n\n/* ... std 共 {len(std_code)} 字符，中间已省略；完整标程已编译，可用 run_std 实测 ... */\n\n"
            + std_code[-4000:]
        )
        job_store.add_progress(job, f"标程过长({len(std_code)}字)，喂给 Agent 的文本已截断；run_std 仍用完整标程")
    std_block = (
        f"\n\n【标程源码（lang={lang}）】\n```\n{std_for_prompt}\n```\n"
        f"请先读标程，确认输入格式（是否首行 T、每行字段、分隔符、范围），"
        f"gen 的输出必须能被该标程正确读入。"
    )

    # 输出描述：作为生成器和 special judge 的判定参考
    output_plain = to_plain_for_llm(output_desc or "")
    output_block = ""
    if output_plain.strip():
        output_block = (
            f"\n\n【输出描述】\n{output_plain}\n"
            f"写 gen.cpp 时请确保输出格式与上述描述一致。"
        )

    # 保留原始题面关键句作为参考，减少 LLM 简化导致的信息损失
    raw_stmt_for_prompt = problem_statement or ""
    raw_range_for_prompt = data_range_desc or ""
    if len(raw_stmt_for_prompt) > 6000:
        raw_stmt_for_prompt = raw_stmt_for_prompt[:3000] + "\n\n...（原始题面过长，中间省略）...\n\n" + raw_stmt_for_prompt[-2000:]
    if len(raw_range_for_prompt) > 3000:
        raw_range_for_prompt = raw_range_for_prompt[:1500] + "\n\n...（原始范围描述过长，中间省略）...\n\n" + raw_range_for_prompt[-1000:]
    original_ref_block = ""
    if raw_stmt_for_prompt.strip() or raw_range_for_prompt.strip():
        original_ref_block = (
            f"\n\n【原始题面 / 范围描述（未简化，供参考）】\n"
            f"{raw_stmt_for_prompt}\n\n{raw_range_for_prompt}\n"
            f"以上原始文本可能与简化版有差异，若冲突请优先以原始文本为准。"
        )

    special_judge_block = ""
    if special_judge:
        spj_output_hint = (
            f"\n\n【输出描述参考】\n{output_plain}\n"
            if output_plain.strip()
            else ""
        )
        special_judge_block = (
            f"\n\n【本题为 Special Judge】\n"
            f"答案不唯一或需要额外判定，请额外调用 write_checker 写一个 testlib special judge。"
            f"checker.cpp 必须 #include \"testlib.h\"，main 里调用 registerTestlibCmd(argc, argv)，"
            f"按 (inf, ouf, ans) 顺序读取文件并判定。"
            f"{spj_output_hint}"
            f"写完后做 gen→validate→std→checker 四连自检：用 run_std 得到标程输出后，"
            f"再用 run_std 的输出作为 ans 文件、跑 checker 验证 checker 能正确通过标程答案。"
        )

    # 扫题面关键词，命中特殊结构约束（哈密顿/欧拉/DAG/连通/二分图等）时追加针对性提醒。
    # 这种约束往往是标程算法的隐含假设，生成器不保证就会导致「格式合法但语义错误」。
    struct_hint_block = scan_structural_hints(stmt_plain) or scan_structural_hints(problem_statement or "")
    if struct_hint_block:
        job_store.add_progress(job, "检测到题面特殊结构约束，已注入针对性提醒")

    preset = None
    if range_json and isinstance(range_json, dict) and range_json.get("constraints"):
        preset = normalize_range_json(dict(range_json))
        preset.pop("std_cmd", None)
        errs0 = validate_range_json(preset)
        if errs0:
            raise RuntimeError("GUI 提供的 range.json 不合法:\n" + "\n".join(f"  - {e}" for e in errs0))
        (job_dir / "range.json").write_text(
            json.dumps(preset, ensure_ascii=False, indent=2), encoding="utf-8")
        job_store.add_progress(
            job,
            f"【跳过写 range】使用 GUI 已给方案: count={preset.get('count')} "
            f"edge_cases={preset.get('edge_cases')}",
        )
        # 若预设方案里带了 special_constraints，单独提示一次，确保写 gen 的 Agent 不会忽略
        sp_note = ""
        sp = preset.get("special_constraints") or []
        if sp:
            sp_note = (
                f"\n\n【range.json 已标注的特殊结构约束 — gen/validator 必须显式保证】\n"
                + "\n".join(f"- {c}" for c in sp)
                + "\n每条约束都要在 gen.cpp 的某个 --type 分支里真正实现，"
                "并在 validator.cpp 用 ensuref 校验。不要只写 random 分支。\n"
            )
        range_block = (
            f"\n\n【已给定 range.json — 禁止再调用 write_range，不要修改它】\n"
            f"```json\n{json.dumps(preset, ensure_ascii=False, indent=2)}\n```\n"
            f"请直接写 gen.cpp / validator.cpp，--type 必须覆盖 edge_cases 中每一个名字；"
            f"对每种 edge_type 做 run_gen→run_validate→run_std 三连自检，全过后 finish。"
            f"{sp_note}"
        )
        task = (
            f"请为下面的算法题生成测试数据（range 已给定）。\n\n"
            f"【题面】（已自动简化为纯文本）\n{stmt_plain}\n\n"
            f"【数据范围描述】\n{range_plain}\n"
            f"{output_block}"
            f"{original_ref_block}"
            f"{std_block}"
            f"{range_block}"
            f"{struct_hint_block}"
            f"{few_shot_block}"
            f"{special_judge_block}"
        )
        job_store.add_progress(job, "【阶段 2/5】启动 Agent（跳过 write_range，写 gen/validator）")
    else:
        task = (
            f"请为下面的算法题生成测试数据。\n\n"
            f"【题面】（已自动简化为纯文本）\n{stmt_plain}\n\n"
            f"【数据范围描述】\n{range_plain}\n"
            f"{output_block}"
            f"{original_ref_block}"
            f"{std_block}"
            f"\n\n要求：range.json 的 count 默认 15；"
            f"对 [L,R] 规模变量必须用 --index/--count 分层，15 组里既有小数据也有大数据（禁止只抽到 100 以内）。"
            f"若有多测 T 且 sum n 有上限：必须同时覆盖「大T+小n」和「小T+大n」，禁止先抽大 n 再令 T=S/n（会把 T 压成 1~2）。"
            f"按契约：先 write_range，再写 gen.cpp/validator.cpp，"
            f"对每种 edge_type 做 run_gen→run_validate→run_std 三连自检，全过后调 finish。"
            f"{struct_hint_block}"
            f"{few_shot_block}"
            f"{special_judge_block}"
        )
        job_store.add_progress(job, "【阶段 2/5】启动 Agent（写 range.json / gen.cpp / validator.cpp）")

    summary = agent_run(task, max_steps=40, verbose=False, on_event=on_event)
    job_store.add_progress(job, f"Agent 结束: {summary}")

    # 把 Agent 的进度日志落盘，方便事后排查（服务器重启后内存进度会丢）。
    # 尤其是当 Agent 没产出 gen/validator 时，这份日志是定位根因的唯一线索。
    try:
        with job.lock:
            log_lines = list(job.progress)
        (job_dir / "agent_log.txt").write_text(
            "\n".join(log_lines) + "\n",
            encoding="utf-8",
        )
    except Exception as e:
        job_store.add_progress(job, f"agent_log.txt 写盘失败（非致命）: {type(e).__name__}: {e}")

    # 3) 校验产物
    job_store.add_progress(job, "【阶段 3/5】校验产物 (range.json / gen / validator)")
    range_file = job_dir / "range.json"
    if not range_file.exists():
        raise RuntimeError("Agent 没有产出 range.json")
    try:
        produced = json.loads(range_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Agent 产出的 range.json 不是合法 JSON: {e}")
    # 若用户给了预设，以预设为准（防止 Agent 偷偷改写）
    if preset is not None:
        produced = dict(preset)
        range_file.write_text(json.dumps(produced, ensure_ascii=False, indent=2), encoding="utf-8")
    produced["std_cmd"] = std_cmd
    before = list(produced.get("edge_cases") or [])
    produced = normalize_range_json(produced)
    after = produced.get("edge_cases") or []
    if before != after:
        job_store.add_progress(job, f"已清洗 edge_cases: {before} -> {after}（去掉 random / 空项 / 重复）")
    errs = validate_range_json(produced)
    if errs:
        raise RuntimeError("range.json 不合法:\n" + "\n".join(f"  - {e}" for e in errs))

    # range.json 里若带 special_constraints（range_agent 提取的特殊结构约束清单），
    # 显式记一条进度，方便用户在 GUI 看到题目被识别出了哪些约束。
    sp_constraints = produced.get("special_constraints") or []
    if sp_constraints:
        job_store.add_progress(
            job,
            f"题目特殊结构约束: {sp_constraints}",
        )

    _exe = lambda b: b + (".exe" if os.name == "nt" else "")
    has_gen = (job_dir / _exe("gen")).exists() or (job_dir / "gen.py").exists() or (job_dir / "gen.cpp").exists()
    has_val = (job_dir / _exe("validator")).exists() or (job_dir / "validate.py").exists() or (job_dir / "validator.cpp").exists()
    if not has_gen or not has_val:
        # 报错时带上 Agent summary 和最后几条进度，方便定位根因。
        # 常见根因：LLM 没调工具就返回文本（被当成 finish）、预算用尽、write_gen 编译失败循环。
        with job.lock:
            tail = list(job.progress)[-8:]
        missing = []
        if not has_gen:
            missing.append("gen")
        if not has_val:
            missing.append("validator")
        raise RuntimeError(
            f"Agent 没有产出 {' / '.join(missing)}。\n"
            f"Agent summary: {summary!r}\n"
            f"最后几条进度:\n" + "\n".join(f"  {t}" for t in tail)
            + "\n常见原因：LLM 未调工具就返回文本（被当成 finish）、"
            f"40 步预算用尽、或 write_gen/write_validate 编译失败循环。"
            f"完整日志见 {job_dir / 'agent_log.txt'}。"
        )
    if special_judge:
        has_checker = (job_dir / _exe("checker")).exists() or (job_dir / "checker.cpp").exists()
        if not has_checker:
            raise RuntimeError("已开启 special judge，但 Agent 没有产出 checker.cpp / checker")
        job_store.add_progress(job, "checker 产物 OK")

    job_store.add_progress(
        job,
        f"产物 OK: count={produced.get('count')} edge_cases={produced.get('edge_cases')}",
    )

    # 4) pipeline 批量生成
    job_store.add_progress(job, "【阶段 4/5】批量生成 .in / .out")
    stats = gen_data.generate(produced, str(job_dir), str(out_dir), verbose=False)
    job_store.add_progress(job, f"生成统计: {stats}")

    if stats.get("bad", 0) > 0:
        failures = stats.get("failures", [])
        for f in failures:
            job_store.add_progress(
                job,
                f"生成失败 #{f['index']} (planned_type={f['planned_type']}):\n{f['error']}",
            )
        raise RuntimeError(
            f"批量生成有 {stats['bad']}/{stats['count']} 组失败，数据不完整，已中止打包"
        )

    # 5) 打包
    job_store.add_progress(job, "【阶段 5/5】打包 zip（仅 .in / .out）")
    meta = {"problem": str(job_dir), "range": produced, "stats": stats}
    pack.pack(str(out_dir), str(zip_path), meta)
    job.zip_path = str(zip_path)
    job_store.add_progress(job, f"打包完成: {zip_path}")

    # 5.5) 把成功任务加入 RAG 语料库（非致命）
    try:
        added = add_job_to_corpus(job_dir, stats=stats, problem_type=eff_type)
        if added:
            rate = added.get("valid_rate")
            rate_s = f", valid_rate={rate:.4f}" if isinstance(rate, (int, float)) else ""
            job_store.add_progress(
                job,
                f"RAG 语料库已更新: {added['key']} (type={added.get('problem_type') or eff_type}{rate_s})",
            )
        else:
            job_store.add_progress(
                job,
                "RAG 语料库未更新（质量过滤未通过、重复或已存在）",
            )
    except Exception as e:
        job_store.add_progress(job, f"RAG 语料库更新失败（非致命）: {type(e).__name__}: {e}")

    if special_judge:
        job_store.add_progress(job, "打包 special judge（checker.zip）")
        pack_checker(str(job_dir), str(checker_zip_path))
        job.checker_zip_path = str(checker_zip_path)
        job_store.add_progress(job, f"checker 打包完成: {checker_zip_path}")
