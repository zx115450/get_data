"""基于 RAG 的 few-shot 模板召回（阶段一 + 阶段二）。

语料来源：
  1. 固定模板：few_shots_cpp.py 中的 6 个 C++ testlib 范例
  2. 历史任务：每次成功生成后，把 job 目录中的题面、range.json、gen.cpp
     加入语料，embedding 后持久化

持久化位置：
  - data/few_shots_rag_corpus.json  —— 可随仓库分享的种子语料
  - .cache/few_shots_rag_corpus.json —— 本地运行副本（gitignore）

检索方式：把用户题面/范围/标程片段也做 embedding，按余弦相似度召回 Top-K。
embedding 默认调用 OpenAI 兼容接口（agent.llm.embed_text）。
若对方使用的 embedding 模型与语料中记录的不一致，会自动清空向量并重算。
"""
from __future__ import annotations

import json
import math
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from agent.llm import embed_text
from server.few_shots_cpp import (
    CPP_ARRAY_EXAMPLE,
    CPP_TREE_EXAMPLE,
    CPP_GRAPH_EXAMPLE,
    CPP_STRING_EXAMPLE,
    CPP_NUMBER_THEORY_EXAMPLE,
    CPP_MULTI_TEST_EXAMPLE,
)


# 固定模板：key -> 模板完整内容
_TEMPLATE_CONTENT: dict[str, str] = {
    "array": CPP_ARRAY_EXAMPLE,
    "tree": CPP_TREE_EXAMPLE,
    "graph": CPP_GRAPH_EXAMPLE,
    "string": CPP_STRING_EXAMPLE,
    "number_theory": CPP_NUMBER_THEORY_EXAMPLE,
    "multi_test": CPP_MULTI_TEST_EXAMPLE,
}

# 项目根目录
_ROOT = Path(__file__).resolve().parent.parent

# 可随仓库分享的种子语料（别人 clone/下载后直接可用）
_SEED_FILE = _ROOT / "data" / "few_shots_rag_corpus.json"

# 本地运行副本（.cache 仍 gitignore；首次从 seed 复制）
_CACHE_DIR = _ROOT / ".cache"
_CORPUS_FILE = _CACHE_DIR / "few_shots_rag_corpus.json"

# 语料项类型
_SOURCE_TEMPLATE = "template"
_SOURCE_JOB = "job"


def _default_embedding_model() -> str:
    return os.getenv("LLM_EMBEDDING_MODEL", "text-embedding-3-small")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _normalize_item(item: dict) -> dict:
    """补齐运营元数据字段（兼容旧语料）。"""
    item.setdefault("disabled", False)
    item.setdefault("problem_type", "")
    item.setdefault("valid_rate", None)
    item.setdefault("created_at", "")
    item.setdefault("source", _SOURCE_TEMPLATE)
    # 旧模板条目：用 key 回填题型
    if not item.get("problem_type") and item.get("source") == _SOURCE_TEMPLATE:
        item["problem_type"] = item.get("key") or ""
    return item


def _item_summary(item: dict, include_content: bool = False) -> dict:
    """返回给前端的条目摘要（不含 embedding）。"""
    _normalize_item(item)
    out = {
        "key": item.get("key", ""),
        "source": item.get("source", _SOURCE_TEMPLATE),
        "disabled": bool(item.get("disabled")),
        "problem_type": item.get("problem_type") or "",
        "valid_rate": item.get("valid_rate"),
        "created_at": item.get("created_at") or "",
        "text_preview": (item.get("text") or "")[:200],
        "has_embedding": bool(item.get("embedding")),
    }
    if include_content:
        out["text"] = item.get("text") or ""
        out["content"] = item.get("content") or ""
    return out


def _extract_summary_from_template(text: str) -> str:
    """从模板字符串中提取用于 embedding 的摘要：标题 + 题面 + 数据范围。"""
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    parts = []
    seen = set()

    def _add(line: str) -> None:
        if line and line not in seen:
            seen.add(line)
            parts.append(line)

    if lines:
        _add(lines[0].replace("【", "").replace("】", ""))

    for ln in lines[1:10]:
        if ln.startswith("题面：") or ln.startswith("题面:"):
            _add(ln)
        elif ln.startswith("数据范围：") or ln.startswith("数据范围:"):
            _add(ln)
        elif "输入格式" in ln or "输出格式" in ln:
            _add(ln)

    if len("\n".join(parts)) < 80:
        for ln in lines[1:4]:
            _add(ln)

    return "\n".join(parts).strip()


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """纯 Python 余弦相似度，返回 [-1, 1]。"""
    if len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0 or nb == 0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


def _read_corpus_file(path: Path) -> dict | None:
    """读取语料 JSON；失败或空 items 返回 None。"""
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not data.get("items"):
            return None
        return data
    except Exception:
        return None


def _load_corpus() -> dict:
    """加载语料库（含 embedding）。

    优先级：本地 .cache → 仓库内 data/ 种子 → 仅用 6 个固定模板初始化。
    """
    data = _read_corpus_file(_CORPUS_FILE)
    if data is not None:
        return data

    data = _read_corpus_file(_SEED_FILE)
    if data is not None:
        # 首次把种子复制到本地 cache，后续读写走 cache
        _save_corpus(data, sync_seed=False)
        return data

    return _init_template_corpus()


def _save_corpus(data: dict, sync_seed: bool = True) -> None:
    """保存语料库到本地 .cache；默认同步到 data/ 以便随项目分享。"""
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _CORPUS_FILE.write_text(payload, encoding="utf-8")
    if sync_seed:
        _SEED_FILE.parent.mkdir(parents=True, exist_ok=True)
        _SEED_FILE.write_text(payload, encoding="utf-8")


def _init_template_corpus() -> dict:
    """用 6 个固定模板初始化语料库（无 embedding）。"""
    now = _now_iso()
    items = []
    for key, content in _TEMPLATE_CONTENT.items():
        items.append(_normalize_item({
            "key": key,
            "source": _SOURCE_TEMPLATE,
            "text": _extract_summary_from_template(content),
            "content": content,
            "embedding": None,
            "problem_type": key,
            "valid_rate": 1.0,
            "created_at": now,
            "disabled": False,
        }))
    return {"model": _default_embedding_model(), "items": items}


def _build_corpus_embeddings(force_refresh: bool = False) -> dict:
    """确保语料库中每个 item 都有 embedding，返回完整语料数据。

    策略：
      - 模型变更 -> 全部重新计算
      - force_refresh=True -> 全部重新计算
      - 新增 item embedding 缺失 -> 只补算缺失项
    """
    corpus = _load_corpus()
    current_model = _default_embedding_model()
    model_changed = corpus.get("model") != current_model

    if model_changed or force_refresh:
        corpus["model"] = current_model
        for item in corpus.get("items", []):
            item["embedding"] = None

    need_fetch = [item for item in corpus.get("items", []) if not item.get("embedding")]
    if need_fetch:
        for item in need_fetch:
            item["embedding"] = embed_text(item["text"])
        _save_corpus(corpus)

    return corpus


def _build_query_text(
    problem_statement: str,
    data_range_desc: str = "",
    std_code: str = "",
) -> str:
    """把用户输入拼接成查询文本。"""
    parts = []
    if problem_statement:
        parts.append(problem_statement.strip())
    if data_range_desc:
        parts.append(data_range_desc.strip())
    if std_code:
        code = std_code.strip()
        if len(code) > 2000:
            code = code[:1000] + "\n...\n" + code[-500:]
        parts.append(code)
    return "\n\n".join(parts).strip()


def _build_job_content(
    job_id: str,
    statement: str,
    range_json: dict,
    gen_code: str,
    validator_code: str = "",
    gen_label: str = "gen.cpp",
    val_label: str = "validator.cpp",
) -> str:
    """把历史任务格式化成与固定模板类似的 few-shot 内容。"""
    range_text = json.dumps(range_json, ensure_ascii=False, indent=2)
    blocks = [
        f"【历史范例：{job_id}】",
        f"题面：{statement.strip()}",
        "数据范围：见下方 range.json",
        "",
        "range.json:",
        range_text,
        "",
        f"{gen_label}:",
        gen_code.strip(),
    ]
    if validator_code:
        blocks.extend(["", f"{val_label}:", validator_code.strip()])
    return "\n".join(blocks)


def _build_job_text(
    statement: str,
    range_json: dict,
    gen_code: str,
) -> str:
    """从历史任务提取用于 embedding 的摘要文本。"""
    parts = [statement.strip()]
    cons = range_json.get("constraints") or {}
    if cons:
        cons_summary = ", ".join(
            f"{k}∈[{v[0]},{v[1]}]" for k, v in cons.items()
        )
        parts.append(f"数据范围：{cons_summary}")
    edge_cases = range_json.get("edge_cases") or []
    if edge_cases:
        parts.append(f"边界类型：{', '.join(edge_cases[:8])}")
    # 从 gen.cpp 中抽一些结构关键词
    gen_hints = []
    for hint in ["testlib", "registerGen", "n =", "T =", "string", "vector"]:
        if hint in gen_code:
            gen_hints.append(hint)
    if gen_hints:
        parts.append(f"生成器特征：{', '.join(gen_hints[:6])}")
    return "\n".join(parts).strip()


def retrieve_few_shots(
    problem_statement: str,
    data_range_desc: str = "",
    std_code: str = "",
    top_k: int = 2,
) -> list[dict]:
    """按题面语义召回最相似的 k 个范例。

    返回列表项：
      {
        "key": 模板名 / job_id,
        "source": "template" | "job",
        "score": 余弦相似度,
        "content": 完整范例内容（可直接拼进 prompt）,
      }
    """
    corpus = _build_corpus_embeddings()
    items = corpus.get("items", [])
    if not items:
        return []

    query_text = _build_query_text(problem_statement, data_range_desc, std_code)
    query_vec = embed_text(query_text)

    scored = []
    for item in items:
        _normalize_item(item)
        if item.get("disabled"):
            continue
        vec = item.get("embedding")
        if not vec:
            continue
        score = _cosine_similarity(query_vec, vec)
        scored.append({
            "key": item["key"],
            "source": item.get("source", _SOURCE_TEMPLATE),
            "score": round(score, 4),
            "content": item["content"],
            "problem_type": item.get("problem_type") or "",
        })

    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:top_k]


def format_rag_few_shots(examples: list[dict]) -> str:
    """把召回的范例列表格式化成可塞进 Agent prompt 的字符串。"""
    if not examples:
        return ""
    blocks = []
    for ex in examples:
        label = "模板" if ex.get("source") == _SOURCE_TEMPLATE else "历史任务"
        blocks.append(
            f"【RAG 召回范例：{label} {ex['key']} (相似度 {ex['score']})】\n"
            f"{ex['content']}"
        )
    return (
        "\n\n".join(blocks)
        + "\n\n请重点参照上面 RAG 召回的范例风格（与本次题目最相似），"
        "为本次题目写 gen.cpp 和 validator.cpp。"
    )


def _is_quality_job(stats: dict | None, range_json: dict) -> bool:
    """判断一个 job 是否值得加入 RAG 语料库。

    过滤条件：
      - bad 必须为 0，且 ok == count（valid_rate ≈ 1.0）
      - count > 0
      - 生成耗时在 [1, 300] 秒之间（过快可能数据太简单，过慢可能生成器有问题）
      - range.json 必须有 constraints
    """
    if not stats:
        return True

    bad = stats.get("bad", 0)
    count = stats.get("count", 0)
    ok = stats.get("ok", 0)
    valid_rate = stats.get("valid_rate", 0.0)
    elapsed_s = stats.get("elapsed_s", 0.0)

    if bad > 0 or ok < count:
        return False
    if valid_rate < 0.999:
        return False
    if count <= 0:
        return False
    if elapsed_s < 1.0 or elapsed_s > 300.0:
        return False

    cons = range_json.get("constraints") if isinstance(range_json, dict) else None
    if not cons or not isinstance(cons, dict):
        return False

    return True


def _is_duplicate(
    text: str,
    embedding: list[float],
    corpus: dict,
    threshold: float = 0.95,
) -> tuple[bool, str | None]:
    """检查新文本是否与语料库中已有项重复。返回 (是否重复, 最相似的已有项 key)

    已禁用条目不参与去重，避免挡住重新入库。
    """
    best_key: str | None = None
    best_score = 0.0
    for item in corpus.get("items", []):
        _normalize_item(item)
        if item.get("disabled"):
            continue
        existing_vec = item.get("embedding")
        if not existing_vec:
            continue
        score = _cosine_similarity(embedding, existing_vec)
        if score > best_score:
            best_score = score
            best_key = item.get("key")
        if score > threshold:
            return True, best_key
    return False, best_key


def add_job_to_corpus(
    job_dir: str | Path,
    stats: dict | None = None,
    dedup_threshold: float = 0.95,
    problem_type: str = "",
) -> dict | None:
    """把一次成功生成后的 job 目录加入 RAG 语料库。

    读取的文件：
      - statement_simplified.txt
      - range.json
      - gen.cpp / gen.py
      - validator.cpp / validate.py（若存在）

    质量过滤：
      - stats 中 bad 必须为 0，ok == count，valid_rate >= 0.999
      - 生成耗时在 [1, 300] 秒之间
      - range.json 必须有 constraints

    去重：与已有语料项 embedding 余弦相似度超过 dedup_threshold 则跳过。

    入库元数据：problem_type / valid_rate / created_at / disabled=False

    返回加入的 item；若读取失败、已存在、质量不过关或重复则返回 None。
    """
    job_dir = Path(job_dir)
    if not job_dir.is_dir():
        return None

    job_id = job_dir.name
    stmt_file = job_dir / "statement_simplified.txt"
    range_file = job_dir / "range.json"
    gen_file = job_dir / "gen.cpp" if (job_dir / "gen.cpp").exists() else job_dir / "gen.py"
    val_file = job_dir / "validator.cpp" if (job_dir / "validator.cpp").exists() else job_dir / "validate.py"

    if not stmt_file.exists() or not range_file.exists() or not gen_file.exists():
        return None

    try:
        statement = stmt_file.read_text(encoding="utf-8").strip()
        range_json = json.loads(range_file.read_text(encoding="utf-8"))
        gen_code = gen_file.read_text(encoding="utf-8")
        validator_code = val_file.read_text(encoding="utf-8") if val_file.exists() else ""
    except Exception:
        return None

    if not statement or not gen_code.strip():
        return None

    # 质量过滤
    if not _is_quality_job(stats, range_json):
        return None

    corpus = _load_corpus()
    existing_keys = {item["key"] for item in corpus.get("items", [])}
    if job_id in existing_keys:
        return None

    # 确保已有语料项都有 embedding（用于去重）
    try:
        corpus = _build_corpus_embeddings()
    except Exception:
        return None

    text = _build_job_text(statement, range_json, gen_code)
    content = _build_job_content(
        job_id, statement, range_json, gen_code, validator_code,
        gen_label=gen_file.name,
        val_label=val_file.name if val_file.exists() else "",
    )
    try:
        embedding = embed_text(text)
    except Exception:
        return None

    # 去重（跳过已禁用项，避免禁用范例挡住新入库）
    is_dup, dup_key = _is_duplicate(text, embedding, corpus, dedup_threshold)
    if is_dup:
        return None

    valid_rate = None
    if stats is not None:
        valid_rate = stats.get("valid_rate")
        if valid_rate is None:
            count = stats.get("count") or 0
            ok = stats.get("ok") or 0
            valid_rate = (ok / count) if count else None

    new_item = _normalize_item({
        "key": job_id,
        "source": _SOURCE_JOB,
        "text": text,
        "content": content,
        "embedding": embedding,
        "problem_type": problem_type or "",
        "valid_rate": valid_rate,
        "created_at": _now_iso(),
        "disabled": False,
    })
    corpus.setdefault("items", []).append(new_item)
    corpus["model"] = _default_embedding_model()
    _save_corpus(corpus)
    return new_item


def refresh_corpus_cache() -> None:
    """强制重新计算并缓存所有语料项 embedding（含模板 + 已入库历史任务）。"""
    _build_corpus_embeddings(force_refresh=True)


# ---- 语料运营：列表 / 详情 / 禁用 / 删除 / 合并 ----

def list_corpus_items(
    source: str = "",
    problem_type: str = "",
    include_disabled: bool = True,
) -> dict:
    """列出语料摘要，可按 source / problem_type 筛选。"""
    corpus = _load_corpus()
    items = []
    for item in corpus.get("items", []):
        _normalize_item(item)
        if source and item.get("source") != source:
            continue
        if problem_type and (item.get("problem_type") or "") != problem_type:
            continue
        if not include_disabled and item.get("disabled"):
            continue
        items.append(_item_summary(item))
    # job 在前按时间倒序，模板按 key
    items.sort(key=lambda x: (
        0 if x["source"] == _SOURCE_JOB else 1,
        x.get("created_at") or "",
        x.get("key") or "",
    ), reverse=True)
    return {
        "model": corpus.get("model") or _default_embedding_model(),
        "total": len(items),
        "items": items,
    }


def get_corpus_item(key: str) -> dict | None:
    """获取单条完整内容（不含 embedding）。"""
    corpus = _load_corpus()
    for item in corpus.get("items", []):
        if item.get("key") == key:
            return _item_summary(item, include_content=True)
    return None


def set_corpus_item_disabled(key: str, disabled: bool) -> dict | None:
    """启用/禁用一条语料；禁用后召回时跳过。"""
    corpus = _load_corpus()
    for item in corpus.get("items", []):
        if item.get("key") == key:
            item["disabled"] = bool(disabled)
            _normalize_item(item)
            _save_corpus(corpus)
            return _item_summary(item)
    return None


def delete_corpus_item(key: str) -> bool:
    """从语料库删除一条（模板与 job 均可删）。"""
    corpus = _load_corpus()
    items = corpus.get("items", [])
    new_items = [it for it in items if it.get("key") != key]
    if len(new_items) == len(items):
        return False
    corpus["items"] = new_items
    _save_corpus(corpus)
    return True


def merge_corpus_from_data(
    incoming: dict,
    dedup_threshold: float = 0.95,
    skip_disabled: bool = True,
) -> dict:
    """合并外部语料 JSON（别人贡献的 data/*.json）。

    策略：
      - 同 key 已存在 → 跳过
      - embedding 缺失 → 稍后按需补算（合并时若有 text 则尝试 embedding）
      - 与已有未禁用项相似度过高 → 跳过
      - 默认不导入对方已禁用的条目

    返回统计：added / skipped_key / skipped_dup / skipped_disabled / skipped_invalid
    """
    if not isinstance(incoming, dict) or not isinstance(incoming.get("items"), list):
        raise ValueError("无效语料文件：需要含 items 数组的 JSON 对象")

    try:
        corpus = _build_corpus_embeddings()
    except Exception:
        corpus = _load_corpus()

    existing_keys = {item.get("key") for item in corpus.get("items", [])}
    stats = {
        "added": 0,
        "skipped_key": 0,
        "skipped_dup": 0,
        "skipped_disabled": 0,
        "skipped_invalid": 0,
        "added_keys": [],
    }

    for raw in incoming["items"]:
        if not isinstance(raw, dict):
            stats["skipped_invalid"] += 1
            continue
        key = (raw.get("key") or "").strip()
        text = (raw.get("text") or "").strip()
        content = (raw.get("content") or "").strip()
        if not key or not content:
            stats["skipped_invalid"] += 1
            continue
        if skip_disabled and raw.get("disabled"):
            stats["skipped_disabled"] += 1
            continue
        if key in existing_keys:
            stats["skipped_key"] += 1
            continue

        item = _normalize_item({
            "key": key,
            "source": raw.get("source") or _SOURCE_JOB,
            "text": text or _extract_summary_from_template(content),
            "content": content,
            "embedding": raw.get("embedding"),
            "problem_type": raw.get("problem_type") or "",
            "valid_rate": raw.get("valid_rate"),
            "created_at": raw.get("created_at") or _now_iso(),
            "disabled": bool(raw.get("disabled", False)),
        })

        # embedding 维度/模型可能不兼容：无向量或模型不同时重算
        need_embed = not item.get("embedding")
        if incoming.get("model") and incoming.get("model") != corpus.get("model"):
            need_embed = True
            item["embedding"] = None

        if need_embed:
            try:
                item["embedding"] = embed_text(item["text"])
            except Exception:
                stats["skipped_invalid"] += 1
                continue

        is_dup, _ = _is_duplicate(item["text"], item["embedding"], corpus, dedup_threshold)
        if is_dup:
            stats["skipped_dup"] += 1
            continue

        corpus.setdefault("items", []).append(item)
        existing_keys.add(key)
        stats["added"] += 1
        stats["added_keys"].append(key)

    if stats["added"]:
        corpus["model"] = _default_embedding_model()
        _save_corpus(corpus)
    return stats


def merge_corpus_from_file(path: str | Path, **kwargs) -> dict:
    """从本地 JSON 文件合并语料。"""
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"文件不存在: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    return merge_corpus_from_data(data, **kwargs)


def list_data_corpus_files() -> list[dict]:
    """列出 data/ 下可供合并的 *.json 文件。"""
    data_dir = _ROOT / "data"
    if not data_dir.is_dir():
        return []
    out = []
    for p in sorted(data_dir.glob("*.json")):
        out.append({
            "name": p.name,
            "path": str(p),
            "size": p.stat().st_size,
            "is_seed": p.resolve() == _SEED_FILE.resolve(),
        })
    return out
