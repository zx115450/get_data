"""Workspace persistence — thin wrapper over storage.problem_store."""
from storage import problem_store
from storage.problem_store import (
    JOBS_DIR,
    PROBLEMS_DIR,
    SESSION_PATH,
    delete_problem,
    empty_workspace,
    extract_title,
    list_jobs_with_workspace,
    list_problems,
    load_job_workspace,
    load_problem,
    load_session,
    save_session,
    save_to_job,
    upsert_problem,
)

__all__ = [
    "problem_store",
    "JOBS_DIR",
    "PROBLEMS_DIR",
    "SESSION_PATH",
    "delete_problem",
    "empty_workspace",
    "extract_title",
    "list_jobs_with_workspace",
    "list_problems",
    "load_job_workspace",
    "load_problem",
    "load_session",
    "save_session",
    "save_to_job",
    "upsert_problem",
]
