"""兼容层：请使用 runners.job.run_job。"""
from runners.job import *  # noqa: F401,F403
from runners import run_job

__all__ = ["run_job"]
