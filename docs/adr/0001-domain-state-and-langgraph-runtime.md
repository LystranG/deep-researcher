---
status: accepted
---

# 业务状态归领域模块，LangGraph 只管理运行编排

ResearchRun、ResearchTask、RunEvent、Message、Citation、Long-term Memory、Skill 和 Tool Approval 继续以领域数据库为唯一用户可见事实源；LangGraph 负责节点顺序、条件分支、并行、暂停审批和恢复执行，其 checkpoint 仅保存以 `run:{run_id}` 标识的内部恢复资料。Graph 通过领域 Interface 召回有效记忆或提交 Memory Candidate，不使用 LangGraph Store 决定记忆是否生效、冲突、过期、停用或跨 Workspace 可见。这样避免业务表与 checkpoint 形成两个事实源，保留 Workspace ACL、治理、审计和 SSE 重放语义，并允许未来替换编排框架而不迁移核心业务事实。
