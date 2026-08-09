# 深度研究工作台实施进度

**更新时间：** 2026-08-08  
**权威范围：** PRD M1 → M2 → M3  
**状态定义：** ✅ 已完成；🟡 部分完成；⬜ 未开始；🔴 当前验证失败

> “已完成”必须同时满足真实持久化、可操作路径和业务行为验证。只有代码或页面存在、但缺少完整闭环或验收证据的项目标为“部分完成”。

## 1. 总体状态

| 范围 | 状态 | 结论 |
|---|---|---|
| 工程基线 | 🟡 | API、Web、迁移、测试和本地 PostgreSQL 已建立；全新独立 Compose project 的启动、迁移和健康检查已验证 | 统一错误体/request ID、正式 scripts 目录和完整运行文档仍缺 |
| M1 多轮空间与可信资料 | 🟡 | 核心用户闭环、M1 AC 和全新 Compose 启动已验证 | 部分计划项与生产运维文档未收口，因此不按完成定义宣称 M1 全部完成 |
| M2 记忆、核验与执行能力 | 🟡 | 本地第一版已完成：Memory、Docker Sandbox、受限任务 DAG、Researcher 部分失败、Evidence Check、受信任 Skill、预算/Usage Ledger、本地受信 MCP、有效工具交集与一次性 Tool Approval 已形成公共 API/SSE/UI 闭环 | 当前迁移与预算/审批逻辑的真实 PostgreSQL 专项，以及 OpenAI、Brave、远程 MCP、签名服务外部证据未验证 |
| M3 插件市场与协作治理 | 🟡 | 仅完成 Workspace token/费用 Quota Policy 与 Run Usage Ledger 的后端基础能力 | Marketplace、签名审核、升级回滚、协作角色、管理 UI 与组织治理尚未开始 |
| 完整项目收口 | ⬜ | 尚未达到；不得宣称完整项目完成 |

## 2. 工程与 M1 明细

| 模块 | 状态 | 已完成 | 未完成/限制 |
|---|---|---|---|
| 工程基线 | 🟡 | FastAPI、SQLAlchemy/Alembic、React/Vite、uv/npm 锁文件、PostgreSQL/SQLite 测试路径、Auth、健康检查、独立 Compose 全新库启动 | React Router、统一错误体与 request ID 未完整落地；scripts/生产运维入口和 Serena 收口未完成 |
| M1-1 Workspace | ✅ | 创建、列表、编辑、归档/恢复、删除预览、确认删除、成员隔离和 UI | M3 多人角色不属于本阶段 |
| M1-2 Conversation/Message | 🟡 | 多会话 CRUD、持久化 Message、当前会话纠正、跨会话隔离 | 独立 MessageRevision、通用会话摘要/窗口策略仍未完成 |
| M1-3 Run/SSE/幂等 | 🟡 | Run/Task/Event 持久化、原子 seq、SSE 重放、latest/active run 恢复、客户端去重；等待审批时 SSE 会在发送现有事件后有界返回，批准/拒绝后恢复同一 Run | 显式 heartbeat 和更完整 checkpoint/retry 仍缺 |
| M1-4 取消与失败恢复 | 🟡 | 持久取消信号、provider delta 间取消 token、任务取消传播、Researcher 单分支失败后降级回答、停止 UI、取消负向测试 | 单任务重试和部分失败后的前端用户操作闭环未完整实现 |
| M1-5 Attachment | 🟡 | 文件上传、大小/hash、安全 key、解析状态、PDF/DOCX/TXT/Markdown/CSV/JSON/代码解析、图片元数据；前端切换会话会清除上一会话附件展示 | OCR/病毒扫描 Adapter、完整伪 MIME 检测和前端逐文件重传仍缺 |
| M1-6 Workspace Document | ✅ | 显式提升、跨会话检索、不可变版本、旧 Citation 保留 | — |
| M1-7 Retrieval/Citation | 🟡 | Workspace + 当前会话范围检索、PDF 页码、网页快照、Citation 详情和来源抽屉 | 通用结构化 Citation Validator、来源失效刷新流程和引用精度评测未完成 |
| M1-8 Research Engine | 🟡 | Extractive/OpenAI Adapter、Brave Adapter、任务和流式回答 UI | 简单/复杂问题路由仍是固定流程；OpenAI/Brave 凭证下真实外部集成未执行 |
| M1-9 验收门 | 🟡 | M1 Playwright `1 passed`；PostgreSQL seq 并发专项通过；M1 AC 已进入自动化回归；独立 Compose 全新库 API/Web/PG 健康验证通过 | 生产准备、清理、故障处理文档未齐；历史 Compose volume 仍不能直接启动，见第 5 节 |

## 3. M2 明细

| 模块 | 状态 | 已完成 | 未完成/限制 |
|---|---|---|---|
| M2-1 Long-term Memory | 🟡 | 候选/确认/自动生效、secret 拦截、敏感度、编辑/停用/删除、来源修订链、冲突三种解决动作、过期过滤与可见 `expired` 状态、`memory_used`、Memory Center、Workspace 自动生效开关 UI、跨 Workspace 负向测试 | 自动提取目前只识别明确“请记住：”；只召回词项最高分的一条；过期状态在用户读取 Memory Center/详情时转换，尚无独立后台治理任务 |
| M2-2 Python Sandbox/Artifact | 🟡 | 真实 Docker、无网络、只读根、非 root、cap/PID/CPU/内存/tmpfs/超时限制、显式只读输入、取消、Artifact hash/下载/ACL、daemon 不可用不回退宿主；Agent 按研究需要创建持久化 Sandbox Todo，结果/失败摘要投影回 Todo、SSE、回答和 Artifact 展示，前端不再要求用户手写代码触发；独立 Worker 复用同一 Sandbox Adapter | 容器化 API 尚未拆分为独立 Sandbox Worker 进程；硬磁盘配额和生产级销毁监控未完成 |
| M2-3 受限 Subagent DAG | 🟡 | 固定 researcher/verifier/writer、深度 ≤ 1、工具 allowlist、Planner 后与 Writer 前 token/费用预算预留、Usage Ledger 结算/释放、父 Run 取消传播；新增 Todo 状态持久化、Sandbox Todo 幂等键、取消优先和运行完成前等待 Sandbox 终态；预算不足会在模型调用前失败并跳过下游任务；Researcher 单分支失败可继续 Verifier/Writer 并以“证据不足”降级，未知 Citation 仍被拒绝 | 尚未覆盖多模型/工具/沙箱/存储的细粒度预算；禁止递归和部分失败后的前端重试操作未完整实现；新预算逻辑尚无真实 PostgreSQL 并发验证 |
| M2-4 Evidence Check | 🟡 | Message 版本和选区校验、四态后端判定、原 Citation、理由/证据/模型版本持久化、AC-08 支持态测试、回答选区到核验结果的前端操作 UI | Claim 拆分、补充检索、完整四态/跨 Workspace 业务测试仍未完成 |
| M2-5 Skill 目录 | ✅ | 受信任不可执行 manifest、版本/hash/schema/capability/allowed tool、用户安装与 Workspace 启用分离、Conversation override、可操作扩展抽屉、内置“来源比较”真实研究行为与 `skill_applied` 事件 | 当前目录仅含内置 `来源比较`；第三方发布与审核仍属于 M3 |
| M2-6 MCP Gateway | ✅ | 官方 MCP Python SDK v2 本机 Streamable HTTP Adapter、分页工具发现、Workspace grant、Conversation Skill override、Skill `allowed_tools` 与 Agent allowlist 有效交集；扩展 UI 展示实际子集；Tool Call/Approval/Run、参数 hash、到期和用户绑定、LangGraph interrupt/resume、Tool Run CAS、调用前最终校验、批准后一次执行、拒绝/过期/停用/取消零副作用、失败分类安全摘要均有公共行为证据 | 仅验证本机受信 HTTP MCP；远程 MCP、Marketplace 和组织策略不在当前本地闭环 |
| M2-7 M2 验收门 | 🟡 | 本地 deterministic M2 总验收通过；Memory/Sandbox/Evidence/MCP 已有公共 API 与 UI 负向回归，Playwright 覆盖审批刷新、拒绝、取消、ACL、附件隔离和 Memory/Skill 分层停用；运维、安全与恢复说明已补齐 | 未配置真实 PostgreSQL 专项 URL；OpenAI、Brave、远程 MCP 和外部签名服务均缺少当前会话真实证据 |

## 4. M3 明细

除 M3-4 的局部后端基础能力外，以下范围均为 ⬜ 未开始：

- M3-1 Marketplace Package/Version/Review、Ed25519 签名与发布审核。
- M3-2 显式升级、失败保护、回滚和 Rating。
- M3-3 owner/admin/editor/viewer、邀请、移除和协作审计。
- M3-4 为 🟡 部分完成：Workspace token/费用 Quota Policy、Run 预算预留/结算/释放和 Usage Ledger 已实现；搜索/工具/沙箱/存储细分、管理 UI、组织级禁用策略和真实 PostgreSQL 并发扣减验证未完成。
- M3-5 全项目 AC-01~12、备份恢复、生产准备、安全与可观测性收口。

## 5. 当前验证证据与已知失败

2026-08-08 M2 本地第一版收口后的最近一次全量门禁：

- Backend：Tests run `71`，passed `66`，failed `0`，skipped `5`；新增计算型研究的 Agent Sandbox Todo 公共 HTTP/SSE 验收。
- Tool Approval/MCP focused：Tests run `13`，passed `13`，failed `0`，skipped `0`，包含有效工具交集、Agent allowlist 和真实本机 Streamable HTTP MCP Server。
- Frontend Vitest：Tests run `7`，passed `7`，failed `0`，skipped `0`。
- Playwright Chromium：Tests run `5`，passed `5`，failed `0`，skipped `0`；本机测试 MCP 仅用于审批 UI 拒绝和恢复验收。
- Ruff：`apps/api/src` 和 `apps/api/tests` 通过。
- Mypy：`31 source files` 通过。
- TypeScript typecheck、Vite build、`git diff --check`：通过。
- 全新隔离 SQLite migration：升级到 `h5i6j7k8l9` 且 `alembic check` 通过。

本地第一版结论：M1/M2 的可获得 deterministic 公共行为、UI、迁移和安全负向门禁已通过；M2 不再有本机代码闭环阻塞。由于 PostgreSQL 专项与外部服务配置缺失，只能表述为“本地第一版完成、外部证据未验证”，不能表述为生产就绪或完整 M3 项目完成。运维与恢复边界见 `docs/operations/2026-08-08-m2-operations-and-recovery.md`。

本会话未配置 `DEEP_RESEARCHER_POSTGRES_TEST_URL`，PostgreSQL 专项 Tests run `5`，passed `0`，failed `0`，skipped `5`。早于 `g4h5i6j7k8`、新预算逻辑和 Tool Approval/MCP 的 PostgreSQL 结果不作为当前迁移、并发预留、结算、释放、checkpoint 恢复或工具幂等的验证证据。

历史 Compose 状态（保留的原 volume）：

- PostgreSQL：healthy。
- Web：运行在 `:8080`。
- API：🔴 `Exited (1)`。
- 已确认原因：现有 PostgreSQL volume 曾由 `Base.metadata.create_all` 建表，但没有对应 Alembic version；API 启动执行 `alembic upgrade head` 时在首个迁移遇到 `DuplicateTable: relation "users" already exists`。
- 不对该 volume 执行 `alembic stamp` 或清理。全新独立 Compose project 已验证通过，但若要修复该历史 volume，仍需先确认数据价值与校准策略。

未执行的外部集成：OpenAI/LiteLLM 有凭证调用、Brave Search 有凭证调用、远程 MCP、外部签名托管。本机 MCP 自动化不得表述为远程 MCP 已验证。

## 6. 知识图谱候选评估（未批准、未实现）

知识图谱适合补强以下能力：

- 从多个文档中统一实体，例如公司、产品、指标和时间范围。
- 保存“主张—关系—证据片段—来源版本”，支持沿关系追溯证据。
- 表达相互支持、相互矛盾、替代、依赖和时效关系。
- 为 Evidence Check 和长期研究提供多跳检索候选。

它不应替代：Workspace ACL、原文件/Chunk、Long-term Memory 状态机、Citation、Run Event 或审计日志。

建议采用渐进路线：

1. 先在 PostgreSQL 增加 Workspace-scoped `entities`、`relations`、`claim_edges`，每条边强制关联 SourceChunk/Citation、来源版本、置信度和有效时间。
2. 先做派生索引，可从原始文档和 Citation 重建；不要让图成为唯一事实源。
3. 只有实体/关系规模和多跳查询证明关系表不足时，再评估 Neo4j 等独立图数据库。

复杂度判断：轻量关系索引为中等；可靠实体消歧、时态冲突和证据传播为高；双写图数据库、GraphRAG、Workspace ACL 和删除传播为很高。当前不建议在 M2 Skill/MCP 与 M3 完成前引入独立图数据库。

## 7. 后续严格顺序

1. 保留历史 Compose volume；仅在明确数据价值和校准策略后处理其 API 启动失败。
2. 配置隔离的 `DEEP_RESEARCHER_POSTGRES_TEST_URL` 后，使用当前迁移和预算/审批逻辑完成真实 PostgreSQL 专项。
3. 有真实配置时分别验证 OpenAI、Brave、远程 MCP 和签名服务，不得互相替代证据。
4. 外部证据收口并重新评审 M2 总门后才能进入 M3；不得提前实现 Marketplace 空壳。
5. 知识图谱仅作为后续增强候选，不插队替代当前权威实施顺序。

## 8. 2026-08-07 LangGraph 实施增量

本会话已完成一条可运行的确定性纵向切片：

- 复杂研究预算耗尽时，当前任务为 `failed`、下游任务为 `skipped`，每个任务都有可见 `failure_impact`，Run 通过 `run_failed` 事件结束
- 应用启动改为执行 `alembic upgrade head`，移除 runtime `Base.metadata.create_all()`；新增 Worker lease/heartbeat/attempt 迁移 `a1f2b3c4d5e6`
- 新增 PostgreSQL 行锁队列 `RunQueue`、租约续期和独立 Worker 入口 `deep-researcher-worker`
- 新增 Flat LangGraph `ResearchState`，使用 `Send` 完成受限 researcher fan-out/fan-in，并固定 `thread_id=run:{run_id}`
- 模型生成式路径改为 LiteLLM SDK Adapter；无凭证时仍需显式使用 Extractive Adapter，provider 失败不静默 fallback，暂时性错误最多重试两次

验证统计：Backend `36 passed, 0 failed, 1 skipped`；Frontend Vitest `5 passed, 0 failed, 0 skipped`；TypeScript typecheck、Vite build、Ruff、Mypy 和全新 SQLite Alembic upgrade/check 通过；Playwright Chromium E2E `1 passed, 0 failed, 0 skipped`（浏览器安装到 `/private/tmp/deep-researcher-playwright`）。

尚未完成：PostgreSQL Worker/AsyncPostgresSaver 真实连接专项、完整 RunEvent Projector、四类独立 Agent Module、Citation Validator、MCP/审批和更完整的 Playwright 负向场景。外部 OpenAI、Brave、远程 MCP 和签名服务仍未真实验证。
