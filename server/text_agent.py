"""题面 / 输入描述的简化与美化（大模型改写）。"""
from __future__ import annotations

import re

from agent.llm import chat_text
from utils.markup import to_plain_for_llm

_KIND_LABEL = {
    "statement": "题面",
    "range": "数据范围 / 输入描述",
}


def _strip_markup_symbols(text: str) -> str:
    """简化结果再清一层：去掉残留 $, \\cmd、markdown 标记，方便人读和模型读。"""
    s = to_plain_for_llm(text)
    s = re.sub(r"`+", "", s)
    s = re.sub(r"[*_~]{1,3}", "", s)
    s = re.sub(r"\$+", "", s)
    s = re.sub(r"\\[a-zA-Z]+", "", s)
    s = re.sub(r"[{}]", "", s)
    s = re.sub(r"[ \t]+\n", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def simplify_text(text: str, kind: str = "statement", extra_hint: str = "") -> str:
    """markup 清洗后，再让 LLM 总结成无符号的纯文本。"""
    kind = kind if kind in _KIND_LABEL else "statement"
    label = _KIND_LABEL[kind]
    plain = to_plain_for_llm(text or "")
    if not plain.strip():
        raise ValueError("内容为空，无法简化")

    system = (
        f"你是算法竞赛题面编辑。把用户给的「{label}」改写成清晰、简短的【纯文本】。"
        "要求：\n"
        "1. 不改变题意、输入输出格式、约束与边界含义。\n"
        "2. 禁止使用 Markdown、HTML、LaTeX、美元符、反斜杠命令、公式环境。\n"
        "3. 用中文书面语；数字范围写成「1 到 1e5」或「[1, 100000]」这类普通字符。\n"
        "4. 只输出改写后的正文，不要前言、标题装饰或解释。\n"
    )
    user = f"【原始{label}（已初步去标签）】\n{plain}\n"
    if extra_hint and extra_hint.strip():
        user += f"\n【用户额外要求】\n{extra_hint.strip()}\n"
    out = chat_text(system, user, temperature=0.2)
    return _strip_markup_symbols(out)


def beautify_text(text: str, kind: str = "statement", extra_hint: str = "") -> str:
    """不改变题意，改写成带 Markdown（可用 LaTeX）的美观版本。"""
    kind = kind if kind in _KIND_LABEL else "statement"
    label = _KIND_LABEL[kind]
    src = (text or "").strip()
    if not src:
        raise ValueError("内容为空，无法美化")
    # 先清洗一遍便于模型理解，但美化输出允许重新加入 markdown/latex
    plain = to_plain_for_llm(src)

    system = (
        f"你是算法竞赛题面排版助手。把「{label}」改写成结构清晰的 Markdown，"
        "可使用 LaTeX 行内公式 $...$ 与常见符号（\\leq、\\times、10^9 等）。\n"
        "要求：\n"
        "1. 严格不改变题意、样例含义、约束数值与输入输出格式。\n"
        "2. 用标题分级（如 ## 题目描述 / ## 输入格式 / ## 输出格式 / ## 数据范围）。\n"
        "3. 列表、加粗适度使用；不要包在代码围栏里输出整篇。\n"
        "4. 只输出 Markdown 正文，不要解释。\n"
    )
    user = f"【原始{label}】\n{src}\n\n【纯文本参考（便于理解）】\n{plain}\n"
    if extra_hint and extra_hint.strip():
        user += f"\n【用户额外要求】\n{extra_hint.strip()}\n"
    return chat_text(system, user, temperature=0.35).strip()
