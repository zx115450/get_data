"""Special FinderAgent：按 Plan 的 need_finder 决策，写 finder → 本地搜索 → 留痕。"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from agent import prompts, tools
from agent.core import run as agent_run
from server import job_store

FINDER_TOOL_SCHEMAS = [
    s for s in tools.TOOL_SCHEMAS
    if s["function"]["name"] in {
        "read_file", "read_range", "write_finder", "run_finder",
        "list_finder_hits", "run_validate", "run_std", "finish",
    }
]

_NEED_FINDER_RE = re.compile(
    r"need_finder\s*[:：]\s*(yes|no|true|false|是|否)",
    re.I,
)
_FINDER_GOAL_RE = re.compile(
    r"finder_goal\s*[:：]\s*(.+)",
    re.I,
)


def parse_need_finder(plan_text: str) -> tuple[bool, str]:
    """从 Plan Markdown 解析 need_finder 与 finder_goal。

    未写明时默认 False（不强行搜索）。
    """
    text = plan_text or ""
    m = _NEED_FINDER_RE.search(text)
    if not m:
        # 兼容「需要 Finder：是/否」
        m2 = re.search(r"(?:需要\s*Finder|调用\s*Finder)\s*[:：]?\s*(是|否|yes|no)", text, re.I)
        if not m2:
            return False, ""
        val = m2.group(1).strip().lower()
        need = val in ("yes", "true", "是")
    else:
        val = m.group(1).strip().lower()
        need = val in ("yes", "true", "是")

    goal = ""
    gm = _FINDER_GOAL_RE.search(text)
    if gm:
        goal = gm.group(1).strip().splitlines()[0].strip()
    return need, goal


def _build_finder_task(
    stmt_plain: str,
    scheme: dict,
    plan_name: str,
    finder_goal: str,
) -> str:
    sid = str(scheme.get("id") or "special")
    goal = finder_goal or "; ".join(scheme.get("must_hold") or []) or scheme.get("construct_hint") or ""
    return (
        "【角色】Special Finder\n"
        f"【目标】为方案 {sid} 编写并运行 finder，搜索满足 must_hold 的合法输入并留痕。\n"
        "【约束】只 write_finder/run_finder；禁止改 gen/gen_special/validator；"
        "允许 O(n^2) 小窗口；默认 timeout=5、memory=1024；"
        "空间必须 O(1)/O(窗口)；exit 1=无命中（正常），不是 OOM。\n\n"
        f"【题面摘要】\n{(stmt_plain or '')[:800]}\n\n"
        f"【本方案】\n```json\n{json.dumps(scheme, ensure_ascii=False, indent=2)}```\n\n"
        f"【finder_goal】{goal}\n"
        f"【计划文件】{plan_name}\n\n"
        "【硬约束】\n"
        "- 禁止 1e6+ 数组/线性筛；禁止 OOM 后只加 memory_limit_mb。\n"
        "- 严格按 must_hold；「之间」默认开区间且非空。\n"
        "- 若含尾巴无桥/ans+=3：按标程区间验证（常见 i∈(n-100,原r]），"
        "禁止 i=n 或 (i,n) 空集平凡命中；禁止只满足开区间再配 n=r+1。\n"
        "- run_finder 不要改 memory；无命中就换窗口或 finish。\n\n"
        "动作：\n"
        f"1. read_file('{plan_name}') 与 read_file('gen.cpp')。\n"
        f"2. write_finder(scheme_id='{sid}')：小内存完整源码 + 复杂度注释。\n"
        "3. run_finder(timeout_sec=5)（默认内存即可）。\n"
        "4. 命中则 validate；无命中或放弃 → finish。\n"
    )


def run_finder_agent(
    job: job_store.Job,
    job_dir: Path,
    scheme: dict,
    plan_name: str,
    plan_text: str,
    typ: str,
    on_event,
    stmt_plain: str = "",
    *,
    force_need: bool = False,
    force_goal: str = "",
) -> dict:
    """若 Plan 要求 need_finder（或 force_need），则跑 FinderAgent；返回结果摘要 dict。"""
    sid = str(scheme.get("id") or "special")
    need, goal = parse_need_finder(plan_text)
    if force_need:
        need = True
        if force_goal:
            goal = force_goal
        elif not goal:
            goal = "; ".join(scheme.get("must_hold") or []) or str(scheme.get("construct_hint") or "")
    result = {
        "scheme_id": sid,
        "need_finder": need,
        "finder_goal": goal,
        "ran": False,
        "hits": [],
        "summary": "skipped",
    }
    if not need:
        job_store.add_progress(job, f"【Finder】方案 {sid} Plan 决定 need_finder=no，跳过")
        result["summary"] = "skipped: need_finder=no"
        return result

    job_store.add_progress(
        job,
        f"【Finder】方案 {sid} need_finder=yes，goal={goal or '(must_hold)'}",
    )
    tools.set_context(str(job_dir), tools.STD_CMD or "")
    task = _build_finder_task(stmt_plain, scheme, plan_name, goal)
    try:
        summary = agent_run(
            task,
            max_steps=8,
            verbose=False,
            on_event=on_event,
            system_prompt=prompts.build_special_finder_prompt(typ),
            tool_schemas=FINDER_TOOL_SCHEMAS,
            write_check_discipline=False,
        )
    except Exception as e:
        summary = f"FinderAgent 异常: {type(e).__name__}: {e}"
        job_store.add_progress(job, summary)
        result["summary"] = summary
        return result

    # 读取留痕（tools.set_context 已指向 job_dir）
    fdir = tools.finder_dir(sid)
    meta_path = fdir / "meta.json"
    hits: list[str] = []
    status = "unknown"
    if meta_path.is_file():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            hits = list(meta.get("hits") or [])
            status = str(meta.get("status") or "")
        except json.JSONDecodeError:
            pass

    result.update({
        "ran": True,
        "hits": hits,
        "status": status,
        "summary": str(summary)[:500],
    })
    job_store.add_progress(
        job,
        f"【Finder】{sid} 结束: status={status} hits={len(hits)} | {str(summary)[:200]}",
    )
    # 决策快照写入 findings
    decision_path = fdir / "decision.json"
    decision_path.parent.mkdir(parents=True, exist_ok=True)
    decision_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return result
