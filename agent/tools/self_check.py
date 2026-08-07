"""自检：run_self_check / ensure_self_check_prereqs / 进度格式化。"""
import json
import re
from pathlib import Path

from sandbox.run import EXIT_MEMORY, is_stack_overflow, safe_run
from pipeline.gen_data import DEFAULT_REGULAR_COUNT

from .context import (
    _exe,
    _load_range_json,
    _memory_limit_mb,
    _std,
    _std_timeout_s,
    _wd,
)
from .run import _is_special_type, run_gen, run_property_check, run_validate
from .write import write_gen, write_special_check, write_special_gen, write_validate

# random 小档压测：多 seed × 小规模档 index，尽早撞出空候选 rnd.next(0,-1)
_MIN_TIER_STRESS_SEEDS_FAST = 3
_MIN_TIER_STRESS_SEEDS_FULL = 2
_MIN_TIER_STRESS_BASE_SEED = 2200


def _constraint_lo(constraints: dict | None, keys: tuple[str, ...] = ("n", "N", "S", "|S|", "len", "length")) -> int | None:
    """从 constraints 取规模下界（优先 n / S / |S|）。"""
    if not isinstance(constraints, dict):
        return None
    lower = {str(k).lower(): k for k in constraints}
    for want in keys:
        raw_key = lower.get(want.lower())
        if raw_key is None:
            continue
        v = constraints[raw_key]
        if isinstance(v, (list, tuple)) and len(v) >= 1:
            try:
                return int(v[0])
            except (TypeError, ValueError):
                continue
        try:
            return int(v)
        except (TypeError, ValueError):
            continue
    return None


def _small_n_tier_indices(count: int) -> list[int]:
    """与常见 gen 解轴 bB=(index//3)%3 对齐：返回小规模档 index（优先 0,1,2）。

    index%9 ∈ {0,1,2} ⇒ bB=0（小 n）；其中 0/1/2 覆盖 bA 小/中/大 × 小 n。
    """
    c = max(1, int(count or 1))
    out: list[int] = []
    for i in range(min(c, 9)):
        if (i // 3) % 3 == 0:
            out.append(i)
    return out or [0]


def _append_min_tier_random_stress(
    checks: list[tuple[str, int, int, int | None]],
    count: int,
    *,
    seeds_per_index: int,
    base_seed: int = _MIN_TIER_STRESS_BASE_SEED,
) -> None:
    """追加 random 小档多 seed 压测（不落盘）。"""
    seeds_per_index = max(1, int(seeds_per_index))
    for j, idx in enumerate(_small_n_tier_indices(count)):
        for s in range(seeds_per_index):
            checks.append(("random", base_seed + j * 10 + s, idx, None))


def _rnd_empty_hint(fails: list[str]) -> str:
    """空候选 / lo>hi 类 gen 崩溃时的定点修复提示。"""
    blob = "\n".join(fails)
    if "n must be positive" not in blob and "must be positive" not in blob.lower():
        return ""
    return (
        "【定点】random_t::next n must be positive：多为空候选或 lo>hi。"
        "若先 rnd 一个位置再 pool 兼容位置 → 改一次枚举所有不重叠 (p1,p2)，非空再采；"
        "n=各模式长之和时特判拼接。禁止对空 vector 调 rnd.next(0,sz-1)。"
    )


def _val_readstring_hint(fails: list[str]) -> str:
    """validator 误用 readString 导致假 |S| out of range 时的定点提示。"""
    blob = "\n".join(fails)
    if "out of range" not in blob.lower() and "|S|" not in blob:
        return ""
    # 几乎所有 type 都 validate 失败且像长度问题 → 高度疑似读空串
    val_fails = [f for f in fails if "validate" in f.lower()]
    if len(val_fails) < 2:
        return ""
    return (
        "【定点】多组 validate 报 |S|/长度 out of range：优先查 validator——"
        "禁止 readInt(T) 后用 readString/readLine（只读当前行剩余，常得空串）；"
        "单行一词改 inf.readToken(\"[a-z]{L,R}\", \"S\")。确认后再查 gen。"
    )
def ensure_self_check_prereqs(
    skip_recompile: set[str] | frozenset[str] | None = None,
    require_special: bool | None = None,
) -> tuple[bool, str]:
    """自检前确保 gen / validator 可执行文件就绪。

    - 若 exe 已存在：直接通过
    - 若 exe 缺失但对应 .cpp 在磁盘上：尝试用现有源码重编译
      （除非 skip_recompile 含该名字——本步 write_* 刚编译失败时不要用同一份坏源码再编一次）
    - 若源码也不存在 / 跳过重编译：返回缺失项，由调用方提示 Agent 先 write_*

    skip_recompile: 可选 {"gen", "validator", "gen_special", "check_special"}，
      表示本步已确认编译失败，禁止重试。
    require_special:
      - None：按 range.json 是否启用特殊样例自动决定
      - False：不要求 gen_special（普通 Gen 阶段）
      - True：若启用特殊样例则必须有 gen_special + check_special（SpecialCoder 阶段）

    返回 (ready, message)。
    """
    skip = set(skip_recompile or ())
    notes: list[str] = []
    missing: list[str] = []
    just_failed: list[str] = []

    gen_exe = _wd() / _exe("gen")
    val_exe = _wd() / _exe("validator")
    gen_src = _wd() / "gen.cpp"
    val_src = _wd() / "validator.cpp"

    # 是否启用特殊样例
    rj = _load_range_json()
    special_count = int(rj.get("special_samples_count") or 0)
    range_has_special = special_count > 0 and bool((rj.get("special_samples_desc") or "").strip())
    if require_special is None:
        has_special = range_has_special
    else:
        has_special = bool(require_special) and range_has_special

    if not gen_exe.exists():
        if "gen" in skip:
            missing.append("gen")
            just_failed.append("gen")
        elif gen_src.is_file():
            try:
                content = gen_src.read_text(encoding="utf-8")
            except Exception as e:
                return False, f"读取 gen.cpp 失败: {type(e).__name__}: {e}"
            msg = write_gen(content)
            if not str(msg).startswith("OK"):
                return False, f"尝试重编译 gen.cpp 失败: {msg}"
            notes.append("已从磁盘重编译 gen")
        else:
            missing.append("gen")

    if has_special:
        special_exe = _wd() / _exe("gen_special")
        special_src = _wd() / "gen_special.cpp"
        if not special_exe.exists():
            if "gen_special" in skip:
                missing.append("gen_special")
                just_failed.append("gen_special")
            elif special_src.is_file():
                try:
                    content = special_src.read_text(encoding="utf-8")
                except Exception as e:
                    return False, f"读取 gen_special.cpp 失败: {type(e).__name__}: {e}"
                msg = write_special_gen(content)
                if not str(msg).startswith("OK"):
                    return False, f"尝试重编译 gen_special.cpp 失败: {msg}"
                notes.append("已从磁盘重编译 gen_special")
            else:
                missing.append("gen_special")
        # 仅 SpecialCoder（require_special=True）强制性质检查器
        if require_special is True:
            check_exe = _wd() / _exe("check_special")
            check_src = _wd() / "check_special.cpp"
            if not check_exe.exists():
                if "check_special" in skip:
                    missing.append("check_special")
                    just_failed.append("check_special")
                elif check_src.is_file():
                    try:
                        content = check_src.read_text(encoding="utf-8")
                    except Exception as e:
                        return False, f"读取 check_special.cpp 失败: {type(e).__name__}: {e}"
                    msg = write_special_check(content)
                    if not str(msg).startswith("OK"):
                        return False, f"尝试重编译 check_special.cpp 失败: {msg}"
                    notes.append("已从磁盘重编译 check_special")
                else:
                    missing.append("check_special")

    if not val_exe.exists():
        if "validator" in skip:
            missing.append("validator")
            just_failed.append("validator")
        elif val_src.is_file():
            try:
                content = val_src.read_text(encoding="utf-8")
            except Exception as e:
                return False, f"读取 validator.cpp 失败: {type(e).__name__}: {e}"
            msg = write_validate(content)
            if not str(msg).startswith("OK"):
                return False, f"尝试重编译 validator.cpp 失败: {msg}"
            notes.append("已从磁盘重编译 validator")
        else:
            missing.append("validator")

    if missing:
        hints = []
        if "gen" in missing:
            if "gen" in just_failed:
                hints.append("本步 write_gen 已编译失败，请修复 gen.cpp 后重新 write_gen（勿用同一份坏源码空转）")
            else:
                hints.append("请先 write_gen 写出完整可编译的 gen.cpp")
        if "gen_special" in missing:
            if "gen_special" in just_failed:
                hints.append("本步 write_special_gen 已编译失败，请修复 gen_special.cpp 后重新 write_special_gen（勿用同一份坏源码空转）")
            else:
                hints.append("range.json 启用了特殊样例，请先 write_special_gen 写出完整可编译的 gen_special.cpp")
        if "check_special" in missing:
            if "check_special" in just_failed:
                hints.append(
                    "本步 write_special_check 已编译失败，请修复 check_special.cpp 后重新 write_special_check"
                )
            else:
                hints.append(
                    "请先 write_special_check 写出完整可编译的 check_special.cpp"
                    "（判定 must_hold：成立 exit 0，否则 exit 1）"
                )
        if "validator" in missing:
            if "validator" in just_failed:
                hints.append(
                    "本步 write_validate 已失败，请修复 validator.cpp 后重新 write_validate"
                    "（以编译/运行报错为准）"
                )
            else:
                hints.append("请先 write_validate 写出完整可编译的 validator.cpp")
        return False, "缺少已编译产物: " + "、".join(missing) + "。" + "；".join(hints)

    return True, ("；".join(notes) if notes else "gen/validator 已就绪")


def _triple_check(
    seed: int,
    typ: str,
    index: int,
    count: int,
    std_timeout: int,
    mem_mb=None,
) -> tuple[str, str, str]:
    """单组 gen→validate→[property_check]→std。

    返回 (msg, input_text, output_text)。
    成功时 msg 以 OK 开头，input/output 为可落盘内容；失败时后两者为空。
    """
    gen_out = run_gen(seed=seed, type=typ, index=index, count=count)
    if isinstance(gen_out, str) and gen_out.startswith("ERROR"):
        return f"FAIL type={typ} seed={seed}: {gen_out}", "", ""
    val = run_validate(gen_out)
    if not val.startswith("OK"):
        extra = ""
        raw = gen_out if isinstance(gen_out, str) else ""
        if (not raw.strip()) and (
            "Unexpected end of file" in val
            or "token expected" in val
            or "EOF" in val
        ):
            extra = (
                f' 【疑似缺分支】gen 对 type={typ} 输出为空；'
                f'请确认含 type == "{typ}"（与 range.edge_cases 逐字符一致）'
            )
        return f"FAIL type={typ} seed={seed} validate: {val}{extra}", "", ""
    if _is_special_type(typ):
        if not (_wd() / _exe("check_special")).is_file():
            return (
                f"FAIL type={typ} seed={seed}: check_special 未编译，"
                f"请先 write_special_check",
                "",
                "",
            )
        prop = run_property_check(gen_out if isinstance(gen_out, str) else "")
        if not (isinstance(prop, str) and prop.startswith("OK")):
            return f"FAIL type={typ} seed={seed} property_check: {prop}", "", ""
    # 直接调 safe_run 以便用 std_timeout（run_std 会再读 range，此处统一）
    rc, out, err = safe_run(
        _std(), stdin=gen_out, timeout=std_timeout, memory_limit_mb=mem_mb
    )
    if rc != 0:
        if rc == 124:
            return f"FAIL type={typ} seed={seed}: std TIMEOUT after {std_timeout}s", "", ""
        if rc == EXIT_MEMORY:
            return f"FAIL type={typ} seed={seed}: std MEMORY_LIMIT ({mem_mb} MB)", "", ""
        if is_stack_overflow(rc):
            return (
                f"FAIL type={typ} seed={seed}: std STACK_OVERFLOW "
                f"(递归过深；请确认标程已用加大栈编译，或降低链深度)",
                "",
                "",
            )
        return f"FAIL type={typ} seed={seed}: std rc={rc} {(err or '').strip()}", "", ""
    # rc==0 时允许空 stdout（合法 .out）；套件级再检查是否「全部」为空
    # 空输入：保留真正空文件（EOF），不要强行补换行，否则 edge_m0 等会失真
    raw_in = gen_out if isinstance(gen_out, str) else ""
    if not raw_in.strip():
        inp = ""
    else:
        inp = raw_in.rstrip("\n") + "\n"
    raw = out or ""
    ans = raw if raw.endswith("\n") else (raw + "\n")
    msg = (
        f"OK type={typ} seed={seed} index={index} "
        f"in_chars={len(inp)} out_chars={len(ans)}"
    )
    return msg, inp, ans


def _clear_out_pairs(out_dir: Path, count: int) -> None:
    """清空 out/ 下 1..count 的成对测例，避免复用过期数据。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    for i in range(1, count + 1):
        for suffix in (".in", ".out"):
            p = out_dir / f"{i}{suffix}"
            if p.is_file():
                try:
                    p.unlink()
                except Exception:
                    pass


def _persist_self_check_fail(text: str) -> None:
    """把完整自检失败文本写入工作目录（兼容旧路径 + errors/）。"""
    body = text or ""
    try:
        (_wd() / "self_check_last_fail.txt").write_text(body, encoding="utf-8")
    except Exception:
        pass
    try:
        from agent.errors import persist_error

        persist_error(_wd(), body, tool="run_self_check", kind="self_check")
    except Exception:
        pass


def format_self_check_for_progress(
    text: str,
    *,
    ok_limit: int = 500,
    fail_limit: int = 8000,
) -> str:
    """进度日志用：成功短预览；失败保留全部 FAIL/TIMEOUT 行，避免在数字中间截断。

    完整正文已由 _persist_self_check_fail 落盘时，进度里仍尽量带齐 FAIL 行。
    """
    s = text or ""
    if not s.startswith("ERROR") and "\nFAIL " not in ("\n" + s):
        return s if len(s) <= ok_limit else s[:ok_limit] + "…"

    lines = s.splitlines()
    keep: list[str] = []
    # 头几行（mode / constraints）
    for ln in lines[:4]:
        keep.append(ln)
    for ln in lines[4:]:
        u = ln.upper()
        if (
            ln.startswith("FAIL")
            or ln.startswith("ERROR")
            or "TIMEOUT" in u
            or "MEMORY_LIMIT" in u
            or "STACK_OVERFLOW" in u
            or ln.startswith("请根据 FAIL")
            or "【TIMEOUT" in ln
            or "有效状态预算" in ln
        ):
            keep.append(ln)
    # 若没抓到 FAIL（异常格式），退回全文前 fail_limit
    body = "\n".join(keep) if any(x.startswith("FAIL") for x in keep) else s
    if len(body) <= fail_limit:
        return body
    # 超长时保头+全部 FAIL 行，必要时再截但按整行
    out_lines: list[str] = []
    n = 0
    for ln in body.splitlines():
        add = len(ln) + 1
        if out_lines and n + add > fail_limit and ln.startswith("FAIL"):
            # FAIL 行优先保留：丢掉较早的非 FAIL 头以外行
            while out_lines and n + add > fail_limit and not out_lines[-1].startswith("FAIL"):
                dropped = out_lines.pop()
                n -= len(dropped) + 1
        if n + add > fail_limit:
            out_lines.append(f"…(进度摘要截断，完整见 self_check_last_fail.txt，共 {len(s)} 字)")
            break
        out_lines.append(ln)
        n += add
    return "\n".join(out_lines)


def _self_check_branch_miss_hint(fails: list[str]) -> str:
    """空输出 / 首行 EOF 时提示可能缺 --type 分支或分支名不一致。

    不把 Unexpected white-space - token expected 算作缺分支（那是 validator 格式/strict 问题）。
    """
    hints: list[str] = []
    for f in fails:
        typ_m = re.search(r"FAIL type=(\S+)", f)
        if not typ_m:
            continue
        typ = typ_m.group(1)
        # 空白格式失败 ≠ 缺分支
        if "Unexpected white-space" in f:
            continue
        emptyish = "in_chars=0" in f or re.search(r"gen.*empty|空输出|空文件", f, re.I)
        # 真·首行/读尽 EOF（排除已读到中间行的格式错误）
        eofish = (
            emptyish
            or re.search(r"Unexpected end of file.*\(stdin, line 1\)", f) is not None
            or ("in_chars=0" in f and "token expected" in f)
        )
        if eofish or emptyish:
            hints.append(
                f'【疑似缺分支】type={typ} 输出为空或首行 EOF：请确认 gen 含 '
                f'type == "{typ}"（字符串须与 range.json edge_cases 逐字符一致；'
                f'勿写成 "edge_{typ}" 若 range 无此外前缀，反之亦然）。'
            )
    # 去重保序
    seen: set[str] = set()
    out: list[str] = []
    for h in hints:
        if h not in seen:
            seen.add(h)
            out.append(h)
    return "\n".join(out)


def run_self_check(
    fast_mode: bool = False,
    tiny_mode: bool = False,
    skip_special: bool = False,
    special_only: bool = False,
) -> str:
    """按 range.json 做强化自检。

    默认完整模式（交付对齐）：
      1) 按批量同一调度跑 i=0..count-1（seed=i, type=pick_type(i), index=i），
         成功则写入 out/{i+1}.in/.out，供阶段 4 批量 reuse_existing 复用；
      2) 再对每个 edge_case 补一次 index=count-1 的最大档压测（不落盘）。
    fast_mode=True：减少组数并使用中等规模，用于修复循环中间轮次；不写 out/。
      额外对 random 小档 index（bB=0，通常 0/1/2）跑多 seed 压测，尽早暴露空候选采样崩溃。
    tiny_mode=True：只跑最小档，用于完整自检失败后的返工轮；不写 out/。
      同样追加小档多 seed。
    skip_special=True：跳过特殊样例（普通 Gen 阶段；不要求 gen_special）。
    special_only=True：只测 special_samples（SpecialCoder 阶段）。

    通过返回以 OK 开头的摘要；任一失败返回 ERROR/... 详情，供 Agent 修改后重试。
    """
    from pipeline.gen_data import (
        _selected_scheme_schedule,
        normalize_range_json,
        pick_type,
        special_enabled,
    )

    if skip_special and special_only:
        return "ERROR: skip_special 与 special_only 不能同时为 True"

    p = _wd() / "range.json"
    if not p.exists():
        return "ERROR: range.json not found，请先 write_range"
    try:
        rj = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return f"ERROR: range.json 非法: {e}"

    rj = normalize_range_json(dict(rj))
    count = int(rj.get("count") or DEFAULT_REGULAR_COUNT)
    edge_cases = list(rj.get("edge_cases") or [])
    scheme_schedule = _selected_scheme_schedule(rj)
    special_count = sum(n for _, n in scheme_schedule) if scheme_schedule else int(
        rj.get("special_samples_count") or 0
    )
    range_has_special = special_enabled(rj)
    has_special = range_has_special and not skip_special
    if special_only:
        if not range_has_special:
            return "ERROR: special_only=True 但 range.json 未启用特殊样例"
        has_special = True

    if not special_only and not (_wd() / _exe("gen")).exists():
        return "ERROR: gen 未编译，请先 write_gen"
    if has_special and not (_wd() / _exe("gen_special")).exists():
        return "ERROR: range.json 启用了特殊样例，但 gen_special 未编译，请先 write_special_gen"
    if special_only and not (_wd() / _exe("check_special")).exists():
        return (
            "ERROR: special_only 需要 check_special，请先 write_special_check"
            "（判定 must_hold：成立 exit 0，否则 exit 1）"
        )
    if not (_wd() / _exe("validator")).exists():
        return "ERROR: validator 未编译，请先 write_validate"

    constraints = rj.get("constraints") or {}
    std_timeout = _std_timeout_s(rj)
    mem_mb = _memory_limit_mb(rj)

    multi_hints = [
        "big_T_small_n", "small_T_big_n", "edge_Tmax", "edge_T1",
        "sum_full", "single_max_case",
    ]

    # checks: (type, seed, index, persist_slot|None)
    # persist_slot 为 1-based 文件号；None 表示仅压测不落盘
    checks: list[tuple[str, int, int, int | None]] = []

    if special_only:
        mid_index = max(1, count // 2)
        # 每个选中方案至少测一组；完整模式按调度落盘
        scheme_types = [
            (f"special:{sid}" if sid else "special_samples")
            for sid, _ in scheme_schedule
        ] or ["special_samples"]
        if tiny_mode:
            for j, st in enumerate(scheme_types):
                checks.append((st, 2500 + j, 0, None))
        elif fast_mode:
            for j, st in enumerate(scheme_types):
                checks.append((st, 2500 + j, mid_index, None))
        else:
            out_dir = _wd() / "out"
            for i in range(max(0, count - special_count), count):
                slot = i + 1
                for suffix in (".in", ".out"):
                    pth = out_dir / f"{slot}{suffix}"
                    if pth.is_file():
                        try:
                            pth.unlink()
                        except Exception:
                            pass
                st = pick_type(i, count, edge_cases, special_count, scheme_schedule)
                checks.append((st, i, i, slot))
    elif tiny_mode:
        for i, typ in enumerate(edge_cases):
            checks.append((typ, 1000 + i, 0, None))
        checks.append(("random", 2000, 0, None))
        _append_min_tier_random_stress(checks, count, seeds_per_index=2)
        if has_special:
            checks.append(("special_samples", 2500, 0, None))
        for name in multi_hints:
            if name in edge_cases:
                checks.append((name, 4000 + hash(name) % 100, 0, None))
    elif fast_mode:
        mid_index = max(1, count // 2)
        for i, typ in enumerate(edge_cases):
            checks.append((typ, 1000 + i, min(i, mid_index), None))
        checks.append(("random", 2000, 0, None))
        checks.append(("random", 2001, mid_index, None))
        # 小档多 seed：尽早撞 n≈下界时空候选 rnd.next 崩溃（勿等 full 才进 Rewrite）
        _append_min_tier_random_stress(
            checks, count, seeds_per_index=_MIN_TIER_STRESS_SEEDS_FAST,
        )
        if has_special:
            checks.append(("special_samples", 2500, mid_index, None))
        for name in multi_hints:
            if name in edge_cases:
                checks.append((name, 4000 + hash(name) % 100, mid_index, None))
    else:
        # 完整模式：与批量生成同一调度，成功则写入 out/ 供后续复用
        out_dir = _wd() / "out"
        if skip_special and special_count > 0:
            # 只清常规组，保留特殊组槽位给后续 SpecialCoder
            for i in range(count - special_count):
                for suffix in (".in", ".out"):
                    pth = out_dir / f"{i + 1}{suffix}"
                    if pth.is_file():
                        try:
                            pth.unlink()
                        except Exception:
                            pass
            for i in range(count - special_count):
                typ = pick_type(i, count, edge_cases, special_count, scheme_schedule)
                checks.append((typ, i, i, i + 1))
        else:
            _clear_out_pairs(out_dir, count)
            for i in range(count):
                typ = pick_type(i, count, edge_cases, special_count, scheme_schedule)
                checks.append((typ, i, i, i + 1))
        # 额外最大档压测：每个 edge（及必要的 random max）再跑一次，不落盘
        max_idx = max(0, count - 1 - (special_count if skip_special else 0))
        for i, typ in enumerate(edge_cases):
            checks.append((typ, 10000 + i, max_idx, None))
        has_max_case = any(
            name in edge_cases for name in ("edge_nmax", "nmax", "edge_n_max", "max_n")
        )
        if not has_max_case:
            checks.append(("random", 10000 + len(edge_cases), max_idx, None))
        for name in multi_hints:
            if name in edge_cases:
                checks.append((name, 11000 + hash(name) % 100, max_idx, None))
        # 小档多 seed 补充压测（不落盘）：降低「调度未抽到极短 n」漏网
        _append_min_tier_random_stress(
            checks, count, seeds_per_index=_MIN_TIER_STRESS_SEEDS_FULL,
        )

    # 去重保序：交付组按 persist_slot 优先；压测按 (type, index, seed)
    seen = set()
    uniq: list[tuple[str, int, int, int | None]] = []
    for typ, seed, idx, slot in checks:
        key = ("slot", slot) if slot is not None else ("stress", typ, idx, seed)
        if key in seen:
            continue
        seen.add(key)
        uniq.append((typ, seed, idx, slot))

    limit_note = f"std_timeout={std_timeout}s"
    if mem_mb:
        limit_note += f" memory_limit_mb={mem_mb}"
    mode_note = "tiny" if tiny_mode else ("fast" if fast_mode else "full")
    if special_only:
        mode_note += "+special_only"
    elif skip_special:
        mode_note += "+skip_special"
    lines = [f"self_check start: mode={mode_note} count={count} edges={edge_cases} {limit_note}"]
    if not fast_mode and not tiny_mode:
        lines.append("full: aligned with batch schedule; OK cases written to out/ for reuse")
    if isinstance(constraints, dict) and constraints:
        lines.append(f"constraints_keys={list(constraints.keys())}")
        n_lo = _constraint_lo(constraints)
        min_idxs = _small_n_tier_indices(count)
        if n_lo is not None:
            lines.append(
                f"stress: random min-tier indices={min_idxs} (constraints n_lo={n_lo})"
            )
        else:
            lines.append(f"stress: random min-tier indices={min_idxs}")
    fails = []
    written = 0
    nonempty_out = 0
    out_dir = _wd() / "out"
    for typ, seed, idx, slot in uniq:
        msg, inp, ans = _triple_check(seed, typ, idx, count, std_timeout, mem_mb=mem_mb)
        if slot is not None:
            tag = f" [out/{slot}.in]"
        else:
            tag = " [stress]"
        lines.append(msg + tag)
        if msg.startswith("FAIL") or msg.startswith("ERROR"):
            fails.append(msg)
            continue
        if (ans or "").strip():
            nonempty_out += 1
        # 成功即落盘（含空 .in：允许空输入题）
        if slot is not None:
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / f"{slot}.in").write_text(inp if inp is not None else "", encoding="utf-8")
            (out_dir / f"{slot}.out").write_text(ans if ans is not None else "", encoding="utf-8")
            written += 1

    if fails:
        timeoutish = any(
            ("TIMEOUT" in f) or ("MEMORY_LIMIT" in f) for f in fails
        )
        branch_hint = _self_check_branch_miss_hint(fails)
        rnd_hint = _rnd_empty_hint(fails)
        val_hint = _val_readstring_hint(fails)
        if special_only:
            fix_hint = (
                "请根据 FAIL 修复 gen_special.cpp 后重新 write_special_gen 再 run_self_check。"
            )
        elif timeoutish:
            fix_hint = (
                "请根据 FAIL 修复 gen/validator 后重新 write_* 再 run_self_check。"
                "【TIMEOUT/MEMORY】仅把该 type（及同类最大档）有效状态压到 K≤200，"
                "用有限域复用凑满规模；保留小中档多样；禁止略微收窄取值区间；"
                "满 constraints 上界 ≠ 状态数拉满；勿只靠加时限/内存。"
            )
        else:
            fix_hint = (
                "请根据 FAIL 修复 gen/validator 后重新 write_* 再 run_self_check。"
            )
        if val_hint:
            fix_hint = val_hint + "\n" + fix_hint
        if rnd_hint:
            fix_hint = rnd_hint + "\n" + fix_hint
        if branch_hint:
            fix_hint = branch_hint + "\n" + fix_hint
        result = (
            "ERROR: self_check failed\n"
            + "\n".join(lines)
            + "\n" + fix_hint
        )
        _persist_self_check_fail(result)
        return result
    if nonempty_out == 0:
        result = (
            "ERROR: self_check failed: 全部测例 stdout 为空"
            "（标程可能未输出，或 gen 从未生成查询类操作）\n"
            + "\n".join(lines)
            + "\n请保证至少部分测例含会触发输出的操作/查询，或检查标程是否写了输出。"
        )
        _persist_self_check_fail(result)
        return result
    if not fast_mode and not tiny_mode:
        lines.append(f"persisted {written}/{count} pairs to out/ for batch reuse")
    return "OK: self_check passed\n" + "\n".join(lines)

