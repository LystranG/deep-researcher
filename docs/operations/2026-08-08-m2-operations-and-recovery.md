# M2 运维、安全与恢复

更新时间：2026-08-08

## 1. 运行拓扑

- API 负责身份、Workspace ACL、Message/Research Run 创建、取消和审批命令
- 独立 Worker 通过 PostgreSQL queue 领取 Research Run，并持有 lease、heartbeat 和 attempt
- LangGraph checkpoint 使用 `thread_id=run:{run_id}`；业务表仍是用户可见状态、SSE、Citation、Memory 和 Tool Approval 的事实源
- Schema 只通过 `rtk uv run alembic upgrade head` 迁移，应用不得调用 ORM 自动建表
- SQLite 只用于本地开发和 deterministic 验收；多 Worker、行锁与持久 checkpoint 必须使用真实 PostgreSQL

## 2. MCP 与审批安全边界

本地受信 MCP 只接受 `http://127.0.0.1` 或 `http://localhost`。当前会话有效工具是系统配置、MCP Server 工具目录、Workspace grant、Conversation Skill override、Skill `allowed_tools` 与 Agent allowlist 的交集。扩展抽屉和公共 API 展示该交集，未授权工具不会创建 Tool Approval 或 Tool Run。

高风险调用必须满足：

- Tool Approval 绑定 run、用户、精确参数 hash 和过期时间
- 拒绝、过期、Workspace 停用和取消均不创建 Tool Run
- Tool Call 行锁与 Tool Run 唯一约束共同形成跨 Worker CAS
- 外部调用前重新读取取消标记、Workspace grant、审批状态、过期时间和有效工具交集
- Run Event 使用业务 event key 幂等；SSE 重放不触发外部调用
- 只持久化参数 hash、安全摘要和安全错误分类，不保存 secret、token、完整 prompt 或隐式思维链

本机 Streamable HTTP MCP Server 仅用于 deterministic 和 UI 验收，不是远程 MCP 生产证据。

## 3. 取消、Worker 接管与恢复

- `queued` 或 `waiting_approval` 取消会在同一业务事务内进入 `cancelled`，释放未结算预算并写唯一终态事件
- 取消统一优先进入 `cancelled`；Graph 节点、模型 delta、Sandbox 和外部调用前检查取消标记，取消后不得新建 Artifact、Citation 或回答结论
- Worker 只有持有匹配 lease owner 时才能追加事件或写终态；lease 过期后由新 Worker 增加 attempt 并接管
- checkpoint 只恢复 Graph 阶段；最终 Message、Citation、Usage Ledger、Tool Run 和终态仍通过业务表的幂等约束写入
- Agent Todo 使用 `run_id + idempotency_key` 唯一边界；Sandbox Todo 关联唯一 `SandboxExecution`，Worker 恢复只投影已存在结果，不重新创建执行
- 计算型研究在发布最终回答前等待 Sandbox Todo 进入终态；成功 stdout 可进入回答，失败只公开安全失败摘要，不生成研究结论
- Todo 状态 `pending/running/completed/skipped/failed/cancelled` 通过 `/runs/{run_id}/todos` 和 `todo_created/todo_updated` 事件恢复
- `waiting_approval` 的 SSE 在发完已有事件后返回；批准或拒绝重新排队同一 Run，客户端按 `Last-Event-ID` 继续读取

若 Run 长时间停留在 `running`，先检查 Worker heartbeat 和 lease owner；不要手工修改 checkpoint。若停留在 `waiting_approval`，检查审批是否 pending、是否过期、Workspace MCP 是否停用以及 Run 是否已取消。

## 4. 备份与恢复

SQLite 开发环境：

1. 停止 API 和 Worker
2. 在同一时间点备份数据库文件与对象目录
3. 恢复后运行 `rtk uv run alembic upgrade head`
4. 通过 `/healthz`、登录、Workspace 列表和一个只读 Research Run 检查恢复结果

PostgreSQL 环境：

1. 停止新 Run 入队并等待正在执行的外部调用结束
2. 使用 `pg_dump` 备份业务库，同时备份对象存储
3. 恢复对象存储和数据库后运行 `rtk uv run alembic upgrade head`
4. 启动 API，再启动 Worker
5. 检查 Alembic head、Worker heartbeat、过期 lease 接管、SSE 重放和 Citation 下载

数据库与对象存储必须来自一致时间点。不要单独删除对象文件，否则历史 Citation 或 Artifact 会失效。现有旧 Compose volume 缺少 Alembic version 且含重复表，未经数据价值确认不得 stamp、清理或覆盖。

## 5. 验证命令

```bash
rtk uv run pytest apps/api/tests
rtk uv run pytest apps/api/tests/integration/test_postgres_event_log.py
rtk uv run ruff check apps/api/src apps/api/tests
rtk uv run mypy apps/api/src/deep_researcher
rtk npm --prefix apps/web test -- --run
rtk npm --prefix apps/web run typecheck
rtk npm --prefix apps/web run build
rtk env PLAYWRIGHT_BROWSERS_PATH=/private/tmp/deep-researcher-playwright npm --prefix apps/web run test:e2e
```

只有配置 `DEEP_RESEARCHER_POSTGRES_TEST_URL` 后，PostgreSQL 专项才构成当前迁移、预算、审批并发和 checkpoint 恢复证据。OpenAI/LiteLLM、Brave Search、远程 MCP 和外部签名服务必须分别使用真实配置验证；缺少配置时报告 skipped 或 unverified。

## 6. 当前限制

- 本地第一版不包含 MCP Marketplace、第三方发布审核、签名托管、升级回滚或组织级策略
- Docker Sandbox 已验证隔离 Adapter，但容器化部署仍需独立 Sandbox Worker 和生产级磁盘配额
- OpenAI、Brave、远程 MCP、外部签名托管和当前迁移后的真实 PostgreSQL 结果不得由 deterministic 证据替代
