"""CLI 入口。

两种模式：
  1. demo  —— 第一周验收：让 Agent 写 out/hello.txt
  2. 出数据 —— 第二周验收：从 std + range.json 到 data.zip

用法：
    python main.py demo
    python main.py gen --problem problems/example --out out --zip data.zip
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

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


def main():
    parser = argparse.ArgumentParser(description="ACM 出数据智能体")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("demo", help="跑第一周 hello demo")

    g = sub.add_parser("gen", help="跑第二周出数据流水线")
    g.add_argument("--problem", default="problems/example", help="题目目录（含 range.json / std）")
    g.add_argument("--out", default="out", help="测例输出目录")
    g.add_argument("--zip", default="data.zip", help="最终 zip 路径")
    g.add_argument("--max-steps", type=int, default=30, help="Agent 最大循环步数")
    g.add_argument("--quiet", action="store_true", help="少打印")

    args = parser.parse_args()
    if args.cmd == "demo":
        cmd_demo()
    elif args.cmd == "gen":
        cmd_gen(args.problem, args.out, args.zip, args.max_steps, not args.quiet)


if __name__ == "__main__":
    main()
