from typing import Any
from uuid import UUID

from deep_researcher.event_log import RunEventLog


class RunEventProjector:
    """把 Graph 更新投影为稳定的研究进度业务事件"""

    def __init__(self, event_log: RunEventLog) -> None:
        self._event_log = event_log

    def project(
        self,
        run_id: UUID,
        node_name: str,
        update: dict[str, Any],
        *,
        lease_owner: str | None = None,
    ) -> None:
        """投影单个 Graph 节点更新并按内容去重"""
        stage = {
            "planner": "planning",
            "researcher": "researching",
            "verifier": "verifying",
            "writer": "writing",
        }.get(node_name)
        if stage is None:
            return
        self._event_log.append(
            run_id,
            "research_progress",
            {"stage": stage, "status": "completed"},
            event_key=f"graph-stage:{stage}",
            lease_owner=lease_owner,
        )
