"""单个任务的执行：准备 std -> 跑 Agent 写 gen/validate -> pipeline 出数据 -> 打包 zip。"""
import hashlib
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import prompts, tools
from agent.core import run as agent_run
from agent.tools import BUILTIN_CHECKERS, use_builtin_checker
from pipeline import gen_data, pack
from pipeline.gen_data import validate_range_json, normalize_range_json
from pipeline.pack import pack_checker, pack_sources
from sandbox.run import safe_run
from server import job_store
from server.few_shots import get_few_shot_rag, detected_type
from server.few_shots_rag import add_job_to_corpus
from server.struct_hints import scan_structural_hints
from server.text_agent import simplify_text
from utils.markup import to_plain_for_llm

JOBS_DIR = Path("jobs")


# 阶段工具集：按阶段只暴露该阶段允许的工具，避免模型分心。
RANGE_TOOL_SCHEMAS = [
    s for s in tools.TOOL_SCHEMAS
    if s["function"]["name"] in {"write_range", "read_file", "read_range", "finish"}
]
GEN_TOOL_SCHEMAS = [
    s for s in tools.TOOL_SCHEMAS
    if s["function"]["name"] not in {"write_checker", "use_builtin_checker"}
]
FIXER_TOOL_SCHEMAS = [
    s for s in tools.TOOL_SCHEMAS
    if s["function"]["name"] in {
        "read_file", "write_gen", "run_gen", "run_validate", "run_std",
        "run_self_check", "finish",
    }
]
CHECKER_TOOL_SCHEMAS = [
    s for s in tools.TOOL_SCHEMAS
    if s["function"]["name"] in {
        "write_checker", "use_checker_template", "read_file",
        "run_checker", "run_checker_self_check", "finish",
    }
]
REVIEWER_TOOL_SCHEMAS = [
    s for s in tools.TOOL_SCHEMAS
    if s["function"]["name"] in {
        "read_file", "read_range", "run_gen", "run_validate", "run_std", "finish",
    }
]


def _adaptive_steps(range_json: dict | None, problem_type: str, resume_info: dict | None) -> int:
    """根据题目复杂度给 Gen Agent 自适应步数。"""
    base = 50
    if resume_info:
        base += 10
    if not range_json:
        return base
    constraints = range_json.get("constraints") or {}
    max_val = 1
    for v in constraints.values():
        if isinstance(v, (list, tuple)) and len(v) >= 2:
            try:
                max_val = max(max_val, int(v[1]))
            except (ValueError, TypeError):
                pass
    edge_cases = len(range_json.get("edge_cases") or [])
    special = len(range_json.get("special_constraints") or [])
    score = 0
    if max_val > 100000:
        score += 2
    elif max_val > 10000:
        score += 1
    if edge_cases > 8:
        score += 2
    elif edge_cases > 5:
        score += 1
    if special > 2:
        score += 2
    elif special > 0:
        score += 1
    if problem_type in ("graph", "tree", "geometry", "interactive", "dp"):
        score += 1
    if problem_type in ("string", "matrix"):
        score += 1
    # 复杂度越高步数越多，最低不低于 50，最高不超过 100
    steps = base + score * 10
    return max(50, min(steps, 100))


def _build_checker_task(
    stmt_plain: str,
    range_plain: str,
    output_plain: str,
    std_for_prompt: str,
    range_json: dict,
    failure_context: str = "",
) -> str:
    """为自定义 checker 构造第二轮 Agent 的 task。"""
    parts = [
        "请为本题写一个 checker.cpp（Special Judge）。",
        f"\n【题面】\n{stmt_plain}",
        f"\n【数据范围】\n{range_plain}",
    ]
    if output_plain.strip():
        parts.append(f"\n【输出描述 / 判定规则】\n{output_plain}")
    if std_for_prompt.strip():
        parts.append(f"\n【标程源码片段】\n{std_for_prompt}")
    parts.append(
        "\n【已有产物】\n"
        f"- range.json: {json.dumps(range_json, ensure_ascii=False, indent=2)}\n"
        "- 工作目录已有 gen.cpp / validator.cpp / 标程，可用 read_file 查看。\n"
    )
    if failure_context:
        parts.append(f"\n{failure_context}\n")
    parts.append(
        "\n要求：\n"
        "1. 优先用 use_checker_template(\"construct_verify\") 安装骨架，再 read_file(\"checker.cpp\") 查看 TODO 位置。\n"
        "2. 用 write_checker 写完整 checker.cpp，必须 #include \"testlib.h\" 并调用 registerTestlibCmd(argc, argv)。\n"
        "3. 按 (inf, ouf, ans) 顺序读取文件并判定；多解时检查选手输出的合法性，不要直接字符串全等。\n"
        "4. 编译成功后必须调用 run_checker_self_check()：正例（标程输出）必须 _ok，负例（扰动输出）必须 _wa/_pe。\n"
        "5. run_checker_self_check() 返回 OK 后调 finish。"
    )
    return "\n".join(parts)


def _build_reviewer_task(
    stmt_plain: str,
    range_plain: str,
    range_json: dict,
    gen_agent_summary: str,
) -> str:
    """为 Reviewer Agent 构造 task。"""
    parts = [
        "请审查当前工作目录的 gen.cpp。",
        f"\n【题面】\n{stmt_plain}",
        f"\n【数据范围】\n{range_plain}",
        f"\n【range.json】\n{json.dumps(range_json, ensure_ascii=False, indent=2)}",
        f"\n【Gen Agent 执行摘要】\n{gen_agent_summary}",
        "\n工作目录已有 gen.cpp / validator.cpp / range.json / 标程。"
        "请用 read_file 和 run_gen / run_validate / run_std 进行审查，"
        "最后调 finish(summary)，summary 必须是结构化审查报告。",
    ]
    return "\n".join(parts)


def _build_fixer_task(
    stmt_plain: str,
    range_plain: str,
    range_json: dict,
    review_report: str,
    gen_agent_summary: str,
) -> str:
    """为 Fixer Agent 构造 task。"""
    parts = [
        "请根据 Reviewer 报告修复当前工作目录的 gen.cpp。",
        f"\n【题面】\n{stmt_plain}",
        f"\n【数据范围】\n{range_plain}",
        f"\n【range.json】\n{json.dumps(range_json, ensure_ascii=False, indent=2)}",
        f"\n【Gen Agent 执行摘要】\n{gen_agent_summary}",
        f"\n【Reviewer 报告】\n{review_report}",
        "\n工作目录已有 gen.cpp / validator.cpp / review_report.txt。"
        "请优先 read_file(\"review_report.txt\") 和 read_file(\"gen.cpp\")，"
        "按 MUST_FIX 问题修复，用 write_gen 写完整源码并编译，"
        "最后调 run_self_check() 自检，通过后 finish。",
    ]
    return "\n".join(parts)


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


def _text_hash(text: str) -> str:
    """返回文本的 sha256 摘要。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _detect_artifacts(job_dir: Path) -> list[str]:
    """列出 job_dir 下可复用的产物文件名。"""
    candidates = [
        "range.json",
        "gen.cpp", "gen.py",
        "validator.cpp", "validate.py",
        "checker.cpp",
    ]
    found = []
    for name in candidates:
        if (job_dir / name).is_file():
            found.append(name)
    if os.name == "nt":
        for name in ("gen.exe", "validator.exe", "checker.exe"):
            if (job_dir / name).is_file():
                found.append(name)
    return found


def _detect_good_artifacts(job_dir: Path) -> list[str]:
    """列出通过校验的"好版本"产物（含 out.good 测例快照）。"""
    found = []
    for name in ("gen.cpp", "gen.py", "validator.cpp", "validate.py"):
        good = name + ".good"
        if (job_dir / good).is_file():
            found.append(good)
    for name in ("gen.exe", "validator.exe"):
        good = name + ".good"
        if (job_dir / good).is_file():
            found.append(good)
    if (job_dir / "out.good").is_dir():
        found.append("out.good")
    return found


def _copy_resume_artifacts(parent_dir: Path, job_dir: Path, resume_info: dict | None = None) -> list[str]:
    """把父任务的可复用产物拷到当前目录。返回实际拷贝的列表。

    - gen/validator 的 .good 快照：始终复用（后续 Agent 可在好版本上修）
    - out.good：始终复用（已通过 gen→validate→std 的测例对，即使后续 gen 改坏也保留）
    - 普通源码产物：始终复用
    """
    copied = []
    # 好版本快照：不论失败阶段，合法 in/out 与通过校验的 gen 都应带走
    for name in _detect_good_artifacts(parent_dir):
        src = parent_dir / name
        dst = job_dir / name
        if src.is_dir():
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)
        copied.append(name)
    # 普通产物（源码/规划文件等）始终复用
    for name in _detect_artifacts(parent_dir):
        if name in copied:
            continue
        src = parent_dir / name
        dst = job_dir / name
        shutil.copy2(src, dst)
        copied.append(name)
    # 同时把简化题面带过来，避免重复 LLM 调用
    if (parent_dir / "statement_simplified.txt").is_file():
        shutil.copy2(
            parent_dir / "statement_simplified.txt",
            job_dir / "statement_simplified.txt",
        )
        copied.append("statement_simplified.txt")
    return copied


def _save_good_snapshot(job_dir: Path, include_in_out: bool = False) -> None:
    """把当前通过校验的 gen/validator 保存为 .good 快照；可选合并 out → out.good。"""
    for name in ("gen.cpp", "gen.py", "validator.cpp", "validate.py"):
        src = job_dir / name
        if src.is_file():
            shutil.copy2(src, job_dir / (name + ".good"))
    if os.name == "nt":
        for name in ("gen.exe", "validator.exe"):
            src = job_dir / name
            if src.is_file():
                shutil.copy2(src, job_dir / (name + ".good"))
    if include_in_out:
        _merge_out_into_good(job_dir)


def _pair_indices_in_dir(data_dir: Path) -> set[int]:
    """扫描目录里成对的 {i}.in + {i}.out，返回已完整的索引集合（1-based）。"""
    if not data_dir.is_dir():
        return set()
    ins = set()
    outs = set()
    for f in data_dir.iterdir():
        if not f.is_file():
            continue
        name = f.name
        if name.endswith(".in"):
            stem = name[:-3]
            if stem.isdigit():
                ins.add(int(stem))
        elif name.endswith(".out"):
            stem = name[:-4]
            if stem.isdigit():
                outs.add(int(stem))
    return ins & outs


def _merge_out_into_good(job_dir: Path) -> int:
    """把 out/ 里成对的合法测例合并进 out.good/，返回合并的组数。"""
    out_dir = job_dir / "out"
    good_dir = job_dir / "out.good"
    good_dir.mkdir(parents=True, exist_ok=True)
    merged = 0
    for i in _pair_indices_in_dir(out_dir):
        for suffix in (".in", ".out"):
            src = out_dir / f"{i}{suffix}"
            dst = good_dir / f"{i}{suffix}"
            if src.is_file():
                shutil.copy2(src, dst)
        merged += 1
    return merged


def _restore_good_snapshot(job_dir: Path) -> None:
    """把 .good 快照还原为正式产物（源码 + 已合法测例）。"""
    for name in ("gen.cpp", "gen.py", "validator.cpp", "validate.py"):
        good = job_dir / (name + ".good")
        if good.is_file():
            shutil.copy2(good, job_dir / name)
    if os.name == "nt":
        for name in ("gen.exe", "validator.exe"):
            good = job_dir / (name + ".good")
            if good.is_file():
                shutil.copy2(good, job_dir / name)
    good_dir = job_dir / "out.good"
    out_dir = job_dir / "out"
    if good_dir.is_dir():
        out_dir.mkdir(parents=True, exist_ok=True)
        for i in _pair_indices_in_dir(good_dir):
            for suffix in (".in", ".out"):
                src = good_dir / f"{i}{suffix}"
                if src.is_file():
                    shutil.copy2(src, out_dir / f"{i}{suffix}")


def _has_complete_in_out(job_dir: Path, range_json: dict, *, prefer_good: bool = False) -> bool:
    """out/（或 out.good）是否已有完整的 count 组成对测例。"""
    count = int(range_json.get("count") or 15)
    data_dir = job_dir / ("out.good" if prefer_good else "out")
    pairs = _pair_indices_in_dir(data_dir)
    return all(i in pairs for i in range(1, count + 1))


def _load_parent_failure_context(parent_dir: Path) -> dict[str, Any] | None:
    """读取父任务的 failure_context.json，若不存在或非法则返回 None。"""
    p = parent_dir / "failure_context.json"
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("v") != 1:
            return None
        return data
    except Exception:
        return None


def _find_matching_parent_job(statement_hash: str, std_hash: str, lang: str) -> tuple[Path, dict[str, Any]] | None:
    """扫描 jobs/ 下所有目录，找题面、标程、语言都一致的最新任务。"""
    if not JOBS_DIR.is_dir():
        return None
    candidates = []
    for d in JOBS_DIR.iterdir():
        if not d.is_dir():
            continue
        ctx = _load_parent_failure_context(d)
        if not ctx:
            continue
        if (
            ctx.get("statement_hash") == statement_hash
            and ctx.get("std_hash") == std_hash
            and ctx.get("lang") == lang
        ):
            # 用目录名（时间戳）排序，越新越好
            candidates.append((d.name, d, ctx))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    _, parent_dir, parent_ctx = candidates[0]
    return parent_dir, parent_ctx


def _build_resume_failure_block(resume_info: dict[str, Any]) -> str:
    """把失败上下文格式化成一段 prompt 追加语。"""
    stage = resume_info.get("stage", "unknown")
    error_summary = resume_info.get("error_summary", "")
    parent_id = resume_info.get("parent_job_id", "")
    return (
        "\n\n【从上次失败续跑】\n"
        f"- 父任务: {parent_id}\n"
        f"- 失败阶段: {stage}\n"
        f"- 错误摘要: {error_summary}\n"
        "- 工作目录已复制上次产物，请 read_file 查看；"
        "请针对性修复导致失败的问题，不要从零重写。\n"
    )


def has_gen_val_at_resume(job_dir: Path) -> bool:
    """检查当前目录是否已有 gen + validator 可执行产物。"""
    _exe = lambda b: b + (".exe" if os.name == "nt" else "")
    has_gen = (job_dir / _exe("gen")).exists() or (job_dir / "gen.py").exists() or (job_dir / "gen.cpp").exists()
    has_val = (job_dir / _exe("validator")).exists() or (job_dir / "validate.py").exists() or (job_dir / "validator.cpp").exists()
    return has_gen and has_val


def _run_job_impl(
    job: job_store.Job, std_code: str, lang: str,
    problem_statement: str, data_range_desc: str,
    problem_type: str,
    output_desc: str,
    range_json,
    special_judge: bool,
    builtin_checker: str,
    resume_context: dict[str, Any] | None,
) -> None:
    """run_job 的实际实现。"""
    builtin_checker = (builtin_checker or "").strip().lower()
    if builtin_checker and builtin_checker not in BUILTIN_CHECKERS:
        raise ValueError(
            f"builtin_checker 只支持: {', '.join(BUILTIN_CHECKERS)}，收到 {builtin_checker!r}"
        )

    job_dir = JOBS_DIR / job.id
    job_dir.mkdir(parents=True, exist_ok=True)
    out_dir = job_dir / "out"
    zip_path = job_dir / "data.zip"
    sources_zip_path = job_dir / "sources.zip"
    checker_zip_path = job_dir / "checker.zip"

    # 1.0) 同题复用：自动扫描 jobs/ 下最新同题任务，不再依赖客户端 resume_context
    stmt_hash = _text_hash(problem_statement or "")
    std_hash = _text_hash(std_code or "")
    resume_info: dict[str, Any] | None = None
    resume_failure_block = ""
    auto_parent = _find_matching_parent_job(stmt_hash, std_hash, lang)
    if auto_parent is None:
        job_store.add_progress(
            job,
            "【复用】未找到同题历史任务，按新任务执行。"
        )
    else:
        parent_dir, parent_ctx = auto_parent
        parent_id = parent_dir.name
        if not parent_dir.is_dir():
            job_store.add_progress(
                job,
                f"【复用】父任务目录 {parent_dir} 不存在，按新任务执行。"
            )
        else:
            copied = _copy_resume_artifacts(parent_dir, job_dir, parent_ctx)
            job_store.add_progress(
                job,
                f"【复用】同题校验通过（父任务 {parent_id}），复制 {len(copied)} 项产物: {copied}"
            )
            resume_info = dict(parent_ctx)
            resume_info["parent_job_id"] = parent_id
            resume_failure_block = _build_resume_failure_block(resume_info)

    # 兼容旧前端：如果仍传了 resume_context，仅做日志记录，不再作为唯一依据
    if resume_context:
        job_store.add_progress(
            job,
            f"【复用】收到客户端 resume_context（已废弃，仅记录），父任务 {resume_context.get('parent_job_id')!r}"
        )

    def on_event(step, name, args, preview):
        brief = {}
        if name == "run_gen":
            brief = {"seed": args.get("seed"), "type": args.get("type")}
        elif name in ("write_gen", "write_validate", "write_range", "write_checker"):
            content = args.get("content") or ""
            brief = {"chars": len(content)}
        elif name == "use_builtin_checker":
            brief = {"name": args.get("name")}
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
    # 预先拷贝 generator.h。generator.h 已内嵌 testlib.h，且封装了数组/排列/树/图/几何等便捷 API。
    # 对任意题型都预置，让大模型写 gen 时可以直接使用 generator.h 中的 rnd / Tree / Graph / Sequence 等工具。
    try:
        tools.prewarm_generator_headers()
        job_store.add_progress(job, "已预置 generator.h（含 ACM-generator 封装）")
    except Exception as e:
        job_store.add_progress(job, f"预置 generator.h 失败（可忽略）: {e}")
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
    elif few_shot_summary.startswith("未配置 Embedding"):
        job_store.add_progress(job, f"few-shot: {few_shot_summary}")
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
            f"{resume_failure_block}"
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
            f"{resume_failure_block}"
        )
        job_store.add_progress(job, "【阶段 2/5】启动 Agent（写 range.json / gen.cpp / validator.cpp）")

    # 按阶段切换 System Prompt：先写 range 的短 prompt，再切 gen/validator 的完整 prompt。
    # 若 GUI 已给定 range，则直接进入 gen 阶段。
    full_prompt = prompts.build_full_prompt(
        eff_type,
        range_json=range_json,
        problem_statement=stmt_plain,
        std_code=std_code,
    )
    # 续跑分支：同题复用后，如果已有 range.json，直接进 gen 阶段；
    # 若父任务失败在 checker 阶段，则跳过 gen 阶段只跑 checker。
    resume_stage = resume_info.get("stage") if resume_info else None
    if resume_stage == "checker" and (job_dir / "range.json").is_file() and has_gen_val_at_resume(job_dir):
        stage_prompts = {}
        stage_tool_schemas = {}
        job_store.add_progress(job, "【续跑】父任务 checker 阶段失败，跳过 gen/validator Agent")
    elif resume_info and (job_dir / "range.json").is_file():
        stage_prompts = {"gen": full_prompt}
        stage_tool_schemas = {"gen": GEN_TOOL_SCHEMAS}
        job_store.add_progress(job, "【续跑】已有 range.json，直接进入 gen/validator 修复阶段")
    elif preset is not None:
        stage_prompts = {"gen": full_prompt}
    else:
        stage_prompts = {
            "range": prompts.build_range_prompt(),
            "gen": full_prompt,
        }
    stage_tool_schemas = {
        "range": RANGE_TOOL_SCHEMAS,
        "gen": GEN_TOOL_SCHEMAS,
    }
    job_store.add_progress(
        job,
        f"Agent prompt stages: {list(stage_prompts.keys())} | type={eff_type}"
    )

    # 续跑 checker 阶段：不需要跑 gen Agent，直接空过
    if resume_stage == "checker" and not stage_prompts:
        summary = "checker 阶段续跑：跳过 gen/validator Agent"
        job_store.add_progress(job, f"Agent 结束: {summary}")
    else:
        gen_steps = _adaptive_steps(range_json, eff_type, resume_info)
        job_store.add_progress(job, f"Gen Agent 自适应步数: {gen_steps}")
        # 如果已有好版本快照，先把源码还原为可复用版本，Agent 可在此基础上修复或验证
        if (job_dir / "gen.cpp.good").is_file() or (job_dir / "gen.py.good").is_file():
            _restore_good_snapshot(job_dir)
            job_store.add_progress(job, "已还原 gen/validator 好版本快照")
        summary = agent_run(
            task,
            max_steps=gen_steps,
            verbose=False,
            on_event=on_event,
            stage_prompts=stage_prompts,
            stage_tool_schemas=stage_tool_schemas,
        )
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
            f"Gen Agent 步数预算用尽、或 write_gen/write_validate 编译失败循环。"
            f"完整日志见 {job_dir / 'agent_log.txt'}。"
        )

    # 3.5) Reviewer Agent：对 gen.cpp 做性能与正确性审查
    job_store.add_progress(job, "【阶段 3.5/5】Reviewer 审查 gen.cpp")
    try:
        review_task = _build_reviewer_task(
            stmt_plain, range_plain, produced, summary
        )
        review_summary = agent_run(
            review_task,
            max_steps=15,
            verbose=False,
            on_event=on_event,
            system_prompt=prompts.build_reviewer_prompt(),
            tool_schemas=REVIEWER_TOOL_SCHEMAS,
        )
        job_store.add_progress(job, f"Reviewer 结束: {review_summary}")
        try:
            (job_dir / "review_report.txt").write_text(
                str(review_summary), encoding="utf-8"
            )
        except Exception as e:
            job_store.add_progress(job, f"review_report.txt 写盘失败（非致命）: {type(e).__name__}: {e}")

        # 解析报告：有 MUST_FIX 时触发 Fixer Agent 自动修复
        has_must_fix, issue_summary = _extract_review_issues(review_summary)
        if has_must_fix and (job_dir / "gen.cpp").is_file():
            job_store.add_progress(job, f"Reviewer 发现 MUST_FIX，启动 Fixer Agent: {issue_summary[:200]}")
            try:
                fixer_task = _build_fixer_task(
                    stmt_plain, range_plain, produced, review_summary, summary
                )
                fixer_summary = agent_run(
                    fixer_task,
                    max_steps=15,
                    verbose=False,
                    on_event=on_event,
                    system_prompt=prompts.build_fixer_prompt(),
                    tool_schemas=FIXER_TOOL_SCHEMAS,
                )
                job_store.add_progress(job, f"Fixer 结束: {fixer_summary}")
                # 刷新产物存在性（Fixer 可能替换了 gen）
                has_gen = (job_dir / _exe("gen")).exists() or (job_dir / "gen.py").exists() or (job_dir / "gen.cpp").exists()
                if not has_gen:
                    job_store.add_progress(job, "Fixer Agent 未能保留 gen.cpp，继续使用原生成器")
            except Exception as e:
                job_store.add_progress(job, f"Fixer 调用失败（非致命）: {type(e).__name__}: {e}")
    except Exception as e:
        job_store.add_progress(job, f"Reviewer 调用失败（非致命）: {type(e).__name__}: {e}")

    if special_judge:
        job_store.add_progress(job, "【阶段 3.5/5】生成 checker")
        if builtin_checker:
            # 指定内置 checker：直接安装，不经过 LLM
            tools.set_context(str(job_dir), std_cmd)
            msg = use_builtin_checker(builtin_checker)
            job_store.add_progress(job, f"安装内置 checker: {msg}")
        else:
            # 自定义 checker：单独开一轮 Agent 只写 checker
            checker_task = _build_checker_task(
                stmt_plain, range_plain, output_plain, std_for_prompt, produced,
                failure_context=resume_failure_block,
            )
            checker_summary = agent_run(
                checker_task,
                max_steps=25,
                verbose=False,
                on_event=on_event,
                system_prompt=prompts.build_checker_prompt(),
                tool_schemas=CHECKER_TOOL_SCHEMAS,
            )
            job_store.add_progress(job, f"Checker Agent 结束: {checker_summary}")

        has_checker = (job_dir / _exe("checker")).exists() or (job_dir / "checker.cpp").exists()
        if not has_checker:
            raise RuntimeError(
                "已开启 special judge，但 checker 生成失败"
                "（未指定内置 checker，且自定义 checker 未能产出）"
            )
        job_store.add_progress(job, "checker 产物 OK")

    job_store.add_progress(
        job,
        f"产物 OK: count={produced.get('count')} edge_cases={produced.get('edge_cases')}",
    )
    # gen/validator 已通过校验，保存源码快照；in/out 尚未生成，不存快照
    _save_good_snapshot(job_dir, include_in_out=False)
    job_store.add_progress(job, "已保存 gen/validator 好版本快照")

    # 4) pipeline 批量生成
    # 先把父任务留下的合法测例（out.good）还原到 out/，再只补缺失组
    if (job_dir / "out.good").is_dir() and _pair_indices_in_dir(job_dir / "out.good"):
        n_good = len(_pair_indices_in_dir(job_dir / "out.good"))
        _restore_good_snapshot(job_dir)
        job_store.add_progress(
            job,
            f"已还原 out.good 中 {n_good} 组合法测例，将只补缺失组"
        )

    if _has_complete_in_out(job_dir, produced):
        job_store.add_progress(job, "【阶段 4/5】测例已齐全，跳过批量生成")
        stats = {
            "count": produced.get("count", 15),
            "ok": produced.get("count", 15),
            "bad": 0,
            "reused": produced.get("count", 15),
            "failures": [],
            "restored": True,
        }
        _merge_out_into_good(job_dir)
    else:
        job_store.add_progress(job, "【阶段 4/5】批量生成 .in / .out（复用已有合法组）")
        stats = gen_data.generate(
            produced, str(job_dir), str(out_dir), verbose=False, reuse_existing=True
        )
        job_store.add_progress(job, f"生成统计: {stats}")

        # 无论是否全部成功：把本轮通过 gen→validate→std 的组合并进 out.good
        merged = _merge_out_into_good(job_dir)
        if merged:
            job_store.add_progress(job, f"已把 {merged} 组合法测例合并进 out.good")

        if stats.get("bad", 0) > 0:
            failures = stats.get("failures", [])
            for f in failures:
                job_store.add_progress(
                    job,
                    f"生成失败 #{f['index']} (planned_type={f['planned_type']}):\n{f['error']}",
                )
            raise RuntimeError(
                f"批量生成有 {stats['bad']}/{stats['count']} 组失败，数据不完整，已中止打包"
                f"（已成功的 {stats.get('ok', 0)} 组已写入 out.good，下次同题可复用）"
            )

    # 5) 打包
    job_store.add_progress(job, "【阶段 5/5】打包 zip（仅 .in / .out）")
    meta = {"problem": str(job_dir), "range": produced, "stats": stats}
    pack.pack(str(out_dir), str(zip_path), meta)
    job.zip_path = str(zip_path)
    job_store.add_progress(job, f"打包完成: {zip_path}")

    try:
        pack_sources(str(job_dir), str(sources_zip_path))
        job.sources_zip_path = str(sources_zip_path)
        job_store.add_progress(job, f"源码包完成: {sources_zip_path}")
    except Exception as e:
        job_store.add_progress(job, f"源码包打包失败（非致命）: {type(e).__name__}: {e}")

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


def _infer_failure_stage(progress: list[str]) -> str:
    """根据进度日志推断失败阶段。"""
    for msg in reversed(progress):
        if "阶段 4/5" in msg or "批量生成" in msg:
            return "batch_generate"
        if "阶段 3.5/5" in msg or "生成 checker" in msg:
            return "checker"
        if "阶段 3/5" in msg or "校验产物" in msg:
            return "validate"
        if "阶段 2/5" in msg or "启动 Agent" in msg:
            return "agent"
        if "阶段 1/5" in msg or "准备标程" in msg:
            return "std_compile"
    return "unknown"


def _extract_review_issues(review_summary: str) -> tuple[bool, str]:
    """解析 Reviewer 报告，返回 (是否有 MUST_FIX, 问题摘要)。"""
    text = (review_summary or "").lower()
    has_must_fix = "must_fix" in text or "must fix" in text or "必须修" in text
    lines = [ln.strip() for ln in (review_summary or "").splitlines() if ln.strip()]
    issue_lines = [ln for ln in lines if any(k in ln.lower() for k in ("must_fix", "must fix", "should_fix", "should fix", "严重", "超时", "性能问题"))]
    summary = "\n".join(issue_lines[:10]) or ("MUST_FIX" if has_must_fix else "")
    return has_must_fix, summary


def _write_failure_context(
    job_dir: Path,
    job_id: str,
    stage: str,
    error: str,
    statement: str,
    std_code: str,
    lang: str,
    range_file: Path | None,
    review_report: str = "",
) -> None:
    """失败时把可携带的上下文写入 failure_context.json。"""
    artifacts = _detect_artifacts(job_dir) + _detect_good_artifacts(job_dir)
    # 去重保序
    seen = set()
    artifacts = [a for a in artifacts if not (a in seen or seen.add(a))]
    ctx = {
        "v": 1,
        "parent_job_id": job_id,
        "stage": stage,
        "statement_hash": _text_hash(statement or ""),
        "std_hash": _text_hash(std_code or ""),
        "lang": lang,
        "range_hash": "",
        "artifacts": artifacts,
        "error_summary": error[:1000],
        "review_report": review_report[:2000],
        "created_at": datetime.now().isoformat(),
    }
    if range_file and range_file.is_file():
        try:
            ctx["range_hash"] = _text_hash(range_file.read_text(encoding="utf-8"))
        except Exception:
            pass
    try:
        (job_dir / "failure_context.json").write_text(
            json.dumps(ctx, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass


def _check_cancel(job: job_store.Job) -> None:
    """如果客户端请求取消，抛出 RuntimeError 让任务进入 finally 落 failure_context。"""
    with job.lock:
        if job.cancel_requested:
            raise RuntimeError("客户端请求取消")


def run_job(job: job_store.Job, std_code: str, lang: str,
            problem_statement: str, data_range_desc: str,
            problem_type: str = "",
            output_desc: str = "",
            range_json=None,
            special_judge: bool = False,
            builtin_checker: str = "",
            resume_context: dict[str, Any] | None = None) -> None:
    """在 worker 线程里跑完整流程。

    range_json: 若 GUI 已提供，则跳过 Agent 写 range 的步骤，直接用给定方案写 gen/validator。
    special_judge: 若 True，则额外要求 Agent 产出 checker（自定义或内置）并打包 checker.zip。
    builtin_checker: 可选 lcmp/wcmp/rcmp4/rcmp6/rcmp9/yesno；
      - special_judge=True 时作为默认推荐（Agent 可用 use_builtin_checker）；
      - special_judge=False 时若指定，任务结束后自动安装并打包该内置 checker。
    resume_context: 已废弃，保留仅作兼容；现在 runner 会自动扫描 jobs/ 下最新同题任务。
    """
    job_dir = JOBS_DIR / job.id
    range_file = job_dir / "range.json"
    cancelled = False
    try:
        _run_job_impl(
            job, std_code, lang,
            problem_statement, data_range_desc,
            problem_type,
            output_desc,
            range_json,
            special_judge,
            builtin_checker,
            resume_context,
        )
    except (Exception, KeyboardInterrupt, SystemExit) as e:
        cancelled = isinstance(e, (KeyboardInterrupt, SystemExit)) or "客户端请求取消" in str(e)
        stage = _infer_failure_stage(list(job.progress))
        review_report = ""
        try:
            p = job_dir / "review_report.txt"
            if p.is_file():
                review_report = p.read_text(encoding="utf-8")
        except Exception:
            pass
        _write_failure_context(
            job_dir, job.id, stage, f"{type(e).__name__}: {e}",
            problem_statement, std_code, lang, range_file,
            review_report=review_report,
        )
        if cancelled:
            job_store.add_progress(job, "任务已取消，但 failure_context 已落盘，可同题续跑")
            job_store.add_progress(job, f"取消原因: {e}")
        raise
    finally:
        # 正常完成时由 _run_job_impl 内部处理；异常/取消时上面已落盘。
        # 这里确保 job 状态正确：若被 KeyboardInterrupt，重新抛出一个 RuntimeError 给上层。
        if cancelled and not job.status.value == "error":
            with job.lock:
                job.status = job_store.JobStatus.CANCELLED


