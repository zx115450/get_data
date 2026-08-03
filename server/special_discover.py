"""Range 阶段：单独调用大模型，产出【一条】特殊样例构造方案。

流程（与写 range 的 Agent 分离）：
1. 一次 LLM：理解 + 产出 1 条 scheme（含 preferred_mode / property_checks）
2. 代码规则盖章 construct_mode（mutate 窄门，默认偏 build）
3. GUI 可手动点「模式」切换（force_mutate / force_build）
"""
from __future__ import annotations

import json
import re
from typing import Any

from agent.llm import chat_text
from pipeline.gen_data import DEFAULT_REGULAR_COUNT, _clamp_regular_count

SAMPLES_PER_SCHEME_DEFAULT = 1
# 产品策略：特殊样例每方案 1 组即可；旧方案/题库里的 5 一律压到上限
SAMPLES_PER_SCHEME_CAP = 1
# 特殊 gen 通常 1 条方案；当用户同时要求独立结构性质与标程分支陷阱时允许拆成 2 条
MAX_CANDIDATE_SCHEMES = 2
MUST_HOLD_CAP = 2

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
# 存在性 / 强结构 → 禁止 mutate
_BLOCK_MUTATE_RE = re.compile(
    r"(不存在|存在|区间内|之间|范围内|之内|开区间|闭区间|"
    r"DAG|有向无环|连通|二分图|哈密顿|欧拉|生成树|强连通|双连通|"
    r"必须存在|保证存在|图必须|树必须|无环|匹配|流网络|拓扑)",
    re.I,
)
# construct_hint 里「改字段」迹象
_MUTATE_FIELD_HINT_RE = re.compile(
    r"(改|替换|设为|置为|改为|改成|patch|字段|全相同|全相等|极值|全\s*0|全\s*1)",
    re.I,
)

_DISCOVER_JSON_SHAPE = """
{
  "summary": "用 2～4 句概括这道题在测什么、特殊样例要卡什么",
  "preferred_mode": "mutate 或 build",
  "mode_reason": "为何选该 mode（一句话）",
  "mutate_angle": "若用 mutate：改哪些字段（1～2 个）、如何仍合法",
  "build_angle": "若用 build：从零保证什么结构/性质",
  "schemes": [
    {
      "id": "snake_case英文标识",
      "title": "中文短标题",
      "why": "为什么值得造",
      "must_hold": ["可检验的性质1", "最多2条"],
      "property_checks": ["可代码化断言，与 must_hold 对应"],
      "construct_hint": "可执行构造步骤/参数骨架（与 mode 一致）",
      "mutate_fields": ["仅 mutate 时：拟改字段名，1～2 个"],
      "construct_mode": "mutate 或 build",
      "source": "std_branch|statement|user_hint|generic_trap",
      "priority": 1
    }
  ]
}
"""

DISCOVER_SYSTEM = """你是算法竞赛「特殊测例」助手。一次调用完成：理解题面/标程/用户提示，并恰好产出 1 条构造方案。

构造模式：
- mutate：普通合法底稿上局部替换最少字段（仅适合全相同/极值/改 1～2 字段等）
- build：从零构造保证结构/特殊性质（默认；不确定时选 build）

要求：
1. 只输出一个 JSON 对象，不要 Markdown 围栏，不要解释。格式：
""" + _DISCOVER_JSON_SHAPE + """
2. schemes 恰好 1 条（若用户同时要求独立结构性质与标程分支陷阱，最多 2 条）。
3. must_hold ≤ 2 条，必须可检验；禁止用空集合/端点重合让性质平凡成立。
4. property_checks 与 must_hold 对应，写成可代码判定的断言（供 check_special 使用）。
5. construct_hint 必须可执行（步骤或参数骨架）；禁止写「先搜索」「碰运气」。
6. 仅当能写清「改 ≤2 个字段且仍合法」时才选 mutate，并填写 mutate_fields；否则 build。
7. 存在/不存在/区间内无某类对象/强图结构 → 必须 build，并给出确定性或半随机构造步骤。
8. 不要把多条独立性质擅自 AND；用户明确都要时可拆成多条 scheme。
9. 结合标程分支，但不要把「贴近分支」擅自加成用户未要求的 must_hold。
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


def _extract_mutate_fields(scheme: dict) -> list[str]:
    raw = scheme.get("mutate_fields")
    if isinstance(raw, str) and raw.strip():
        return [raw.strip()]
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()][:2]
    return []


def seal_construct_mode(
    scheme: dict,
    *,
    honor_force: bool = True,
) -> tuple[str, str]:
    """规则盖章 mode：mutate 窄门，否则 build。

    返回 (mode, reason)。GUI 可设 force_mutate / force_build / force_construct_mode。
    """
    if honor_force:
        forced = scheme.get("force_construct_mode") or scheme.get("force_mode")
        if scheme.get("force_build"):
            forced = CONSTRUCT_MODE_BUILD
        if scheme.get("force_mutate"):
            forced = CONSTRUCT_MODE_MUTATE
        if forced is not None and str(forced).strip():
            mode = normalize_construct_mode(forced, CONSTRUCT_MODE_BUILD)
            return mode, f"user_force:{mode}"

    must = [str(x).strip() for x in (scheme.get("must_hold") or []) if str(x).strip()]
    hint = str(scheme.get("construct_hint") or "")
    title = str(scheme.get("title") or "")
    why = str(scheme.get("why") or "")
    blob = " ".join([title, why, hint] + must)
    fields = _extract_mutate_fields(scheme)
    requested = normalize_construct_mode(
        scheme.get("construct_mode"),
        infer_construct_mode(title=title, why=why, hint=hint, must_hold=must),
    )

    if len(must) > MUST_HOLD_CAP:
        return CONSTRUCT_MODE_BUILD, f"must_hold>{MUST_HOLD_CAP}"
    if _BLOCK_MUTATE_RE.search(blob):
        return CONSTRUCT_MODE_BUILD, "blocked_by_existence_or_structure"
    if requested != CONSTRUCT_MODE_MUTATE:
        return CONSTRUCT_MODE_BUILD, "default_build"

    # 申请 mutate：明确字段列表（1～2）即可；否则需 mutate 词 + 改字段迹象
    has_field_signal = bool(fields) or bool(_MUTATE_FIELD_HINT_RE.search(hint))
    has_mutate_kw = bool(_MUTATE_HINT_RE.search(blob))
    if fields and 1 <= len(fields) <= 2:
        scheme["mutate_fields"] = fields
        return CONSTRUCT_MODE_MUTATE, "narrow_mutate_ok"
    if has_mutate_kw and has_field_signal:
        return CONSTRUCT_MODE_MUTATE, "narrow_mutate_ok"
    return CONSTRUCT_MODE_BUILD, "mutate_gate_failed"


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
    must_hold = [str(x).strip() for x in must if str(x).strip()][:MUST_HOLD_CAP]
    if not must_hold:
        must_hold = [title or why or "满足特殊构造"]
    props = raw.get("property_checks") or []
    if isinstance(props, str):
        props = [props]
    property_checks = [str(x).strip() for x in props if str(x).strip()][:MUST_HOLD_CAP]
    if not property_checks:
        property_checks = list(must_hold)
    source = str(raw.get("source") or "user_hint").strip()
    try:
        priority = int(raw.get("priority") or (idx + 1))
    except (TypeError, ValueError):
        priority = idx + 1
    scheme = {
        "id": sid,
        "title": title,
        "why": why,
        "must_hold": must_hold,
        "property_checks": property_checks,
        "construct_hint": hint,
        "mutate_fields": _extract_mutate_fields(raw),
        "construct_mode": normalize_construct_mode(
            forced_mode if forced_mode else raw.get("construct_mode"),
            infer_construct_mode(
                title=title, why=why, hint=hint, must_hold=must_hold,
            ),
        ),
        "source": source,
        "priority": priority,
        "selected": True,
        "samples_per_scheme": SAMPLES_PER_SCHEME_DEFAULT,
    }
    for k in ("force_mutate", "force_build", "force_construct_mode", "force_mode"):
        if k in raw:
            scheme[k] = raw[k]
    mode, reason = seal_construct_mode(scheme, honor_force=True)
    scheme["construct_mode"] = mode
    scheme["mode_sealed_reason"] = reason
    return scheme


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
    """挖掘失败时的兜底：单条方案，mode 经规则盖章。"""
    hint = (user_hint or "").strip() or "用户指定的特殊情况"
    u = understanding or {}
    must = [hint[:200]]
    if u.get("user_intent"):
        must.append(str(u["user_intent"])[:200])
    must = must[:MUST_HOLD_CAP]
    angle = str(u.get("build_angle") or u.get("mutate_angle") or "").strip()
    if not angle:
        angle = f"从零构造满足特殊意图：{hint[:160]}"
    reason = str(u.get("mode_reason") or "").strip()
    scheme = {
        "id": "user_special",
        "title": "用户描述的特殊情况",
        "why": reason or "用户直接给出的特殊样例描述",
        "must_hold": must,
        "property_checks": list(must),
        "construct_hint": angle,
        "construct_mode": normalize_construct_mode(
            u.get("preferred_mode"),
            infer_construct_mode(hint=hint, must_hold=must),
        ),
        "source": "user_hint",
        "priority": 1,
        "selected": True,
        "samples_per_scheme": samples_per_scheme,
    }
    mode, seal_reason = seal_construct_mode(scheme)
    scheme["construct_mode"] = mode
    scheme["mode_sealed_reason"] = seal_reason
    if mode == CONSTRUCT_MODE_MUTATE and u.get("mutate_angle"):
        scheme["construct_hint"] = str(u.get("mutate_angle"))
    return [scheme]


# 兼容旧内部名
_fallback_scheme = fallback_user_scheme


def _fallback_auto_scheme(
    samples_per_scheme: int,
    understanding: dict | None = None,
) -> list[dict]:
    """自动挖掘失败时的兜底：单条，默认偏 build。"""
    u = understanding or {}
    hint = str(u.get("build_angle") or "根据标程分支与题面边界从零构造合法输入")
    must = str(u.get("build_angle") or "触发标程特殊分支或题面结构约束")[:200]
    scheme = {
        "id": "auto_special",
        "title": "标程特殊分支或题面退化",
        "why": str(u.get("mode_reason") or "自动挖掘未返回有效方案时的兜底"),
        "must_hold": [must],
        "property_checks": [must],
        "construct_hint": hint,
        "construct_mode": normalize_construct_mode(
            u.get("preferred_mode"), CONSTRUCT_MODE_BUILD,
        ),
        "source": "std_branch",
        "priority": 1,
        "selected": True,
        "samples_per_scheme": samples_per_scheme,
    }
    mode, seal_reason = seal_construct_mode(scheme)
    scheme["construct_mode"] = mode
    scheme["mode_sealed_reason"] = seal_reason
    return [scheme]


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
    """兼容旧接口：内部走单次 discover，返回摘要字段。"""
    schemes = discover_special_schemes(
        problem_statement,
        data_range_desc,
        std_code=std_code,
        user_hint=user_hint,
        problem_type=problem_type,
        auto_discover=auto_discover,
    )
    if not schemes:
        hint = (user_hint or "").strip()
        mode = infer_construct_mode(hint=hint, must_hold=[hint[:200]] if hint else None)
        return {
            "summary": hint or "根据题面与标程挖掘特殊边界",
            "preferred_mode": mode,
            "mode_reason": "empty",
            "schemes": [],
        }
    s0 = schemes[0]
    return {
        "summary": str(s0.get("understanding_summary") or s0.get("why") or ""),
        "preferred_mode": s0.get("construct_mode"),
        "mode_reason": s0.get("mode_sealed_reason") or s0.get("mode_reason") or "",
        "mutate_angle": s0.get("construct_hint") if s0.get("construct_mode") == CONSTRUCT_MODE_MUTATE else "",
        "build_angle": s0.get("construct_hint") if s0.get("construct_mode") == CONSTRUCT_MODE_BUILD else "",
        "schemes": schemes,
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
    """一次 LLM 挖掘特殊样例方案；mode 经 seal_construct_mode 盖章。"""
    del max_schemes  # 保留参数兼容旧调用
    samples_per_scheme = clamp_samples_per_scheme(samples_per_scheme)
    hint = (user_hint or "").strip()
    if hint == AUTO_DISCOVER_DESC_MARKER:
        hint = ""
    auto_mode = (not hint) and bool(auto_discover)
    if not hint and not auto_mode:
        return []

    def _fail_fallback(understanding: dict | None = None) -> list[dict]:
        if auto_mode:
            return _fallback_auto_scheme(samples_per_scheme, understanding)
        return fallback_user_scheme(hint, samples_per_scheme, understanding)

    ctx = _context_block(
        problem_statement, data_range_desc, std_code, problem_type, hint,
        auto_mode=auto_mode,
    )
    user = (
        f"{ctx}\n"
        "请输出理解摘要 + 恰好 1 条 scheme 的 JSON（含 preferred_mode、property_checks）。\n"
        "不确定 mode 时选 build；mutate 仅当能写清改 ≤2 个字段。"
    )

    print(
        f"[special_discover] single LLM: discover "
        f"(hint_len={len(hint)}, auto={auto_mode}, type={problem_type or '-'})",
        flush=True,
    )
    understanding: dict = {}
    try:
        text = chat_text(DISCOVER_SYSTEM, user, temperature=0.3)
        obj = _extract_json_obj(text)
        if not isinstance(obj, dict) or not obj:
            print("[special_discover] discover empty → fallback one", flush=True)
            return _fail_fallback()
        understanding = {
            "summary": str(obj.get("summary") or "").strip(),
            "preferred_mode": normalize_construct_mode(
                obj.get("preferred_mode"), CONSTRUCT_MODE_BUILD,
            ),
            "mode_reason": str(obj.get("mode_reason") or "").strip(),
            "mutate_angle": str(obj.get("mutate_angle") or "").strip(),
            "build_angle": str(obj.get("build_angle") or "").strip(),
        }
        raw_list = obj.get("schemes")
        if not isinstance(raw_list, list) and obj.get("id"):
            raw_list = [obj]
        if not isinstance(raw_list, list) or not raw_list:
            print("[special_discover] schemes empty → fallback one", flush=True)
            return _fail_fallback(understanding)
    except Exception as e:
        print(f"[special_discover] discover failed: {type(e).__name__}: {e}", flush=True)
        return _fail_fallback()

    normalized: list[dict] = []
    for i, raw in enumerate(raw_list[:MAX_CANDIDATE_SCHEMES]):
        if isinstance(raw, dict) and not raw.get("construct_mode"):
            raw = dict(raw)
            raw["construct_mode"] = understanding.get("preferred_mode")
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
        return _fail_fallback(understanding)

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
        mode, seal_reason = seal_construct_mode(s, honor_force=True)
        s["construct_mode"] = mode
        s["mode_sealed_reason"] = seal_reason
        if summary:
            s.setdefault("understanding_summary", summary)
        if mode_reason:
            s.setdefault("mode_reason", mode_reason)

    ids = ", ".join(
        f"{s.get('id')}={s.get('construct_mode')}({s.get('mode_sealed_reason')})"
        for s in split
    )
    print(f"[special_discover] scheme(s) ready: {ids}", flush=True)
    return split


def selected_schemes(schemes: list[dict] | None) -> list[dict]:
    """返回 selected 的方案（保持顺序）；mode 经规则盖章。"""
    out = []
    for s in schemes or []:
        if not isinstance(s, dict):
            continue
        if s.get("selected", True) is False:
            continue
        mh = list(s.get("must_hold") or [])
        if len(mh) > MUST_HOLD_CAP:
            s["must_hold"] = mh[:MUST_HOLD_CAP]
        if not s.get("property_checks"):
            s["property_checks"] = list(s.get("must_hold") or [])
        mode, reason = seal_construct_mode(s, honor_force=True)
        s["construct_mode"] = mode
        s["mode_sealed_reason"] = reason
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
    old_total = int(data.get("count") or DEFAULT_REGULAR_COUNT)
    old_special = int(data.get("special_samples_count") or 0)
    if regular_count is None:
        if old_special > 0 and old_total > old_special:
            regular_count = old_total - old_special
        else:
            regular_count = old_total
    regular_count = _clamp_regular_count(regular_count)

    for s in schemes:
        if "selected" not in s:
            s["selected"] = True
        s["samples_per_scheme"] = clamp_samples_per_scheme(
            s.get("samples_per_scheme"), SAMPLES_PER_SCHEME_DEFAULT,
        )
        mh = list(s.get("must_hold") or [])
        if len(mh) > MUST_HOLD_CAP:
            s["must_hold"] = mh[:MUST_HOLD_CAP]
        if not s.get("property_checks"):
            s["property_checks"] = list(s.get("must_hold") or [])
        mode, reason = seal_construct_mode(s, honor_force=True)
        s["construct_mode"] = mode
        s["mode_sealed_reason"] = reason

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
