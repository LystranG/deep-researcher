"""Shared test isolation for configuration-backed integrations."""

import os
from collections.abc import Iterator

import pytest

_PROVIDER_ENVIRONMENT = {
    "DEEP_RESEARCHER_OPENAI_API_KEY": "",
    "DEEP_RESEARCHER_OPENAI_API_BASE": "",
    "DEEP_RESEARCHER_OPENAI_MODEL": "test-model",
    "DEEP_RESEARCHER_EMBEDDING_API_KEY": "",
    "DEEP_RESEARCHER_EMBEDDING_API_BASE": "",
    "DEEP_RESEARCHER_EMBEDDING_MODEL": "",
    "DEEP_RESEARCHER_RERANK_API_KEY": "",
    "DEEP_RESEARCHER_RERANK_API_BASE": "",
    "DEEP_RESEARCHER_RERANK_MODEL": "",
    "DEEP_RESEARCHER_BRAVE_SEARCH_API_KEY": "",
    "DEEP_RESEARCHER_JINA_READER_API_KEY": "",
    "DEEP_RESEARCHER_FIRECRAWL_API_KEY": "",
    "DEEP_RESEARCHER_TRUSTED_MCP_URL": "",
}


@pytest.fixture(autouse=True)
def isolate_provider_environment(request, monkeypatch) -> Iterator[None]:
    """Prevent shell and dotenv provider values from reaching deterministic tests."""
    path = str(request.node.fspath)
    live_integration = (
        ("test_hybrid_retrieval_provider.py" in path
         and os.getenv("DEEP_RESEARCHER_RUN_HYBRID_RETRIEVAL_LIVE") == "1")
        or ("test_ticket12_provider_deployment.py" in path
            and os.getenv("DEEP_RESEARCHER_RUN_TICKET12_LIVE") == "1")
    )
    if not live_integration:
        monkeypatch.setenv("DEEP_RESEARCHER_DATABASE_URL", "sqlite://")
        for key, value in _PROVIDER_ENVIRONMENT.items():
            monkeypatch.setenv(key, value)
    yield
