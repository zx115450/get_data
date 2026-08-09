"""按 gitignore 打包项目源码（git ls-files -c -o --exclude-standard）。"""
from __future__ import annotations

import subprocess
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# 额外排除：异常目录名 / 本脚本产物
EXTRA_EXCLUDE_PREFIXES = ("{4}",)


def list_pack_paths() -> list[str]:
    raw = subprocess.check_output(
        ["git", "ls-files", "-c", "-o", "--exclude-standard", "-z"],
        cwd=ROOT,
    )
    out: list[str] = []
    for p in raw.decode("utf-8", errors="replace").split("\0"):
        if not p:
            continue
        norm = p.replace("\\", "/")
        if norm.endswith(".zip"):
            continue
        if any(norm == pref or norm.startswith(pref + "/") for pref in EXTRA_EXCLUDE_PREFIXES):
            continue
        out.append(p)
    return out


def main() -> None:
    for old in ROOT.glob("get_data_src_*.zip"):
        old.unlink()
        print("removed", old.name)

    paths = list_pack_paths()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = ROOT / f"get_data_src_{stamp}.zip"

    written = 0
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for rel in paths:
            fp = ROOT / rel
            if not fp.is_file():
                continue
            zf.write(fp, arcname=rel.replace("\\", "/"))
            written += 1

    mb = out.stat().st_size / (1024 * 1024)
    print(f"out={out}")
    print(f"files={written} size_mb={mb:.2f}")
    with zipfile.ZipFile(out) as zf:
        tops = sorted({n.split("/")[0] for n in zf.namelist()})
    print("top:", ", ".join(tops))


if __name__ == "__main__":
    main()
