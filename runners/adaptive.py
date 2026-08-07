"""Gen Agent 自适应步数。"""

from knowledge.few_shots import normalize_problem_types


def adaptive_steps(range_json: dict | None, problem_type: str | list[str] | None, resume_info: dict | None) -> int:
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
    types = set(normalize_problem_types(problem_type))
    if types & {"graph", "tree", "geometry", "interactive", "dp"}:
        score += 1
    if types & {"string", "matrix"}:
        score += 1
    steps = base + score * 10
    return max(50, min(steps, 100))
