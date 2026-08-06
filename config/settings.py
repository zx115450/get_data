"""集中配置：启动时一次性校验 LLM / 端口 / 超时等。"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """从环境变量 / .env 读取；字段名对应 LLM_API_KEY 等大写环境变量。"""

    model_config = SettingsConfigDict(
        env_file=str(_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    llm_api_key: str = ""
    llm_base_url: str = ""
    llm_model: str = "gpt-4o-mini"
    llm_max_tokens: int = 16384
    # 单次任务累计 token 上限；0 表示不限制
    llm_max_total_tokens: int = 0
    # 429 / 5xx 重试
    llm_retry_max: int = 3
    llm_retry_min_wait: float = 2.0
    llm_retry_max_wait: float = 30.0

    embedding_api_key: str = ""
    embedding_base_url: str = ""
    llm_embedding_model: str = "text-embedding-3-small"

    server_host: str = "127.0.0.1"
    server_port: int = 8000

    # Job 自动清理：保留天数；max_keep>0 时额外按 LRU 只留 N 个
    job_retention_days: int = 30
    job_max_keep: int = 0

    get_data_max_jobs: int = 1

    @field_validator("server_port")
    @classmethod
    def _port_range(cls, v: int) -> int:
        if not (1 <= int(v) <= 65535):
            raise ValueError(f"SERVER_PORT 必须在 1–65535，当前为 {v}")
        return int(v)

    @field_validator("llm_retry_max")
    @classmethod
    def _retry_nonneg(cls, v: int) -> int:
        if int(v) < 0:
            raise ValueError("LLM_RETRY_MAX 不能为负")
        return int(v)

    @field_validator("job_retention_days", "job_max_keep", "llm_max_total_tokens")
    @classmethod
    def _nonneg(cls, v: int) -> int:
        if int(v) < 0:
            raise ValueError("该配置不能为负")
        return int(v)

    def validate_runtime(self, *, require_llm_key: bool = True) -> list[str]:
        """返回错误列表；空列表表示通过。"""
        errs: list[str] = []
        if require_llm_key and not (self.llm_api_key or "").strip():
            errs.append("LLM_API_KEY 未设置（请在 .env 中配置）")
        if not (self.llm_model or "").strip():
            errs.append("LLM_MODEL 不能为空")
        if self.llm_retry_min_wait < 0 or self.llm_retry_max_wait < 0:
            errs.append("LLM_RETRY_*_WAIT 不能为负")
        if self.llm_retry_max_wait < self.llm_retry_min_wait:
            errs.append("LLM_RETRY_MAX_WAIT 不能小于 LLM_RETRY_MIN_WAIT")
        if self.get_data_max_jobs < 1:
            errs.append("GET_DATA_MAX_JOBS 至少为 1")
        return errs


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """进程内单例；测试可 clear 缓存后重建。"""
    return Settings()


def validate_runtime(*, require_llm_key: bool = True) -> Settings:
    """启动入口调用：失败则抛出 ValueError，列出全部问题。"""
    # 清除缓存以便读到最新 .env（例如 setup 刚写完）
    get_settings.cache_clear()
    s = get_settings()
    errs = s.validate_runtime(require_llm_key=require_llm_key)
    if errs:
        raise ValueError("配置校验失败:\n  - " + "\n  - ".join(errs))
    return s
