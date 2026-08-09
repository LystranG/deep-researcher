from collections.abc import Callable


class RunCancelledError(RuntimeError):
    """研究运行收到取消信号时抛出"""


class CancellationToken:
    """在节点和模型增量之间传播持久化取消信号"""

    def __init__(self, is_cancelled: Callable[[], bool]) -> None:
        self._is_cancelled = is_cancelled

    def raise_if_cancelled(self) -> None:
        """检测取消状态并终止当前运行"""
        if self._is_cancelled():
            raise RunCancelledError("研究已停止")
