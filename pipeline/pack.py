"""把 out/ 下的 .in/.out 打包成 zip（只含测例文件，不含 meta.json）。

同时支持：
- checker.zip：special judge / 内置 checker 产物
- sources.zip：gen/validator/range 等源码产物（方便改生成器）
"""
import os
import zipfile
from pathlib import Path

_SANDBOX = Path(__file__).resolve().parent.parent / "sandbox"


def _exe(base: str) -> str:
    return base + (".exe" if os.name == "nt" else "")


def _resolve_pack_file(work: Path, name: str) -> Path | None:
    """优先用 job 目录文件；头文件可回退到公共 sandbox。"""
    local = work / name
    if local.is_file():
        return local
    if name in ("testlib.h", "generator.h"):
        shared = _SANDBOX / name
        if shared.is_file():
            return shared
    return None


def pack(out_dir: str, zip_path: str, meta: dict = None) -> str:
    """打包 out_dir 下的 .in/.out 文件到 zip_path，返回 zip 路径。

    zip 内是扁平结构：1.in 1.out 2.in 2.out ...，不含 meta.json、不含外层目录。
    meta 参数保留以兼容旧调用，但不再写入 zip。
    """
    out = Path(out_dir)
    zip_path = Path(zip_path)
    zip_path.parent.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(out.glob("*")):
            if f.is_file() and f.suffix in (".in", ".out"):
                z.write(f, f.name)

    return str(zip_path)


def pack_checker(work_dir: str, checker_zip_path: str) -> str:
    """把 work_dir 下的 checker 相关产物打包成 checker.zip，返回 zip 路径。

    包含：checker.cpp、编译好的 checker 二进制、testlib.h（job 无则取 sandbox）。
    """
    work = Path(work_dir)
    checker_zip_path = Path(checker_zip_path)
    checker_zip_path.parent.mkdir(parents=True, exist_ok=True)

    names = ["checker.cpp", _exe("checker"), "testlib.h"]
    with zipfile.ZipFile(checker_zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for name in names:
            f = _resolve_pack_file(work, name)
            if f is not None:
                z.write(f, name)

    return str(checker_zip_path)


def pack_sources(work_dir: str, sources_zip_path: str) -> str:
    """打包生成器/校验器源码与 range.json，便于二次修改。

    尽量包含：range.json、gen.cpp、validator.cpp、checker.cpp（若有）、
    testlib.h / generator.h（job 无则从 sandbox 取）。不含 .exe 与测例。
    """
    work = Path(work_dir)
    sources_zip_path = Path(sources_zip_path)
    sources_zip_path.parent.mkdir(parents=True, exist_ok=True)

    names = [
        "range.json",
        "gen.cpp",
        "gen_special.cpp",
        "gen.py",
        "validator.cpp",
        "validate.py",
        "checker.cpp",
        "testlib.h",
        "generator.h",
        "std.cpp",
        "std.py",
    ]
    with zipfile.ZipFile(sources_zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for name in names:
            f = _resolve_pack_file(work, name)
            if f is not None:
                z.write(f, name)
        # Finder 留痕（若有）
        findings = work / "special_findings"
        if findings.is_dir():
            for f in findings.rglob("*"):
                if f.is_file() and f.suffix.lower() in {".cpp", ".json", ".in", ".txt", ".md"}:
                    z.write(f, f.relative_to(work).as_posix())

    return str(sources_zip_path)
