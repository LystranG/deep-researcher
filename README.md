# 深度研究工作台

以 Workspace 为长期隔离边界的多会话研究工作台。当前本地第一版包含持久化 Message/Research Run、可续传 SSE、停止与幂等、会话附件、Workspace Document 版本、检索与稳定 Citation、长期记忆、Docker Sandbox、受限任务 DAG、Evidence Check、Skill 和本地受信 MCP 审批。实际完成状态见 [`docs/status/2026-08-07-implementation-progress.md`](docs/status/2026-08-07-implementation-progress.md)，运行恢复与安全边界见 [`docs/operations/2026-08-08-m2-operations-and-recovery.md`](docs/operations/2026-08-08-m2-operations-and-recovery.md)。

## 本地启动

要求：Python 3.13、uv 0.12+、Node.js 24+、npm 11+。

```bash
uv sync
npm --prefix apps/web install
uv run alembic upgrade head
make dev-api
make dev-web
```

浏览器访问 `http://127.0.0.1:5173`。默认数据库是 `var/deep-researcher.db`，默认对象目录是 `var/objects`。没有 OpenAI key 时走本地抽取式研究 Adapter；它会基于实际可见的会话纠正和文档 Evidence Span 回答，不会伪装成外部模型调用。

## 模型与网页检索配置

在项目根目录创建 `.env`，按所使用的 OpenAI 兼容服务填写模型配置：

```dotenv
DEEP_RESEARCHER_OPENAI_API_KEY=你的模型密钥
DEEP_RESEARCHER_OPENAI_API_BASE=https://你的兼容服务地址/v1
DEEP_RESEARCHER_OPENAI_MODEL=openai/你的模型ID
DEEP_RESEARCHER_BRAVE_SEARCH_API_KEY=你的BraveSearch密钥
```

模型凭证只提供文本生成能力；自动网页研究使用独立的 Brave Search Adapter。未配置 `DEEP_RESEARCHER_BRAVE_SEARCH_API_KEY` 时，系统会明确提示网页检索不可用，不会把无资料误报为研究结论，也不会继续消耗模型 token。配置变更后重启 API。

使用 PostgreSQL 的完整本地部署：

```bash
docker-compose -f infra/docker-compose.yml up --build
```

然后访问 `http://127.0.0.1:8080`。

## 验证

```bash
make verify
```

外部 OpenAI 调用不属于无凭证测试的通过证据；有凭证集成结果必须单独报告。

## 数据与备份

- SQLite：停止 API 后复制 `var/deep-researcher.db` 与整个 `var/objects`，两者必须来自同一时间点。
- PostgreSQL：使用 `pg_dump` 备份数据库，同时保存 `object-data` volume；恢复时先恢复对象，再恢复数据库并运行 `alembic upgrade head`。
- 逻辑删除会立即阻断在线读取/检索；物理清理任务和可配置保留期将在收口阶段交付。不要手工删除单个对象文件，否则历史 Citation 会标记来源失效。

## 当前外部边界

- OpenAI Responses 是可选 Adapter，默认模型为当前配置中的 `gpt-5.6-sol`。
- M1 的本地可复现证据来自 Workspace Document/Conversation Attachment；真实网页搜索凭证集成将在对应报告中单列。
- Python 绝不回退到宿主执行；当前 Docker Sandbox Adapter 已通过真实隔离测试，但容器化 API 的独立 Sandbox Worker 和生产运行收口尚未完成。

项目不修改兄弟目录 `../backend`，也不在未经明确要求时 commit 或 push。
