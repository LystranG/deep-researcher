from deep_researcher import worker_main


def test_standalone_worker_initializes_runtime_before_polling(monkeypatch) -> None:
    """验证独立 Worker 在领取 Run 前初始化共享运行时"""
    calls: list[str] = []

    class Worker:
        """记录独立 Worker 的轮询启动顺序"""

        def run_forever(self) -> None:
            """记录开始领取数据库 Run"""
            calls.append("run_forever")

    async def initialize_runtime(_state) -> None:
        """记录业务 schema 与 checkpoint 初始化"""
        calls.append("initialize_runtime")

    app = type(
        "App",
        (),
        {
            "state": type(
                "State",
                (),
                {
                    "initialize_runtime": initialize_runtime,
                    "run_worker": Worker(),
                },
            )()
        },
    )()
    monkeypatch.setattr(worker_main, "create_app", lambda *args, **kwargs: app)

    worker_main.main()

    assert calls == ["initialize_runtime", "run_forever"]
