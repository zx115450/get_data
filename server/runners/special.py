"""特殊样例：模板 Plan → SpecialCoder（gen_special + check_special）→ Fixer。

construct_mode:
- mutate：普通合法底稿 + 局部替换（窄门，由 discover 规则盖章）
- build：从零特殊构造
mutate 修复失败后可降级为 build 再修一轮。
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from agent import prompts, tools
from agent.core import run as agent_run
from pipeline.gen_data import special_enabled
from server import job_store
from server.runners.snapshot import restore_good_snapshot, save_good_snapshot
from server.special_discover import (
    CONSTRUCT_MODE_BUILD,
    CONSTRUCT_MODE_MUTATE,
    apply_schemes_to_range,
    clamp_samples_per_scheme,
    infer_construct_mode,
    normalize_construct_mode,
    seal_construct_mode,
    selected_schemes,
)

SPECIAL_PLAN_FILE = "gen_special_plan.md"
SPECIAL_CASES_FILE = "special_cases.json"

SPECIAL_CODER_TOOL_SCHEMAS = [
    s for s in tools.TOOL_SCHEMAS
    if s["function"]["name"] in {
        "read_file", "read_range", "write_special_gen", "write_special_check",
        "run_gen", "run_validate", "run_property_check", "run_std",
        "run_self_check", "finish",
    }
]
SPECIAL_FIXER_TOOL_SCHEMAS = [
    s for s in tools.TOOL_SCHEMAS
    if s["function"]["name"] in {
        "read_file", "read_range", "write_special_gen", "write_special_check",
        "run_gen", "run_validate", "run_property_check", "run_std",
        "run_self_check", "finish",
    }
]


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
            "再局部替换 construct_hint 中列出的最少字段；禁止整份无关手写输入。"
        )
    return (
        "construct_mode=build：在 gen_special 内从零构造，直接保证 must_hold；"
        "适合强结构，不要退化成无约束 random。"
    )


def _property_checks(scheme: dict) -> list[str]:
    raw = scheme.get("property_checks")
    if isinstance(raw, str) and raw.strip():
        return [raw.strip()]
    if isinstance(raw, list):
        out = [str(x).strip() for x in raw if str(x).strip()]
        if out:
            return out
    return [str(x).strip() for x in (scheme.get("must_hold") or []) if str(x).strip()]


def _write_template_plan(job_dir: Path, scheme: dict, plan_name: str) -> str:
    """用 scheme 字段拼短计划（0 次 LLM）。"""
    sid = str(scheme.get("id") or "special")
    mode = _scheme_mode(scheme)
    stype = _scheme_type(scheme)
    must = list(scheme.get("must_hold") or [])
    props = _property_checks(scheme)
    patch = list(scheme.get("mutate_fields") or [])
    lines = [
        f"# Special Plan (template) `{sid}`",
        "",
        "## 1. CLI / 格式契约",
        f"- `--type={stype}`；CLI 与 gen.cpp 一致：`--seed/--type/--index/--count`",
        "- stdout 格式与 gen.cpp 完全一致；constraints 注册方式对齐 gen.cpp",
        "- 禁止修改 validator.cpp",
        "",
        "## 2. must_hold",
    ]
    if must:
        for i, m in enumerate(must, 1):
            lines.append(f"{i}. {m}")
    else:
        lines.append("（无）")
    lines += ["", "## 3. property_checks（写入 check_special）"]
    if props:
        for i, p in enumerate(props, 1):
            lines.append(f"{i}. {p}")
    else:
        lines.append("与 must_hold 相同")
    lines += [
        "",
        f"## 4. construct_mode={mode}",
        _mode_strategy_blurb(mode),
    ]
    if mode == CONSTRUCT_MODE_MUTATE and patch:
        lines.append("拟改字段：" + ", ".join(str(x) for x in patch))
    if scheme.get("mode_sealed_reason"):
        lines.append(f"mode 盖章：{scheme.get('mode_sealed_reason')}")
    lines += [
        "",
        "## 5. construct_hint",
        str(scheme.get("construct_hint") or "（无）"),
        "",
        "## 6. 自检顺序",
        "run_gen → run_validate → run_property_check → run_std → run_self_check(special_only)",
        "- check_special：stdin 读入，must_hold 成立 exit 0，否则 exit 1 + stderr 原因",
        "- 禁止空区间/端点重合等平凡真",
    ]
    text = "\n".join(lines) + "\n"
    (job_dir / plan_name).write_text(text, encoding="utf-8")
    overview = job_dir / SPECIAL_PLAN_FILE
    prev = overview.read_text(encoding="utf-8") if overview.is_file() else ""
    overview.write_text(
        prev + ("" if not prev else "\n\n---\n\n")
        + f"# Scheme {sid} (mode={mode})\n\n" + text,
        encoding="utf-8",
    )
    return text


def _build_scheme_coder_task(
    stmt_plain: str,
    range_json: dict,
    scheme: dict,
    plan_name: str,
    is_first: bool,
) -> str:
    typ = _scheme_type(scheme)
    mode = _scheme_mode(scheme)
    per = clamp_samples_per_scheme(scheme.get("samples_per_scheme"))
    props = _property_checks(scheme)
    action = (
        "写出首版 gen_special.cpp + check_special.cpp"
        if is_first
        else "在现有 gen_special.cpp 上追加本方案分支（保留已有），并更新 check_special"
    )
    if mode == CONSTRUCT_MODE_MUTATE:
        impl = (
            "本分支实现 mutate：内联「合法底稿 + 局部 patch」。"
            "底稿风格对齐 gen.cpp 的 random/edge；只改 construct_hint / mutate_fields 要求的最少字段；"
            "patch 后必须过 validator、property_check 且 must_hold 成立。"
        )
    else:
        impl = (
            "本分支实现 build：从零构造，直接保证 must_hold；"
            "可复用 gen.cpp 工具函数风格，禁止无约束 random。"
        )
    return (
        "【角色】SpecialCoder\n"
        f"【目标】{action}，支持 --type {typ}，construct_mode={mode}，"
        f"samples_per_scheme={per}。\n"
        "【约束】只 write_special_gen / write_special_check；禁止改 gen.cpp / validator.cpp；"
        "输出格式/CLI 与 gen.cpp 一致；必须保证 must_hold，并由 check_special 判定。\n"
        f"{impl}\n"
        "【禁止平凡退化】空集合、端点重合、长度 0/1 让性质平凡成立 → property_check 必须 FAIL。\n"
        f"【题面摘要】\n{(stmt_plain or '')[:1000]}\n\n"
        f"【本方案】\n```json\n{json.dumps(scheme, ensure_ascii=False, indent=2)}```\n\n"
        f"【property_checks】{props}\n"
        f"【计划文件】{plan_name}\n"
        f"【range.json】special_samples_count={range_json.get('special_samples_count')}\n\n"
        "动作：\n"
        f"1. read_file('{plan_name}') 与 read_file('gen.cpp')"
        + ("；若已有 gen_special.cpp / check_special.cpp 也要读。" if not is_first else "")
        + "\n"
        f"2. write_special_gen（完整源码，本方案分支 type==\"{typ}\"）；"
        "同时 write_special_check（按 property_checks 判定，exit 0/1）。\n"
        f"3. run_gen(type='{typ}') → run_validate → run_property_check → run_std。\n"
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
            f"\n【Coder 决策记忆（special_meta/{sid}/decision.json）】\n"
            f"```json\n{json.dumps(decision, ensure_ascii=False, indent=2)}\n```\n"
        )
    extra = decision_block
    if force_build:
        extra += (
            "\n【降级】原 construct_mode=mutate 已失败，请将本方案分支改为 build："
            "从零构造，不再依赖局部 patch；同步更新 check_special；保留其他方案分支。\n"
        )
    if "property_check" in err:
        extra += (
            "\n【property_check 失败】优先加强构造使 must_hold 真正成立；"
            "若断言写错也可修 check_special；禁止放宽成空区间平凡真。\n"
        )
    if any(k in err for k in ("平凡", "退化", "空集合", "端点重合")):
        extra += (
            "\n【退化输出】禁止再 read_file 空转：第一步必须 write_special_gen / write_special_check。\n"
        )
    return (
        "【角色】Special Fixer\n"
        f"【目标】修复 gen_special.cpp / check_special.cpp 中方案 "
        f"{scheme.get('id')}（type={typ}，mode={mode}）。\n"
        "【约束】只 write_special_gen / write_special_check；禁止改 gen.cpp / validator.cpp。\n"
        f"{extra}\n"
        f"【must_hold】{scheme.get('must_hold')}\n"
        f"【property_checks】{_property_checks(scheme)}\n\n"
        f"【自检失败】\n```\n{err}\n```\n\n"
        f"第 {attempt}/{max_attempts} 轮。"
        "写完后 run_gen → validate → property_check → self_check，通过则 finish。"
    )


def _write_special_decision(
    job_dir: Path,
    scheme: dict,
    mode: str,
    coder_summary: str = "",
) -> None:
    sid = str(scheme.get("id") or "special")
    fdir = tools.special_meta_dir(sid)
    fdir.mkdir(parents=True, exist_ok=True)
    decision = {
        "scheme_id": sid,
        "construct_mode": mode,
        "must_hold": list(scheme.get("must_hold") or []),
        "property_checks": _property_checks(scheme),
        "mutate_fields": list(scheme.get("mutate_fields") or []),
        "mode_sealed_reason": scheme.get("mode_sealed_reason") or "",
        "forced_build_reason": scheme.get("forced_build_reason") or "",
        "coder_summary": coder_summary,
    }
    (fdir / "decision.json").write_text(
        json.dumps(decision, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _read_special_decision(job_dir: Path, scheme: dict) -> dict:
    del job_dir  # tools 上下文已指向 job_dir
    sid = str(scheme.get("id") or "special")
    path = tools.special_meta_dir(sid) / "decision.json"
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


# 整份输入归一化后命中这些，视为明显凑数（不声称验过 must_hold）
_DEGENERATE_INPUT_EXACT = frozenset({
    "1", "0", "1 1", "1 2", "1 2 3", "1 1 1", "0 0 0", "0 1",
    "1\n2 3", "1\n1 1", "1\n1 2",
})


def _looks_existence_must_hold(must_hold: list[str]) -> bool:
    blob = " ".join(must_hold or [])
    keys = ("不存在", "存在", "区间", "之间", "范围内", "之内", "开区间", "闭区间")
    return any(k in blob for k in keys)


def _reject_degenerate_input(
    inp: str,
    must_hold: list[str] | None = None,
    scheme_title: str = "",
) -> str:
    """极便宜启发式：只拦明显假/退化输入。"""
    del scheme_title
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
    """抽查：gen → validate → property_check → 退化启发式 → std → special_only 快检。"""
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
    inp = gen_out if isinstance(gen_out, str) else ""
    val = tools.run_validate(inp)
    prop = tools.run_property_check(inp)
    std = tools.run_std(inp)
    deg = _reject_degenerate_input(
        inp,
        list(scheme.get("must_hold") or []),
        str(scheme.get("title") or sid),
    )
    bad = []
    for name, msg in (
        ("validate", val),
        ("property_check", prop),
        ("std", std),
        ("degenerate_check", deg),
    ):
        if isinstance(msg, str) and msg.startswith("ERROR"):
            bad.append(f"{name}={msg}")
    if bad:
        return f"ERROR: scheme {sid}\n" + "\n".join(bad)
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
    del range_plain, existing_ids  # 模板 Plan 不再塞长题面/已有 id 列表
    sid = scheme.get("id") or f"scheme_{scheme_index}"
    stype = _scheme_type(scheme)
    # 规则盖章 mutate 窄门（GUI 手改 force_mutate 可保留）
    mode, reason = seal_construct_mode(scheme, honor_force=True)
    scheme["construct_mode"] = mode
    if reason:
        scheme["mode_sealed_reason"] = reason
    sc_args = {"special_only": True}
    plan_name = f"gen_special_plan_{sid}.md"
    is_first = scheme_index == 0

    job_store.add_progress(
        job,
        f"【Special {scheme_index + 1}/{total_schemes}】方案 {sid}（{scheme.get('title')}）"
        f" mode={mode} 模板 Plan"
        + (f"（{reason}）" if reason else ""),
    )
    _write_template_plan(job_dir, scheme, plan_name)
    _write_special_decision(job_dir, scheme, mode)

    job_store.add_progress(job, f"【SpecialCoder】实现方案 {sid}（{mode}）")
    coder_summary = agent_run(
        _build_scheme_coder_task(
            stmt_plain, range_json, scheme, plan_name, is_first=is_first,
        ),
        max_steps=12,
        verbose=False,
        on_event=on_event,
        system_prompt=prompts.build_special_coder_prompt(typ),
        tool_schemas=SPECIAL_CODER_TOOL_SCHEMAS,
        write_check_discipline=True,
        self_check_fast=True,
        self_check_args=sc_args,
    )
    job_store.add_progress(job, f"SpecialCoder({sid}) 结束: {coder_summary}")

    scheme["samples_per_scheme"] = clamp_samples_per_scheme(scheme.get("samples_per_scheme"))
    schemes_all = list(range_json.get("special_schemes") or [])
    if schemes_all:
        for s in schemes_all:
            if str(s.get("id") or "") == sid:
                s["samples_per_scheme"] = scheme["samples_per_scheme"]
                s["construct_mode"] = mode
                if scheme.get("mode_sealed_reason"):
                    s["mode_sealed_reason"] = scheme["mode_sealed_reason"]
                if scheme.get("property_checks"):
                    s["property_checks"] = list(scheme["property_checks"])
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
    _write_special_decision(
        job_dir, scheme, mode, coder_summary=str(coder_summary)[:500],
    )

    self_check_result = _check_scheme(
        scheme, sid, stype, scheme_index, range_json, seed=2500 + scheme_index,
    )
    job_store.add_progress(job, f"方案 {sid} 检查: {str(self_check_result)[:400]}")

    if isinstance(self_check_result, str) and self_check_result.startswith("OK"):
        return

    save_good_snapshot(job_dir, include_in_out=False, suffix=".fixer_bak")

    # Fixer×1；mutate 仍失败再强制 build×1
    attempts: list[tuple[bool, str]] = [(False, mode)]
    if mode == CONSTRUCT_MODE_MUTATE:
        attempts.append((True, CONSTRUCT_MODE_BUILD))

    for attempt_i, (force_build, run_mode) in enumerate(attempts, 1):
        if force_build:
            scheme["construct_mode"] = CONSTRUCT_MODE_BUILD
            scheme["forced_build_reason"] = "mutate_fixer_failed"
            job_store.add_progress(
                job, f"【Special】{sid} mutate 失败，降级为 build 重写",
            )
        job_store.add_progress(
            job,
            f"【Special Fixer】{sid} 第 {attempt_i}/{len(attempts)}"
            + ("（build 降级）" if force_build else f"（{run_mode}）"),
        )
        fixer_summary = agent_run(
            _build_scheme_fixer_task(
                scheme, self_check_result, attempt_i, len(attempts),
                force_build=force_build,
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
            scheme, sid, stype, scheme_index, range_json, seed=2600 + attempt_i,
        )
        if isinstance(self_check_result, str) and self_check_result.startswith("OK"):
            _write_special_decision(
                job_dir, scheme, scheme["construct_mode"],
                coder_summary=str(fixer_summary)[:500],
            )
            return

    restore_good_snapshot(job_dir, suffix=".fixer_bak")
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
    """常规数据之后：按选中方案逐个模板 Plan + Execute。"""
    if not special_enabled(range_json) and not (special_samples_desc or "").strip():
        return "未启用特殊样例，跳过 SpecialCoder"

    schemes = selected_schemes(range_json.get("special_schemes"))
    if not schemes:
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

    for s in schemes:
        s["samples_per_scheme"] = clamp_samples_per_scheme(s.get("samples_per_scheme"))
        mode, reason = seal_construct_mode(s, honor_force=True)
        s["construct_mode"] = mode
        if reason:
            s["mode_sealed_reason"] = reason
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

    job_store.add_progress(job, "【Special】已成功方案后跑完整 special_only 自检")
    full = tools.run_self_check(fast_mode=False, special_only=True)
    job_store.add_progress(job, f"Special 完整自检: {full[:500]}")
    if not (isinstance(full, str) and full.startswith("OK")):
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
