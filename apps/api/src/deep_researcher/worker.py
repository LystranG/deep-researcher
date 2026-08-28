from threading import Event, Thread
from typing import Protocol
from uuid import UUID, uuid4

from deep_researcher.coordinator import ResearchCoordinator
from deep_researcher.run_queue import RunQueue


class RunRuntime(Protocol):
    """The only execution boundary used by the durable Run worker."""

    def execute_run(self, run_id: UUID, *, lease_owner: str) -> None:
        """Execute one leased Run without owning queue or lease lifecycle."""


class SandboxRuntime(Protocol):
    def run_once(self) -> bool:
        """Execute one durably claimed Sandbox Job."""


class RuntimeV2EntryPoint:
    """Runtime v2 entry point bound to the currently configured run implementation."""

    def __init__(self, coordinator: ResearchCoordinator) -> None:
        self._coordinator = coordinator

    def execute_run(self, run_id: UUID, *, lease_owner: str) -> None:
        self._coordinator.execute_run(run_id, lease_owner=lease_owner)


class RunWorker:
    """从数据库队列领取并执行研究运行的 Worker"""

    def __init__(
        self,
        queue: RunQueue,
        runtime: RunRuntime,
        *,
        sandbox_runtime: SandboxRuntime | None = None,
        poll_interval_seconds: float = 0.05,
        owner: str | None = None,
    ) -> None:
        self._queue = queue
        self._runtime = runtime
        self._sandbox_runtime = sandbox_runtime
        self._poll_interval_seconds = poll_interval_seconds
        self._owner = owner or f"worker-{uuid4()}"
        self._stop_event = Event()
        self._thread: Thread | None = None

    def run_once(self) -> bool:
        """领取并执行一个运行，返回是否实际领取到任务"""
        run_id = self._queue.claim(self._owner)
        if run_id is None:
            return (
                self._sandbox_runtime.run_once()
                if self._sandbox_runtime is not None
                else False
            )
        heartbeat_stop = Event()

        def heartbeat_loop() -> None:
            while not heartbeat_stop.wait(5):
                self._queue.heartbeat(run_id, self._owner)

        heartbeat_thread = Thread(target=heartbeat_loop, name="research-worker-heartbeat")
        heartbeat_thread.start()
        try:
            self._runtime.execute_run(run_id, lease_owner=self._owner)
        finally:
            heartbeat_stop.set()
            heartbeat_thread.join(timeout=2)
            self._queue.release(run_id, self._owner)
        return True

    def start(self) -> None:
        """启动独立 Worker 线程等待数据库队列任务"""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()

        self._thread = Thread(target=self.run_forever, name="research-worker", daemon=True)
        self._thread.start()

    def run_forever(self) -> None:
        """持续轮询数据库队列，供独立 Worker 进程作为主循环使用"""
        while not self._stop_event.is_set():
            if not self.run_once():
                self._stop_event.wait(self._poll_interval_seconds)

    def stop(self) -> None:
        """停止 Worker 并等待当前任务结束"""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
