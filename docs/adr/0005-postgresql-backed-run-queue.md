---
status: accepted
---

# Research Run 队列使用 PostgreSQL

独立 Worker 通过 PostgreSQL-backed queue 领取 Research Run，使用 `FOR UPDATE SKIP LOCKED`、lease、heartbeat、attempt 和明确的恢复状态；不在首版引入 Redis、Celery 或另一套队列事实源。Run 创建、入队、取消和最终业务状态可以在同一数据库事实边界内协调，避免 API 数据库与外部队列双写不一致，并保留未来替换 Queue Adapter 的接口。
