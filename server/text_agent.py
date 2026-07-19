"""题面 / 输入描述 / 输出描述的简化与美化（大模型改写）。

GUI 把题目拆成三个独立字段：
- 题面（statement）：只有题目描述本身，不含输入/输出格式、数据范围。
- 数据范围 / 输入描述（range）：变量范围与输入格式。
- 输出描述（output）：输出格式与答案判定规则。

简化 / 美化的提示词按字段区分，避免把「输入格式 / 输出格式 / 数据范围」
塞进题面，或给题面加不属于它的章节标题。
"""
from __future__ import annotations

import re

from agent.llm import chat_text
from utils.markup import to_plain_for_llm

_KIND_LABEL = {
    "statement": "题面",
    "range": "数据范围 / 输入描述",
    "output": "输出描述",
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


# 按字段的简化提示词。题面只含题目描述本身，不含输入/输出/范围，所以不提那些。
_SIMPLIFY_SYSTEM = {
    "statement": (
        "你是算法竞赛题面编辑。把用户给的「题面」改写成清晰、简短的【纯文本】。"
        "注意：题面只包含题目描述本身（背景、定义、求解目标），"
        "不包含输入格式、输出格式、数据范围——那些是其它字段，不要在此补充或改写。\n"
        "要求：\n"
        "1. 不改变题意与定义含义。\n"
        "2. 禁止使用 Markdown、HTML、LaTeX、美元符、反斜杠命令、公式环境。\n"
        "3. 用中文书面语；数学符号写成普通字符（如「a 整除 b」「1 到 1e5」「[1, 100000]」）。\n"
        "4. 只输出改写后的正文，不要前言、标题装饰或解释。\n"
    ),
    "range": (
        "你是算法竞赛题面编辑。把用户给的「数据范围 / 输入描述」改写成清晰、简短的【纯文本】。"
        "注意：本字段只包含输入格式说明与变量约束（如 n 的范围、T 的范围、sum n 上限等），"
        "不包含题目描述本身或输出格式。\n"
        "要求：\n"
        "1. 不改变约束数值、变量名、输入格式含义。\n"
        "2. 禁止使用 Markdown、HTML、LaTeX、美元符、反斜杠命令、公式环境。\n"
        "3. 用中文书面语；范围写成「1 到 1e5」或「[1, 100000]」这类普通字符。\n"
        "4. 只输出改写后的正文，不要前言、标题装饰或解释。\n"
    ),
    "output": (
        "你是算法竞赛题面编辑。把用户给的「输出描述」改写成清晰、简短的【纯文本】。"
        "注意：本字段只包含输出格式说明与答案判定规则（如「输出一行一个整数」「若有多解输出任意一组」），"
        "不包含题目描述本身或输入格式。\n"
        "要求：\n"
        "1. 不改变输出格式含义与判定规则。\n"
        "2. 禁止使用 Markdown、HTML、LaTeX、美元符、反斜杠命令、公式环境。\n"
        "3. 用中文书面语。\n"
        "4. 只输出改写后的正文，不要前言、标题装饰或解释。\n"
    ),
}


# 按字段的美化提示词。题面不要加「## 输入格式 / ## 输出格式 / ## 数据范围」等不属于它的标题。
_BEAUTIFY_SYSTEM = {
    "statement": (
        "你是算法竞赛题面排版助手。把「题面」改写成结构清晰的 Markdown，"
        "可使用 LaTeX 行内公式 $...$ 与常见符号（\\leq、\\times、10^9 等）。\n"
        "注意：题面只包含题目描述本身（背景、定义、求解目标），"
        "不要在此添加「输入格式」「输出格式」「数据范围」等章节——那些是其它字段。\n"
        "要求：\n"
        "1. 严格不改变题意与定义含义。\n"
        "2. 可用 ## 题目描述 / ## 题目背景 / ## 定义 等与描述相关的标题分级，"
        "但不要加 ## 输入格式 / ## 输出格式 / ## 数据范围。\n"
        "3. 列表、加粗适度使用；不要包在代码围栏里输出整篇。\n"
        "4. 只输出 Markdown 正文，不要解释。\n"
    ),
    "range": (
        "你是算法竞赛题面排版助手。把「数据范围 / 输入描述」改写成结构清晰的 Markdown，"
        "可使用 LaTeX 行内公式 $...$ 与常见符号（\\leq、\\times、10^9 等）。\n"
        "注意：本字段只包含输入格式说明与变量约束，不要添加题目描述或输出格式。\n"
        "要求：\n"
        "1. 严格不改变约束数值、变量名、输入格式含义。\n"
        "2. 可用 ## 输入格式 / ## 数据范围 等相关标题分级。\n"
        "3. 列表、加粗适度使用；不要包在代码围栏里输出整篇。\n"
        "4. 只输出 Markdown 正文，不要解释。\n"
    ),
    "output": (
        "你是算法竞赛题面排版助手。把「输出描述」改写成结构清晰的 Markdown，"
        "可使用 LaTeX 行内公式 $...$ 与常见符号。\n"
        "注意：本字段只包含输出格式说明与答案判定规则，不要添加题目描述或输入格式。\n"
        "要求：\n"
        "1. 严格不改变输出格式含义与判定规则。\n"
        "2. 可用 ## 输出格式 / ## 评分规则 等相关标题分级。\n"
        "3. 列表、加粗适度使用；不要包在代码围栏里输出整篇。\n"
        "4. 只输出 Markdown 正文，不要解释。\n"
    ),
}


def simplify_text(text: str, kind: str = "statement", extra_hint: str = "") -> str:
    """markup 清洗后，再让 LLM 总结成无符号的纯文本。"""
    kind = kind if kind in _KIND_LABEL else "statement"
    label = _KIND_LABEL[kind]
    plain = to_plain_for_llm(text or "")
    if not plain.strip():
        raise ValueError("内容为空，无法简化")

    system = _SIMPLIFY_SYSTEM[kind]
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

    system = _BEAUTIFY_SYSTEM[kind]
    user = f"【原始{label}】\n{src}\n\n【纯文本参考（便于理解）】\n{plain}\n"
    if extra_hint and extra_hint.strip():
        user += f"\n【用户额外要求】\n{extra_hint.strip()}\n"
    return chat_text(system, user, temperature=0.35).strip()
