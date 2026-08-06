"""Few-shot 样板、RAG 语料与题面结构约束。"""
from knowledge.few_shots import (
    FEW_SHOTS,
    KNOWN_PROBLEM_TYPES,
    PROBLEM_TYPE_RANGE_HINT,
    detect_problem_type,
    detected_type,
    get_few_shot,
    get_few_shot_rag,
    normalize_problem_type,
    resolve_problem_type_from_range,
)
from knowledge.few_shots_rag import (
    add_job_to_corpus,
    delete_corpus_item,
    get_corpus_item,
    list_corpus_items,
    list_data_corpus_files,
    merge_corpus_from_data,
    merge_corpus_from_file,
    retrieve_few_shots,
    set_corpus_item_disabled,
    sync_templates_from_code,
)
from knowledge.struct_hints import scan_structural_hints, scan_structural_titles

__all__ = [
    "FEW_SHOTS",
    "KNOWN_PROBLEM_TYPES",
    "PROBLEM_TYPE_RANGE_HINT",
    "detect_problem_type",
    "detected_type",
    "get_few_shot",
    "get_few_shot_rag",
    "normalize_problem_type",
    "resolve_problem_type_from_range",
    "add_job_to_corpus",
    "delete_corpus_item",
    "get_corpus_item",
    "list_corpus_items",
    "list_data_corpus_files",
    "merge_corpus_from_data",
    "merge_corpus_from_file",
    "retrieve_few_shots",
    "set_corpus_item_disabled",
    "sync_templates_from_code",
    "scan_structural_hints",
    "scan_structural_titles",
]
