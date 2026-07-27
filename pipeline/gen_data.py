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


def _gen_cmd(work: Path) -> str:
    """优先用编译好的 gen 二进制，否则回退 python gen.py。"""
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
      - time_limit_ms、memory_limit_mb（正整数；缺省表示不限制内存）
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


def normalize_range_json(rj: dict) -> dict:
    """清洗 range.json：去掉 edge_cases 里的 'random'（系统会自动补），去重保序；count 缺省补 15。"""
    if not isinstance(rj, dict):
        return rj
    if "count" not in rj or rj.get("count") in (None, 0):
        rj["count"] = 15
    ec = rj.get("edge_cases")
    if isinstance(ec, list):
        seen = set()
        cleaned = []
        for e in ec:
            if not isinstance(e, str) or not e or e == "random":
                continue
            if e not in seen:
                seen.add(e)
                cleaned.append(e)
        rj["edge_cases"] = cleaned
    return rj


def pick_type(i: int, count: int, edge_cases: list) -> str:
    """前几组按顺序覆盖边界类型，其余 random；始终保留至少 count - len(edge_cases) 个随机组。"""
    edge_cases = list(edge_cases or [])
    random_min = max(1, count - len(edge_cases))  # 最少要保留这么多 random 组
    edge_max = min(len(edge_cases), max(0, count - random_min))
    if i < edge_max:
        return edge_cases[i]
    return "random"


def generate(range_json: dict, work_dir: str, out_dir: str, verbose: bool = True) -> dict:
    """按 range.json 批量生成测例，返回统计信息。"""
    range_json = normalize_range_json(dict(range_json))  # 拷贝后再洗，避免改调用方
    errs = validate_range_json(range_json)
    if errs:
        raise ValueError("range.json 不合法:\n" + "\n".join(f"  - {e}" for e in errs))

    work = Path(work_dir)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    gen_cmd = _gen_cmd(work)
    validator_cmd = _validator_cmd(work)
    std_cmd = range_json["std_cmd"]
    count = range_json["count"]
    edge_cases = range_json.get("edge_cases", [])
    # gen 单独硬限 5s：超过即视为算法不达标（O(n^2) 枚举等），直接判该组失败，
    # 不让慢生成器拖垮整批；validate/std 仍按 range.json 的 time_limit_ms。
    gen_timeout_s = 5
    other_timeout_s = max(range_json.get("time_limit_ms", 1000) / 1000, 1)
    mem_mb = parse_memory_limit_mb(range_json)
    if verbose and mem_mb:
        print(f"memory_limit_mb={mem_mb}（作用于 gen / validator / std）")

    ok = 0
    bad = 0
    t0 = time.perf_counter()
    max_retries = 3  # 同类型失败后最多重试几次

    def _gen_one(i: int, typ: str, fallback_types: list[str]):
        """生成第 i 组测例（含重试 + 降级类型），返回 (i, success, inp, ans, error_log)。"""
        types_to_try = [typ] + [t for t in fallback_types if t and t != typ]
        error_log = []
        for try_typ in types_to_try:
            last_error = ""
            for attempt in range(max_retries + 1):
                # 重试时换 seed，避免命中同一个随机坏点；index/count 仍保持当前组号
                seed = i + attempt * count
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
                    if verbose:
                        print(f"[{i+1}/{count}] attempt {attempt+1}/{max_retries+1} {last_error}")
                    continue

                if try_typ != typ:
                    if verbose:
                        print(f"[{i+1}/{count}] 降级成功：原类型 {typ} -> {try_typ} OK")
                if verbose:
                    print(f"[{i+1}/{count}] OK type={try_typ} in={len(inp)}B ans={len(ans)}B")
                return (i, True, inp, ans, "")

            error_log.append(f"type={try_typ} 在 {max_retries+1} 次尝试后失败: {last_error}")
            if verbose:
                print(f"[{i+1}/{count}] GIVE UP type={try_typ}")

        return (i, False, "", "", "\n".join(error_log))

    # 并行生成，默认最多 8 个并发，避免外部标程互相抢 CPU
    max_workers = min(8, os.cpu_count() or 1, count)
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {}
        for i in range(count):
            typ = pick_type(i, count, edge_cases)
            # 降级类型：优先回退到 random，再尝试前几个 edge_case
            fallback = ["random"] + [e for e in edge_cases[:3] if e != typ]
            futures[executor.submit(_gen_one, i, typ, fallback)] = i
        for fut in concurrent.futures.as_completed(futures):
            results.append(fut.result())

    # 按索引顺序落盘，保证 .in/.out 文件名顺序稳定
    results.sort(key=lambda x: x[0])
    failures = []
    for i, success, inp, ans, err in results:
        if success:
            (out / f"{i+1}.in").write_text(inp, encoding="utf-8")
            (out / f"{i+1}.out").write_text(ans, encoding="utf-8")
            ok += 1
        else:
            bad += 1
            failures.append({"index": i + 1, "planned_type": pick_type(i, count, edge_cases), "error": err})

    elapsed = time.perf_counter() - t0
    return {
        "count": count,
        "ok": ok,
        "bad": bad,
        "valid_rate": ok / count if count else 0,
        "elapsed_s": round(elapsed, 2),
        "failures": failures,
    }
