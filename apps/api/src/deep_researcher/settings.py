from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """应用配置；所有可变部署决策都从这里进入。"""

    model_config = SettingsConfigDict(
        env_file=None,
        env_prefix="DEEP_RESEARCHER_",
        extra="ignore",
    )

    database_url: str = "sqlite:///var/deep-researcher.db"
    object_store_root: Path = Path("var/objects")
    auth_session_days: int = 30
    research_step_delay_seconds: float = 0.0
    run_token_budget: int = 10_000
    run_cost_budget_usd: float = 1.0
    model_context_tokens: int = 128_000
    model_output_token_reserve: int = 4_096
    model_context_safety_margin: int = 1_024
    map_work_max_concurrency: int = 4
    workspace_token_quota: int = 100_000
    workspace_cost_quota_usd: float = 50.0
    max_attachment_bytes: int = 50 * 1024 * 1024
    openai_api_key: str | None = None
    openai_api_base: str | None = None
    openai_model: str = "gpt-5.6-sol"
    openai_reasoning_effort: Literal["none", "low", "medium", "high", "xhigh", "max"] = "medium"
    embedding_api_key: str | None = None
    embedding_api_base: str | None = None
    embedding_model: str | None = None
    rerank_api_key: str | None = None
    rerank_api_base: str | None = None
    rerank_model: str | None = None
    brave_search_api_key: str | None = None
    jina_reader_api_key: str | None = None
    firecrawl_api_key: str | None = None
    trusted_mcp_url: str | None = None
    trusted_mcp_timeout_seconds: float = 30.0
    tool_approval_ttl_seconds: int = 900
    sandbox_image: str = "python:3.13-slim"
    sandbox_output_root: Path = Path("var/sandbox-output")
    sandbox_max_timeout_seconds: int = 60
    sandbox_max_artifact_bytes: int = 10 * 1024 * 1024
