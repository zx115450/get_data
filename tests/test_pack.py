"""pipeline/pack：zip 内容完整性。"""
from __future__ import annotations

import zipfile
from pathlib import Path

from pipeline.pack import pack, pack_sources


class TestPack:
    def test_pack_in_out_only(self, tmp_path: Path):
        out = tmp_path / "out"
        out.mkdir()
        (out / "1.in").write_text("1\n", encoding="utf-8")
        (out / "1.out").write_text("2\n", encoding="utf-8")
        (out / "notes.txt").write_text("skip", encoding="utf-8")
        zpath = tmp_path / "data.zip"
        pack(str(out), str(zpath), meta={"x": 1})
        with zipfile.ZipFile(zpath) as z:
            names = set(z.namelist())
        assert names == {"1.in", "1.out"}
        assert "notes.txt" not in names
        assert "meta.json" not in names

    def test_pack_sources(self, tmp_path: Path):
        work = tmp_path / "job"
        work.mkdir()
        (work / "range.json").write_text("{}", encoding="utf-8")
        (work / "gen.cpp").write_text("int main(){}", encoding="utf-8")
        (work / "validator.cpp").write_text("int main(){}", encoding="utf-8")
        zpath = tmp_path / "sources.zip"
        pack_sources(str(work), str(zpath))
        with zipfile.ZipFile(zpath) as z:
            names = set(z.namelist())
        assert "range.json" in names
        assert "gen.cpp" in names
        assert "validator.cpp" in names
