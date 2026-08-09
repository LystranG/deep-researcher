# 深度研究工作台技术设计

**状态：** 实施基线 v1  
**日期：** 2026-08-06  
**权威来源：** `../product` 中的 PRD（位于父仓库）、父级 `CONTEXT.md`、可行性调研和项目交接文档。发生冲突时以 PRD 为准。

## 1. 设计目标与不可破坏约束

本项目交付一个可本地运行、可自动化验证的完整 Web 工作台，而不是接口占位或静态演示。所有读写路径都先解析当前用户和 Workspace，再访问 Conversation、Attachment、Workspace Document、Memory、Artifact、Plugin 或 Citation。

不可破坏的约束：

- Workspace 是数据、检索、记忆、产物和扩展授权的隔离边界。
- Conversation Attachment 默认只属于当前 Conversation；只有显式提升后才生成 Workspace Document。
- Message、Research Run、Research Task 和 Run Event 持久化；SSE 只是事件日志的传输视图。
- Citation 永久指向当时使用的不可变来源版本或网页快照，不随当前文档内容漂移。
- Long-term Memory 与 Conversation Context 分离，且必须有状态、范围和来源。
- Python、子代理和 MCP 工具只能通过受限 seam 调用；高风险操作先审批后执行。
- 不记录或展示模型隐式思维链，只保存用户可见进度、工具输入摘要、证据、产出和结果。
- 删除先从所有在线读取、检索和授权路径中立即失效；物理清理由可审计清理任务完成。

## 2. 技术栈与仓库结构

```text
deep-researcher/
  apps/
    api/                 FastAPI、SQLAlchemy、Alembic、研究编排与后台任务
    web/                 React 19、TypeScript、Vite
  docs/                  设计、计划、运行、安全、备份与限制
  infra/                 Docker Compose、数据库与沙箱运行配置
  scripts/               启动、验证、数据清理和备份入口
  var/                   本地对象、产物和开发数据库（不入库）
```

- Python 3.13，使用 `uv` 管理依赖与锁文件。
- FastAPI 提供 JSON、multipart upload 和 SSE；采用当前 `EventSourceResponse`/`ServerSentEvent` 语义。
- SQLAlchemy 2 + Alembic。开发/生产主路径使用 PostgreSQL；自动化业务测试使用 SQLite 临时库，PostgreSQL 专项测试验证锁和并发语义。
- React 19 + TypeScript + Vite；Vitest/Testing Library 验证用户交互，Playwright 验证端到端闭环。
- 原文件与 Artifact 经 `ObjectStore` interface 保存。首个 Adapter 是受根目录约束的本地文件存储；部署可替换为 S3 兼容 Adapter，领域层不接触宿主绝对路径。
- 模型经 `ModelGateway` interface 调用。OpenAI Adapter 使用 Responses API typed streaming，默认模型由配置指定；本地 Extractive Adapter 是可用的无密钥生产降级路径，不伪装成生成式模型。
- Web 搜索经 `WebSearchGateway` interface 调用；Brave Adapter 有密钥时启用，无密钥时任务明确标记为不可用/跳过，空间文档研究仍可完成。

## 3. 模块与 seam

### 3.1 API/Auth 模块

Interface：解析身份、校验 Workspace membership/role、验证请求 schema、返回稳定错误码。M1 支持内建本地用户并保留正式注册/登录数据模型；M3 开放成员管理 UI。任何 repository 方法都必须接收 `workspace_id`，跨 Workspace 主键查询不得绕过授权模块。

### 3.2 Workspace/Conversation 模块

Interface：创建、修改、归档、恢复、删除 Workspace 和 Conversation；提供删除影响预览。实现采用软删除/状态字段立即阻断读取，后台清理对象与派生索引。重名通过 UUID 区分。

### 3.3 Run Coordinator 模块

Interface：`start_run(command) -> run_id`、`cancel_run(run_id)`、`stream_events(run_id, after_seq)`。调用者不需要知道线程、模型或检索实现。

实现规则：

1. 发送消息时携带 `Idempotency-Key`；数据库唯一约束为 `(conversation_id, idempotency_key)`。
2. 同一事务创建 user Message、assistant 占位 Message 和 Research Run。
3. 每个后台工作线程使用独立 SQLAlchemy Session；Session 绝不跨线程或 asyncio task 共享。
4. 每条 Run Event 有 `(run_id, seq)` 唯一键。PostgreSQL 下锁定 Run 行并递增 `next_event_seq`；SQLite 测试 Adapter 额外使用进程锁。
5. SSE 的 `id` 等于 `seq`，客户端通过 `Last-Event-ID` 或 `after` 续传。重放只读取事件，不重新执行研究。
6. assistant delta 可作为事件增量显示；最终 Message 只在一次原子完成事务中写定。重新连接不会创建第二条 Message。
7. `cancel_requested_at` 是持久化取消信号。任务在检索、模型、工具和写事件前检查；已开始的外部调用设置短超时并在返回后丢弃取消后的新结论。

状态：

```text
Research Run: queued -> running -> waiting_approval -> completed
                               \-> cancel_requested -> cancelled
                               \-> failed
Research Task: pending -> running -> completed | skipped | failed | cancelled
```

### 3.4 Research Engine 模块

Interface：输入冻结的 `ResearchContext`（Workspace 指令、当前 Conversation 窗口、有效记忆、可见来源、预算），输出 task/state events、证据集合和带 Citation 标记的最终回答。

- 简单问题可直接走 retrieve/write；复杂问题由 Planner 生成有限 DAG。
- M1 固定 planner/researcher/writer 三个角色思想，但不创建自由递归代理。
- 每个任务独立保存查询、角色、状态、失败影响和预算。
- Writer 只能引用当前 Run 已检索并冻结的 Evidence Span；结构化输出失败时由 Citation Validator 拒绝未知引用并降级为抽取式回答。
- 参考 backend 只迁移规划、检索、总结、报告和可见工具事件的思想；不迁移跨请求 `SummaryState`、共享全局 SearchTool、Markdown NoteTool 状态或线程共享 ORM Session。

### 3.5 Attachment/Document Processing 模块

上传采用两阶段：流式写临时对象并校验大小/hash/MIME，再在事务中登记 Attachment。限制默认单文件 50 MB、每条消息 20 个，前端发送前展示。

状态：`uploading -> processing -> ready | failed`。解析器：

- PDF：页级文本和页码；DOCX：段落顺序；TXT/Markdown/CSV/JSON/代码：确定性文本读取。
- PNG/JPEG/WebP：保存尺寸和哈希；可用 OCR Adapter 时保存文本块与坐标，不可识别时明确说明，绝不生成伪 OCR。
- 所有 chunk 保存 `ordinal/page/start_offset/end_offset/content_hash`。

提升 Attachment 时生成 Workspace Document 与不可变 Document Version，并复用同一 blob/hash/chunk；替换只新增版本。旧 Citation 始终保留旧 version。

### 3.6 Retrieval/Citation 模块

Interface：`retrieve(scope, query, limit) -> EvidenceSpan[]`。scope 同时包含 Workspace、当前 Conversation 和允许的来源类型。

- M1 先实现确定性全文/词项排名；查询条件必须是 `workspace_id AND (workspace document OR current conversation attachment)`。
- 网页结果先抓取并保存不可变 Source Snapshot、URL、抓取时间、hash，再切 chunk；模型不能只引用搜索摘要中的裸 URL。
- Citation 保存 answer Message 版本、answer 字符范围、source kind/version、chunk、Evidence Span 范围和 snapshot hash。
- Citation 打开时返回当时的文件名/页码/原文，或标记网页来源失效；不静默替换。

### 3.7 Memory 模块（M2）

Interface：创建候选、确认/自动生效、冲突处理、编辑、停用、删除、按查询召回。

- 状态：`candidate | active | conflicted | inactive | deleted | expired`。
- 范围：`user | workspace | conversation`；空间记忆必须带 Workspace。
- 每条记录包含结构化类型、内容、敏感级别、来源 Message/Event、有效期和修订链。
- 低风险偏好可按空间策略自动生效；身份、健康、财务、机密和高影响事实必须确认。
- 冲突不能覆盖旧值；创建 conflict group，由用户保留、替换或并存。
- 召回只返回 active/未过期/范围匹配记录，并在影响回答时生成 memory_used 事件。

### 3.8 Python Sandbox 模块（M2）

Interface：提交代码、授权输入和预算，返回可取消执行与 Artifact；领域层不接触 Docker socket。

首个真实 Adapter 使用短生命周期 Docker 容器：非 root、`--network none`、只读根文件系统、`cap-drop=ALL`、`no-new-privileges`、PID/CPU/内存/磁盘/输出/超时限制；只将用户明确选择的输入只读挂载，并将独立输出目录作为唯一可写挂载。进程 ID 与容器 ID 仅存于执行器内部，取消时终止并清理容器。Docker 不可用时 UI 明确显示“沙箱不可用”，不能回退到宿主 Python。

### 3.9 Subagent 模块（M2）

子代理是 Research Run 内的受限 Research Task Adapter：固定角色 `researcher/analyst/verifier/writer`，最大深度 1、并发/时间/token/费用预算、工具 allowlist 和父取消信号。子代理只能追加自己的事件、证据和候选笔记，不能改成员、权限、Workspace 指令或 active Memory。

### 3.10 Evidence Check 模块（M2）

输入包含 Message 版本、选区 start/end/text 和原 Citation IDs。先校验字符范围，再拆分主张，优先读取原 Citation，必要时在同一 Workspace 范围补检索。每个 claim 只返回 `supported | contradicted | insufficient | not_checkable`，并保存证据、理由、模型版本和“当前证据支持度”声明。

### 3.11 Extension Gateway 模块（M2）

Skill 是只读、版本化 manifest：instructions、输入/输出 schema、allowed tools、required capabilities 和 hash，不含可执行代码。

MCP Plugin 的有效工具集合为以下交集：

```text
system enabled
∩ installed version
∩ Workspace grant
∩ Conversation override
∩ agent role allowlist
∩ current approval (high-risk only)
```

Server 自报 `readOnlyHint`/`destructiveHint` 只作提示；后端风险分类和管理员策略才有权决定。工具调用先写 Tool Run；写入、删除、发送、发布和代码执行创建一次性 Tool Approval，批准绑定精确参数 hash、run、user 和过期时间。拒绝/停用立即使等待审批失效。日志只保存安全摘要，不保存 token/secret。

### 3.12 Marketplace/Collaboration Governance 模块（M3）

- Plugin/Skill Package 与 Version 分离；版本内容不可变，checksum 与 Ed25519 签名可验证。
- 发布流程：draft -> submitted -> approved/rejected -> published -> suspended；只有审核通过版本可安装。
- Installation 固定版本；升级先校验签名/权限差异，支持回滚到仍受信版本。
- Workspace Member 角色：owner/admin/editor/viewer；权限在服务端逐操作检查。
- Audit Event 记录成员、授权、发布、升级、审批和高风险调用。
- Usage Ledger 按 Workspace/Run/Tool 记录请求、token、沙箱秒数和存储增量；Quota Policy 在执行前确定性拒绝超额请求。

## 4. 持久化模型

核心表按里程碑增量迁移：

- 身份/空间：`users`, `auth_sessions`, `workspaces`, `workspace_members`, `audit_events`。
- 会话：`conversations`, `messages`, `message_revisions`。
- 运行：`research_runs`, `research_tasks`, `run_events`, `run_checkpoints`。
- 文件：`attachments`, `message_attachments`, `documents`, `document_versions`, `chunks`, `artifacts`。
- 来源：`source_snapshots`, `citations`。
- 记忆/核验：`memories`, `memory_revisions`, `memory_conflicts`, `verification_jobs`, `verification_claims`, `verification_evidence`。
- 扩展：`extension_packages`, `extension_versions`, `extension_reviews`, `extension_installations`, `extension_grants`, `conversation_extension_overrides`, `tool_approvals`, `tool_runs`, `ratings`。
- 治理：`usage_ledger`, `quota_policies`。

所有 Workspace 所属表均冗余保存 `workspace_id`，并使用复合唯一键/外键防止把 A 空间子对象关联到 B 空间父对象。服务层 scope 检查与数据库约束形成双层防护。

## 5. 前端边界与交互

- Auth：注册/登录与当前身份。
- Workspace 列表/概览：创建、编辑、归档、恢复、删除影响预览。
- Conversation 工作台：会话列表、消息流、上传区、Research Task 时间线、停止/重试、SSE 恢复。
- Document/Citation 抽屉：处理状态、提升空间、版本、页码和 Evidence Span。
- Memory Center：候选/有效/冲突/停用记录和来源。
- Sandbox/Artifact：代码、目的、输入、限制、审批、日志和下载。
- Evidence Check：选区操作、claim 四态与证据。
- Extensions/Marketplace：目录、权限、版本、启用范围、审批、调用历史、升级/回滚、审核。
- Members/Usage：成员角色、审计、配额和用量。

SSE 状态由独立 Event Store 管理，React 通过 `useSyncExternalStore` 订阅；store 按 `(run_id, seq)` 去重并持久化最后序号，组件挂载/卸载不会重复注册流。

## 6. 接口草案

接口以 `/api/v1` 为前缀。核心路由：

- `/auth/register|login|logout|me`
- `/workspaces`, `/workspaces/{id}/archive|restore|delete-preview`
- `/workspaces/{id}/conversations`, `/conversations/{id}/messages`
- `/conversations/{id}/attachments`, `/attachments/{id}/promote`
- `/workspaces/{id}/documents`, `/documents/{id}/versions`
- `/runs/{id}`, `/runs/{id}/cancel`, `/runs/{id}/events`
- `/citations/{id}`
- `/workspaces/{id}/memories`, `/memories/{id}/confirm|deactivate|resolve-conflict`
- `/runs/{id}/sandbox-executions`, `/sandbox-executions/{id}/cancel`
- `/messages/{id}/evidence-checks`
- `/extensions/catalog|installations|grants`, `/tool-approvals/{id}/approve|reject`
- `/marketplace/packages|versions|reviews|ratings`, `/workspaces/{id}/members|usage|policies`

错误体统一为 `code/message/details/request_id`；越权对象统一返回 404，避免泄露其存在。

## 7. 测试与完成证据

测试只验证业务行为，不验证枚举、字段、类或方法数量。

- Backend unit：chunk 坐标、权限策略、引用校验、记忆冲突、预算和风险分类。
- Backend integration：真实临时数据库、HTTP、对象目录和事件日志；关键并发语义另跑 PostgreSQL。
- Contract：OpenAPI schema 和前端客户端字段兼容。
- Frontend：用户能看见并操作每个状态，拒绝/失败/恢复路径有断言。
- E2E：浏览器完成 AC-01 至 AC-12；外部模型/搜索使用本地确定性 Adapter 验证产品闭环，另设带凭证的真实集成套件并分别报告。
- Security negative：跨空间读取、私有附件检索、路径穿越、超限上传、未审批工具、停用插件、沙箱文件/网络、角色越权。
- Recovery：SSE 断开重连、相同幂等键重试、运行取消、后台任务失败重试、历史 Citation 版本。

每个里程碑报告实际命令以及 `passed/failed/skipped`；外部集成未执行时单列，不与本地业务测试混为“已验证”。

## 8. 明确假设与待确认项

以下采用 PRD 允许的非阻塞默认，写入配置而不是散落在代码中：

1. M1 以单用户本地部署为首要体验，但从第一版保存 User/Workspace membership；M3 开放协作 UI。
2. 单文件 50 MB、单消息 20 个；Workspace 总容量默认 5 GB，可由 Quota Policy 调整。
3. 删除立即逻辑失效；开发默认保留 7 天后物理清理，生产部署必须显式配置。
4. 所有消息走统一 Research Engine；简单问题由 planner 决定直接回答，不另设分叉产品模式。
5. M2 低风险记忆自动生效有 Workspace 总开关，默认开启且始终可见/可撤销。
6. M2 内置 `来源比较` Skill 和一个只读 HTTP MCP 示例；任意第三方发布留到 M3 审核流程。
7. M3 支持受审核目录，不默认开放无需审核的社区自由发布。
8. OpenAI、Brave、远程 MCP 和外部签名托管均为可选外部集成；本地核心闭环不能依赖其凭证。

这些假设不改变 M1 产品方向；若用户后续明确不同决策，以迁移/配置方式调整。

## 9. 设计依据核验

- FastAPI 当前文档支持带 `id` 的 SSE 事件及 `Last-Event-ID` 续传，因此采用事件日志重放而非重新执行。
- SQLAlchemy 2 文档明确 Session 非线程安全，因此每个请求/后台线程独立 Session，事务在最外层划定。
- React 19 使用 `useSyncExternalStore` 管理外部流式状态订阅，避免组件 Effect 重复连接。
- OpenAI 当前 Responses API 使用 typed SSE；Adapter 仅消费 `response.output_text.delta`、完成和错误等公开事件，不把供应商事件直接暴露为产品事件。
- Docker 隔离只是一个真实 Sandbox Adapter，不把容器等同于绝对安全；无 Docker 时拒绝执行，绝不降级宿主 Python。

