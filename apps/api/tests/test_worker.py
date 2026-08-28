from uuid import uuid4

from deep_researcher.worker import RunWorker


class Queue:
    def __init__(self) -> None:
        self.run_id = uuid4()
        self.released: list[tuple[object, str]] = []

    def claim(self, owner: str):
        return self.run_id

    def heartbeat(self, run_id, owner: str) -> bool:
        return True

    def release(self, run_id, owner: str) -> None:
        self.released.append((run_id, owner))


class Runtime:
    def __init__(self) -> None:
        self.calls: list[tuple[object, str]] = []

    def execute_run(self, run_id, *, lease_owner: str) -> None:
        self.calls.append((run_id, lease_owner))


def test_worker_executes_the_configured_runtime_entrypoint() -> None:
    queue = Queue()
    runtime = Runtime()
    worker = RunWorker(queue, runtime, owner="runtime-worker")

    assert worker.run_once() is True
    assert runtime.calls == [(queue.run_id, "runtime-worker")]
    assert queue.released == [(queue.run_id, "runtime-worker")]
