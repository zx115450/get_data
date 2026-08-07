"""汇总 jobs/*/agent_trace.jsonl：工具失败率、题型 token、nudge 空转。

用法（CLI）：
    python main.py analyze-traces
    python main.py analyze-traces --jobs-dir jobs --jobs-dir gui/jobs --top 15
"""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable


# 非「真正工具调用」的控制面事件（仍计入 nudge/放弃统计）
_META_TOOLS = frozenset({"nudge", "give_up", "budget"})


def _is_fail_result(result: str) -> bool:
    r = (result or "").strip()
    if not r:
        return False
    if r == "OK" or r.startswith("OK"):
        return False
    upper = r.upper()
    return (
        r.startswith("ERROR")
        or r.startswith("FAIL")
        or "ERROR" in upper[:20]
        or "FAIL" in upper[:20]
    )


def _job_problem_type(job_dir: Path) -> str:
    """从 meta.json / range.json / success_context 推断题型。"""

    def _format_pt(raw):
        if isinstance(raw, list):
            return ", ".join(str(x) for x in raw if x)
        if isinstance(raw, str):
            return raw.strip()
        return ""

    meta = job_dir / "meta.json"
    if meta.is_file():
        try:
            data = json.loads(meta.read_text(encoding="utf-8"))
            pt = _format_pt(data.get("problem_type"))
            if pt:
                return pt
        except (OSError, json.JSONDecodeError, TypeError):
            pass
    for name in ("range.json", "success_context.json"):
        p = job_dir / name
        if not p.is_file():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                pt = _format_pt(data.get("problem_type") or data.get("type"))
                if pt:
                    return pt
        except (OSError, json.JSONDecodeError, TypeError):
            pass
    return "unknown"


def iter_trace_files(roots: Iterable[Path]) -> list[Path]:
    """在若干 jobs 根目录下找 agent_trace.jsonl。"""
    out: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        root = Path(root)
        if not root.is_dir():
            continue
        for path in sorted(root.glob("*/agent_trace.jsonl")):
            key = path.resolve()
            if key in seen:
                continue
            seen.add(key)
            out.append(path)
        # 兼容：根目录直接放一份
        direct = root / "agent_trace.jsonl"
        if direct.is_file():
            key = direct.resolve()
            if key not in seen:
                seen.add(key)
                out.append(direct)
    return out


def load_events(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return events
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            events.append(row)
    return events


@dataclass
class ToolStat:
    calls: int = 0
    fails: int = 0

    @property
    def fail_rate(self) -> float:
        return (self.fails / self.calls) if self.calls else 0.0


@dataclass
class JobStat:
    job_id: str
    path: Path
    problem_type: str = "unknown"
    events: int = 0
    tool_calls: int = 0
    tool_fails: int = 0
    nudges: int = 0
    give_ups: int = 0
    max_nudge_streak: int = 0
    tokens_total: int = 0
    tokens_prompt: int = 0
    tokens_completion: int = 0


@dataclass
class TraceReport:
    jobs: list[JobStat] = field(default_factory=list)
    tools: dict[str, ToolStat] = field(default_factory=lambda: defaultdict(ToolStat))
    by_type_tokens: dict[str, list[int]] = field(
        default_factory=lambda: defaultdict(list)
    )
    by_type_nudges: dict[str, list[int]] = field(
        default_factory=lambda: defaultdict(list)
    )

    def tool_ranking(self) -> list[tuple[str, ToolStat]]:
        items = [(name, st) for name, st in self.tools.items() if st.calls > 0]
        items.sort(key=lambda x: (-x[1].fails, -x[1].fail_rate, -x[1].calls, x[0]))
        return items


def analyze_traces(
    roots: Iterable[Path | str] | None = None,
    *,
    project_root: Path | str | None = None,
) -> TraceReport:
    """扫描并汇总。默认看项目下 jobs/ 与 gui/jobs/。"""
    root = Path(project_root) if project_root else Path(__file__).resolve().parent.parent
    if roots is None:
        roots = [root / "jobs", root / "gui" / "jobs"]
    paths = iter_trace_files(Path(r) for r in roots)
    report = TraceReport()

    for path in paths:
        events = load_events(path)
        if not events:
            continue
        job_dir = path.parent
        job_id = job_dir.name if path.name == "agent_trace.jsonl" else path.stem
        # 事件里的 job 字段优先
        for ev in events:
            if ev.get("job"):
                job_id = str(ev["job"])
                break
        ptype = _job_problem_type(job_dir)
        js = JobStat(job_id=job_id, path=path, problem_type=ptype, events=len(events))

        streak = 0
        max_streak = 0
        for ev in events:
            tool = str(ev.get("tool") or "")
            result = str(ev.get("result") or "")
            if tool == "nudge":
                js.nudges += 1
                streak += 1
                max_streak = max(max_streak, streak)
            else:
                streak = 0
            if tool == "give_up":
                js.give_ups += 1

            # 累计 token：取整条轨迹中的最大值（trace 里是累计用量）
            for key, attr in (
                ("tokens_total", "tokens_total"),
                ("tokens_prompt", "tokens_prompt"),
                ("tokens_completion", "tokens_completion"),
            ):
                try:
                    v = int(ev.get(key) or 0)
                except (TypeError, ValueError):
                    v = 0
                if v > getattr(js, attr):
                    setattr(js, attr, v)

            if tool in _META_TOOLS or not tool:
                continue
            js.tool_calls += 1
            st = report.tools[tool]
            st.calls += 1
            if _is_fail_result(result):
                js.tool_fails += 1
                st.fails += 1

        js.max_nudge_streak = max_streak
        report.jobs.append(js)
        report.by_type_tokens[ptype].append(js.tokens_total)
        report.by_type_nudges[ptype].append(js.nudges)

    report.jobs.sort(key=lambda j: (-j.tokens_total, -j.tool_fails, j.job_id))
    return report


def format_report(report: TraceReport, *, top: int = 15) -> str:
    """人类可读汇总。"""
    lines: list[str] = []
    n_jobs = len(report.jobs)
    lines.append(f"=== Agent Trace 分析（{n_jobs} 个 job）===")
    if n_jobs == 0:
        lines.append("未找到 agent_trace.jsonl。请指定 --jobs-dir，或先跑几次出题任务。")
        return "\n".join(lines)

    # 1) 工具失败
    lines.append("")
    lines.append("## 1. 工具失败排行（按失败次数）")
    ranking = report.tool_ranking()
    if not ranking:
        lines.append("（无工具调用）")
    else:
        lines.append(f"{'tool':<28} {'calls':>6} {'fails':>6} {'rate':>8}")
        for name, st in ranking[:top]:
            lines.append(
                f"{name:<28} {st.calls:>6} {st.fails:>6} {st.fail_rate:>7.1%}"
            )
        zero_fail = [n for n, st in ranking if st.fails == 0]
        if zero_fail and len(ranking) > top:
            lines.append(f"… 另有 {len(ranking) - top} 个工具未列出")

    # 2) 题型 token
    lines.append("")
    lines.append("## 2. 题型 token（按平均 tokens_total）")
    type_rows: list[tuple[str, int, float, int, int]] = []
    for ptype, vals in report.by_type_tokens.items():
        if not vals:
            continue
        avg = sum(vals) / len(vals)
        type_rows.append((ptype, len(vals), avg, min(vals), max(vals)))
    type_rows.sort(key=lambda r: (-r[2], r[0]))
    lines.append(f"{'type':<20} {'jobs':>5} {'avg':>10} {'min':>10} {'max':>10}")
    for ptype, cnt, avg, mn, mx in type_rows:
        lines.append(f"{ptype:<20} {cnt:>5} {avg:>10.0f} {mn:>10} {mx:>10}")

    # 3) nudge 空转
    lines.append("")
    lines.append("## 3. Nudge / 空转")
    nudged = [j for j in report.jobs if j.nudges > 0]
    give_up = [j for j in report.jobs if j.give_ups > 0]
    lines.append(
        f"有 nudge 的 job: {len(nudged)}/{n_jobs}；"
        f"give_up: {len(give_up)}；"
        f"nudge 总计: {sum(j.nudges for j in report.jobs)}"
    )
    if nudged:
        lines.append(f"{'job':<22} {'type':<14} {'nudges':>6} {'streak':>7} {'tokens':>10}")
        for j in sorted(nudged, key=lambda x: (-x.nudges, -x.max_nudge_streak))[:top]:
            lines.append(
                f"{j.job_id:<22} {j.problem_type:<14} {j.nudges:>6} "
                f"{j.max_nudge_streak:>7} {j.tokens_total:>10}"
            )

    # 4) 高失败 / 高 token job
    lines.append("")
    lines.append("## 4. 高失败 job（tool fails）")
    failed_jobs = [j for j in report.jobs if j.tool_fails > 0]
    if not failed_jobs:
        lines.append("（无工具失败）")
    else:
        lines.append(f"{'job':<22} {'type':<14} {'fails':>6} {'calls':>6} {'tokens':>10}")
        for j in sorted(failed_jobs, key=lambda x: (-x.tool_fails, -x.tokens_total))[:top]:
            lines.append(
                f"{j.job_id:<22} {j.problem_type:<14} {j.tool_fails:>6} "
                f"{j.tool_calls:>6} {j.tokens_total:>10}"
            )

    lines.append("")
    lines.append("## 5. Token Top job")
    lines.append(f"{'job':<22} {'type':<14} {'tokens':>10} {'prompt':>10} {'compl':>10}")
    for j in report.jobs[:top]:
        lines.append(
            f"{j.job_id:<22} {j.problem_type:<14} {j.tokens_total:>10} "
            f"{j.tokens_prompt:>10} {j.tokens_completion:>10}"
        )

    return "\n".join(lines)


def report_to_dict(report: TraceReport) -> dict[str, Any]:
    """便于 --json 输出。"""
    return {
        "job_count": len(report.jobs),
        "tools": {
            name: {"calls": st.calls, "fails": st.fails, "fail_rate": st.fail_rate}
            for name, st in report.tool_ranking()
        },
        "by_problem_type": {
            ptype: {
                "jobs": len(vals),
                "tokens_avg": (sum(vals) / len(vals)) if vals else 0,
                "tokens_min": min(vals) if vals else 0,
                "tokens_max": max(vals) if vals else 0,
                "nudges_avg": (
                    sum(report.by_type_nudges[ptype]) / len(report.by_type_nudges[ptype])
                    if report.by_type_nudges[ptype]
                    else 0
                ),
            }
            for ptype, vals in sorted(report.by_type_tokens.items())
        },
        "jobs": [
            {
                "job_id": j.job_id,
                "problem_type": j.problem_type,
                "tool_calls": j.tool_calls,
                "tool_fails": j.tool_fails,
                "nudges": j.nudges,
                "give_ups": j.give_ups,
                "max_nudge_streak": j.max_nudge_streak,
                "tokens_total": j.tokens_total,
                "tokens_prompt": j.tokens_prompt,
                "tokens_completion": j.tokens_completion,
                "trace": str(j.path),
            }
            for j in report.jobs
        ],
    }
