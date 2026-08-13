import asyncio

from deep_researcher.app import create_app
from deep_researcher.settings import Settings


def main() -> None:
    """启动复用 API Sandbox Adapter 的独立 Research Worker"""
    settings = Settings()
    app = create_app(settings, embedded_worker=False)
    asyncio.run(app.state.initialize_runtime())
    app.state.run_worker.run_forever()


if __name__ == "__main__":
    main()
