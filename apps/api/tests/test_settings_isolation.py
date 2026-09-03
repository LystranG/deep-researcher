from deep_researcher.settings import Settings


def test_default_test_settings_ignore_repository_provider_configuration() -> None:
    """默认 Settings 不应从仓库 .env 启用外部 Provider。"""
    settings = Settings()

    assert settings.database_url == "sqlite://"
    assert not settings.openai_api_key
    assert not settings.embedding_api_key
    assert not settings.embedding_model
    assert not settings.rerank_api_key
    assert not settings.rerank_model
    assert not settings.brave_search_api_key
    assert not settings.jina_reader_api_key
    assert not settings.firecrawl_api_key
    assert not settings.trusted_mcp_url
    assert settings.openai_model == "test-model"
