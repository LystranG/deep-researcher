---
status: accepted
---

# Agent Runtime 从首版起运行在独立 Worker 中

LangGraph Runtime 不在 FastAPI 进程内通过 `ThreadPoolExecutor` 执行，而是从首个重构版本起由独立 Worker 领取并运行 Research Run。API 只负责校验身份和 Workspace ACL、在事务中创建 Message/ResearchRun/首个 RunEvent，以及持久化取消或审批命令；Worker 负责租约、heartbeat、并发限制、Graph checkpoint、重试和恢复。这样消除提交事务后再向内存线程池 submit 的崩溃窗口，使 API 发布与长任务生命周期解耦，并避免先建设一套随后必须删除的临时 Graph Runner。
