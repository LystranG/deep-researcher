from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from fastapi.testclient import TestClient

from deep_researcher.app import create_app
from deep_researcher.settings import Settings


@contextmanager
def running_worker_client(settings: Settings, **app_kwargs: Any) -> Iterator[TestClient]:
    """创建不内嵌 Worker 的 API 客户端并显式管理独立 Worker 生命周期"""
    app = create_app(settings, embedded_worker=False, **app_kwargs)
    with TestClient(app) as client:
        app.state.run_worker.start()
        try:
            yield client
        finally:
            app.state.run_worker.stop()
