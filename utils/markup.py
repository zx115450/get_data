"""题面 / 范围描述的格式处理：Markdown / HTML / LaTeX。

- to_plain_for_llm: 转成大模型易读的纯文本（去标签、展开常见 LaTeX）
- to_preview_html: 生成可在浏览器里渲染的 HTML（含 MathJax）
"""
from __future__ import annotations

import html as html_lib
import re
from html.parser import HTMLParser


class _HTMLTextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        t = tag.lower()
        if t in ("script", "style"):
            self._skip += 1
            return
        if self._skip:
            return
        if t in ("br", "hr"):
            self._parts.append("\n")
        elif t in ("p", "div", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6"):
            self._parts.append("\n")
        elif t == "td":
            self._parts.append("\t")

    def handle_endtag(self, tag):
        t = tag.lower()
        if t in ("script", "style") and self._skip:
            self._skip -= 1
            return
        if self._skip:
            return
        if t in ("p", "div", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table"):
            self._parts.append("\n")

    def handle_data(self, data):
        if not self._skip and data:
            self._parts.append(data)

    def text(self) -> str:
        return "".join(self._parts)


def looks_like_html(text: str) -> bool:
    if not text:
        return False
    return bool(re.search(r"</?[a-zA-Z][^>]*>", text))


def html_to_text(text: str) -> str:
    p = _HTMLTextExtractor()
    try:
        p.feed(text)
        p.close()
    except Exception:
        # 坏 HTML：粗暴去标签
        return re.sub(r"<[^>]+>", "", text)
    return p.text()


def _replace_frac(s: str) -> str:
    """反复展开 \\frac{a}{b} -> (a)/(b)。"""
    pat = re.compile(r"\\frac\s*\{([^{}]*)\}\s*\{([^{}]*)\}")
    for _ in range(20):
        ns, n = pat.subn(r"(\1)/(\2)", s)
        s = ns
        if n == 0:
            break
    return s


def latex_to_readable(text: str) -> str:
    """把常见 LaTeX / KaTeX 符号换成可读文本，方便 LLM。"""
    if not text:
        return text
    s = text
    # 块公式 / 行内公式定界符先拿掉，保留内容
    s = re.sub(r"\$\$([\s\S]*?)\$\$", r" \1 ", s)
    s = re.sub(r"(?<!\$)\$(?!\$)([^$\n]+?)\$(?!\$)", r" \1 ", s)
    s = re.sub(r"\\\(([\s\S]*?)\\\)", r" \1 ", s)
    s = re.sub(r"\\\[([\s\S]*?)\\\]", r" \1 ", s)

    s = _replace_frac(s)
    s = re.sub(r"\\sqrt\s*\{([^{}]*)\}", r"sqrt(\1)", s)
    s = re.sub(r"\\sqrt\s*(\w)", r"sqrt(\1)", s)
    s = re.sub(r"\\sum_\{([^{}]*)\}\^\{([^{}]*)\}", r"sum_{\1}^{\2}", s)
    s = re.sub(r"\\sum_\{([^{}]*)\}", r"sum_{\1}", s)
    s = re.sub(r"\\prod_\{([^{}]*)\}\^\{([^{}]*)\}", r"prod_{\1}^{\2}", s)
    s = re.sub(r"\\prod_\{([^{}]*)\}", r"prod_{\1}", s)
    s = re.sub(r"\\max_\{([^{}]*)\}", r"max_{\1}", s)
    s = re.sub(r"\\min_\{([^{}]*)\}", r"min_{\1}", s)

    repl = [
        (r"\\leqslant", "≤"), (r"\\geqslant", "≥"),
        (r"\\leq", "≤"), (r"\\leq", "≤"), (r"\\le\b", "≤"),
        (r"\\geq", "≥"), (r"\\ge\b", "≥"),
        (r"\\neq", "≠"), (r"\\ne\b", "≠"),
        (r"\\approx", "≈"), (r"\\equiv", "≡"),
        (r"\\times", "×"), (r"\\cdot", "·"), (r"\\div", "÷"),
        (r"\\pm", "±"), (r"\\mp", "∓"),
        (r"\\infty", "∞"), (r"\\emptyset", "∅"),
        (r"\\subseteq", "⊆"), (r"\\supseteq", "⊇"),
        (r"\\subset", "⊂"), (r"\\supset", "⊃"),
        (r"\\cup", "∪"), (r"\\cap", "∩"),
        (r"\\in\b", "∈"), (r"\\notin", "∉"),
        (r"\\forall", "∀"), (r"\\exists", "∃"),
        (r"\\rightarrow", "→"), (r"\\leftarrow", "←"),
        (r"\\Rightarrow", "⇒"), (r"\\Leftarrow", "⇐"),
        (r"\\leftrightarrow", "↔"),
        (r"\\ldots", "..."), (r"\\cdots", "..."), (r"\\dots", "..."),
        (r"\\log", "log"), (r"\\ln", "ln"), (r"\\lg", "lg"),
        (r"\\sin", "sin"), (r"\\cos", "cos"), (r"\\tan", "tan"),
        (r"\\bmod", " mod "), (r"\\mod", " mod "),
        (r"\\operatorname\{([^{}]*)\}", r"\1"),
        (r"\\text\{([^{}]*)\}", r"\1"),
        (r"\\mathrm\{([^{}]*)\}", r"\1"),
        (r"\\mathbf\{([^{}]*)\}", r"\1"),
        (r"\\textit\{([^{}]*)\}", r"\1"),
        (r"\\textbf\{([^{}]*)\}", r"\1"),
        (r"\\left", ""), (r"\\right", ""),
        (r"\\,", " "), (r"\\;", " "), (r"\\!", ""), (r"~", " "),
        (r"\\%", "%"), (r"\\_", "_"), (r"\\&", "&"),
        (r"\\#", "#"),
    ]
    for pat, rep in repl:
        s = re.sub(pat, rep, s)

    # 上下标 ^{}/_{} 简化
    s = re.sub(r"\^\{([^{}]*)\}", r"^(\1)", s)
    s = re.sub(r"_\{([^{}]*)\}", r"_(\1)", s)
    # 残留反斜杠命令：\alpha -> alpha
    s = re.sub(r"\\([a-zA-Z]+)", r"\1", s)
    # 多余花括号
    s = re.sub(r"\{([^{}]{1,40})\}", r"\1", s)
    return s


def markdown_to_plain(text: str) -> str:
    """轻量去 Markdown 标记，保留正文。"""
    s = text
    # fenced code：保留代码内容
    s = re.sub(r"```[\w]*\n([\s\S]*?)```", r"\n\1\n", s)
    s = re.sub(r"`([^`]+)`", r"\1", s)
    # 图片 / 链接
    s = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", s)
    s = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", s)
    # 标题 / 粗斜体 / 引用 / 列表
    s = re.sub(r"(?m)^#{1,6}\s*", "", s)
    s = re.sub(r"(\*\*|__)(.*?)\1", r"\2", s)
    s = re.sub(r"(\*|_)(.*?)\1", r"\2", s)
    s = re.sub(r"(?m)^>\s?", "", s)
    s = re.sub(r"(?m)^[\*\-+]\s+", "- ", s)
    s = re.sub(r"(?m)^\d+\.\s+", "", s)
    # 表格竖线弱化
    s = re.sub(r"(?m)^\s*\|", "", s)
    s = re.sub(r"\|", " | ", s)
    s = re.sub(r"(?m)^[\-\s|:]+$", "", s)
    return s


def _collapse_blank(s: str) -> str:
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = re.sub(r"[ \t]+\n", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def to_plain_for_llm(text: str) -> str:
    """题面/范围描述 → 纯文本，供 Agent / 关键词检测使用。"""
    if not text or not str(text).strip():
        return (text or "").strip()
    s = str(text)
    if looks_like_html(s):
        s = html_to_text(s)
    s = latex_to_readable(s)
    s = markdown_to_plain(s)
    # HTML 实体
    try:
        s = html_lib.unescape(s)
    except Exception:
        pass
    return _collapse_blank(s)


def _md_to_html_body(text: str) -> str:
    try:
        import markdown  # type: ignore
        return markdown.markdown(
            text,
            extensions=["fenced_code", "tables", "sane_lists"],
        )
    except Exception:
        # 无 markdown 库时：若已是 HTML 直接用，否则 <pre>
        if looks_like_html(text):
            return text
        return "<pre style='white-space:pre-wrap;font-family:Consolas,monospace'>" + html_lib.escape(text) + "</pre>"


def to_preview_html(text: str, title: str = "预览") -> str:
    """生成带 MathJax 的完整 HTML，可用浏览器打开渲染 MD/HTML/LaTeX。"""
    raw = text or ""
    if looks_like_html(raw) and re.search(r"<(html|body|div|p|h[1-6])\b", raw, re.I):
        body = raw
    else:
        body = _md_to_html_body(raw)
    title_esc = html_lib.escape(title)
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{title_esc}</title>
<style>
  body {{ font-family: "Segoe UI", "Microsoft YaHei", sans-serif; max-width: 900px;
         margin: 24px auto; padding: 0 16px; line-height: 1.6; color: #222; }}
  pre, code {{ font-family: Consolas, "Courier New", monospace; }}
  pre {{ background: #f6f8fa; padding: 12px; overflow-x: auto; border-radius: 6px; }}
  table {{ border-collapse: collapse; }}
  th, td {{ border: 1px solid #ddd; padding: 6px 10px; }}
  h1,h2,h3 {{ border-bottom: 1px solid #eee; padding-bottom: 4px; }}
</style>
<script>
window.MathJax = {{
  tex: {{ inlineMath: [['$','$'], ['\\\\(','\\\\)']], displayMath: [['$$','$$'], ['\\\\[','\\\\]']] }},
  svg: {{ fontCache: 'global' }}
}};
</script>
<script defer src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-svg.js"></script>
</head>
<body>
{body}
</body>
</html>
"""
