"""把 out/ 下的 .in/.out 打包成 zip（只含测例文件，不含 meta.json）。

同时支持：
- checker.zip：special judge / 内置 checker 产物
- sources.zip：gen/validator/range 等源码产物（方便改生成器）
"""
import os
import zipfile
from pathlib import Path


def _exe(base: str) -> str:
    return base + (".exe" if os.name == "nt" else "")


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

    包含：checker.cpp、编译好的 checker 二进制、testlib.h（若存在）。
    """
    work = Path(work_dir)
    checker_zip_path = Path(checker_zip_path)
    checker_zip_path.parent.mkdir(parents=True, exist_ok=True)

    candidates = [
        work / "checker.cpp",
        work / _exe("checker"),
        work / "testlib.h",
    ]

    with zipfile.ZipFile(checker_zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for f in candidates:
            if f.exists() and f.is_file():
                z.write(f, f.name)

    return str(checker_zip_path)


def pack_sources(work_dir: str, sources_zip_path: str) -> str:
    """打包生成器/校验器源码与 range.json，便于二次修改。

    尽量包含：range.json、gen.cpp、validator.cpp、checker.cpp（若有）、
    testlib.h / generator.h（若有）。不含 .exe 与测例。
    """
    work = Path(work_dir)
    sources_zip_path = Path(sources_zip_path)
    sources_zip_path.parent.mkdir(parents=True, exist_ok=True)

    names = [
        "range.json",
        "gen.cpp",
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
            f = work / name
            if f.exists() and f.is_file():
                z.write(f, f.name)

    return str(sources_zip_path)
