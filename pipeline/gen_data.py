"""批量生成 .in / .out，再打包成 zip。

这一步不调 LLM：Agent 交付 gen.cpp/validator.cpp（testlib，已编译成二进制）后，
本脚本接管，按 seed 循环跑 gen → validator → std，写出测例对。
也兼容旧的 Python gen.py/validate.py（找不到二进制时回退）。
"""
import concurrent.futures
import json
import os
import time
from pathlib import Path

import sys
sys.path.append(str(Path(__file__).resolve().parent.parent))

from sandbox.run import EXIT_MEMORY, parse_memory_limit_mb, safe_run


def _exe(base: str) -> str:
    return base + (".exe" if os.name == "nt" else "")


def _is_special_type(typ: str) -> bool:
    t = (typ or "").strip()
    return t == "special_samples" or t.startswith("special:")


def _gen_cmd(work: Path, typ: str = "") -> str:
    """优先用编译好的 gen 二进制，否则回退 python gen.py。

    若 typ 为 special_samples / special:<id> 且存在 gen_special 二进制，则使用 gen_special。
    """
    if _is_special_type(typ) and (work / _exe("gen_special")).exists():
        return _exe("gen_special")
    if (work / _exe("gen")).exists():
        return _exe("gen")
    if (work / "gen.py").exists():
        return "python gen.py"
    raise FileNotFoundError(f"缺少 {work / _exe('gen')} 或 {work / 'gen.py'}")


def _validator_cmd(work: Path) -> str:
    """优先用编译好的 validator 二进制，否则回退 python validate.py。"""
    if (work / _exe("validator")).exists():
        return _exe("validator")
    if (work / "validate.py").exists():
        return "python validate.py"
    raise FileNotFoundError(f"缺少 {work / _exe('validator')} 或 {work / 'validate.py'}")


# range.json 里的 edge_cases 名直接作为 gen.py 的 --type 取值（Agent 自定义）。
# 系统会自动给非边界组补 "random"，所以 edge_cases 里不需要写 random。


def validate_range_json(rj) -> list:
    """校验 range.json 结构，返回错误信息列表（空列表表示合法）。

    要求字段：
      - count: 正整数
      - constraints: dict（各变量 -> [min, max]）
      - edge_cases: 字符串列表（每个是 gen.py --type 能接受的取值）
    可选字段：
      - std_cmd（后端会强制注入）
      - time_limit_ms、memory_limit_mb（正整数；缺省由 normalize 补为 5000 / 1024）
      - special_constraints: 字符串列表，题面里提取出的特殊结构约束
        （如 "图是 DAG"、"图必须存在哈密顿路径"）。缺省视为 []。
    """
    errs = []
    if not isinstance(rj, dict):
        return ["range.json 顶层不是 JSON 对象"]

    count = rj.get("count")
    if not isinstance(count, int) or isinstance(count, bool) or count <= 0:
        errs.append(f"count 必须是正整数，当前为 {count!r}")

    cons = rj.get("constraints")
    if not isinstance(cons, dict) or not cons:
        errs.append("constraints 必须是非空对象（各变量名 -> [min,max]）")
    elif isinstance(cons, dict):
        for k, v in cons.items():
            if (not isinstance(v, list) or len(v) != 2
                    or not all(isinstance(x, int) for x in v)
                    or v[0] > v[1]):
                errs.append(f"constraints['{k}'] 必须是 [min,max] 且 min<=max，当前为 {v!r}")

    ec = rj.get("edge_cases")
    if ec is None:
        errs.append("缺少 edge_cases（边界类型列表，可为空数组 []）")
    elif not isinstance(ec, list) or not all(isinstance(e, str) and e for e in ec):
        errs.append("edge_cases 必须是字符串数组（每个非空）")

    # special_constraints 是 range_agent 提取的特殊结构约束清单，可选字段。
    # 只校验类型（字符串数组），不校验内容是否与题面一致（那是 LLM 的责任）。
    sc = rj.get("special_constraints")
    if sc is not None and not isinstance(sc, list):
        errs.append(f"special_constraints 必须是字符串数组或 null，当前为 {type(sc).__name__}")
    elif isinstance(sc, list):
        for s in sc:
            if not isinstance(s, str) or not s.strip():
                errs.append(f"special_constraints 每条必须是非空字符串，当前含 {s!r}")
                break

    # 特殊样例描述与数量：可选字段，启用时 count 必须包含这些样例
    ssd = rj.get("special_samples_desc")
    ssc = rj.get("special_samples_count")
    if ssd is not None:
        if not isinstance(ssd, str) or not ssd.strip():
            errs.append("special_samples_desc 若提供必须是非空字符串")
    if ssc is not None:
        if isinstance(ssc, bool) or not isinstance(ssc, int) or ssc <= 0:
            errs.append(f"special_samples_count 必须是正整数，当前为 {ssc!r}")
    if (ssd or "").strip() and isinstance(ssc, int) and ssc > 0:
        if not isinstance(count, int) or isinstance(count, bool) or count < ssc + 1:
            errs.append(
                f"启用特殊样例时 count 必须 >= special_samples_count + 1，"
                f"当前 count={count} special_samples_count={ssc}"
            )

    ml = rj.get("memory_limit_mb")
    if ml is not None:
        if isinstance(ml, bool) or not isinstance(ml, int) or ml <= 0:
            errs.append(f"memory_limit_mb 必须是正整数（MB），当前为 {ml!r}")

    tl = rj.get("time_limit_ms")
    if tl is not None:
        if isinstance(tl, bool) or not isinstance(tl, int) or tl <= 0:
            errs.append(f"time_limit_ms 必须是正整数（毫秒），当前为 {tl!r}")

    # 'random' 由系统自动补，Agent 若写进 edge_cases 会在 normalize 时剥掉，这里不报错

    return errs


# 仅在多测（constraints 含 T/t）时才有意义的边界名；无 T 时 normalize 会剔除
_MULTI_T_ONLY_EDGES = frozenset({
    "edge_T1", "edge_Tmax", "edge_t1", "edge_tmax",
    "edge_T_min", "edge_T_max", "edge_t_min", "edge_t_max",
    "big_T_small_n", "small_T_big_n", "max_tests", "min_tests",
})


def _constraints_have_multi_t(constraints) -> bool:
    """constraints 是否声明多测组数 T/t。"""
    if not isinstance(constraints, dict):
        return False
    for k in constraints:
        if str(k).strip().lower() == "t":
            return True
    return False


# 未写时限/内存时的默认（与 GUI / Range 提示一致）
DEFAULT_TIME_LIMIT_MS = 5000
DEFAULT_MEMORY_LIMIT_MB = 1024


def normalize_range_json(rj: dict) -> dict:
    """清洗 range.json：去掉 edge_cases 里的 'random'（系统会自动补），去重保序；count 缺省补 15。

    若启用特殊样例（special_samples_desc 非空），确保 count 至少为 special_samples_count + 1。
    无多测 T 时剔除 edge_T1 / edge_Tmax 等仅多测边界。
    缺省补 time_limit_ms=5000、memory_limit_mb=1024。
    """
    if not isinstance(rj, dict):
        return rj
    if "count" not in rj or rj.get("count") in (None, 0):
        rj["count"] = 15
    ec = rj.get("edge_cases")
    if isinstance(ec, list):
        seen = set()
        cleaned = []
        drop_multi_t = not _constraints_have_multi_t(rj.get("constraints"))
        for e in ec:
            if not isinstance(e, str) or not e or e == "random" or e == "special_samples":
                continue
            if drop_multi_t and e in _MULTI_T_ONLY_EDGES:
                continue
            if e not in seen:
                seen.add(e)
                cleaned.append(e)
        rj["edge_cases"] = cleaned

    # 时限 / 内存：缺省或非法时用默认 5s / 1024MB
    tl = rj.get("time_limit_ms")
    try:
        tl_i = int(tl) if tl is not None and not isinstance(tl, bool) else 0
    except (TypeError, ValueError):
        tl_i = 0
    if tl_i <= 0:
        rj["time_limit_ms"] = DEFAULT_TIME_LIMIT_MS

    ml = rj.get("memory_limit_mb")
    try:
        ml_i = int(ml) if ml is not None and not isinstance(ml, bool) else 0
    except (TypeError, ValueError):
        ml_i = 0
    if ml_i <= 0:
        rj["memory_limit_mb"] = DEFAULT_MEMORY_LIMIT_MB

    # LLM 常写 "special_samples_desc": "" / null；有键却为空会过不了 validate，直接删掉
    ssd = rj.get("special_samples_desc")
    if ssd is None or (isinstance(ssd, str) and not ssd.strip()):
        rj.pop("special_samples_desc", None)

    # 特殊样例：有 schemes 时按选中方案重算 count；否则兼容旧字段
    schemes = rj.get("special_schemes")
    if isinstance(schemes, list) and schemes:
        sched = _selected_scheme_schedule(rj)
        ssc = sum(n for _, n in sched)
        rj["special_samples_count"] = ssc
        # 尽量保留常规数：若原 count 偏小则抬到 常规15 + 特殊
        regular = max(1, int(rj.get("count") or 15) - int(ssc or 0))
        if regular + ssc != rj.get("count"):
            # 仅当 special 变化导致不一致时，以「至少常规1」校正
            if int(rj.get("count") or 0) < ssc + 1:
                rj["count"] = 15 + ssc
    else:
        ssd = rj.get("special_samples_desc")
        ssc = rj.get("special_samples_count")
        if (ssd or "").strip():
            if not isinstance(ssc, int) or isinstance(ssc, bool) or ssc <= 0:
                rj["special_samples_count"] = 1
                ssc = 5
            if rj["count"] < ssc + 1:
                rj["count"] = ssc + 15  # 常规 15 + 特殊样例
    return rj


def _selected_scheme_schedule(range_json: dict) -> list[tuple[str, int]]:
    """返回 [(scheme_id, samples_per_scheme), ...] 仅含选中方案。

    若存在 special_schemes 字段（即使全未选中），以该列表为准，不再回退旧 desc。
    """
    if not isinstance(range_json, dict):
        return []
    schemes = range_json.get("special_schemes")
    if isinstance(schemes, list):
        out: list[tuple[str, int]] = []
        for s in schemes:
            if not isinstance(s, dict) or s.get("selected", True) is False:
                continue
            sid = str(s.get("id") or "").strip() or "special"
            try:
                n = int(s.get("samples_per_scheme") or 1)
            except (TypeError, ValueError):
                n = 5
            out.append((sid, max(1, n)))
        return out
    # 兼容旧字段：无 schemes 时整块 special 用 special_samples
    try:
        sc = int(range_json.get("special_samples_count") or 0)
    except (TypeError, ValueError):
        sc = 0
    if sc > 0 and (range_json.get("special_samples_desc") or "").strip():
        return [("", sc)]
    return []


def special_enabled(range_json: dict) -> bool:
    """range.json 是否启用特殊样例（有选中方案或旧字段 desc+count）。"""
    return bool(_selected_scheme_schedule(range_json))


def pick_type(i: int, count: int, edge_cases: list, special_count: int = 0,
              scheme_schedule: list[tuple[str, int]] | None = None) -> str:
    """前几组按顺序覆盖边界类型，最后 special 组按方案调度，其余 random。

    scheme_schedule: [(scheme_id, n_samples), ...]；scheme_id 空则 type=special_samples。
    """
    edge_cases = list(edge_cases or [])
    special_count = max(0, int(special_count or 0))
    if scheme_schedule:
        special_count = sum(n for _, n in scheme_schedule)
    # 除去特殊样例后，至少保留 1 个 random 组
    random_min = max(1, count - len(edge_cases) - special_count)
    edge_max = min(len(edge_cases), max(0, count - special_count - random_min))
    if i < edge_max:
        return edge_cases[i]
    if i >= count - special_count:
        local = i - (count - special_count)
        if scheme_schedule:
            acc = 0
            for sid, n in scheme_schedule:
                if local < acc + n:
                    return f"special:{sid}" if sid else "special_samples"
                acc += n
            sid, _ = scheme_schedule[-1]
            return f"special:{sid}" if sid else "special_samples"
        return "special_samples"
    return "random"


def generate(range_json: dict, work_dir: str, out_dir: str, verbose: bool = True,
             reuse_existing: bool = True,
             skip_special: bool = False,
             only_special: bool = False) -> dict:
    """按 range.json 批量生成测例，返回统计信息。

    reuse_existing=True 时：若 out_dir 已有成对的 {i}.in/{i}.out，则跳过该组，
    只补缺失组。这样父任务里已通过 gen→validate→std 的合法测例可以复用。
    skip_special=True：跳过最后 special_samples_count 组（留给 SpecialCoder 之后补）。
    only_special=True：只生成最后 special_samples_count 组。
    """
    if skip_special and only_special:
        raise ValueError("skip_special 与 only_special 不能同时为 True")

    range_json = normalize_range_json(dict(range_json))  # 拷贝后再洗，避免改调用方
    errs = validate_range_json(range_json)
    if errs:
        raise ValueError("range.json 不合法:\n" + "\n".join(f"  - {e}" for e in errs))

    work = Path(work_dir)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    validator_cmd = _validator_cmd(work)
    std_cmd = range_json["std_cmd"]
    count = range_json["count"]
    edge_cases = range_json.get("edge_cases", [])
    scheme_schedule = _selected_scheme_schedule(range_json) if special_enabled(range_json) else []
    special_count = sum(n for _, n in scheme_schedule) if scheme_schedule else int(
        range_json.get("special_samples_count") or 0
    )
    if scheme_schedule:
        range_json["special_samples_count"] = special_count
    if not special_enabled(range_json):
        special_count = 0
        scheme_schedule = []
        skip_special = False
        only_special = False
    # gen 单独硬限 5s：超过即视为算法不达标（O(n^2) 枚举等），直接判该组失败，
    # 不让慢生成器拖垮整批；validate/std 仍按 range.json 的 time_limit_ms。
    gen_timeout_s = 5
    other_timeout_s = max(range_json.get("time_limit_ms", 1000) / 1000, 1)
    mem_mb = parse_memory_limit_mb(range_json)
    if verbose and mem_mb:
        print(f"memory_limit_mb={mem_mb}（作用于 gen / validator / std）")

    # 已存在的成对测例（1-based）可直接复用
    existing_pairs: set[int] = set()
    if reuse_existing:
        for i in range(1, count + 1):
            if (out / f"{i}.in").is_file() and (out / f"{i}.out").is_file():
                existing_pairs.add(i)
                if verbose:
                    print(f"[{i}/{count}] REUSE existing .in/.out")

    ok = len(existing_pairs)
    bad = 0
    reused = len(existing_pairs)
    t0 = time.perf_counter()
    max_retries = 3  # 同类型失败后最多重试几次

    def _gen_one(i: int, typ: str, fallback_types: list[str]):
        """生成第 i 组测例（含重试 + 降级类型），返回 (i, success, inp, ans, error_log, input_preview)。"""
        types_to_try = [typ] + [t for t in fallback_types if t and t != typ]
        error_log = []
        input_preview = ""
        for try_typ in types_to_try:
            last_error = ""
            for attempt in range(max_retries + 1):
                # 重试时换 seed，避免命中同一个随机坏点；index/count 仍保持当前组号
                seed = i + attempt * count
                gen_cmd = _gen_cmd(work, try_typ)
                rc, inp, err = safe_run(
                    f"{gen_cmd} --seed {seed} --type {try_typ} --index {i} --count {count}",
                    timeout=gen_timeout_s,
                    cwd=str(work),
                    memory_limit_mb=mem_mb,
                )
                if rc != 0:
                    if rc == 124:
                        last_error = (
                            f"gen TIMEOUT after {gen_timeout_s}s type={try_typ}: "
                            f"算法太慢（疑似 O(n^2) 枚举/预建大池子），需重写 gen.cpp"
                        )
                    elif rc == EXIT_MEMORY:
                        last_error = (
                            f"gen MEMORY_LIMIT ({mem_mb} MB) type={try_typ}: "
                            f"生成器内存超限，请降低规模或优化 gen.cpp"
                        )
                    else:
                        last_error = f"gen FAILED type={try_typ}: {err.strip()}"
                    if verbose:
                        print(f"[{i+1}/{count}] attempt {attempt+1}/{max_retries+1} {last_error}")
                    continue
                # 空输入保留真正空文件；非空再统一补末尾换行
                if not (inp or "").strip():
                    inp = ""
                else:
                    inp = inp.rstrip("\n") + "\n"

                rc, _, err = safe_run(
                    validator_cmd,
                    stdin=inp,
                    timeout=other_timeout_s,
                    cwd=str(work),
                    memory_limit_mb=mem_mb,
                )
                if rc != 0:
                    if rc == EXIT_MEMORY:
                        last_error = f"validate MEMORY_LIMIT ({mem_mb} MB) type={try_typ}"
                    else:
                        last_error = f"validate FAILED type={try_typ}: {err.strip()}"
                    if not input_preview:
                        input_preview = inp[:800]
                    if verbose:
                        print(f"[{i+1}/{count}] attempt {attempt+1}/{max_retries+1} {last_error}")
                    continue

                rc, ans, err = safe_run(
                    std_cmd,
                    stdin=inp,
                    timeout=other_timeout_s,
                    memory_limit_mb=mem_mb,
                )
                if rc != 0:
                    if rc == EXIT_MEMORY:
                        last_error = (
                            f"std MEMORY_LIMIT ({mem_mb} MB) type={try_typ}: "
                            f"标程超内存（检查数据规模或标程复杂度）"
                        )
                    else:
                        last_error = f"std FAILED type={try_typ}: {err.strip()}"
                    if not input_preview:
                        input_preview = inp[:800]
                    if verbose:
                        print(f"[{i+1}/{count}] attempt {attempt+1}/{max_retries+1} {last_error}")
                    continue

                if try_typ != typ:
                    if verbose:
                        print(f"[{i+1}/{count}] 降级成功：原类型 {typ} -> {try_typ} OK")
                if verbose:
                    print(f"[{i+1}/{count}] OK type={try_typ} in={len(inp)}B ans={len(ans)}B")
                return (i, True, inp, ans, "", "")

            error_log.append(f"type={try_typ} 在 {max_retries+1} 次尝试后失败: {last_error}")
            if verbose:
                print(f"[{i+1}/{count}] GIVE UP type={try_typ}")

        return (i, False, "", "", "\n".join(error_log), input_preview)

    # 只生成缺失组；可按阶段限制常规 / 特殊
    def _in_scope(i: int) -> bool:
        is_special_slot = special_count > 0 and i >= count - special_count
        if skip_special and is_special_slot:
            return False
        if only_special and not is_special_slot:
            return False
        return True

    todo = [i for i in range(count) if (i + 1) not in existing_pairs and _in_scope(i)]
    results = []
    if todo:
        max_workers = min(8, os.cpu_count() or 1, len(todo))
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {}
            for i in todo:
                typ = pick_type(i, count, edge_cases, special_count, scheme_schedule)
                # 降级类型：优先回退到 random，再尝试前几个 edge_case
                # 特殊样例不降级，避免用 random 冒充特殊约束
                if _is_special_type(typ):
                    fallback = []
                else:
                    fallback = ["random"] + [e for e in edge_cases[:3] if e != typ]
                futures[executor.submit(_gen_one, i, typ, fallback)] = i
            for fut in concurrent.futures.as_completed(futures):
                results.append(fut.result())

    # 按索引顺序落盘，保证 .in/.out 文件名顺序稳定
    results.sort(key=lambda x: x[0])
    failures = []
    for i, success, inp, ans, err, preview in results:
        if success:
            (out / f"{i+1}.in").write_text(inp, encoding="utf-8")
            (out / f"{i+1}.out").write_text(ans, encoding="utf-8")
            ok += 1
        else:
            bad += 1
            failures.append({
                "index": i + 1,
                "planned_type": pick_type(i, count, edge_cases, special_count, scheme_schedule),
                "error": err,
                "input_preview": preview,
            })

    elapsed = time.perf_counter() - t0
    # 统计已落盘测例体积（含复用组）
    in_bytes = 0
    out_bytes = 0
    for i in range(1, count + 1):
        ip = out / f"{i}.in"
        op = out / f"{i}.out"
        try:
            if ip.is_file():
                in_bytes += ip.stat().st_size
            if op.is_file():
                out_bytes += op.stat().st_size
        except OSError:
            pass
    return {
        "count": count,
        "ok": ok,
        "bad": bad,
        "reused": reused,
        "valid_rate": ok / count if count else 0,
        "elapsed_s": round(elapsed, 2),
        "failures": failures,
        "in_bytes": in_bytes,
        "out_bytes": out_bytes,
        "total_bytes": in_bytes + out_bytes,
    }
