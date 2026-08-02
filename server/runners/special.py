"""特殊样例：按选中方案逐个 Plan → SpecialCoder → Fixer（参考 gen，不改 validator）。

支持 construct_mode：
- mutate：普通合法底稿 + 局部替换
- build：从零特殊构造
mutate 修复失败后可降级为 build 重写该方案分支。
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from agent import prompts, tools
from agent.core import run as agent_run
from agent.llm import chat_text
from pipeline.gen_data import special_enabled
from server import job_store
from server.runners.snapshot import restore_good_snapshot, save_good_snapshot
from server.runners.finder import parse_need_finder
from server.special_discover import (
    CONSTRUCT_MODE_BUILD,
    CONSTRUCT_MODE_MUTATE,
    apply_schemes_to_range,
    clamp_samples_per_scheme,
    infer_construct_mode,
    normalize_construct_mode,
    selected_schemes,
)

SPECIAL_PLAN_FILE = "gen_special_plan.md"
SPECIAL_CASES_FILE = "special_cases.json"

SPECIAL_CODER_TOOL_SCHEMAS = [
    s for s in tools.TOOL_SCHEMAS
    if s["function"]["name"] in {
        "read_file", "read_range", "write_special_gen",
        "write_finder", "run_finder", "list_finder_hits",
        "run_gen", "run_validate", "run_std", "run_self_check", "finish",
    }
]
SPECIAL_FIXER_TOOL_SCHEMAS = [
    s for s in tools.TOOL_SCHEMAS
    if s["function"]["name"] in {
        "read_file", "read_range", "write_special_gen",
        "list_finder_hits",
        "run_gen", "run_validate", "run_std", "run_self_check", "finish",
    }
]


def _plan_looks_complete(plan_text: str) -> bool:
    if not plan_text or len(plan_text) < 80:
        return False
    text = plan_text.lower()
    has_sections = all(h in text for h in ("1.", "2.", "3.", "4.", "5.", "6."))
    has_finder = ("need_finder" in text) or ("7." in text)
    return has_sections and has_finder


def _scheme_type(scheme: dict) -> str:
    sid = str(scheme.get("id") or "special").strip()
    return f"special:{sid}"


def _scheme_mode(scheme: dict) -> str:
    if scheme.get("construct_mode"):
        return normalize_construct_mode(scheme.get("construct_mode"))
    return infer_construct_mode(
        title=str(scheme.get("title") or ""),
        why=str(scheme.get("why") or ""),
        hint=str(scheme.get("construct_hint") or ""),
        must_hold=list(scheme.get("must_hold") or []),
    )


def _mode_strategy_blurb(mode: str) -> str:
    if mode == CONSTRUCT_MODE_MUTATE:
        return (
            "construct_mode=mutate：先仿 gen.cpp 造合法底稿（random 或合适 edge 风格），"
            "再局部替换最少字段使 must_hold 成立；禁止整份无关手写输入。"
        )
    return (
        "construct_mode=build：在 gen_special 内从零构造，直接保证 must_hold；"
        "适合强结构，不要退化成无约束 random。"
    )


def _clip_std_for_plan(limit: int = 8000) -> str:
    """尽量把标程塞进 Plan，帮助理解特殊样例。"""
    try:
        base = Path(tools._wd())
    except Exception:
        return ""
    for name in ("std.cpp", "std.py"):
        path = base / name
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        if len(text) > limit:
            text = text[: limit // 2] + "\n/* ... */\n" + text[-(limit // 2) :]
        return f"【标程 {name}】\n```\n{text}\n```\n\n"
    return ""


def _build_scheme_planner_user(
    stmt_plain: str,
    range_plain: str,
    range_json: dict,
    scheme: dict,
    existing_scheme_ids: list[str],
) -> str:
    mode = _scheme_mode(scheme)
    std_block = _clip_std_for_plan()
    understand = str(scheme.get("understanding_summary") or "").strip()
    understand_block = (
        f"【特殊样例理解摘要】\n{understand}\n\n" if understand else ""
    )
    return (
        f"请为特殊方案「{scheme.get('title') or scheme.get('id')}」写一份构造计划"
        f"（将并入 gen_special.cpp）。\n\n"
        f"【题面】\n{stmt_plain}\n\n"
        f"【数据范围】\n{range_plain}\n\n"
        f"{std_block}"
        f"{understand_block}"
        f"【本方案】\n```json\n{json.dumps(scheme, ensure_ascii=False, indent=2)}```\n\n"
        f"【construct_mode】{mode}\n{_mode_strategy_blurb(mode)}\n\n"
        f"【已实现的其他方案 id】{existing_scheme_ids or '（无）'}\n\n"
        f"【range.json 摘要】count={range_json.get('count')} "
        f"special_samples_count={range_json.get('special_samples_count')}\n\n"
        "工作目录已有通过自检的 gen.cpp 与 validator.cpp；请结合标程分支理解本方案。\n"
        "计划必须含 7 节：\n"
        "1. 与 gen.cpp 的 CLI/输出格式契约（--type 将为 special:<id>）\n"
        "2. must_hold 特殊条件拆解\n"
        f"3. 构造策略（必须按 construct_mode={mode} 展开）\n"
        "4. 规模分层（index/count）\n"
        "5. 合法性（现有 validator，禁止改 validator）\n"
        "6. 实现顺序与自检\n"
        "7. Finder 决策：必须含一行 `need_finder: yes` 或 `need_finder: no`；"
        "若 yes 再写 `finder_goal: ...`；"
        f"**mutate 默认 need_finder: no**（当前 mode={mode}）；"
        "Finder 无命中时系统会强制改 build\n"
        "不要规划修改 validator。"
    )


def _build_scheme_coder_task(
    stmt_plain: str,
    range_json: dict,
    scheme: dict,
    plan_name: str,
    is_first: bool,
    finder_hits: list[str] | None = None,
) -> str:
    typ = _scheme_type(scheme)
    mode = _scheme_mode(scheme)
    sid = str(scheme.get("id") or "special")
    per = clamp_samples_per_scheme(scheme.get("samples_per_scheme"))
    action = "写出首版 gen_special.cpp" if is_first else "在现有 gen_special.cpp 上追加本方案分支（保留已有方案）"
    if mode == CONSTRUCT_MODE_MUTATE:
        impl = (
            "本分支实现 mutate：内联「合法底稿 + 局部 patch」。"
            "底稿风格对齐 gen.cpp 的 random/edge；只改 construct_hint 要求的最少字段；"
            "patch 后必须过 validator 且 must_hold 成立。"
        )
    else:
        impl = (
            "本分支实现 build：从零构造，直接保证 must_hold；"
            "可复用 gen.cpp 工具函数风格，禁止无约束 random。"
        )
    hit_block = ""
    if finder_hits:
        hit_block = (
            f"\n【Finder 金样例】已找到 {len(finder_hits)} 组，路径：\n"
            + "\n".join(f"- {p}" for p in finder_hits)
            + f"\n请 list_finder_hits('{sid}') 或 read_file 参考。"
            f"samples_per_scheme={per}（通常 1）：参数化命中即可，禁止退化凑数。\n"
        )
    else:
        hit_block = (
            "\n【Finder 无命中】必须在 gen_special 内做小窗口搜索/构造，直接保证 must_hold。\n"
            "【严禁】输出退化点（如 l=r、r=l+1、`1\\n1 2 3\\n`）或「搜不到就 printf 假数据」。\n"
            "搜不到就换窗口继续搜；单文件 T=1（不要把 count 当成 T）。\n"
        )
        if scheme.get("must_hold_relaxed"):
            hit_block += (
                "【已放宽】must_hold 已去掉次要条款，只保证当前 JSON 中列出的性质。\n"
            )
    return (
        "【角色】SpecialCoder\n"
        f"【目标】{action}，支持 --type {typ}，construct_mode={mode}，"
        f"samples_per_scheme={per}。\n"
        "【约束】只 write_special_gen；禁止改 gen.cpp / validator.cpp；"
        "输出格式/CLI（seed/type/index/count + constraints）与 gen.cpp 一致；"
        "必须保证本方案 must_hold 成立。\n"
        f"{impl}\n"
        f"{hit_block}\n"
        f"【题面摘要】\n{(stmt_plain or '')[:1000]}\n\n"
        f"【本方案】\n```json\n{json.dumps(scheme, ensure_ascii=False, indent=2)}```\n\n"
        f"【计划文件】{plan_name}\n"
        f"【range.json】special_samples_count={range_json.get('special_samples_count')}\n\n"
        "动作：\n"
        f"1. read_file('{plan_name}') 与 read_file('gen.cpp')"
        + ("；若已有 gen_special.cpp 也要 read_file。" if not is_first else "")
        + (f"；若有 finder hits 则 list_finder_hits('{sid}')。" if finder_hits else "")
        + "\n"
        "2. 立刻 write_special_gen（完整源码），不要连续空读；"
        f"本方案分支 type==\"{typ}\"；construct_mode={mode}；每文件通常 T=1。\n"
        f"3. run_gen(type='{typ}') → run_validate → run_std；确认非退化后再 finish。\n"
        "4. run_self_check() 通过后 finish。"
    )


def _build_scheme_fixer_task(
    scheme: dict,
    self_check_result: str,
    attempt: int,
    max_attempts: int,
    force_build: bool = False,
    decision: dict | None = None,
) -> str:
    typ = _scheme_type(scheme)
    mode = CONSTRUCT_MODE_BUILD if force_build else _scheme_mode(scheme)
    err = (self_check_result or "")[:1500]
    sid = str(scheme.get("id") or "special")
    decision_block = ""
    if decision:
        decision_block = (
            f"\n【Coder 阶段决策记忆（special_findings/{sid}/decision.json）】\n"
            f"```json\n{json.dumps(decision, ensure_ascii=False, indent=2)}\n```\n"
            "修复前务必 read_file 该 decision.json 了解是否已跑过 Finder、是否命中、是否已强制 build。\n"
        )
    extra = decision_block
    if force_build:
        extra = (
            "\n【降级】原 construct_mode=mutate 已失败，请将本方案分支改为 build："
            "从零构造，不再依赖局部 patch；保留其他方案分支。\n"
        )
    elif mode == CONSTRUCT_MODE_MUTATE and attempt >= max_attempts:
        extra = (
            "\n【提示】若局部 patch 仍无法同时满足合法性与 must_hold，"
            "可在本轮直接改为 build 从零构造。\n"
        )
    if any(k in err for k in ("平凡", "退化", "空集合", "端点重合")):
        extra += (
            "\n【退化输出】当前生成了空集合/平凡边界凑数。"
            "禁止再 read_file 空转：第一步必须 write_special_gen，"
            "内嵌搜索/构造保证 must_hold。\n"
        )
    if "must_hold" in err and any(
        k in err for k in ("l>n", "区间长度不足", "l > n", "无法解析")
    ):
        extra += (
            "\n【注意】若 run_gen 已稳定输出与 finder hit 相同的合法金样例，"
            "且 validate/std 均 OK，则优先 list_finder_hits 核对后 finish；"
            "禁止连续多轮只 read_file 不写代码。真正缺性质时再 write_special_gen。\n"
        )
    return (
        "【角色】Special Fixer\n"
        f"【目标】修复 gen_special.cpp 中方案 {scheme.get('id')}（type={typ}，mode={mode}）。\n"
        "【约束】只 write_special_gen；禁止改 gen.cpp / validator.cpp；保留其他方案分支。\n"
        "禁止连续 ≥2 次只读不写；首步应 read_file decision.json 并 write_special_gen 或 run_gen 取证。\n"
        f"{extra}\n"
        f"【must_hold】{scheme.get('must_hold')}\n\n"
        f"【自检失败】\n```\n{err}\n```\n\n"
        f"第 {attempt}/{max_attempts} 轮。先读决策记忆，再 write_special_gen，再 run_gen/validate/self_check，通过则 finish。"
    )


def _write_special_decision(
    job_dir: Path,
    scheme: dict,
    need_finder: bool,
    finder_goal: str,
    finder_ran: bool,
    finder_hits: list[str],
    mode: str,
    must_hold_relaxed: bool,
    coder_summary: str = "",
) -> None:
    """把本方案 Finder/Coder 的决策落盘，供 Fixer 与后续排查读取。"""
    sid = str(scheme.get("id") or "special")
    fdir = tools.finder_dir(sid)
    fdir.mkdir(parents=True, exist_ok=True)
    decision = {
        "scheme_id": sid,
        "need_finder": need_finder,
        "finder_goal": finder_goal,
        "finder_ran": finder_ran,
        "hits": finder_hits,
        "construct_mode": mode,
        "must_hold_relaxed": must_hold_relaxed,
        "must_hold": list(scheme.get("must_hold") or []),
        "must_hold_full": list(scheme.get("must_hold_full") or []),
        "coder_summary": coder_summary,
    }
    (fdir / "decision.json").write_text(
        json.dumps(decision, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _read_special_decision(job_dir: Path, scheme: dict) -> dict:
    """读取本方案已落盘的决策记忆；不存在返回空 dict。"""
    sid = str(scheme.get("id") or "special")
    fdir = tools.finder_dir(sid)
    path = fdir / "decision.json"
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _filter_finder_hits(
    job: job_store.Job,
    job_dir: Path,
    scheme: dict,
    hits: list[str],
) -> list[str]:
    """用廉价启发式剔除明显退化假命中（不声称验过 must_hold），并同步 meta。"""
    sid = str(scheme.get("id") or "special")
    if not hits:
        return []
    kept: list[str] = []
    dropped: list[str] = []
    for rel in hits:
        p = job_dir / rel
        try:
            text = p.read_text(encoding="utf-8") if p.is_file() else ""
        except OSError:
            text = ""
        mh = _reject_degenerate_input(
            text, list(scheme.get("must_hold") or []), str(scheme.get("title") or sid),
        )
        if isinstance(mh, str) and mh.startswith("OK"):
            kept.append(rel)
        else:
            dropped.append(f"{rel}: {(mh or '')[:120]}")
    if dropped:
        job_store.add_progress(
            job,
            f"【Finder】{sid} 剔除 {len(dropped)} 组明显退化命中（启发式，未验 must_hold）: "
            + "; ".join(dropped)[:400],
        )
    try:
        fdir = tools.finder_dir(sid)
        meta_path = fdir / "meta.json"
        if meta_path.is_file():
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            meta["hits"] = kept
            meta["dropped_false_hits"] = dropped
            meta["status"] = "ok" if kept else "none"
            meta_path.write_text(
                json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8",
            )
            hits_dir = fdir / "hits"
            if hits_dir.is_dir():
                keep_names = {Path(h).name for h in kept}
                for old in hits_dir.glob("*.in"):
                    if old.name not in keep_names:
                        try:
                            old.unlink()
                        except OSError:
                            pass
    except Exception as e:
        job_store.add_progress(
            job, f"【Finder】{sid} 更新假命中 meta 失败: {type(e).__name__}",
        )
    return kept


def _reset_finder_meta_before_coder(scheme: dict) -> None:
    """Coder 阶段开始前清空旧 meta 的运行状态，避免读取到上轮的 status/hits。"""
    sid = str(scheme.get("id") or "special")
    fdir = tools.finder_dir(sid)
    meta_path = fdir / "meta.json"
    if not meta_path.is_file():
        return
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return
    for key in ("status", "rc", "hits", "validated", "stderr_tail", "seed", "max_hits", "timeout_sec", "memory_limit_mb"):
        meta.pop(key, None)
    try:
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def _read_finder_hits_from_meta(job_dir: Path, scheme: dict) -> tuple[bool, list[str]]:
    """读取 meta.json 返回 (ran, hits)。未运行过返回 (False, [])。"""
    sid = str(scheme.get("id") or "special")
    fdir = tools.finder_dir(sid)
    meta_path = fdir / "meta.json"
    if not meta_path.is_file():
        return False, []
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False, []
    # 只有 run_finder 才会写入 status/rc；write_finder 只写入 compiled
    ran = bool(meta.get("status") or meta.get("rc") is not None)
    return ran, list(meta.get("hits") or [])


def _shrink_samples_if_few_hits(
    job: job_store.Job,
    job_dir: Path,
    range_json: dict,
    scheme: dict,
    finder_hits: list[str],
) -> int:
    """Finder 命中不足目标组数时，按命中数减少 samples_per_scheme（mutate/build 均适用）。

    返回调整后的 samples_per_scheme（至少 1；无命中则不改）。
    """
    try:
        target = max(1, int(scheme.get("samples_per_scheme") or 1))
    except (TypeError, ValueError):
        target = 1
    n_hits = len(finder_hits or [])
    if n_hits <= 0 or n_hits >= target:
        return target

    sid = str(scheme.get("id") or "special")
    mode = _scheme_mode(scheme)
    scheme["samples_per_scheme"] = n_hits
    scheme["samples_shrunk_reason"] = f"{mode}_finder_hits={n_hits}<{target}"

    schemes = list(range_json.get("special_schemes") or [])
    for s in schemes:
        if str(s.get("id") or "") == sid:
            s["samples_per_scheme"] = n_hits
            s["samples_shrunk_reason"] = scheme["samples_shrunk_reason"]
            break
    else:
        if schemes:
            schemes[0]["samples_per_scheme"] = n_hits
        else:
            schemes = [scheme]
    old_total = int(range_json.get("count") or 15)
    old_special = int(range_json.get("special_samples_count") or 0)
    regular = max(1, old_total - old_special) if old_special else max(1, old_total - target)
    updated = apply_schemes_to_range(
        range_json, schemes,
        user_hint=str(range_json.get("special_samples_desc") or ""),
        regular_count=regular,
    )
    range_json.clear()
    range_json.update(updated)
    (job_dir / "range.json").write_text(
        json.dumps(range_json, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tools.set_context(str(job_dir), range_json.get("std_cmd", ""))
    job_store.add_progress(
        job,
        f"【Special】{sid} ({mode}) 仅找到 {n_hits} 组有效金样例，"
        f"samples_per_scheme {target} → {n_hits}（special_count="
        f"{range_json.get('special_samples_count')}）",
    )
    return n_hits


def _looks_existence_must_hold(must_hold: list[str]) -> bool:
    """存在性/搜索类性质：倾向强制 Finder，避免臆造参数。"""
    blob = " ".join(must_hold or [])
    keys = ("不存在", "存在", "区间", "之间", "范围内", "之内", "开区间", "闭区间")
    return any(k in blob for k in keys)


# 整份输入归一化后命中这些，视为明显凑数（不声称验过 must_hold）
_DEGENERATE_INPUT_EXACT = frozenset({
    "1", "0", "1 1", "1 2", "1 2 3", "1 1 1", "0 0 0", "0 1",
    "1\n2 3", "1\n1 1", "1\n1 2",
})


def _reject_degenerate_input(
    inp: str,
    must_hold: list[str] | None = None,
    scheme_title: str = "",
) -> str:
    """极便宜启发式：只拦明显假/退化输入，不验证 must_hold 是否真正成立。

    通过 → OK: not_obviously_degenerate（明确不声称性质已验）
    失败 → ERROR: ...
    """
    del scheme_title  # 保留签名兼容，启发式不依赖标题
    text = (inp or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return "ERROR: 输入为空（退化）"
    if len(text) < 3:
        return "ERROR: 输入过短（退化）"

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return "ERROR: 输入无有效行（退化）"

    compact = re.sub(r"[ \t]+", " ", text)
    compact_ws = re.sub(r"\s+", " ", text).strip()
    if compact in _DEGENERATE_INPUT_EXACT or compact_ws in _DEGENERATE_INPUT_EXACT:
        return f"ERROR: 疑似凑数退化输入: {compact_ws}"

    nums = [int(x) for x in re.findall(r"-?\d+", text)]
    if not nums:
        return "ERROR: 输入无整数（退化）"

    existence = bool(must_hold) and _looks_existence_must_hold(must_hold)

    # 极短整数序列（1～3 个数）：端点/凑数检查；长测例不碰，避免误伤图/数组题
    if len(nums) == 1:
        return f"ERROR: 仅单个整数（退化）: {nums[0]}"
    if len(nums) == 2:
        a, b = nums[0], nums[1]
        if a > b:
            return f"ERROR: 疑似区间退化 l>r ({a}>{b})"
        if a == b:
            return f"ERROR: 疑似区间退化 l==r ({a})"
        if b - a <= 1:
            return f"ERROR: 疑似空/平凡区间 l,r=({a},{b})"
    if len(nums) == 3:
        a, b, c = nums[0], nums[1], nums[2]
        if (a, b, c) == (1, 2, 3):
            return "ERROR: 疑似凑数退化输入: 1 2 3"
        if a > b:
            return f"ERROR: 疑似区间退化 l>r ({a}>{b})"
        if a == b:
            return f"ERROR: 疑似区间退化 l==r ({a})"
        if existence and b - a <= 1:
            return f"ERROR: 疑似空开区间凑数 l,r=({a},{b})"

    # T=1 + l r [n]：仅当总整数 3～4 且为存在性类（避免「T=1 / n / 数组」误判）
    if existence and nums[0] == 1 and len(nums) in (3, 4):
        l, r = nums[1], nums[2]
        if l > r:
            return f"ERROR: 疑似区间退化（T=1 后）l>r ({l}>{r})"
        if l == r:
            return f"ERROR: 疑似区间退化（T=1 后）l==r ({l})"
        if r - l <= 1:
            return f"ERROR: 疑似空开区间凑数（T=1 后）l,r=({l},{r})"

    return "OK: not_obviously_degenerate"


def _check_scheme(
    scheme: dict,
    sid: str,
    stype: str,
    scheme_index: int,
    range_json: dict,
    seed: int,
) -> str:
    """抽查本方案：gen → validate → std → 退化启发式 → special_only 快检。

    退化启发式只拦明显假输入，不声称 must_hold 已验证。
    """
    # 用方案内小 index，避免 count//2 对「仅少量特殊变体」的 gen_special 抽到怪档位
    try:
        per = max(1, int(scheme.get("samples_per_scheme") or 1))
    except (TypeError, ValueError):
        per = 1
    idx = int(scheme_index) % per
    gen_out = tools.run_gen(
        seed=seed, type=stype, index=idx, count=int(range_json.get("count") or 15),
    )
    if isinstance(gen_out, str) and gen_out.startswith("ERROR"):
        return gen_out
    val = tools.run_validate(gen_out if isinstance(gen_out, str) else "")
    std = tools.run_std(gen_out if isinstance(gen_out, str) else "")
    deg = _reject_degenerate_input(
        gen_out if isinstance(gen_out, str) else "",
        list(scheme.get("must_hold") or []),
        str(scheme.get("title") or sid),
    )
    if (isinstance(val, str) and val.startswith("ERROR")) or (
        isinstance(std, str) and std.startswith("ERROR")
    ) or (isinstance(deg, str) and deg.startswith("ERROR")):
        return (
            f"ERROR: scheme {sid}\nvalidate={val}\nstd={std}\n"
            f"degenerate_check={deg}"
        )
    return tools.run_self_check(fast_mode=True, special_only=True)


def _run_one_scheme(
    job: job_store.Job,
    job_dir: Path,
    range_json: dict,
    typ: str,
    on_event,
    stmt_plain: str,
    range_plain: str,
    scheme: dict,
    scheme_index: int,
    total_schemes: int,
    existing_ids: list[str],
) -> None:
    sid = scheme.get("id") or f"scheme_{scheme_index}"
    stype = _scheme_type(scheme)
    mode = _scheme_mode(scheme)
    scheme["construct_mode"] = mode
    sc_args = {"special_only": True}
    plan_name = f"gen_special_plan_{sid}.md"
    is_first = scheme_index == 0

    job_store.add_progress(
        job,
        f"【Special {scheme_index + 1}/{total_schemes}】方案 {sid}（{scheme.get('title')}）"
        f" mode={mode} Plan",
    )
    plan_path = job_dir / plan_name
    user_prompt = _build_scheme_planner_user(
        stmt_plain, range_plain, range_json, scheme, existing_ids,
    )
    try:
        plan_text = chat_text(
            prompts.build_special_planner_prompt(),
            user_prompt,
            temperature=0.3,
        )
    except Exception as e:
        plan_text = ""
        job_store.add_progress(job, f"Plan 失败: {type(e).__name__}: {e}")

    if not _plan_looks_complete(plan_text):
        try:
            plan_text = chat_text(
                prompts.build_special_planner_prompt(),
                user_prompt + "\n\n请严格按 7 个小节重写，每节至少 2 条要点。"
                f"第 3 节必须按 construct_mode={mode} 写。"
                "第 7 节必须含 `need_finder: yes` 或 `need_finder: no`。",
                temperature=0.3,
            )
        except Exception:
            pass

    if plan_text.strip():
        plan_path.write_text(plan_text, encoding="utf-8")
        # 同步最新方案计划到总览文件
        overview = job_dir / SPECIAL_PLAN_FILE
        prev = overview.read_text(encoding="utf-8") if overview.is_file() else ""
        overview.write_text(
            prev + ("" if not prev else "\n\n---\n\n")
            + f"# Scheme {sid} (mode={mode})\n\n" + plan_text,
            encoding="utf-8",
        )

    need_finder, finder_goal = parse_need_finder(plan_text)
    must_hold = list(scheme.get("must_hold") or [])
    # 存在性类 must_hold：倾向强制 Finder，避免臆造参数跳过搜索
    force_finder = _looks_existence_must_hold(must_hold)
    if force_finder and not need_finder:
        need_finder = True
        if not finder_goal:
            finder_goal = "; ".join(must_hold)
        job_store.add_progress(
            job,
            f"【Finder】方案 {sid} 含存在性 must_hold，强制 need_finder=yes"
            f"（Coder 阶段必须执行一轮 Finder）",
        )
    elif mode == CONSTRUCT_MODE_MUTATE and not need_finder:
        job_store.add_progress(
            job, f"【Finder】方案 {sid} mode=mutate 且 need_finder=no，Coder 不跑 Finder",
        )

    scheme["need_finder"] = bool(need_finder)
    if finder_goal:
        scheme["finder_goal"] = finder_goal

    # 先写入初始决策记忆（ran=False），供 Coder 与 Fixer 参考
    _write_special_decision(
        job_dir, scheme, need_finder, finder_goal or "",
        finder_ran=False, finder_hits=[], mode=mode,
        must_hold_relaxed=bool(scheme.get("must_hold_relaxed")),
    )
    # 清空旧 Finder 运行状态，避免本轮误判
    _reset_finder_meta_before_coder(scheme)

    # SpecialCoder 内按需调用 Finder，但系统硬限制为一轮
    job_store.add_progress(job, f"【SpecialCoder】实现方案 {sid}（{mode}）")
    coder_task = _build_scheme_coder_task(
        stmt_plain, range_json, scheme, plan_name, is_first=is_first,
        finder_hits=[],  # Coder 自己跑 Finder 后读 list_finder_hits
    )
    # 计划需要 Finder 或存在性强制：Coder 可能 write/run finder；其余情况禁掉 Finder 工具更干净
    coder_schemas = list(SPECIAL_CODER_TOOL_SCHEMAS)
    coder_tool_limits: dict[str, int] | None = {"write_finder": 2, "run_finder": 1}
    if not need_finder:
        coder_schemas = [s for s in coder_schemas if s["function"]["name"] not in ("write_finder", "run_finder")]
        coder_tool_limits = None
        job_store.add_progress(job, f"【SpecialCoder】{sid} plan 无需 Finder，工具集已移除 Finder")

    coder_max_steps = 12 if need_finder else 10
    coder_summary = agent_run(
        coder_task,
        max_steps=coder_max_steps,
        verbose=False,
        on_event=on_event,
        system_prompt=prompts.build_special_coder_prompt(typ),
        tool_schemas=coder_schemas,
        tool_limits=coder_tool_limits,
        write_check_discipline=True,
        self_check_fast=True,
        self_check_args=sc_args,
    )
    job_store.add_progress(job, f"SpecialCoder({sid}) 结束: {coder_summary}")

    # Coder 阶段结束后，读取 Finder 实际运行结果与命中
    finder_ran, raw_hits = _read_finder_hits_from_meta(job_dir, scheme)
    finder_hits = _filter_finder_hits(job, job_dir, scheme, raw_hits)
    if finder_ran:
        job_store.add_progress(
            job,
            f"【Finder】{sid} Coder 内执行结果: ran={finder_ran}, "
            f"raw_hits={len(raw_hits)}, kept={len(finder_hits)}",
        )

    # Finder 跑过但无命中 → 强制 build；多条款 must_hold 放宽为前 2 条
    must_hold_relaxed = bool(scheme.get("must_hold_relaxed"))
    if finder_ran and not finder_hits:
        if mode != CONSTRUCT_MODE_BUILD:
            job_store.add_progress(
                job,
                f"【Special】{sid} Finder 无命中，construct_mode {mode} → build",
            )
            mode = CONSTRUCT_MODE_BUILD
            scheme["construct_mode"] = CONSTRUCT_MODE_BUILD
            scheme["forced_build_reason"] = "finder_no_hits"
        mh = list(scheme.get("must_hold") or [])
        if len(mh) > 2 and not must_hold_relaxed:
            scheme["must_hold_full"] = list(mh)
            scheme["must_hold"] = mh[:2]
            scheme["must_hold_relaxed"] = True
            must_hold_relaxed = True
            job_store.add_progress(
                job,
                f"【Special】{sid} Finder 无命中，must_hold {len(mh)} 条 → 保留前 2 条核心性质",
            )

    # Finder 命中不足目标组数 → 减少组数，禁止重复凑数
    if finder_hits:
        _shrink_samples_if_few_hits(
            job, job_dir, range_json, scheme, finder_hits,
        )

    # 产品：每方案特殊样例压到上限（默认 1），并写回 range.json
    scheme["samples_per_scheme"] = clamp_samples_per_scheme(scheme.get("samples_per_scheme"))
    schemes_all = list(range_json.get("special_schemes") or [])
    if schemes_all:
        for s in schemes_all:
            if str(s.get("id") or "") == sid:
                s["samples_per_scheme"] = scheme["samples_per_scheme"]
                if scheme.get("must_hold_relaxed"):
                    s["must_hold"] = list(scheme.get("must_hold") or [])
                    s["must_hold_relaxed"] = True
                if scheme.get("forced_build_reason"):
                    s["forced_build_reason"] = scheme["forced_build_reason"]
                break
    else:
        schemes_all = [scheme]
    old_total = int(range_json.get("count") or 15)
    old_special = int(range_json.get("special_samples_count") or 0)
    regular = max(1, old_total - old_special) if old_special else max(1, old_total - 1)
    updated = apply_schemes_to_range(
        range_json, schemes_all,
        user_hint=str(range_json.get("special_samples_desc") or ""),
        regular_count=regular,
    )
    range_json.clear()
    range_json.update(updated)
    (job_dir / "range.json").write_text(
        json.dumps(range_json, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    tools.set_context(str(job_dir), range_json.get("std_cmd", ""))

    # 更新决策记忆，Fixer 读取
    _write_special_decision(
        job_dir, scheme, need_finder, finder_goal or "",
        finder_ran=finder_ran, finder_hits=finder_hits, mode=mode,
        must_hold_relaxed=must_hold_relaxed,
        coder_summary=str(coder_summary)[:500],
    )

    self_check_result = _check_scheme(
        scheme, sid, stype, scheme_index, range_json, seed=2500 + scheme_index,
    )
    job_store.add_progress(job, f"方案 {sid} 检查: {str(self_check_result)[:400]}")

    if isinstance(self_check_result, str) and self_check_result.startswith("OK"):
        return

    save_good_snapshot(job_dir, include_in_out=False, suffix=".fixer_bak")
    max_fixer = 2
    for attempt in range(1, max_fixer + 1):
        # mutate：第 2 轮强制降级为 build
        force_build = (
            mode == CONSTRUCT_MODE_MUTATE and attempt >= max_fixer
        )
        if force_build:
            scheme["construct_mode"] = CONSTRUCT_MODE_BUILD
            job_store.add_progress(
                job, f"【Special】{sid} mutate 失败，降级为 build 重写",
            )
        job_store.add_progress(
            job,
            f"【Special Fixer】{sid} 第 {attempt}/{max_fixer}"
            + ("（build 降级）" if force_build else f"（{mode}）"),
        )
        fixer_summary = agent_run(
            _build_scheme_fixer_task(
                scheme, self_check_result, attempt, max_fixer, force_build=force_build,
                decision=_read_special_decision(job_dir, scheme),
            ),
            max_steps=8,
            verbose=False,
            on_event=on_event,
            system_prompt=prompts.build_special_fixer_prompt(typ),
            tool_schemas=SPECIAL_FIXER_TOOL_SCHEMAS,
            write_check_discipline=True,
            self_check_fast=True,
            self_check_args=sc_args,
        )
        job_store.add_progress(job, f"Fixer({sid}) 结束: {fixer_summary}")
        self_check_result = _check_scheme(
            scheme, sid, stype, scheme_index, range_json, seed=2600 + attempt,
        )
        if isinstance(self_check_result, str) and self_check_result.startswith("OK"):
            return

    restore_good_snapshot(job_dir, suffix=".fixer_bak")
    # 若曾降级，写回原 mode 避免污染 range（实现失败以报错为准）
    scheme["construct_mode"] = mode
    raise RuntimeError(f"方案 {sid} 未通过: {str(self_check_result)[:500]}")


def run_special_agent(
    job: job_store.Job,
    job_dir: Path,
    range_json: dict,
    typ: str,
    on_event,
    stmt_plain: str = "",
    range_plain: str = "",
    special_samples_desc: str = "",
    special_samples_count: int = 1,
) -> str:
    """常规数据之后：按选中方案逐个 Plan+Execute。"""
    if not special_enabled(range_json) and not (special_samples_desc or "").strip():
        return "未启用特殊样例，跳过 SpecialCoder"

    schemes = selected_schemes(range_json.get("special_schemes"))
    if not schemes:
        # 兼容旧：无 schemes 时退化为单方案
        desc = (special_samples_desc or range_json.get("special_samples_desc") or "").strip()
        if not desc:
            return "无选中特殊方案，跳过 SpecialCoder"
        from server.special_discover import fallback_user_scheme
        schemes = fallback_user_scheme(
            desc,
            clamp_samples_per_scheme(
                special_samples_count or range_json.get("special_samples_count") or 1
            ),
        )

    # 入口即压组数：旧题库/GUI 残留的 5 → 1
    for s in schemes:
        s["samples_per_scheme"] = clamp_samples_per_scheme(s.get("samples_per_scheme"))
    old_total = int(range_json.get("count") or 15)
    old_special = int(range_json.get("special_samples_count") or 0)
    regular = max(1, old_total - old_special) if old_special else max(1, old_total - 1)
    updated = apply_schemes_to_range(
        range_json, list(range_json.get("special_schemes") or schemes),
        user_hint=str(special_samples_desc or range_json.get("special_samples_desc") or ""),
        regular_count=regular,
    )
    range_json.clear()
    range_json.update(updated)
    (job_dir / "range.json").write_text(
        json.dumps(range_json, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    schemes = selected_schemes(range_json.get("special_schemes")) or schemes
    if int(range_json.get("special_samples_count") or 0) != old_special:
        job_store.add_progress(
            job,
            f"【Special】样例组数已规范：special_samples_count "
            f"{old_special} → {range_json.get('special_samples_count')}",
        )

    tools.set_context(str(job_dir), range_json.get("std_cmd", ""))
    (job_dir / SPECIAL_CASES_FILE).write_text(
        json.dumps(
            {
                "user_hint": special_samples_desc or range_json.get("special_samples_desc") or "",
                "schemes": schemes,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    existing_ids: list[str] = []
    ok_ids: list[str] = []
    failed: list[str] = []
    for i, scheme in enumerate(schemes):
        sid = str(scheme.get("id") or f"scheme_{i}")
        try:
            _run_one_scheme(
                job, job_dir, range_json, typ, on_event,
                stmt_plain, range_plain, scheme, i, len(schemes), existing_ids,
            )
            ok_ids.append(sid)
            existing_ids.append(sid)
        except Exception as e:
            failed.append(f"{sid}: {type(e).__name__}: {e}")
            job_store.add_progress(
                job,
                f"【Special】方案 {sid} 失败，继续其余方案: {type(e).__name__}: {e}",
            )

    if not ok_ids:
        raise RuntimeError(
            "全部特殊方案均失败:\n" + "\n".join(f"  - {x}" for x in failed[:6])
        )

    # 全量 special_only 完整自检（写入特殊组）
    job_store.add_progress(job, "【Special】已成功方案后跑完整 special_only 自检")
    full = tools.run_self_check(fast_mode=False, special_only=True)
    job_store.add_progress(job, f"Special 完整自检: {full[:500]}")
    if not (isinstance(full, str) and full.startswith("OK")):
        # 部分方案可用时不整单炸掉：交给 batch 决定是否跳过特殊批量
        raise RuntimeError(
            f"Special 完整自检失败（已成功方案: {ok_ids}）: {full[:500]}"
        )

    save_good_snapshot(job_dir, include_in_out=False)
    modes = ", ".join(
        f"{s.get('id')}={_scheme_mode(s)}" for s in schemes if str(s.get("id") or "") in ok_ids
    )
    msg = f"SpecialCoder 完成 {len(ok_ids)}/{len(schemes)} 个方案（{modes}）"
    if failed:
        msg += f"；失败 {len(failed)} 个"
        job_store.add_progress(job, "失败方案:\n" + "\n".join(f"  - {x}" for x in failed[:6]))
    job_store.add_progress(job, msg)
    return msg
