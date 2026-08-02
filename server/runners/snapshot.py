"""通过校验的产物快照与增量测例合并。"""
import os
import shutil
from pathlib import Path


def detect_artifacts(job_dir: Path) -> list[str]:
    """列出 job_dir 下可复用的产物文件名。"""
    candidates = [
        "range.json",
        "gen.cpp", "gen_special.cpp", "gen.py",
        "validator.cpp", "validate.py",
        "checker.cpp",
    ]
    found = []
    for name in candidates:
        if (job_dir / name).is_file():
            found.append(name)
    if os.name == "nt":
        for name in ("gen.exe", "gen_special.exe", "validator.exe", "checker.exe"):
            if (job_dir / name).is_file():
                found.append(name)
    return found


def detect_good_artifacts(job_dir: Path) -> list[str]:
    """列出通过校验的"好版本"产物（含 out.good 测例快照）。"""
    found = []
    for name in ("gen.cpp", "gen_special.cpp", "gen.py", "validator.cpp", "validate.py"):
        good = name + ".good"
        if (job_dir / good).is_file():
            found.append(good)
    for name in ("gen.exe", "gen_special.exe", "validator.exe"):
        good = name + ".good"
        if (job_dir / good).is_file():
            found.append(good)
    if (job_dir / "out.good").is_dir():
        found.append("out.good")
    return found


def pair_indices_in_dir(data_dir: Path) -> set[int]:
    """扫描目录里成对的 {i}.in + {i}.out，返回已完整的索引集合（1-based）。"""
    if not data_dir.is_dir():
        return set()
    ins = set()
    outs = set()
    for f in data_dir.iterdir():
        if not f.is_file():
            continue
        name = f.name
        if name.endswith(".in"):
            stem = name[:-3]
            if stem.isdigit():
                ins.add(int(stem))
        elif name.endswith(".out"):
            stem = name[:-4]
            if stem.isdigit():
                outs.add(int(stem))
    return ins & outs


def save_good_snapshot(job_dir: Path, include_in_out: bool = False, suffix: str = ".good") -> None:
    """把当前 gen/validator 保存为带后缀的快照；默认 .good，可选合并 out → out.good。"""
    for name in ("gen.cpp", "gen_special.cpp", "gen.py", "validator.cpp", "validate.py"):
        src = job_dir / name
        if src.is_file():
            shutil.copy2(src, job_dir / (name + suffix))
    if os.name == "nt":
        for name in ("gen.exe", "gen_special.exe", "validator.exe"):
            src = job_dir / name
            if src.is_file():
                shutil.copy2(src, job_dir / (name + suffix))
    if include_in_out:
        merge_out_into_good(job_dir)


def merge_out_into_good(job_dir: Path) -> int:
    """把 out/ 里成对的合法测例合并进 out.good/，返回合并的组数。"""
    out_dir = job_dir / "out"
    good_dir = job_dir / "out.good"
    good_dir.mkdir(parents=True, exist_ok=True)
    merged = 0
    for i in pair_indices_in_dir(out_dir):
        for suffix in (".in", ".out"):
            src = out_dir / f"{i}{suffix}"
            dst = good_dir / f"{i}{suffix}"
            if src.is_file():
                shutil.copy2(src, dst)
        merged += 1
    return merged


def restore_good_snapshot(job_dir: Path, suffix: str = ".good") -> None:
    """把带后缀的快照还原为正式产物（源码 + 已合法测例）；默认 .good。"""
    for name in ("gen.cpp", "gen_special.cpp", "gen.py", "validator.cpp", "validate.py"):
        good = job_dir / (name + suffix)
        if good.is_file():
            shutil.copy2(good, job_dir / name)
    if os.name == "nt":
        for name in ("gen.exe", "gen_special.exe", "validator.exe"):
            good = job_dir / (name + suffix)
            if good.is_file():
                shutil.copy2(good, job_dir / name)
    good_dir = job_dir / "out.good"
    out_dir = job_dir / "out"
    if good_dir.is_dir():
        out_dir.mkdir(parents=True, exist_ok=True)
        for i in pair_indices_in_dir(good_dir):
            for suffix in (".in", ".out"):
                src = good_dir / f"{i}{suffix}"
                if src.is_file():
                    shutil.copy2(src, out_dir / f"{i}{suffix}")


def has_complete_in_out(job_dir: Path, range_json: dict, *, prefer_good: bool = False) -> bool:
    """out/（或 out.good）是否已有完整的 count 组成对测例。"""
    count = int(range_json.get("count") or 15)
    data_dir = job_dir / ("out.good" if prefer_good else "out")
    pairs = pair_indices_in_dir(data_dir)
    return all(i in pairs for i in range(1, count + 1))


def has_gen_val_at_resume(job_dir: Path) -> bool:
    """检查当前目录是否已有 gen + validator 产物。

    gen_special 由后续 SpecialCoder 阶段产出，续跑 checker 时不要求已有。
    """
    _exe = lambda b: b + (".exe" if os.name == "nt" else "")
    has_gen = (job_dir / _exe("gen")).exists() or (job_dir / "gen.py").exists() or (job_dir / "gen.cpp").exists()
    has_val = (job_dir / _exe("validator")).exists() or (job_dir / "validate.py").exists() or (job_dir / "validator.cpp").exists()
    return has_gen and has_val
