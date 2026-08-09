---
status: accepted
---

# 使用官方 AsyncPostgresSaver 保存 Graph checkpoint

LangGraph Runtime 使用官方 `AsyncPostgresSaver`，checkpoint 存放在同一 PostgreSQL 集群的专用表中，与业务表保持物理分区和事实源分离。`ResearchRun/ResearchTask/RunEvent/Message/Citation/Memory/ToolApproval` 仍决定用户可见状态；checkpoint 只保存 `thread_id=run:{run_id}` 对应的 Graph 阶段、结构化引用和恢复资料，不作为 SSE、ACL、Citation 或最终 Run status 的来源。这样复用官方持久化实现，避免自研 checkpoint 协议，同时保留业务层的治理和审计语义。
