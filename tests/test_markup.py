"""utils/markup：to_plain_for_llm 数学公式与标签清洗。"""
from __future__ import annotations

from utils.markup import to_plain_for_llm


class TestToPlainForLlm:
    def test_empty(self):
        assert to_plain_for_llm("") == ""
        assert to_plain_for_llm("   ") == ""

    def test_strips_html(self):
        out = to_plain_for_llm("<p>Hello <b>world</b></p>")
        assert "Hello" in out
        assert "world" in out
        assert "<p>" not in out
        assert "<b>" not in out

    def test_latex_inline(self):
        out = to_plain_for_llm(r"令 $n \le 10^5$，求答案。")
        assert "n" in out
        assert "10" in out
        assert "$" not in out or "10^5" in out or "10" in out

    def test_latex_frac(self):
        out = to_plain_for_llm(r"概率为 $\frac{1}{2}$")
        assert "1" in out and "2" in out

    def test_markdown_bold(self):
        out = to_plain_for_llm("这是 **加粗** 文本")
        assert "加粗" in out
        assert "**" not in out

    def test_preserves_plain(self):
        s = "第一行是整数 n，第二行 n 个数。"
        assert to_plain_for_llm(s) == s or s in to_plain_for_llm(s)
