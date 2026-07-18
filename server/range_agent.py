"""单独生成 range.json（第一步），供 GUI 预览后再决定是否开跑全流程。"""
from __future__ import annotations

import json
import tempfile
import uuid
from pathlib import Path

from agent import tools
from agent.core import run as agent_run
from agent.tools import _schema
from pipeline.gen_data import normalize_range_json, validate_range_json
from utils.markup import to_plain_for_llm

RANGE_ONLY_PROMPT = """你是出题数据规划助手。任务：根据题面与数据范围描述，只产出一份 range.json。

可用工具：
- write_range(content): 写入 range.json（合法 JSON 字符串）
- finish(summary): 写完并确认合法后调用

range.json 必须含：
- count: 正整数，默认 15
- constraints: 对象，变量名 -> [min, max]（整数）
- edge_cases: 字符串数组（边界类型名，禁止含 "random"）

规则：
1. 只调用 write_range，不要写 gen/validator，不要编造测例正文。
2. edge_cases 要覆盖最小/最大/典型边界；多测 T 时建议含 edge_T1、edge_Tmax 等。
3. write_range 成功后立刻 finish。
4. 看到 ERROR 要修正后再 write_range。
"""

RANGE_TOOL_SCHEMAS = [
    _schema(
        "write_range",
        "写入 range.json。content 为完整 JSON：count、constraints、edge_cases（不要含 random）。",
        {"content": {"type": "string", "description": "range.json 完整 JSON 字符串"}},
        ["content"],
    ),
    _schema(
        "finish",
        "range.json 已写好后调用结束。",
        {"summary": {"type": "string", "description": "简述"}},
        [],
    ),
]


def propose_range_json(
    problem_statement: str,
    data_range_desc: str,
    problem_type: str = "",
    std_code: str = "",
    lang: str = "cpp",
) -> dict:
    """调 LLM 只生成 range.json，返回清洗并校验后的 dict（不含 std_cmd）。"""
    stmt = to_plain_for_llm(problem_statement)
    rng = to_plain_for_llm(data_range_desc)
    work = Path(tempfile.gettempdir()) / f"acm_range_{uuid.uuid4().hex[:10]}"
    work.mkdir(parents=True, exist_ok=True)
    tools.set_context(str(work), std_cmd="")

    std_hint = ""
    if std_code and std_code.strip():
        code = std_code.strip()
        if len(code) > 4000:
            code = code[:2000] + "\n/* ... */\n" + code[-1500:]
        std_hint = f"\n\n【标程片段 lang={lang}，仅供推断是否有多测 T】\n```\n{code}\n```\n"

    task = (
        f"请只产出 range.json。\n\n"
        f"【题面】\n{stmt}\n\n"
        f"【数据范围描述】\n{rng}\n"
        f"{std_hint}"
        f"\ncount 默认 15。constraints 覆盖题面中的规模变量（如 n、T、m）。"
        f"edge_cases 用简短英文标识符。写完 write_range 后 finish。"
    )
    summary = agent_run(
        task,
        max_steps=8,
        verbose=False,
        system_prompt=RANGE_ONLY_PROMPT,
        tool_schemas=RANGE_TOOL_SCHEMAS,
    )
    path = work / "range.json"
    if not path.exists():
        raise RuntimeError(f"未能生成 range.json（Agent: {summary}）")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise RuntimeError(f"range.json 不是合法 JSON: {e}") from e

    data = normalize_range_json(dict(data))
    data.pop("std_cmd", None)
    errs = validate_range_json(data)
    if errs:
        raise RuntimeError("range.json 不合法:\n" + "\n".join(f"  - {e}" for e in errs))
    return data
