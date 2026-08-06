"""CLI 入口。

用法：
    python main.py demo
    python main.py gen --problem problems/example --out out --zip data.zip
    python main.py clean-jobs --keep-days 30
    python main.py check-config
    python main.py analyze-traces
"""
import argparse
import json
import sys
from pathlib import Path

from agent.core import run
from agent import tools
from pipeline import gen_data, pack


HELLO_PROMPT = """你是一个能调用工具的 Agent。
可用工具：
- write_file(path, content): 写文件
- read_file(path): 读文件
- finish(summary): 任务完成后调用，summary 简述结果

规则：
1. 完成任务必须调用 finish，不要只输出文字就停下。
2. 调用工具时参数要完整、合法。
3. 看到工具返回 ERROR 要修正后再继续。
"""


def cmd_demo():
    from config.settings import validate_runtime

    validate_runtime(require_llm_key=True)
    task = (
        "在 out/ 目录下写一个 hello.txt 文件，"
        "内容是 1 到 10 的求和结果（即 55），只写数字本身。"
        "完成后调用 finish。"
    )
    print("=== Hello Agent demo ===")
    print(f"任务: {task}\n")
    result = run(task, verbose=True, system_prompt=HELLO_PROMPT)
    print(f"\n最终结果: {result}")


def cmd_gen(problem: str, out: str, zip_path: str, max_steps: int, verbose: bool):
    from config.settings import validate_runtime

    validate_runtime(require_llm_key=True)
    problem_dir = Path(problem)
    range_file = problem_dir / "range.json"
    if not range_file.exists():
        print(f"ERROR: 找不到 {range_file}")
        sys.exit(1)

    range_json = json.loads(range_file.read_text(encoding="utf-8"))
    std_cmd = range_json["std_cmd"]

    # 把工作目录设为题目目录，Agent 写的 gen.py/validate.py 落在这里
    tools.set_context(str(problem_dir), std_cmd)

    task = (
        f"请为下面的算法题生成测试数据。数据范围见 range.json（已用 read_range 读取）：\n"
        f"{json.dumps(range_json, ensure_ascii=False, indent=2)}\n\n"
        f"按 System Prompt 里的 CLI 契约写出 gen.py 和 validate.py，"
        f"对每种 edge_type 各试跑一个 seed 自检，全部通过后调 finish。"
    )

    print("=== Agent: 写 gen.py + validate.py ===")
    summary = run(task, max_steps=max_steps, verbose=verbose)
    print(f"\nAgent 结束: {summary}\n")

    gen_py = problem_dir / "gen.py"
    validate_py = problem_dir / "validate.py"
    if not gen_py.exists() or not validate_py.exists():
        print("ERROR: Agent 没能产出 gen.py / validate.py，终止。")
        sys.exit(1)

    print("=== Pipeline: 批量生成 .in / .out ===")
    stats = gen_data.generate(range_json, str(problem_dir), out, verbose=verbose)
    print(f"\n生成统计: {stats}\n")

    meta = {
        "problem": str(problem_dir),
        "range": range_json,
        "stats": stats,
    }
    zip_path = pack.pack(out, zip_path, meta)
    print(f"=== 打包完成: {zip_path} ===")
    print(f"组数={stats['ok']}/{stats['count']} 合法率={stats['valid_rate']:.0%} 耗时={stats['elapsed_s']}s")


def cmd_clean_jobs(keep_days: int | None, max_keep: int | None, dry_run: bool):
    from storage.job_store import clean_jobs, format_bytes

    result = clean_jobs(keep_days=keep_days, max_keep=max_keep, dry_run=dry_run)
    prefix = "[dry-run] " if dry_run else ""
    print(
        f"{prefix}清理完成: 删除 {len(result['removed'])} 个 job，"
        f"保留 {result['kept']} 个，释放 {format_bytes(result['bytes_freed'])}"
        f"（keep_days={result['keep_days']}, max_keep={result['max_keep']}）"
    )
    if result["removed"]:
        for name in result["removed"][:30]:
            print(f"  - {name}")
        if len(result["removed"]) > 30:
            print(f"  … 另有 {len(result['removed']) - 30} 个")


def cmd_check_config():
    from config.settings import get_settings, validate_runtime

    try:
        s = validate_runtime(require_llm_key=True)
    except ValueError as e:
        print(f"ERROR: {e}")
        sys.exit(1)
    print("配置校验通过:")
    print(f"  LLM_MODEL={s.llm_model}")
    print(f"  LLM_BASE_URL={s.llm_base_url or '(default)'}")
    print(f"  LLM_MAX_TOKENS={s.llm_max_tokens}")
    print(f"  LLM_MAX_TOTAL_TOKENS={s.llm_max_total_tokens or '(unlimited)'}")
    print(f"  LLM_RETRY_MAX={s.llm_retry_max}")
    print(f"  SERVER={s.server_host}:{s.server_port}")
    print(f"  JOB_RETENTION_DAYS={s.job_retention_days}")
    print(f"  embedding_configured={bool((s.embedding_api_key or '').strip())}")


def cmd_analyze_traces(jobs_dirs: list[str], top: int, as_json: bool):
    from agent.trace_analyze import analyze_traces, format_report, report_to_dict

    # Windows 控制台默认 GBK 时避免中文汇总乱码
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    roots = [Path(p) for p in jobs_dirs] if jobs_dirs else None
    report = analyze_traces(roots)
    if as_json:
        print(json.dumps(report_to_dict(report), ensure_ascii=False, indent=2))
    else:
        print(format_report(report, top=top))


def main():
    parser = argparse.ArgumentParser(description="ACM 出数据智能体")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("demo", help="跑第一周 hello demo")
    sub.add_parser("check-config", help="启动前校验 .env / LLM 配置")

    g = sub.add_parser("gen", help="跑第二周出数据流水线")
    g.add_argument("--problem", default="problems/example", help="题目目录（含 range.json / std）")
    g.add_argument("--out", default="out", help="测例输出目录")
    g.add_argument("--zip", default="data.zip", help="最终 zip 路径")
    g.add_argument("--max-steps", type=int, default=30, help="Agent 最大循环步数")
    g.add_argument("--quiet", action="store_true", help="少打印")

    c = sub.add_parser("clean-jobs", help="清理过期 jobs/ 目录（释放磁盘）")
    c.add_argument(
        "--keep-days",
        type=int,
        default=None,
        help="保留最近 N 天（默认读 JOB_RETENTION_DAYS，通常 30）",
    )
    c.add_argument(
        "--max-keep",
        type=int,
        default=None,
        help="最多保留 N 个目录（0/不设表示不按数量裁剪）",
    )
    c.add_argument("--dry-run", action="store_true", help="只列出将删除的目录，不实际删除")

    a = sub.add_parser(
        "analyze-traces",
        help="汇总 agent_trace.jsonl：工具失败 / 题型 token / nudge 空转",
    )
    a.add_argument(
        "--jobs-dir",
        action="append",
        default=[],
        dest="jobs_dirs",
        help="jobs 根目录（可多次；默认 jobs/ 与 gui/jobs/）",
    )
    a.add_argument("--top", type=int, default=15, help="各排行显示条数")
    a.add_argument("--json", action="store_true", dest="as_json", help="输出 JSON")

    args = parser.parse_args()
    if args.cmd == "demo":
        cmd_demo()
    elif args.cmd == "gen":
        cmd_gen(args.problem, args.out, args.zip, args.max_steps, not args.quiet)
    elif args.cmd == "clean-jobs":
        cmd_clean_jobs(args.keep_days, args.max_keep, args.dry_run)
    elif args.cmd == "check-config":
        cmd_check_config()
    elif args.cmd == "analyze-traces":
        cmd_analyze_traces(args.jobs_dirs, args.top, args.as_json)


if __name__ == "__main__":
    main()
