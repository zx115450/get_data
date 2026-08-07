"""一次性：把被 day/gay 覆盖的树题目录拆开。

1) 从 jobs/20260807_160042 另存 day/gay 为新题库 ID
2) 从 jobs/20260806_195932 把树题写回原 ID
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from storage import problem_store  # noqa: E402

TREE_ID = (
    "如题_已知一棵包含_N_个结点的树_连通且无环_每个节点上包含一个数值_需要支持_20260806_194439"
)
TREE_JOB = "20260806_195932"
DAY_JOB = "20260807_160042"


def main() -> None:
    day_ws = problem_store.load_workspace_from_dir(
        problem_store.JOBS_DIR / DAY_JOB, source="job"
    )
    if not day_ws:
        raise SystemExit(f"找不到 day/gay job: {DAY_JOB}")
    day_ws["id"] = ""
    day_ws["problem_type"] = "string"
    day_ws["last_job_id"] = DAY_JOB
    day_ws["range_plan"] = day_ws.get("range_plan")
    day_meta = problem_store.upsert_problem(day_ws, force_new=True)
    print(f"[ok] day/gay 新题库: {day_meta['id']}")
    print(f"     title={day_meta.get('title', '')[:60]}")

    # 同步 job 的 problem_meta，指向新题库
    day_job_meta = problem_store.JOBS_DIR / DAY_JOB / "problem_meta.json"
    if day_job_meta.is_file():
        try:
            meta = json.loads(day_job_meta.read_text(encoding="utf-8"))
        except Exception:
            meta = {}
        meta["id"] = day_meta["id"]
        meta["title"] = day_meta.get("title") or meta.get("title") or ""
        meta["problem_type"] = "string"
        meta["problem_lib_id"] = day_meta["id"]
        meta["last_job_id"] = DAY_JOB
        day_job_meta.write_text(
            json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"[ok] 已更新 {DAY_JOB}/problem_meta.json → {day_meta['id']}")

    tree_ws = problem_store.load_workspace_from_dir(
        problem_store.JOBS_DIR / TREE_JOB, source="job"
    )
    if not tree_ws:
        raise SystemExit(f"找不到树题 job: {TREE_JOB}")
    if "树" not in (tree_ws.get("statement") or "") and "结点" not in (
        tree_ws.get("statement") or ""
    ):
        raise SystemExit(f"job {TREE_JOB} 题面不像树题，中止覆盖")
    tree_ws["id"] = TREE_ID
    tree_ws["problem_type"] = "tree"
    tree_ws["last_job_id"] = TREE_JOB
    tree_meta = problem_store.upsert_problem(tree_ws, problem_id=TREE_ID)
    print(f"[ok] 树题已恢复: {tree_meta['id']}")
    print(f"     title={tree_meta.get('title', '')[:60]}")
    print(f"     std_len={len(tree_ws.get('std_code') or '')}")

    # 校验
    restored = problem_store.load_problem(TREE_ID)
    day = problem_store.load_problem(day_meta["id"])
    assert restored and "树" in (restored.get("statement") or "")
    assert day and "day" in (day.get("statement") or "").lower()
    assert not problem_store.should_fork_problem(restored, restored)
    assert problem_store.should_fork_problem(restored, day)
    print("[ok] 校验通过：两题已分离，should_fork 行为正常")


if __name__ == "__main__":
    main()
