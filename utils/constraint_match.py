"""约束一致性检查：从题面/范围描述中抽取数值约束，与 range.json 比对。"""
from __future__ import annotations

import re
from typing import Optional


def _parse_number(s: str) -> Optional[int]:
    """把 '1e5', '10^9', '2^30', '2^(30)', '1,000,000', '100000' 等转成整数。"""
    s = s.strip().replace(",", "").replace(" ", "")
    if not s:
        return None
    # a^b 或 a^(b)，如 10^9 / 2^30 / 2^(30)
    m = re.match(r"^(\d+)\^\(?(\d+)\)?$", s)
    if m:
        base = int(m.group(1))
        exp = int(m.group(2))
        # 避免超大指数导致内存爆炸，限制在合理范围
        if exp > 64:
            return None
        return base ** exp
    # 1e5 / 1E9
    try:
        if "e" in s.lower():
            return int(float(s))
    except ValueError:
        pass
    try:
        return int(s)
    except ValueError:
        return None


def _extract_number(text: str) -> Optional[int]:
    """从一段文本中抽取第一个可识别的数字。"""
    # 优先匹配 10^9 / 1e5 这种
    for pat in [r"10\^\d+", r"\d+e\d+", r"\d{1,3}(?:,\d{3})+", r"\d+"]:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            return _parse_number(m.group(0))
    return None


def _looks_like_number_or_expr(s: str) -> bool:
    """判断一段文本是否像数字、幂或简单表达式。"""
    s = s.strip()
    if not s:
        return False
    # 纯数字 / 幂 / 括号表达式
    if re.match(r"^(\d+e\d+|\d+\^\(?\d+\)?|\d{1,3}(?:,\d{3})+|\d+|\([^\n]*\))$", s, re.IGNORECASE):
        return True
    return False


def extract_numeric_constraints(text: str) -> dict[str, tuple[Optional[int], Optional[int]]]:
    """从纯文本中抽取变量名 -> (min, max) 的约束。

    支持的写法示例：
      - n ∈ [1, 1e5]
      - 1 ≤ n ≤ 100000
      - n in [1, 10^5]
      - total_len ≤ 200000
      - 1 <= |s| <= 1000
      - 1 <= m <= n(n-1)/2   （上界为表达式时，hi 记为 None）
      - 1 <= k < n           （上界为变量时，hi 记为 None）
    无法识别 min 时 min 为 None，无法识别 max 时 max 为 None。
    """
    if not text:
        return {}

    constraints: dict[str, tuple[Optional[int], Optional[int]]] = {}

    # 1) 形如 "x ∈ [1, 1e5]" / "x in [1, 100000]"
    interval_re = re.compile(
        r"([a-zA-Z_][a-zA-Z0-9_]*|\|[a-zA-Z_][a-zA-Z0-9_]*\||[a-zA-Z_][a-zA-Z0-9_]*)\s*"
        r"(?:∈|in|∈|属于)\s*"
        r"[\[\(]\s*([^,\)\]]+)\s*,\s*([^,\)\]]+)\s*[\]\)]",
        re.IGNORECASE,
    )
    for m in interval_re.finditer(text):
        var = m.group(1).strip("| ")
        lo = _parse_number(m.group(2))
        hi = _parse_number(m.group(3))
        constraints[var] = (lo, hi)

    # 2) 形如 "1 ≤ n ≤ 1e5" / "1 <= n <= 100000" / "|s| <= 1000" / "1 <= m <= n(n-1)/2"
    # 同时捕获前后两个数字/表达式与中间变量，避免 lookbehind 宽度不定的问题
    bound_re = re.compile(
        r"\b(\d+e\d+|\d+\^\(?\d+\)?|\d{1,3}(?:,\d{3})+|\d+)\s*"
        r"(?:≤|>=|<=|≥|≤|=<|=>|<|>)\s*"
        r"([a-zA-Z_][a-zA-Z0-9_]*|\|[a-zA-Z_][a-zA-Z0-9_]*\|)\s*"
        r"(?:≤|>=|<=|≥|≤|=<|=>|<|>)\s*"
        r"(\d+e\d+|\d+\^\(?\d+\)?|\d{1,3}(?:,\d{3})+|\d+|\([^\n]*\)|[a-zA-Z_][a-zA-Z0-9_]*)",
        re.IGNORECASE,
    )
    for m in bound_re.finditer(text):
        var = m.group(2).strip("| ")
        lo = _parse_number(m.group(1))
        hi = _parse_number(m.group(3)) if _looks_like_number_or_expr(m.group(3)) else None
        constraints[var] = (lo, hi)

    # 3) 形如 "total_len ≤ 200000" / "|s| ≤ 1000" / "w ≤ 2^30"（只有上限）
    upper_re = re.compile(
        r"([a-zA-Z_][a-zA-Z0-9_]*|\|[a-zA-Z_][a-zA-Z0-9_]*\|)\s*"
        r"(?:≤|<=|≤|<|>=|=>|>|≥)\s*"
        r"(\d+e\d+|\d+\^\(?\d+\)?|\d{1,3}(?:,\d{3})+|\d+)",
        re.IGNORECASE,
    )
    for m in upper_re.finditer(text):
        var = m.group(1).strip("| ")
        hi = _parse_number(m.group(2))
        if var not in constraints:
            constraints[var] = (None, hi)
        else:
            cur_lo, cur_hi = constraints[var]
            if cur_hi is None or (hi is not None and hi > cur_hi):
                constraints[var] = (cur_lo, hi)

    # 4) 形如 "n ≥ 1" / "w ≥ 2^10"（只有下限）
    lower_re = re.compile(
        r"([a-zA-Z_][a-zA-Z0-9_]*|\|[a-zA-Z_][a-zA-Z0-9_]*\|)\s*"
        r"(?:≥|>=|>|≥)\s*"
        r"(\d+e\d+|\d+\^\(?\d+\)?|\d{1,3}(?:,\d{3})+|\d+)",
        re.IGNORECASE,
    )
    for m in lower_re.finditer(text):
        var = m.group(1).strip("| ")
        lo = _parse_number(m.group(2))
        if var not in constraints:
            constraints[var] = (lo, None)
        else:
            cur_lo, cur_hi = constraints[var]
            if cur_lo is None or (lo is not None and lo < cur_lo):
                constraints[var] = (lo, cur_hi)

    return constraints


def _mentioned_variables(text: str) -> set[str]:
    """从文本中识别出可能作为约束变量的独立英文标识符（n, m, k, w 等）。"""
    words = re.findall(r"\b[a-zA-Z_][a-zA-Z0-9_]*\b", text or "")
    # 过滤常见非变量词
    stop = {"and", "or", "not", "is", "in", "of", "to", "the", "a", "an", "for", "with", "as", "by", "on", "at"}
    return {w for w in words if w.lower() not in stop}


def check_constraints_consistency(
    range_json: dict,
    statement: str,
    data_range_desc: str,
    tolerance: float = 0.0,
) -> list[str]:
    """比对 range.json constraints 与文本中抽取的约束，返回不一致的警告列表。

    tolerance: 允许的相对误差（如 0.01 表示 1%）。

    说明：
      - 若题面中某变量上界是表达式（如 m <= n(n-1)/2）或另一个变量（如 k < n），
         extractor 只能识别到已知的一边，另一边为 None；此时只要该变量确实在文本中
        出现过，就不再报“未识别到约束”。
      - 只有当变量在题面中完全未出现，才报缺失。
    """
    warnings: list[str] = []
    text_plain = f"{statement or ''}\n{data_range_desc or ''}"
    text_constraints = extract_numeric_constraints(text_plain)
    mentioned = _mentioned_variables(text_plain)
    range_constraints = range_json.get("constraints") or {}

    # 1) range.json 里有的变量，文本里是否一致
    for var, (rlo, rhi) in range_constraints.items():
        if var not in text_constraints:
            # 若变量名在文本中根本未出现，才报缺失；出现过但范围是表达式无法解析则忽略
            if var not in mentioned:
                warnings.append(f"range.json 含变量 '{var}'，但题面/范围描述中未识别到对应约束")
            continue
        tlo, thi = text_constraints[var]
        if rlo is not None and tlo is not None:
            if not _close(rlo, tlo, tolerance):
                warnings.append(
                    f"变量 '{var}' 最小值不一致：range.json={rlo}，文本识别到={tlo}"
                )
        if rhi is not None and thi is not None:
            if not _close(rhi, thi, tolerance):
                warnings.append(
                    f"变量 '{var}' 最大值不一致：range.json={rhi}，文本识别到={thi}"
                )

    # 2) 文本里有的变量，range.json 里是否也有
    for var, (tlo, thi) in text_constraints.items():
        if var not in range_constraints:
            warnings.append(f"题面/范围描述提到变量 '{var}'，但 range.json constraints 未包含")

    return warnings


def _close(a: int, b: int, tolerance: float) -> bool:
    if a == b:
        return True
    if tolerance <= 0:
        return False
    # 相对误差：以较大者为分母
    maxv = max(abs(a), abs(b), 1)
    return abs(a - b) / maxv <= tolerance


def _format_json(d: dict) -> str:
    """把 dict 格式化成紧凑 JSON 字符串。"""
    return json.dumps(d, ensure_ascii=False, indent=2)


def agent_verify_constraints(
    range_json: dict,
    statement: str,
    data_range_desc: str,
    code_warnings: list[str],
) -> tuple[bool, list[str]]:
    """调用 LLM 对 range.json 与题面/范围描述做语义复核。

    返回 (是否通过, 需要报告的不一致列表)。
    如果 LLM 判断代码检测到的告警都是误报，则返回 ([], []) 表示通过。
    如果 LLM 确认某些告警确实是不一致，则返回这些告警。
    如果 LLM 调用失败，出于安全考虑返回原始 code_warnings（不自动放行）。
    """
    if not code_warnings:
        return True, []

    try:
        from agent.llm import chat_text
    except Exception as e:
        return False, [f"Agent 复核加载失败: {e}"] + code_warnings

    system = (
        "你是算法竞赛数据范围审核专家。你的任务是判断 range.json 中的 constraints "
        "是否与题面/数据范围描述一致。"
        "\n\n规则："
        "\n1. 题面中的约束可能用 LaTeX/表达式书写（如 m ≤ n(n-1)/2、w ≤ 2^30、k < n），"
        "只要 range.json 的数值在这些表达式允许范围内，就算一致。"
        "\n2. 如果代码工具报告某变量在题面中未识别，但你能在题面/范围描述中找到该变量，"
        "则这是代码误报，应视为通过。"
        "\n3. 只有当 range.json 的数值明显与题面冲突（如 n 的上限小于题面给定）时，才认为不一致。"
        "\n4. 输出必须是纯 JSON，格式：{\"pass\": true/false, \"real_warnings\": [\"...\"]}。"
        "\n5. 若 code_warnings 全是误报，返回 {\"pass\": true, \"real_warnings\": []}。"
    )

    user = (
        "【题面】\n" + (statement or "（无）") + "\n\n"
        "【数据范围描述】\n" + (data_range_desc or "（无）") + "\n\n"
        "【range.json】\n" + _format_json(range_json) + "\n\n"
        "【代码工具检测到的潜在不一致】\n" + "\n".join(f"- {w}" for w in code_warnings) + "\n\n"
        "请判断上述告警是否真正不一致。只返回 JSON，不要解释。"
    )

    try:
        resp = chat_text(system, user, temperature=0.1)
    except Exception as e:
        return False, [f"Agent 复核调用失败: {e}"] + code_warnings

    # 尝试从响应中提取 JSON
    try:
        # 先尝试整个响应是否为 JSON
        result = json.loads(resp)
    except json.JSONDecodeError:
        # 尝试从 ```json 块中提取
        block = re.search(r"```json\s*([\s\S]*?)\s*```", resp, re.IGNORECASE)
        if block:
            try:
                result = json.loads(block.group(1))
            except json.JSONDecodeError:
                return False, [f"Agent 复核返回非法 JSON: {resp[:200]}"] + code_warnings
        else:
            return False, [f"Agent 复核返回非法 JSON: {resp[:200]}"] + code_warnings

    if not isinstance(result, dict):
        return False, [f"Agent 复核返回非对象 JSON: {resp[:200]}"] + code_warnings

    passed = bool(result.get("pass"))
    real_warnings = result.get("real_warnings") or []
    if not isinstance(real_warnings, list):
        real_warnings = [str(real_warnings)]

    return passed, [str(w) for w in real_warnings if w]
