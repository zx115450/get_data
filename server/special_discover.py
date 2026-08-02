"""Range 阶段：单独调用大模型，理解特殊样例并产出【一条】构造方案。

流程（与写 range 的 Agent 分离）：
1. understand：题面 + 输入描述 + 标程 + 用户提示 → 深度理解，并建议 preferred_mode
2. discover：基于理解结果，恰好产出 1 条方案；construct_mode 由大模型在 mutate/build 中二选一
3. GUI 可手动点「模式」切换 mutate ↔ build
"""
from __future__ import annotations

import json
import re
from typing import Any

from agent.llm import chat_text

SAMPLES_PER_SCHEME_DEFAULT = 1
# 产品策略：特殊样例每方案 1 组即可；旧方案/题库里的 5 一律压到上限
SAMPLES_PER_SCHEME_CAP = 1
# 特殊 gen 通常 1 条方案；当用户同时要求独立结构性质与标程分支陷阱时允许拆成 2 条
MAX_CANDIDATE_SCHEMES = 2

# construct_mode: mutate = 普通底稿 + 局部替换；build = 从零特殊构造
CONSTRUCT_MODE_MUTATE = "mutate"
CONSTRUCT_MODE_BUILD = "build"
VALID_CONSTRUCT_MODES = {CONSTRUCT_MODE_MUTATE, CONSTRUCT_MODE_BUILD}

# 强结构关键词 → 倾向 build；弱/局部关键词 → 倾向 mutate（仅兜底）
_BUILD_HINT_RE = re.compile(
    r"(DAG|有向无环|连通|二分图|哈密顿|欧拉|平面图|竞赛图|生成树|生成林|"
    r"无环|树形|森林|强连通|双连通|匹配|流网络|拓扑|"
    r"必须存在|保证存在|图必须|树必须)",
    re.I,
)
_MUTATE_HINT_RE = re.compile(
    r"(全相同|全相等|全不同|极值|最大|最小|单调|回文|卡边界|"
    r"只有一个|单一|局部|改权|权值|精度|浮点边界|全\s*0|全\s*1)",
    re.I,
)

_UNDERSTAND_JSON_SHAPE = """
{
  "summary": "用 2～4 句概括这道题在测什么、特殊样例要卡什么",
  "input_format": "输入格式要点（结合标程读入）",
  "std_branches": ["标程里值得针对的特殊分支/边界"],
  "user_intent": "用户特殊提示的意图（无提示则写根据题面/标程自动挖掘）",
  "constraints_note": "与 validator/题面约束相关的注意点",
  "mutate_angle": "若用 mutate：改哪些字段、如何仍合法",
  "build_angle": "若用 build：从零保证什么结构/性质",
  "preferred_mode": "mutate 或 build（二选一，给出更合适的一种）",
  "mode_reason": "为何选该 mode（一句话）"
}
"""

_SCHEME_JSON_SHAPE = """
{
  "schemes": [
    {
      "id": "snake_case英文标识",
      "title": "中文短标题",
      "why": "为什么值得造",
      "must_hold": ["可检验的性质1", "性质2"],
      "construct_hint": "如何构造（文字，不是代码；与所选 mode 一致）",
      "construct_mode": "mutate 或 build（二选一）",
      "source": "std_branch|statement|user_hint|generic_trap",
      "priority": 1
    }
  ]
}
"""

UNDERSTAND_SYSTEM = """你是算法竞赛「特殊测例」分析助手。这是一次独立的大模型调用（不是写 range / gen）。

任务：深度理解题面、标程与用户特殊样例提示，输出结构化分析，并推荐唯一的 construct_mode。

要求：
1. 只输出一个 JSON 对象，不要 Markdown 围栏，不要解释。
2. JSON 格式：
""" + _UNDERSTAND_JSON_SHAPE + """
3. 必须结合标程读入/分支来谈特殊样例，不要只复述用户原文。
4. 语义要严谨：用户说「A 与 B 之间 / 区间内」时，默认指开区间（不含端点），除非题面另有定义；
   禁止用空集合/退化边界（如区间长度为 0、端点重合）让性质平凡成立。
5. 用户提示、题面约束、标程分支是不同来源：只提取用户明确要求的性质；
   不要把「贴近某标程分支」与「用户要的结构性质」擅自 AND 成多重 must_hold。
6. preferred_mode 只能是 mutate 或 build：
   - mutate：弱/局部约束，可在普通合法样例上局部替换（全相同、极值、改起终点、卡数值等）
   - build：强结构或需从零保证的性质（图结构/存在性/需搜索验证等）
   - 不确定时偏 build；需搜索验证的性质优先 build + Finder
7. mutate_angle 与 build_angle 都要写，但 preferred_mode 只选一个。
"""

DISCOVER_SYSTEM = """你是算法竞赛「特殊测例构造方案」助手。这是一次独立的大模型调用。

任务：根据「特殊样例理解」结果，恰好产出 1 条构造方案；construct_mode 由你在 mutate / build 中选择（可参考 preferred_mode，也可推翻并说明 why）。

构造模式只有这两种：
- mutate：先按普通合法逻辑造底稿，再局部替换最少字段，使 must_hold 成立。
- build：在特殊生成器里从零构造，直接保证结构/特殊性质。

要求：
1. 只输出一个 JSON 对象，不要 Markdown 围栏，不要解释。
2. JSON 格式（schemes 通常 1 条；若用户同时要求独立结构性质与标程分支陷阱，可输出 2 条）：
""" + _SCHEME_JSON_SHAPE + """
3. must_hold 必须可检验；不要提出与常见 validator 冲突的非法结构。
4. id 唯一，小写字母数字下划线；construct_hint 必须与所选 construct_mode 一致。
5. 不要输出第 2 条及更多方案。
6. 用户/理解结果里已有可验证的具体数值，必须写进 construct_hint；禁止用退化边界凑性质。
7. must_hold 只写用户真正要的性质，表述与题面/标程变量一致；不要臆造未验证的常数或金样例。
8. 若性质需搜索验证（存在/不存在/区间内无某类对象等）：construct_hint 应要求 Finder
   在合法参数空间内搜索（允许 O(n^2)，默认约 5s/1GB 预算），禁止先瞎编参数再在狭小窗口碰运气。
9. 不要把多条独立性质擅自 AND（除非用户明确都要）；贴近标程分支可以写进 construct_hint，
   但不要因此额外添加用户未要求的 must_hold。
10. 若用户同时要求多条彼此独立的性质，应拆成多条 scheme，而不是在单条 must_hold 里硬 AND。
"""

AUTO_DISCOVER_DESC_MARKER = "（自动挖掘特殊方案）"


def normalize_construct_mode(raw: Any, default: str = CONSTRUCT_MODE_BUILD) -> str:
    """规范化 construct_mode；非法值回退 default。兼容 mute→mutate。"""
    mode = str(raw or "").strip().lower()
    if mode == "mute":
        mode = CONSTRUCT_MODE_MUTATE
    if mode in VALID_CONSTRUCT_MODES:
        return mode
    return default if default in VALID_CONSTRUCT_MODES else CONSTRUCT_MODE_BUILD


def infer_construct_mode(
    title: str = "",
    why: str = "",
    hint: str = "",
    must_hold: list[str] | None = None,
    explicit: Any = None,
) -> str:
    """根据显式字段与文本启发式推断 construct_mode；缺省偏 build。"""
    if explicit is not None and str(explicit).strip():
        return normalize_construct_mode(explicit, CONSTRUCT_MODE_BUILD)
    blob = " ".join(
        [title or "", why or "", hint or ""] + list(must_hold or [])
    )
    has_build = bool(_BUILD_HINT_RE.search(blob))
    has_mutate = bool(_MUTATE_HINT_RE.search(blob))
    if has_build and not has_mutate:
        return CONSTRUCT_MODE_BUILD
    if has_mutate and not has_build:
        return CONSTRUCT_MODE_MUTATE
    return CONSTRUCT_MODE_BUILD


def _extract_json_obj(text: str) -> dict:
    text = (text or "").strip()
    if not text:
        return {}
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if fence:
        text = fence.group(1).strip()
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else {}
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            obj = json.loads(text[start : end + 1])
            return obj if isinstance(obj, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _clip_std(std_code: str, limit: int = 12000) -> str:
    std = (std_code or "").strip()
    if len(std) <= limit:
        return std
    head = limit * 5 // 10
    tail = limit * 4 // 10
    return std[:head] + "\n/* ... truncated ... */\n" + std[-tail:]


def _clip_text(text: str, limit: int) -> str:
    t = text or ""
    if len(t) <= limit:
        return t
    return t[:limit] + "\n…(truncated)…"


def _normalize_scheme(raw: Any, idx: int, forced_mode: str | None = None) -> dict | None:
    if not isinstance(raw, dict):
        return None
    sid = str(raw.get("id") or "").strip()
    if not sid:
        sid = f"scheme_{idx + 1}"
    sid = re.sub(r"[^a-zA-Z0-9_]", "_", sid).lower().strip("_") or f"scheme_{idx + 1}"
    title = str(raw.get("title") or sid).strip()
    why = str(raw.get("why") or "").strip()
    hint = str(raw.get("construct_hint") or "").strip()
    must = raw.get("must_hold") or []
    if isinstance(must, str):
        must = [must]
    must_hold = [str(x).strip() for x in must if str(x).strip()]
    if not must_hold:
        must_hold = [title or why or "满足特殊构造"]
    source = str(raw.get("source") or "user_hint").strip()
    try:
        priority = int(raw.get("priority") or (idx + 1))
    except (TypeError, ValueError):
        priority = idx + 1
    if forced_mode:
        mode = normalize_construct_mode(forced_mode)
    else:
        mode = normalize_construct_mode(
            raw.get("construct_mode"),
            infer_construct_mode(
                title=title, why=why, hint=hint, must_hold=must_hold,
            ),
        )
    return {
        "id": sid,
        "title": title,
        "why": why,
        "must_hold": must_hold,
        "construct_hint": hint,
        "construct_mode": mode,
        "source": source,
        "priority": priority,
        "selected": True,
        "samples_per_scheme": SAMPLES_PER_SCHEME_DEFAULT,
    }


def _pick_one_scheme(
    schemes: list[dict],
    *,
    samples_per_scheme: int,
    user_hint: str = "",
    understanding: dict | None = None,
) -> list[dict]:
    """保留最多 MAX_CANDIDATE_SCHEMES 条方案；mode 以方案自身为准，缺省时用理解推荐。"""
    if not schemes:
        return fallback_user_scheme(user_hint, samples_per_scheme, understanding)
    preferred = normalize_construct_mode(
        (understanding or {}).get("preferred_mode"),
        schemes[0].get("construct_mode") or CONSTRUCT_MODE_BUILD,
    )
    out: list[dict] = []
    for i, raw in enumerate(schemes[:MAX_CANDIDATE_SCHEMES]):
        s = dict(raw)
        # 若该条缺 mode，用理解阶段的推荐
        if not str(s.get("construct_mode") or "").strip():
            s["construct_mode"] = preferred
        else:
            s["construct_mode"] = normalize_construct_mode(s.get("construct_mode"), preferred)
        s["priority"] = i + 1
        s["selected"] = True
        s["samples_per_scheme"] = samples_per_scheme
        out.append(s)
    return out


def fallback_user_scheme(
    user_hint: str,
    samples_per_scheme: int,
    understanding: dict | None = None,
) -> list[dict]:
    """挖掘失败时的兜底：单条方案，mode 取理解推荐或启发式。"""
    hint = (user_hint or "").strip() or "用户指定的特殊情况"
    u = understanding or {}
    mode = normalize_construct_mode(
        u.get("preferred_mode"),
        infer_construct_mode(hint=hint, must_hold=[hint[:200]]),
    )
    angle = (
        str(u.get("mutate_angle") or "").strip()
        if mode == CONSTRUCT_MODE_MUTATE
        else str(u.get("build_angle") or "").strip()
    )
    if not angle:
        angle = (
            f"先造合法底稿再局部修改：{hint[:160]}"
            if mode == CONSTRUCT_MODE_MUTATE
            else f"从零构造满足特殊意图：{hint[:160]}"
        )
    must = [hint[:200]]
    if u.get("user_intent"):
        must.append(str(u["user_intent"])[:200])
    reason = str(u.get("mode_reason") or "").strip()
    return [{
        "id": "user_special",
        "title": "用户描述的特殊情况",
        "why": reason or "用户直接给出的特殊样例描述",
        "must_hold": must,
        "construct_hint": angle,
        "construct_mode": mode,
        "source": "user_hint",
        "priority": 1,
        "selected": True,
        "samples_per_scheme": samples_per_scheme,
    }]


# 兼容旧内部名
_fallback_scheme = fallback_user_scheme


def _fallback_auto_scheme(
    samples_per_scheme: int,
    understanding: dict | None = None,
) -> list[dict]:
    """自动挖掘失败时的兜底：单条，默认偏 build。"""
    u = understanding or {}
    mode = normalize_construct_mode(u.get("preferred_mode"), CONSTRUCT_MODE_BUILD)
    if mode == CONSTRUCT_MODE_MUTATE:
        hint = str(u.get("mutate_angle") or "合法底稿上改极值/全相同等字段")
        must = str(u.get("mutate_angle") or "触发标程非平凡局部边界")[:200]
        title = "标程/题面局部边界"
    else:
        hint = str(u.get("build_angle") or "根据标程分支与题面边界从零构造合法输入")
        must = str(u.get("build_angle") or "触发标程特殊分支或题面结构约束")[:200]
        title = "标程特殊分支或题面退化"
    return [{
        "id": "auto_special",
        "title": title,
        "why": str(u.get("mode_reason") or "自动挖掘未返回有效方案时的兜底"),
        "must_hold": [must],
        "construct_hint": hint,
        "construct_mode": mode,
        "source": "std_branch",
        "priority": 1,
        "selected": True,
        "samples_per_scheme": samples_per_scheme,
    }]


def _context_block(
    problem_statement: str,
    data_range_desc: str,
    std_code: str,
    problem_type: str,
    user_hint: str,
    *,
    auto_mode: bool,
) -> str:
    std = _clip_std(std_code)
    hint_block = (
        "【用户特殊情况提示】（未提供，请从题面/标程自动推断）\n（无）\n"
        if auto_mode
        else f"【用户特殊情况提示（可能模糊，请深入理解）】\n{user_hint}\n"
    )
    return (
        f"【题型】{problem_type or '未知'}\n\n"
        f"【题面】\n{_clip_text(problem_statement, 8000)}\n\n"
        f"【数据范围 / 输入描述】\n{_clip_text(data_range_desc, 4000)}\n\n"
        f"{hint_block}\n"
        f"【标程完整代码（请结合读入与分支理解特殊样例）】\n```\n{std}\n```\n"
    )


def understand_special_samples(
    problem_statement: str,
    data_range_desc: str,
    std_code: str = "",
    user_hint: str = "",
    problem_type: str = "",
    auto_discover: bool = False,
) -> dict:
    """单独调用大模型：理解特殊样例意图，并推荐 preferred_mode。"""
    hint = (user_hint or "").strip()
    if hint == AUTO_DISCOVER_DESC_MARKER:
        hint = ""
    auto_mode = (not hint) and bool(auto_discover)
    user = (
        _context_block(
            problem_statement, data_range_desc, std_code, problem_type, hint,
            auto_mode=auto_mode,
        )
        + "\n请输出特殊样例理解 JSON（含 preferred_mode=mutate|build）。"
    )
    try:
        text = chat_text(UNDERSTAND_SYSTEM, user, temperature=0.2)
        obj = _extract_json_obj(text)
        if isinstance(obj, dict) and obj:
            obj["preferred_mode"] = normalize_construct_mode(
                obj.get("preferred_mode"),
                infer_construct_mode(hint=hint, must_hold=[hint[:200]] if hint else None),
            )
            return obj
    except Exception as e:
        print(f"[special_discover] understand failed: {type(e).__name__}: {e}", flush=True)
    mode = infer_construct_mode(hint=hint, must_hold=[hint[:200]] if hint else None)
    return {
        "summary": hint or "根据题面与标程挖掘特殊边界",
        "user_intent": hint or "自动挖掘",
        "mutate_angle": "合法底稿上做局部替换以逼近特殊意图",
        "build_angle": "从零构造以满足特殊结构/分支",
        "preferred_mode": mode,
        "mode_reason": "启发式兜底",
        "std_branches": [],
    }


def discover_special_schemes(
    problem_statement: str,
    data_range_desc: str,
    std_code: str = "",
    user_hint: str = "",
    problem_type: str = "",
    samples_per_scheme: int = SAMPLES_PER_SCHEME_DEFAULT,
    max_schemes: int = MAX_CANDIDATE_SCHEMES,
    auto_discover: bool = False,
) -> list[dict]:
    """单独调用大模型挖掘特殊样例方案：恰好返回 1 条，mode 由大模型选择。

    步骤：
    1) understand_special_samples（独立 LLM，含 preferred_mode）
    2) 再独立 LLM 产出恰好 1 条 scheme（可覆盖 preferred_mode）
    """
    del max_schemes  # 固定为 1；保留参数兼容旧调用
    samples_per_scheme = clamp_samples_per_scheme(samples_per_scheme)
    hint = (user_hint or "").strip()
    if hint == AUTO_DISCOVER_DESC_MARKER:
        hint = ""
    auto_mode = (not hint) and bool(auto_discover)
    if not hint and not auto_mode:
        return []

    print(
        f"[special_discover] separate LLM: understand "
        f"(hint_len={len(hint)}, auto={auto_mode}, type={problem_type or '-'})",
        flush=True,
    )
    understanding = understand_special_samples(
        problem_statement,
        data_range_desc,
        std_code=std_code,
        user_hint=hint,
        problem_type=problem_type,
        auto_discover=auto_discover,
    )

    def _fail_fallback() -> list[dict]:
        if auto_mode:
            return _fallback_auto_scheme(samples_per_scheme, understanding)
        return fallback_user_scheme(hint, samples_per_scheme, understanding)

    preferred = normalize_construct_mode(
        understanding.get("preferred_mode"), CONSTRUCT_MODE_BUILD,
    )
    ctx = _context_block(
        problem_statement, data_range_desc, std_code, problem_type, hint,
        auto_mode=auto_mode,
    )
    user = (
        f"{ctx}\n"
        f"【特殊样例理解结果】\n"
        f"```json\n{json.dumps(understanding, ensure_ascii=False, indent=2)}\n```\n\n"
        f"请严格输出恰好 1 条 scheme。\n"
        f"理解阶段建议 preferred_mode={preferred}（理由：{understanding.get('mode_reason') or '—'}）；\n"
        f"你可以采纳或改选 mutate/build，但必须只选一种，并让 construct_hint 与之匹配。"
    )

    print(
        f"[special_discover] separate LLM: discover 1 scheme "
        f"(preferred_mode={preferred})",
        flush=True,
    )
    try:
        text = chat_text(DISCOVER_SYSTEM, user, temperature=0.3)
        obj = _extract_json_obj(text)
        raw_list = obj.get("schemes") if isinstance(obj, dict) else None
        # 兼容模型直接返回单对象
        if not isinstance(raw_list, list) and isinstance(obj, dict) and obj.get("id"):
            raw_list = [obj]
        if not isinstance(raw_list, list) or not raw_list:
            print("[special_discover] discover empty → fallback one", flush=True)
            return _fail_fallback()
    except Exception as e:
        print(f"[special_discover] discover failed: {type(e).__name__}: {e}", flush=True)
        return _fail_fallback()

    normalized: list[dict] = []
    for i, raw in enumerate(raw_list[:MAX_CANDIDATE_SCHEMES]):
        s = _normalize_scheme(raw, i)
        if not s:
            continue
        if auto_mode and s.get("source") == "user_hint":
            s["source"] = "std_branch"
        s["samples_per_scheme"] = samples_per_scheme
        normalized.append(s)
        if len(normalized) >= MAX_CANDIDATE_SCHEMES:
            break

    if not normalized:
        return _fail_fallback()

    # LLM 可能已输出多条；取选中方案即可（不再做题特化硬 AND 拆分）
    split = _pick_one_scheme(
        normalized,
        samples_per_scheme=samples_per_scheme,
        user_hint=hint,
        understanding=understanding,
    )[:MAX_CANDIDATE_SCHEMES]

    summary = str(understanding.get("summary") or "").strip()
    mode_reason = str(understanding.get("mode_reason") or "").strip()
    for i, s in enumerate(split):
        s["priority"] = i + 1
        s["selected"] = True
        s["samples_per_scheme"] = samples_per_scheme
        s["construct_mode"] = normalize_construct_mode(
            s.get("construct_mode"),
            infer_construct_mode(
                title=str(s.get("title") or ""),
                why=str(s.get("why") or ""),
                hint=str(s.get("construct_hint") or ""),
                must_hold=list(s.get("must_hold") or []),
            ),
        )
        if summary:
            s.setdefault("understanding_summary", summary)
        if mode_reason:
            s.setdefault("mode_reason", mode_reason)

    ids = ", ".join(f"{s.get('id')}={s.get('construct_mode')}" for s in split)
    print(
        f"[special_discover] scheme(s) ready: {ids}",
        flush=True,
    )
    return split


def selected_schemes(schemes: list[dict] | None) -> list[dict]:
    """返回 selected 的方案（保持顺序）。"""
    out = []
    for s in schemes or []:
        if not isinstance(s, dict):
            continue
        if s.get("selected", True) is False:
            continue
        if "construct_mode" not in s:
            s["construct_mode"] = infer_construct_mode(
                title=str(s.get("title") or ""),
                why=str(s.get("why") or ""),
                hint=str(s.get("construct_hint") or ""),
                must_hold=list(s.get("must_hold") or []),
            )
        else:
            s["construct_mode"] = normalize_construct_mode(s.get("construct_mode"))
        out.append(s)
    return out


def clamp_samples_per_scheme(raw: Any, default: int = SAMPLES_PER_SCHEME_DEFAULT) -> int:
    """每方案特殊样例数：至少 1，且不超过产品上限。"""
    try:
        n = int(raw if raw is not None else default)
    except (TypeError, ValueError):
        n = default
    return max(1, min(n, SAMPLES_PER_SCHEME_CAP))


def compute_special_count(schemes: list[dict] | None, default_per: int = SAMPLES_PER_SCHEME_DEFAULT) -> int:
    """选中方案的样例总数。"""
    total = 0
    for s in selected_schemes(schemes):
        total += clamp_samples_per_scheme(s.get("samples_per_scheme"), default_per)
    return total


def apply_schemes_to_range(
    range_json: dict,
    schemes: list[dict],
    user_hint: str = "",
    regular_count: int | None = None,
) -> dict:
    """把方案写入 range.json，并修正 count / special_samples_count。"""
    data = dict(range_json or {})
    old_total = int(data.get("count") or 15)
    old_special = int(data.get("special_samples_count") or 0)
    if regular_count is None:
        regular_count = max(1, old_total - old_special) if old_special else old_total
        if regular_count <= 0:
            regular_count = 15

    for s in schemes:
        if "selected" not in s:
            s["selected"] = True
        s["samples_per_scheme"] = clamp_samples_per_scheme(
            s.get("samples_per_scheme"), SAMPLES_PER_SCHEME_DEFAULT,
        )
        s["construct_mode"] = normalize_construct_mode(
            s.get("construct_mode"),
            infer_construct_mode(
                title=str(s.get("title") or ""),
                why=str(s.get("why") or ""),
                hint=str(s.get("construct_hint") or ""),
                must_hold=list(s.get("must_hold") or []),
            ),
        )

    special_count = compute_special_count(schemes)
    data["special_schemes"] = schemes
    hint = (user_hint or "").strip()
    if hint:
        data["special_samples_desc"] = hint
    elif schemes and not (data.get("special_samples_desc") or "").strip():
        data["special_samples_desc"] = AUTO_DISCOVER_DESC_MARKER
    data["special_samples_count"] = special_count
    data["count"] = int(regular_count) + special_count
    return data


def scheme_type_name(scheme_id: str) -> str:
    """批量生成用的 --type 名。"""
    sid = re.sub(r"[^a-zA-Z0-9_]", "_", (scheme_id or "special").lower()).strip("_")
    return f"special:{sid}"


def parse_scheme_type(typ: str) -> str | None:
    """若 typ 是 special:xxx / special_samples，返回 scheme id 或 ''。"""
    t = (typ or "").strip()
    if t.startswith("special:"):
        return t[len("special:") :] or None
    if t == "special_samples":
        return ""
    return None
