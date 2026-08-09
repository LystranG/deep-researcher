# 深度研究工作台分任务实现计划

> 实际完成状态、验证统计和未完成项见 [2026-08-07 实施进度](../status/2026-08-07-implementation-progress.md)。本文件保留权威任务范围，不能仅凭复选框推断完成状态。

**实施顺序：** M1 完整验证后才进入 M2；M2 完整验证后才进入 M3。每个任务都交付可操作 UI、真实持久化、失败状态和业务行为测试。

## 0. 工程基线

- [ ] 建立 `apps/api`、`apps/web`、`infra`、`scripts`，生成 uv/npm 锁文件与统一命令。
- [ ] 配置 FastAPI app factory、SQLAlchemy/Alembic、React Router、API client、测试框架、lint/typecheck。
- [ ] 建立 PostgreSQL + 本地对象目录开发环境和 SQLite 临时测试环境。
- [ ] 实现注册/登录/当前用户最小闭环、统一错误体、request ID、健康检查。
- [ ] 补齐 Serena 项目记忆（实施阶段允许写入后执行）。

完成检查：后端/前端可启动，迁移可执行，健康检查、登录和空主页通过；不含业务占位假成功。

## M1：多轮空间与可信资料

### M1-1 Workspace 纵向闭环（FR-WS-001~003）

- [ ] Workspace/User/Member 模型、迁移和 scoped repository。
- [ ] 创建、列表、详情、编辑、归档/恢复、删除影响预览、二次确认删除。
- [ ] Workspace 列表和概览 UI。
- [ ] 测试重名、空名、归档可恢复、删除后不可读取/检索、空 Workspace 无串数据。

### M1-2 Conversation 与持久化 Message（FR-CONV-001~002）

- [ ] Conversation/Message/Revision 模型和 CRUD。
- [ ] 多会话导航、重命名、归档/恢复、删除。
- [ ] 当前 Conversation 消息窗口与摘要输入构造；其他 Conversation 私有消息不注入。
- [ ] AC-04：当前会话纠正影响后续追问，但不影响新会话。

### M1-3 Research Run 事件日志、SSE 与幂等（FR-CONV-003~004）

- [ ] 发送消息事务、幂等键、assistant 占位、Run/Task/Event 表。
- [ ] 原子事件 seq、`Last-Event-ID`/`after` 重放、heartbeat、最终 Message 固化。
- [ ] React Event Store、进度时间线、自动重连与序号去重。
- [ ] AC-06：断线恢复不重复 Message/Run/Event/最终回答。

### M1-4 取消与失败恢复（FR-RUN-002/004）

- [ ] Run/Task 状态机、持久取消信号、重试边界和失败影响。
- [ ] 停止按钮、失败/跳过/取消 UI。
- [ ] AC-12：停止后未完成任务取消，停止序号后不追加新研究结论。

### M1-5 Attachment 上传与处理（FR-FILE-001~002）

- [ ] 流式大小/hash/MIME 校验、安全对象 key、数量限制和路径穿越防护。
- [ ] PDF/DOCX/TXT/Markdown/CSV/JSON/代码解析；图片元数据与可选 OCR。
- [ ] 上传前移除、上传/处理/可用/失败、重传 UI。
- [ ] 测试超限、伪 MIME、解析失败、不可识别图片不编造。

### M1-6 提升 Workspace Document 与版本（FR-FILE-003）

- [ ] Attachment 私有 scope 查询；显式 promote 创建 Document/Version/Chunk。
- [ ] 文档列表、提升操作、替换新版本、旧版本保留。
- [ ] AC-02/AC-03：未提升跨会话不可检索，提升后新会话可检索。

### M1-7 检索与稳定 Citation（FR-CITE-001~002）

- [ ] Workspace + 当前 Conversation 双范围检索和词项排名。
- [ ] Source Snapshot、Evidence Span、Citation Validator 和 Citation 详情。
- [ ] 前端行内 Citation 与来源抽屉。
- [ ] AC-01/AC-07：同名跨空间零泄漏；PDF 数字打开到文件名、页码、原文。

### M1-8 Research Engine 与模型/搜索 Adapter（FR-RUN-001~002）

- [ ] 简单/复杂路由、有限任务计划、研究任务执行、writer 结构化引用。
- [ ] Extractive Model Adapter 提供无凭证真实闭环；OpenAI Responses Adapter typed streaming。
- [ ] Brave Web Search Adapter、网页快照与失败降级；无凭证明确标记。
- [ ] 多轮 UI 展示任务、来源、增量回答和最终报告。

### M1-9 M1 验收门

- [ ] 自动化通过 AC-01、02、03、04、06、07、12。
- [ ] 运行 backend unit/integration、frontend unit、build/typecheck、Playwright E2E。
- [ ] PostgreSQL 事件序号并发专项测试。
- [ ] 记录命令和 passed/failed/skipped；外部 OpenAI/Brave 验证单列。
- [ ] 文档化启动、配置、附件目录、备份、清理和 M1 限制。

只有全部 M1 Must 闭环通过后，更新计划并进入 M2。

## M2：记忆、核验与执行能力

### M2-1 Long-term Memory 治理（FR-MEM-001~005）

- [ ] 候选、风险分类、确认、自动生效开关、编辑、停用、删除、来源修订链。
- [ ] 冲突/时效策略与解决 UI；不保存文件原文、完整对话、secret。
- [ ] 按 scope 召回并产生可见 `memory_used` 事件。
- [ ] AC-05：停用后新会话不再使用；跨 Workspace 不召回。

### M2-2 真实 Python Sandbox 与 Artifact（FR-CODE-001~004）

- [ ] Docker Sandbox Adapter、安全参数、授权输入、资源/时间/输出限制。
- [ ] 可见代码/目的/输入、执行/取消/超时/错误、Artifact 下载。
- [ ] AC-09：未授权文件与网络访问失败；宿主 secret 不进入容器。
- [ ] Docker daemon 不可用时报告未完成集成，不回退宿主执行。

### M2-3 受限 Subagent DAG

- [ ] 固定角色、深度 1、并发/token/时间/费用预算和工具 allowlist。
- [ ] 父取消传播、子任务事件/证据审计、部分失败继续策略。
- [ ] 测试预算耗尽、禁止递归、禁止越权工具、取消传播。

### M2-4 Evidence Check（FR-VERIFY-001~002）

- [ ] Message 版本与选区校验、claim 拆分、原 Citation 优先、补检索。
- [ ] 四态判定、证据/理由/模型版本持久化与 UI。
- [ ] AC-08：四态之一且不是二元“可信/不可信”；跨 Workspace 证据不可见。

### M2-5 Skill 可信目录（FR-EXT-001~003）

- [ ] 不可执行 manifest、版本/hash、schema/capability/allowed tool 校验。
- [ ] 安装与 Workspace 启用分离，Conversation override。
- [ ] 内置 `来源比较` Skill 的真实运行行为测试。

### M2-6 MCP Gateway、审批与审计（FR-EXT-004~007）

- [ ] 受信任 MCP 配置、工具/资源发现、确定性工具交集。
- [ ] 风险分类覆盖 Server 自报注解；secret 隔离与参数安全摘要。
- [ ] 一次性 Tool Approval、批准后精确参数执行、拒绝后研究继续。
- [ ] 停用立即阻断新调用和等待审批。
- [ ] AC-10/11：停用后零新增调用；未批准/拒绝均零副作用。

### M2-7 M2 验收门

- [ ] 自动化通过 AC-05、08、09、10、11，并回归全部 M1 AC。
- [ ] 真实 Docker 沙箱集成；本地受信任 MCP server 端到端测试。
- [ ] 安全负向测试与实际统计；文档化沙箱、审批、secret 和扩展限制。

## M3：插件市场与协作治理

### M3-1 Marketplace Package/Version/Review

- [ ] Package、不可变 Version、checksum、Ed25519 签名和发布者身份。
- [ ] draft/submitted/review/published/suspended 流程与管理员 UI。
- [ ] 只有审核/签名通过版本可安装；恶意/下架版本阻断新安装。

### M3-2 Upgrade/Rollback/Rating

- [ ] 权限差异提示、显式升级、失败不切换、历史受信版本回滚。
- [ ] 评分绑定真实安装用户并防重复；聚合结果真实持久化。
- [ ] 测试签名篡改、升级失败、回滚和停用传播。

### M3-3 Workspace 成员与角色

- [ ] owner/admin/editor/viewer 邀请、改角色、移除和最后 owner 约束。
- [ ] 共享 Workspace UI、服务端逐操作授权和协作 Audit Event。
- [ ] 测试 viewer 写入拒绝、editor 不得管理成员、被移除立即失效。

### M3-4 Usage/Quota/Organization Policy

- [ ] token/搜索/沙箱/存储/工具 Usage Ledger。
- [ ] Workspace 配额和组织禁用工具/域名/模型策略；执行前拒绝。
- [ ] 用量/配额 UI 与审计；并发扣减保持幂等。

### M3-5 M3 验收与全项目收口

- [ ] Marketplace、签名、升级回滚、角色、配额的端到端与负向测试。
- [ ] 回归 AC-01~12、构建、lint、typecheck、迁移、新库启动和备份恢复演练。
- [ ] 完成生产准备、安全模型、数据清理、备份恢复、可观测性和故障排查文档。
- [ ] 列出真实外部集成测试与未执行项；任何未完成 Must 不得标为项目完成。

## 实施纪律

- 每次只做一个小而完整的纵向闭环，先后端业务行为测试，再 UI 行为和 E2E。
- 测试失败代表真实功能损坏；不测试枚举/常量/字段/方法数量，也不以 `not null` 或“被调用”代替业务结果。
- 不修改 `../backend`，不清理任何现有 `.serena`，不 commit、不 push。
- 只有互不写冲突的只读/测试子任务才并行；任何外部凭证缺失都与本地业务证据分开报告。
