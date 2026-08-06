"""Checker / SPJ 相关工具。"""
import shutil

from sandbox.run import safe_run
from pipeline.gen_data import DEFAULT_REGULAR_COUNT

from .context import (
    BUILTIN_CHECKERS,
    CHECKER_SRC_DIR,
    CHECKER_TEMPLATE_DIR,
    CHECKER_TEMPLATES,
    CHECKER_TIMEOUT_S,
    _compile_cpp,
    _compile_err,
    _exe,
    _load_range_json,
    _looks_like_omitted_stub,
    _resolve_std_cmd,
    _wd,
)
from .run import _run_gen_raw, _run_std_raw

def _invalidate_checker_exe() -> None:
    """删除 checker 可执行文件，避免编译失败后仍跑旧模板/旧二进制。"""
    exe = _wd() / _exe("checker")
    try:
        if exe.exists():
            exe.unlink()
    except OSError:
        pass


def _checker_exe_stale_or_placeholder() -> str | None:
    """若 checker 源码与 exe 不一致或仍是未替换模板，返回 SYSTEM 原因；否则 None。"""
    wd = _wd()
    src = wd / "checker.cpp"
    exe = wd / _exe("checker")
    if not exe.exists():
        return "checker 未编译（无可执行文件）"
    if not src.exists():
        return None
    try:
        if src.stat().st_mtime > exe.stat().st_mtime + 0.05:
            return (
                "checker.cpp 新于 checker 可执行文件（上次编译失败后残留旧 exe 已失效或未重编）"
            )
    except OSError:
        pass
    try:
        body = src.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    markers = (
        "TODO: replace template placeholder",
        "accepted by template placeholder",
        "template placeholder",
    )
    if any(m in body for m in markers):
        return "checker.cpp 仍是未替换的模板占位（quitf(_fail/placeholder)），请 write_checker 写入完整判定逻辑"
    return None


def write_checker(content: str) -> str:
    """把 special judge 的 C++ 源码写到 work_dir/checker.cpp，用 -I sandbox 编译成 checker(.exe)。"""
    if _looks_like_omitted_stub(content):
        return (
            "ERROR: content 像是历史摘要，不是完整 checker.cpp。"
            "请重新输出完整 C++ 源码（#include \"testlib.h\" + registerTestlibCmd）。"
            "若需查看上一版，先 read_file(\"checker.cpp\")。"
        )
    if "registerTestlibCmd" not in content and "testlib.h" not in content:
        return "ERROR: checker.cpp 必须 #include \"testlib.h\" 并调用 registerTestlibCmd(...)"
    (_wd() / "checker.cpp").write_text(content, encoding="utf-8")
    rc, out, err = _compile_cpp("checker.cpp", "checker", timeout=60)
    if rc != 0:
        # 编译失败时清掉旧 exe，防止自检误跑模板/上一版并报假 PE/LOGIC
        _invalidate_checker_exe()
        return _compile_err("checker.cpp", rc, out, err)
    return f"OK: wrote & compiled checker ({len(content)} chars)"


def use_builtin_checker(name: str) -> str:
    """使用内置 testlib checker（lcmp/wcmp/rcmp4/rcmp6/rcmp9/yesno），编译为 checker(.exe)。

    适用于答案唯一、只需按行/词/浮点/YesNo 比较的题目；答案不唯一时请改用 write_checker。
    """
    name = (name or "").strip().lower()
    if name not in BUILTIN_CHECKERS:
        return (
            f"ERROR: 未知内置 checker {name!r}，"
            f"可选: {', '.join(BUILTIN_CHECKERS)}"
        )
    src = CHECKER_SRC_DIR / f"{name}.cpp"
    if not src.exists():
        return f"ERROR: 缺少内置 checker 源码 {src}"
    dst_src = _wd() / "checker.cpp"
    shutil.copyfile(src, dst_src)
    # 在源码顶部标注来源，方便打包识别
    note = f"// builtin checker: {name} (from sandbox/checker/{name}.cpp)\n"
    body = dst_src.read_text(encoding="utf-8")
    if not body.lstrip().startswith("// builtin checker:"):
        dst_src.write_text(note + body, encoding="utf-8")
    rc, out, err = _compile_cpp("checker.cpp", "checker", timeout=60)
    if rc != 0:
        return _compile_err(f"builtin checker {name}", rc, out, err)
    return f"OK: installed builtin checker {name} -> checker"


def use_checker_template(name: str) -> str:
    """使用 checker 模板生成骨架 checker.cpp 并编译。

    骨架以 quitf(_fail, \"TODO: replace template placeholder\") 占位，
    不可直接用于自检；模型必须再用 write_checker 替换完整判定逻辑。
    """
    name = (name or "").strip().lower()
    if name not in CHECKER_TEMPLATES:
        return (
            f"ERROR: 未知 checker 模板 {name!r}，"
            f"可选: {', '.join(CHECKER_TEMPLATES)}"
        )
    src = CHECKER_TEMPLATE_DIR / f"{name}.cpp"
    if not src.exists():
        return f"ERROR: 缺少 checker 模板源码 {src}"
    dst = _wd() / "checker.cpp"
    shutil.copyfile(src, dst)
    rc, out, err = _compile_cpp("checker.cpp", "checker", timeout=60)
    if rc != 0:
        _invalidate_checker_exe()
        return _compile_err(f"checker template {name}", rc, out, err)
    return f"OK: installed checker template {name} -> checker.cpp ({len(src.read_text(encoding='utf-8'))} chars)"


def run_checker(input_text: str, output_text: str, answer_text: str) -> str:
    """运行编译好的 checker，判定 output_text 相对 answer_text 是否正确。

    三个参数分别对应 inf / ouf / ans 的内容。会写临时文件、调用 checker、清理临时文件。
    返回包含退出码、stdout、stderr 的摘要。
    """
    exe = _wd() / _exe("checker")
    if not exe.exists():
        return "ERROR: checker 未编译，请先 write_checker 或 use_builtin_checker / use_checker_template"

    inf = _wd() / "_checker_run_inf.txt"
    ouf = _wd() / "_checker_run_ouf.txt"
    ans = _wd() / "_checker_run_ans.txt"
    try:
        inf.write_text(input_text, encoding="utf-8")
        ouf.write_text(output_text, encoding="utf-8")
        ans.write_text(answer_text, encoding="utf-8")

        # 绝对路径，避免 cwd/相对路径导致 testlib 报 Output file not found
        cmd = (
            f'"{exe.resolve()}" '
            f'"{inf.resolve()}" "{ouf.resolve()}" "{ans.resolve()}"'
        )
        rc, out, err = safe_run(cmd, timeout=CHECKER_TIMEOUT_S, cwd=str(_wd()))
        out_s = (out or "").strip()
        err_s = (err or "").strip()
        # safe_run 超时常见：rc 非 0 且 stderr/stdout 含 TIMEOUT
        timed_out = (
            "TIMEOUT" in (out or "").upper()
            or "TIMEOUT" in (err or "").upper()
            or "超时" in (err or "")
            or "超时" in (out or "")
        )
        if timed_out:
            return (
                f"checker TIMEOUT after {CHECKER_TIMEOUT_S}s (SPJ 复杂度超规：设计≤1s / 硬超时{CHECKER_TIMEOUT_S}s)\n"
                f"exit_code={rc}\n"
                f"stdout: {out_s[:500]}{'...' if len(out_s) > 500 else ''}\n"
                f"stderr: {err_s[:1500]}{'...' if len(err_s) > 1500 else ''}"
            )
        return (
            f"checker exit_code={rc}\n"
            f"stdout: {out_s[:500]}{'...' if len(out_s) > 500 else ''}\n"
            f"stderr: {err_s[:1500]}{'...' if len(err_s) > 1500 else ''}"
        )
    finally:
        for p in (inf, ouf, ans):
            try:
                p.unlink(missing_ok=True)
            except Exception:
                pass


def _mutate_output(output: str) -> str:
    """对选手输出做轻微扰动，生成一个应该被判错的负例。"""
    if not output.strip():
        return "wrong\n"
    lines = output.splitlines()
    if not lines:
        return output + "x"
    last = lines[-1]
    if last:
        # 改最后一个 token 的末尾字符
        lines[-1] = last[:-1] if len(last) > 1 else last + "x"
    return "\n".join(lines) + "\n"


def _checker_fail_kind(text: str) -> str:
    """区分 checker 自检失败：SYSTEM（环境/标程）vs LOGIC（判定逻辑）。"""
    t = (text or "").lower()
    system_markers = (
        "找不到指定的路径",
        "no such file",
        "cannot find",
        "the system cannot find",
        "std failed",
        "gen failed",
        "未编译",
        "标程不可用",
        "not found",
        "errno 2",
        "winerror 2",
        "新于 checker",
        "残留旧",
        "未替换的模板",
        "template placeholder",
        "replace template",
    )
    if any(m in t for m in system_markers):
        return "SYSTEM"
    return "LOGIC"


def run_checker_self_check(count: int = 3) -> str:
    """用当前 gen + std + checker 做 reactive 自检。

    正例：用 gen 生成输入，std 跑出答案，把答案同时当 ouf/ans 跑 checker，必须返回 _ok。
    负例：把答案轻微扰动后当 ouf 跑 checker，必须返回非 _ok（优先 _wa；_pe 也可）。
    模板占位 / 源码新于 exe / 未编译 → [SYSTEM]。全部通过返回 OK。
    """
    exe = _wd() / _exe("checker")
    if not exe.exists():
        return (
            "ERROR: checker_self_check failed [SYSTEM]\n"
            "checker 未编译，请先 write_checker 或 use_builtin_checker / use_checker_template"
        )
    stale = _checker_exe_stale_or_placeholder()
    if stale:
        return (
            f"ERROR: checker_self_check failed [SYSTEM]\n"
            f"{stale}\n"
            "请 write_checker 写出完整可编译判定逻辑后再自检（勿用未替换模板跑自检）。"
        )
    if not (_wd() / _exe("gen")).exists():
        return (
            "ERROR: checker_self_check failed [SYSTEM]\n"
            "gen 未编译，无法为 checker 生成测试输入"
        )
    if not _resolve_std_cmd():
        return (
            "ERROR: checker_self_check failed [SYSTEM]\n"
            "标程不可用，无法为 checker 生成标准答案"
        )

    rj = _load_range_json()
    edge_cases = list(rj.get("edge_cases") or [])
    total_count = max(1, int(rj.get("count") or DEFAULT_REGULAR_COUNT))

    # 优先小规模 edge，避免 edge_nmax 导致标程输出爆炸拖垮自检
    checks: list[tuple[str, int, int, bool]] = []
    small_edges = [
        e for e in edge_cases
        if e in ("edge_n1", "edge_n2", "edge_T1")
        or e.endswith("_n1")
        or e.endswith("_n2")
        or "n1" in e
        or "n2" in e
    ]
    for i, name in enumerate(small_edges[:2]):
        checks.append((name, 2100 + i, 0, True))
    # 再补一个低档 random（index=0）
    checks.append(("random", 2000, 0, True))
    if not small_edges and edge_cases:
        # 无小 edge 时取第一个非 nmax 的
        for e in edge_cases:
            if "nmax" in e.lower() or "max" == e.lower():
                continue
            checks.append((e, 2002, 0, True))
            break
    # 负例：优先小 edge
    neg_type = small_edges[0] if small_edges else "random"
    checks.append((neg_type, 3000, 0, False))
    # 去重保序
    seen_keys = set()
    uniq_checks = []
    for c in checks:
        key = (c[0], c[1], c[3])
        if key in seen_keys:
            continue
        seen_keys.add(key)
        uniq_checks.append(c)
    checks = uniq_checks[: max(3, count + 1)]

    lines = [f"checker_self_check start: count={count} planned_checks={len(checks)}"]
    fails = []
    for typ, seed, idx, is_positive in checks:
        gen_out = _run_gen_raw(seed, typ, idx, total_count)
        if gen_out.startswith("ERROR"):
            lines.append(f"FAIL gen seed={seed} type={typ}: {gen_out[:200]}")
            fails.append((typ, seed, "gen failed"))
            continue

        std_out = _run_std_raw(gen_out)
        if std_out.startswith("ERROR"):
            lines.append(f"FAIL std seed={seed} type={typ}: {std_out[:200]}")
            fails.append((typ, seed, "std failed"))
            continue

        ouf = std_out if is_positive else _mutate_output(std_out)
        result = run_checker(gen_out, ouf, std_out)
        timed_out = "TIMEOUT" in (result or "").upper() or "复杂度超规" in (result or "")
        ok = (
            not timed_out
            and (result.startswith("checker exit_code=0") or "_ok" in result)
        )
        status = "OK" if ok else "NOT_OK"
        expected = "expected _ok" if is_positive else "expected _wa or _pe"
        if timed_out:
            expected = (
                f"expected finish within {CHECKER_TIMEOUT_S}s "
                f"(SPJ 设计≤1s；超时=复杂度超规)"
            )
        lines.append(f"{status} type={typ} seed={seed} positive={is_positive} {expected}\n{result[:400]}")
        if timed_out:
            fails.append((typ, seed, "checker TIMEOUT / complexity exceeded"))
        elif is_positive and not ok:
            fails.append((typ, seed, "positive case rejected by checker"))
        elif not is_positive and ok:
            fails.append((typ, seed, "negative case accepted by checker"))

    if fails:
        detail = "\n".join(lines)
        kind = _checker_fail_kind(detail)
        if kind == "SYSTEM":
            hint = (
                "【SYSTEM】环境/标程/生成器问题，不是 checker 逻辑。"
                "不要 rewrite checker；请直接再调用 run_checker_self_check()。"
                "若仍 SYSTEM，finish 并说明环境失败（不计入 write_checker 次数）。"
            )
        else:
            hint = (
                "【LOGIC】checker 判定逻辑有误或复杂度超规（设计≤1s / 硬超时 "
                f"{CHECKER_TIMEOUT_S}s）。"
                "请根据失败信息修复 checker.cpp 后重新 write_checker，再 run_checker_self_check。"
                "超时须改用线性/近线性验证，禁止 MITM/指数/大 N 的 N^2。"
            )
        return (
            f"ERROR: checker_self_check failed [{kind}]\n"
            + detail
            + "\n"
            + hint
        )
    return "OK: checker_self_check passed\n" + "\n".join(lines)
