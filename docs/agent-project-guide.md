# Deep Researcher 项目学习指南

这是一份给新开发者的代码导览。目标不是复述所有类，而是回答三个问题：

1. 这个系统解决什么问题？
2. 一次研究请求从哪里进入，经过哪些 Agent、工具和数据边界？
3. 想修改某个能力时，应该先看哪些文件？

本文描述当前仓库的实际实现，同时明确标出“已经实现但尚未接入主运行路径”的能力。项目的统一术语以根目录 [`CONTEXT.md`](../CONTEXT.md) 为准，架构取舍以 [`docs/adr/`](adr/) 下的 ADR 为准。

## 1. 项目定位

Deep Researcher 是一个以 **Workspace（空间）** 为边界的深度研究工作台。用户在空间中创建会话，发送一个研究问题，系统基于空间内的文档、会话线索、长期记忆和网页来源，执行一次可恢复、可审计的 **Research Run（研究运行）**，最后生成带引用的回答，并可沉淀为跨会话复用的 Research Record（研究记录）。

它不是一个简单的“聊天 + LLM API”应用，核心设计关注：

- 长任务不阻塞 API 进程；
- 每次运行的来源和上下文可冻结、可追溯；
- Agent 的工具权限、审批和副作用可审计；
- 检索同时支持精确词项与语义召回；
- 研究结果必须经过证据核验和 Citation 校验；
- 取消、重试、Worker 崩溃和恢复都有持久化事实；
- 模型失败不能被静默伪装成成功。

## 2. 先记住的核心术语

### 2.1 Workspace、Conversation、Message

- **Workspace（空间）**：长期研究的顶层边界，包含成员、空间文档、共享知识、记忆、会话和启用的扩展。权限和检索可见性都以 Workspace 为重要边界。
- **Conversation（会话）**：空间内的一组多轮消息。
- **Message（消息）**：用户、助手或系统产生的一次可引用内容单元。

模型不要直接把“项目”“知识库”“线程”当作这些对象的同义词。代码中的数据库模型见 [`models.py`](../apps/api/src/deep_researcher/models.py)，API 资源和权限入口主要在 [`app.py`](../apps/api/src/deep_researcher/app.py)。

### 2.2 Research Run、Plan、Task

- **Research Run（研究运行）**：由一条用户消息触发的一次研究过程，有明确的 queued、running、waiting、completed、partial、cancelled、failed 等生命周期。
- **Research Plan（研究计划）**：一次运行内的有界子任务集合。它有依赖、完成条件、工具范围和预算。
- **Plan Version（计划版本）**：不可变的计划快照。重规划产生新版本，不原地修改旧计划。
- **Research Task（研究任务）**：计划中的一个有限工作单元，不等于 Agent 类型。
- **Task Outcome（任务结果）**：控制器基于执行事实作出的不可变裁决，不是模型自己说“完成了”。
- **Task Result Proposal（任务结果提案）**：模型提交的候选结果，必须经过结构、证据和完成条件核验，才能形成 Task Outcome。
- **Task Iteration（任务迭代）**：一个有限推进周期的执行记录，通常包含一次 Model Turn、工具调用、Observation、用量和继续/等待原因。

Research Run、ResearchPlan 和 ResearchTask 的 ORM 定义位于 [`models.py:154`](../apps/api/src/deep_researcher/models.py:154)。计划和 Task Runtime 的值对象与控制器位于 [`task_runtime.py`](../apps/api/src/deep_researcher/task_runtime.py)。

### 2.3 证据、主张与引用

- **Knowledge Source（知识来源）**：空间文档、当前会话附件或网页内容。
- **Source Snapshot / Source Chunk**：来源在某一时点的不可变快照和分块。
- **Evidence Span（证据片段）**：能直接支持或反驳主张的最小原文范围。
- **Claim（主张）**：准备表达、可被证据支持或反驳的陈述。
- **Citation（引用）**：回答具体文本与可定位来源的连接。
- **Derived Evidence（派生证据）**：由固定来源或证据片段经过成功 Sandbox 计算得到的、带执行记录的证据候选。
- **Evidence Gap（证据缺口）**：缺少支持某个研究目标的证据，或证据之间存在冲突。

关键原则是：向量分数、Rerank 分数、历史会话摘要和模型自己的判断都不能直接成为 Citation。最终引用必须回到冻结来源的原文范围和 hash。

### 2.4 记忆与执行状态

项目没有一个名为“中期记忆”的单一数据库表。实际需要区分以下几类状态：

- **Conversation Context**：当前会话的近期消息和摘要，用于保持当前对话连贯。
- **Conversation History Lead**：从同一空间其他会话找回的低信任线索，只用于定位旧讨论，不能单独作为事实证据。
- **Long-term Memory（长期记忆）**：经过规则或用户确认后生效，可跨会话召回的偏好、事实、约束或决策。
- **Research Record（研究记录）**：已经通过核验的 Claim + Evidence，属于 Workspace 级长期研究知识，不等同于 Memory。
- **Research Ledger（研究账本）**：某一次 Research Run 的可恢复执行事实，包括目标、进度、覆盖判断、证据积累和停止原因。
- **TaskModelTurn / TaskObservation**：单个 Task 的模型轮次和工具观察，是任务中期执行记录。
- **Graph checkpoint**：LangGraph 恢复资料，只保存 Graph 内部阶段和结构化引用，不是用户可见事实源。
- **SourceMapWork**：长来源 Map 阶段的可恢复中间工作，完成后经过 reduce 和原文核验。

这个划分是 ADR-0001 和 ADR-0012 的核心。领域数据库负责用户可见事实，Graph checkpoint 只负责编排恢复。

## 3. 总体架构

### 3.1 运行时分层

```text
Web UI
  ↓ HTTP / SSE
FastAPI
  ├─ 身份认证、Workspace ACL
  ├─ 创建 Message / ResearchRun / 初始 RunEvent
  └─ 写入取消、审批等用户命令
        ↓ PostgreSQL Run Queue
Dedicated Worker
  ├─ claim + lease + heartbeat + fencing epoch
  └─ ResearchCoordinator.execute_runtime_v2()
        ├─ Workspace Retrieval
        ├─ freeze_research_context()
        ├─ ResearchGraphRunner / LangGraph
        ├─ Sandbox Job Worker
        └─ 领域事实、账本、引用、事件投影
```

API 不在进程内用线程池运行长时 Agent。Worker 从 PostgreSQL 队列领取运行，租约过期后可以被其他 Worker 恢复。相关决策：

- [ADR-0004](adr/0004-agent-runtime-runs-in-a-dedicated-worker.md)
- [`run_queue.py`](../apps/api/src/deep_researcher/run_queue.py)
- [`worker.py`](../apps/api/src/deep_researcher/worker.py)
- [`worker_main.py`](../apps/api/src/deep_researcher/worker_main.py)

### 3.2 事实源与恢复源

领域数据库中的 `ResearchRun`、`ResearchTask`、`TaskOutcome`、`RunEvent`、`Citation`、`Memory`、`ToolApproval` 等决定产品对用户展示的状态。

LangGraph checkpoint 使用固定的 `thread_id`：

```text
run:{run_id}
```

它只保存 Graph 阶段、内部状态和恢复资料。SSE 不读取 checkpoint 直接展示，最终状态也不能靠 Graph 是否到达 `END` 推断。相关实现见 [`graph.py:37`](../apps/api/src/deep_researcher/graph.py:37) 和 ADR-0006。

### 3.3 事件投影与 SSE

Graph 回调不会把任意内部状态直接暴露给用户。协调器只把已提交的业务事实交给 [`RunEventProjector`](../apps/api/src/deep_researcher/run_event_projector.py)，再由 [`RunEventLog`](../apps/api/src/deep_researcher/event_log.py) 在数据库行锁下分配连续序号和幂等 `event_key`。

典型事件包括：

- `run_started`
- `retrieval_completed`
- `plan_created`
- `task_*`
- `model_turn_committed`
- `tool_call_committed`
- `observation_committed`
- `verifier_decided`
- `writer_committed`
- `citation_committed`
- `artifact_published`
- terminal event

前端通过 SSE 读取事件序列，因此断线后可以按 `seq` 重放，而不是依赖内存中的增量消息。入口可以从 [`app.py`](../apps/api/src/deep_researcher/app.py) 搜索 `EventSourceResponse` 和 `RunEvent`。

## 4. 一次研究运行的完整流程

### 4.1 API 创建阶段

用户发送消息后，API 负责：

1. 验证访问令牌和 Workspace 成员权限；
2. 创建或确认 Conversation；
3. 持久化 Message；
4. 创建 `ResearchRun(status="queued")`；
5. 写入初始 RunEvent；
6. 让 PostgreSQL 队列中的运行等待 Worker 领取。

API 不直接调用模型，不直接执行网页搜索或 MCP 副作用。

### 4.2 Worker 领取阶段

[`RunQueue.claim_with_epoch()`](../apps/api/src/deep_researcher/run_queue.py) 使用：

- `FOR UPDATE SKIP LOCKED`；
- `lease_owner`；
- `lease_expires_at`；
- `heartbeat_at`；
- `attempt / fencing_epoch`。

Worker 领取后启动 heartbeat。执行结束后释放租约。旧 Worker 即使在网络分区后恢复，也不能使用旧 epoch 继续提交领域事实。

### 4.3 Coordinator 准备上下文

主入口是 [`ResearchCoordinator.execute_runtime_v2()`](../apps/api/src/deep_researcher/coordinator.py:171)。它大致执行：

1. 校验运行状态、租约 owner 和 fencing epoch；
2. 读取触发 Message；
3. 查找待恢复的工具审批；
4. 计算当前生效的 Skill 和工具能力；
5. 读取会话纠正；
6. 执行 Workspace 检索；
7. 选择最佳证据、长期记忆和历史会话线索；
8. 必要时触发网页搜索；
9. 对来源做 Source Map 计划；
10. 调用 `freeze_research_context()` 固定单次运行输入；
11. 启动 Graph；
12. 处理 Sandbox 结果；
13. 校验回答和 Citation；
14. 固化账本、研究记录、消息和事件。

冻结上下文的结构见 [`research_context.py`](../apps/api/src/deep_researcher/research_context.py)：

```python
{
    "question": ...,
    "sources": [...],
    "correction": ...,
    "memory": ...,
    "conversation_leads": [...],
    "skills": [...],
}
```

运行开始后，原始来源不会随着 Workspace 后续变化而漂移；新来源只能以新的不可变版本追加。

## 5. LangGraph 编排

当前实际构建的 Graph 在 [`graph.py:271`](../apps/api/src/deep_researcher/graph.py:271)：

```text
START
  ↓
source_mapper
  ↓
planner
  ↓
tool_prepare
  ├─ 没有 ToolCall ───────────────┐
  └─ 有 ToolCall → tool_execution │
                                  ↓
                              researcher
                                  ↓
                              verifier
                                  ↓
                               writer
                                  ↓
                          citation_validator
                                  ↓
                                END
```

### 5.1 source_mapper

只有需要分析整页长来源时才有 Map work。`SourceMapLedger` 把多个 Chunk 按 token budget 分成有限工作单元。模型只能输出：

- Chunk digest；
- candidate claims；
- span locator；
- unresolved questions。

它不能直接创建 Citation、Evidence Span 或已核验 Claim。相关 Prompt 和 schema 在 [`source_map.py`](../apps/api/src/deep_researcher/source_map.py)。

### 5.2 planner

当前 Planner 是独立模块，不递归生成 Agent。它输出有界的 `TaskSpec` 和 `ResearchBrief`，包括：

- 角色；
- 目标；
- 成功条件；
- 依赖；
- token/time budget；
- allowed tools；
- depth。

实现见 [`agents/planner.py`](../apps/api/src/deep_researcher/agents/planner.py)。需要新增计划策略时先修改这里和对应测试，不要直接在 Graph 节点里拼计划。

### 5.3 tool_prepare 与 tool_execution

`tool_prepare` 在副作用前规范化工具意图，调用 [`ToolExecutionService.prepare()`](../apps/api/src/deep_researcher/tool_execution.py)，创建幂等的 `ToolCall` 和 `ToolApproval` 事实。

`tool_execution` 使用 LangGraph `interrupt()`：

1. 保存非敏感调用快照；
2. 将运行置为 `waiting_approval`；
3. 等用户审批；
4. 使用不含原始参数的 resume payload 恢复；
5. 再验证参数 hash、审批用户、有效期和 Workspace grant；
6. 在 `ToolRun` 幂等边界中执行一次副作用。

所以“进入 Graph 节点”不等于“工具已经执行”。工具调用需要经过 Policy、Grant、Approval 和参数 hash 检查。

### 5.4 researcher fan-out / verifier fan-in

`_route_researchers()` 用 `Send` 为每个 `ResearchBrief` 创建一个受限分支。Researcher gateway 返回 `ResearchFinding`，失败分支也会显式返回 `status="failed"`，不会被伪装成成功。

Verifier 收集全部 findings 后判断：

- `supported`；
- `contradicted`；
- `insufficient`；
- `not_checkable`。

这里的 fan-out 是有限并行研究分支，不是无限递归 Agent。

### 5.5 writer 与 citation_validator

Writer 从冻结上下文读取：

- 用户问题；
- 可引用证据；
- 纠正；
- 长期记忆；
- 历史会话线索；
- Source Manifest；
- 邻近原文窗口；
- 已启用 Skill。

Writer 只生成待校验草稿。`citation_validator` 再验证引用标记是否存在、位置是否正确、编号是否对应可引用来源。失败时可能退化为 citation-free partial answer 或使用冻结来源生成确定性 fallback，但不会凭空创造来源。

实现分别见 [`agents/writer.py`](../apps/api/src/deep_researcher/agents/writer.py) 和 [`citation_validator.py`](../apps/api/src/deep_researcher/citation_validator.py)。

## 6. 记忆系统与 RAG

### 6.1 文档与来源索引

附件处理器在 [`document_processor.py`](../apps/api/src/deep_researcher/document_processor.py)：

1. 从 txt、Markdown、代码、PDF、DOCX 等格式提取文本；
2. 按页或文本范围切 Chunk；
3. 保存 `start_offset`、`end_offset` 和 `content_hash`；
4. 如果 embedding gateway 可用，生成向量；
5. 将 embedding 状态显式记录为 ready 或 failed。

Embedding 失败不能静默伪装为成功索引。

### 6.2 Hybrid Retrieval

[`HybridRetrieval.retrieve()`](../apps/api/src/deep_researcher/retrieval.py:787) 支持四类候选：

- `source_chunk`：当前空间内的文档/来源 Chunk；
- `conversation_segment`：其他会话的低信任历史线索；
- `memory`：当前运行可见的长期记忆；
- `research_record`：已经核验、可跨会话复用的研究记录。

实际排序过程：

```text
Query
  ↓
Embedding
  ↓
各类 Adapter 召回
  ↓
Workspace ACL / resource scope / soft delete / validity 过滤
  ↓
content_hash 去重
  ↓
Lexical ranking + vector ranking
  ↓
RRF 融合
  ↓
Rerank Adapter
  ↓
Token Context Budget 组页
```

词项检索保留数字、版本号、实体名和精确短语能力；向量检索补充语义召回；RRF 融合两种排序；Rerank 做候选精排；最后还要按上下文 token 容量截断。

配置和组装在 [`app.py:780`](../apps/api/src/deep_researcher/app.py:780)。当前默认设计是 PostgreSQL 全文检索、pgvector 和 LiteLLM 配置的 Rerank Adapter，详情见 ADR-0012。

### 6.3 长期记忆

长期记忆对应 `Memory` 和 `MemoryRevision`，模型不是直接写入“最终记忆”。典型流程是：

```text
当前会话 / 研究运行
  ↓
候选记忆或用户明确操作
  ↓
scope、有效期、冲突检查
  ↓
Memory / MemoryRevision
  ↓
embedding 索引
  ↓
后续 Workspace Retrieval
```

运行中召回的长期记忆会被复制进 `FrozenResearchContext`，以保证本次回答使用的是启动时看到的有效版本。

长期记忆与 Research Record 的区别：

| 对象 | 内容 | 来源 | 作用 |
|---|---|---|---|
| Long-term Memory | 偏好、约束、空间事实、决策 | 用户确认或规则 | 影响未来回答 |
| Research Record | 已核验 Claim + Evidence | 研究运行 | 复用研究结论 |
| Conversation History Lead | 历史对话线索 | 其他会话 | 定位旧讨论 |
| Source Chunk | 原始来源分块 | 文档、网页、附件 | 直接支持 Citation |

### 6.4 “中期记忆”如何理解

如果从 Agent 系统常见的短期/中期/长期分类来理解本项目：

- **短期**：当前 `Conversation`、Message 和 `AnswerContext`；
- **中期**：ResearchLedger、Plan Version、TaskModelTurn、TaskObservation、SourceMapWork、ToolRun 和 Graph checkpoint；
- **长期**：Memory、MemoryRevision、ResearchRecord、研究文件和已发布 Artifact。

中期状态的特点是“服务一次研究运行的恢复与推进”，不是自动跨会话召回。它最终可能促成 Research Record 或 Memory Candidate，但不会自动变成长期事实。

## 7. 工具系统

### 7.1 工具分类

工具有两套相关但不同的抽象：

1. **Research Graph 工具**：由 [`ToolExecutionService`](../apps/api/src/deep_researcher/tool_execution.py) 管理，用于受信 MCP/外部能力，支持审批和 ToolRun。
2. **Task Runtime 工具**：由 [`ToolRegistry`](../apps/api/src/deep_researcher/task_runtime.py) 管理，给单个 ResearchTask 提供静态 schema、风险等级、handler、结果契约和 allowlist。

当前显式能力包括：

| 工具 | 用途 | 风险/策略 |
|---|---|---|
| `document_search` | 搜索空间文档 | Researcher allowlist |
| `web_search` | 获取网页搜索结果 | Researcher allowlist，外部模型可能强制要求 |
| `file_list` | 列出研究文件 | Task allowlist |
| `file_stat` | 读取文件元信息 | Task allowlist |
| `file_read` | 读取受限文件版本 | Task allowlist |
| `file_write` | 写入运行工作文件 | Task allowlist |
| `file_publish` | 发布研究产物 | 更高治理边界 |
| `citation_read` | 回读引用来源 | Verifier allowlist |
| `research_notes.create` | 写入受信研究笔记 | MCP grant + approval |
| `python_sandbox` | 受限计算、转换和派生证据 | 隔离执行，不逐次审批 |

实际 allowlist 位于 [`tool_execution.py` 顶部](../apps/api/src/deep_researcher/tool_execution.py)，文件工具定义位于 [`research_file_space.py`](../apps/api/src/deep_researcher/research_file_space.py)。

### 7.2 工具系统的优化与安全边界

工具调用的主要优化不是“让模型多调用工具”，而是减少不必要的副作用和不可恢复状态：

- **Prepare / Execute 分离**：先创建调用事实，再跨副作用边界；
- **参数 hash**：审批和执行必须匹配同一调用意图；
- **幂等 ToolRun**：重复恢复不会重复创建外部操作；
- **Tool Registry snapshot**：每个 Model Turn 使用不可变的工具定义快照；
- **Workspace grant**：外部 MCP 需要空间级授权；
- **风险策略**：工具有 safe、review、dangerous 等风险；
- **等待恢复**：审批或外部结果未完成时返回 waiting，而不是忙等；
- **安全摘要**：事件记录 safe summary，不把敏感参数直接暴露到用户事件；
- **失败分类**：权限、协议、传输、业务、校验失败分开；
- **预算和调用次数**：Task Runtime 受 turn、token、time 和工具预算约束。

### 7.3 Python Sandbox

研究型 Python Sandbox 是特殊工具：因为它在隔离环境执行，所以不需要每次进入用户审批，但仍要审计。

约束包括：

- 无网络；
- 非 root；
- 只读根文件系统；
- 只能接收显式输入引用；
- 不接收宿主路径或宿主凭证；
- 受 timeout、资源和输出大小限制；
- 输入、代码 hash、结果和 Evidence 都可追溯。

流程由 [`sandbox_jobs.py`](../apps/api/src/deep_researcher/sandbox_jobs.py) 管理：

```text
submit
  ↓
durable SandboxJob
  ↓
claim + lease
  ↓
DockerSandbox execute
  ↓
SandboxObservation
  ↓
DerivedEvidence / file revision / artifact
```

ADR-0011 明确了“隔离的研究计算无需逐次审批”的边界：如果代码需要外部系统访问、扩大输入范围或产生外部影响，就不再属于这个例外。

## 8. 上下文工程

### 8.1 上下文不是简单拼接全部历史

系统会先做权限和范围过滤，再根据 token budget 选择内容。一次回答的 `AnswerContext` 在 [`model_gateway.py`](../apps/api/src/deep_researcher/model_gateway.py) 中包括：

- `question`
- `evidence / evidences`
- `correction`
- `memory`
- `conversation_leads`
- `source_manifests`
- `source_windows`
- `skills`
- `cancellation_token`

这意味着不同信息的可信度和用途被显式分开：

| 上下文块 | 能否直接作为事实 | 能否直接作为 Citation | 用途 |
|---|---:|---:|---|
| Evidence 原文 | 是 | 是 | 支持回答 |
| Memory | 作为已选长期上下文 | 通常否 | 偏好/约束 |
| Conversation lead | 否 | 否 | 定位旧讨论 |
| Source Manifest | 否 | 否 | 导航长来源 |
| Source window | 辅助理解 | 引用仍回到原始来源 | 标题和邻近语境 |
| Skill | 不是事实 | 否 | 行为和格式约束 |
| Correction | 当前会话约束 | 否 | 修正模型误解 |

### 8.2 Prompt 拼接策略

LiteLLM Writer 在 [`LiteLLMModelGateway.astream_answer()`](../apps/api/src/deep_researcher/model_gateway.py) 中按固定结构拼接：

```text
目标与语言要求
成功标准
用户问题
会话纠正
长期记忆
历史会话线索
Source Manifest 导航
邻近原文窗口
已启用 Skill
可用证据（带 [n] 标签）
```

成功标准明确告诉模型：

- 只能把证据和会话纠正当作事实；
- Citation 必须紧跟使用的事实；
- 历史线索和 Manifest 不能直接引用；
- 没有证据要明确说不足；
- 不输出思维链、工具过程或不存在的引用。

### 8.3 上下文优化

项目目前采用几种互补的优化：

1. **Hybrid Retrieval**：先召回和排序，不把全部 Workspace 内容塞给模型；
2. **ContextBudget**：按输入预算和输出 reserve 计算 Evidence capacity；
3. **去重**：按 content hash 避免同一内容重复占用 token；
4. **分页/游标**：RetrievalPage 保存 omitted IDs、cursor 和 remaining tokens；
5. **Source Manifest**：长来源先用轻量导航定位，再按 chunk 回读；
6. **Source Window**：只提供邻近上下文，避免一次塞入整页；
7. **冻结上下文**：运行内保持输入稳定，避免回答中途来源变化；
8. **结构化输出**：Source Map 使用 schema，降低自由文本解析风险；
9. **流式回答**：Writer 可以 streaming，但首个 delta 产生后不透明重放请求；
10. **取消优先**：模型增量和工具执行前后都会检查 CancellationToken。

### 8.4 模型接入和重试

模型抽象在 [`ModelGateway`](../apps/api/src/deep_researcher/model_gateway.py)。首版 Planner、Researcher、Verifier、Writer 共享受控 `model_alias`，但模块、schema、工具和预算独立。

模型提供两类能力：

- structured completion：Source Map 或结构化 Agent 输出；
- streaming answer：Writer 生成最终回答。

LiteLLM 策略：

- 首选进程内 LiteLLM SDK Adapter；
- timeout、429、暂时性 5xx 最多有限重试；
- 已经产生 streaming delta 后不重放；
- 不做静默 provider fallback；
- 无外部凭证时使用显式选择的 `ExtractiveModelGateway` 开发 Adapter。

相关 ADR：0007、0008、0009、0010。

## 9. 多 Agent 系统

### 9.1 Agent 角色

当前逻辑角色包括：

- **Planner**：把问题拆成有限任务和研究分支；
- **Researcher**：在限定来源、工具和深度内取得研究发现；
- **Verifier**：检查证据是否支持、缺失或冲突；
- **Writer**：根据冻结上下文形成回答草稿；
- **Citation Validator**：检查回答是否可以安全发布；
- **python_sandbox task role**：在隔离环境完成计算或生成派生证据。

这些角色不是无限递归的独立人格 Agent，而是受顶层 Research Run 约束的模块。每个角色拥有不同输入输出、工具、权限、预算和生命周期。

### 9.2 两种并行模型

Graph 层的并行：

```text
Planner
  ↓
Send(ResearchBrief 1)
Send(ResearchBrief 2)
Send(ResearchBrief N)
  ↓
ResearchFinding[]
  ↓
Verifier
```

Task Runtime 层的并行/调度：

```text
ResearchPlan
  ↓
ready frontier
  ↓
Task claim
  ↓
one Model Turn
  ↓
Task Outcome / next advance / waiting
```

第二套 Task Runtime 在 [`task_runtime.py`](../apps/api/src/deep_researcher/task_runtime.py) 中已经实现了更细的 Task 级控制，包括依赖、租约、Task Model Turn、Observation、Result Proposal、预算和有界 ReAct。

### 9.3 重要的当前边界

当前生产协调器 `execute_runtime_v2()` 实际调用的是：

```python
graph_state = self._graph_runner.run(...)
```

因此当前主路径是 `ResearchGraphRunner` 的固定 Graph。`ReActTaskController` 的有界“Model Turn → Tool → Observation → 下一轮 Model Turn”能力已经存在并有测试，但从当前 `execute_runtime_v2()` 的主 Graph 看，它不是每个 Researcher 分支自动进入的循环。

这点在修改 Agent Runtime 前必须确认，否则很容易误以为“已经把 Task Runtime 接入生产 Graph”。如果要真正接入，需要明确：

- Task Runtime 是否替代当前 Graph 的 Researcher 分支；
- Graph checkpoint 和 Task Model Turn 谁负责恢复；
- Tool Approval 如何映射到 Task Observation；
- Task Outcome 如何反馈给 Verifier；
- 预算是否由 Run 和 Task 两层同时裁剪；
- SSE 事件如何保持单一事实源。

不要通过简单地在 Graph 中加一条回边来解决，因为这会同时影响幂等、预算、事件序号、租约、取消和 Citation 语义。

## 10. 失败、恢复与治理

### 10.1 取消

取消命令写入数据库，Worker 通过 `CancellationToken` 在：

- Graph 节点开始前；
- 模型流式增量之间；
- 工具执行前后；
- Sandbox 作业阶段；

检查并中止。取消优先于普通重试和后续事实提交。

### 10.2 Worker 崩溃

恢复依靠：

- ResearchRun lease expiration；
- attempt / fencing epoch；
- LangGraph checkpoint；
- ToolCall / ToolRun 幂等；
- RunEvent event key；
- SandboxJob lease 和 attempt；
- 数据库中的任务和观察记录。

`ReActTaskController` 还有 `_recover_inflight_tool()`，用于处理“逻辑 ToolCall 已落库但进程在 Observation 前崩溃”的情况。

### 10.3 部分结果

部分研究分支失败、证据不全、引用校验只能生成 fallback 或 Sandbox 失败时，系统可以形成 `partial` 结果。部分结果不是成功，也不是空失败；它必须携带缺口或失败影响。

### 10.4 Schema 和迁移

数据库 schema 的唯一权威是 Alembic，见 ADR-0003 和 [`apps/api/migrations/`](../apps/api/migrations/)。不要只修改 ORM 模型而不添加 migration，也不要把 Graph checkpoint 当作业务表 schema 的替代品。

## 11. 代码阅读路线

建议按以下顺序学习：

### 第一阶段：产品和领域语言

1. [`CONTEXT.md`](../CONTEXT.md)
2. [`docs/agents/domain.md`](agents/domain.md)
3. ADR-0001、0004、0012
4. [`models.py`](../apps/api/src/deep_researcher/models.py) 中的 `Workspace`、`Conversation`、`ResearchRun`、`ResearchTask`、`RunEvent`、`Memory`、`ResearchRecord`、`EvidenceSpan`

### 第二阶段：运行主链

1. [`app.py`](../apps/api/src/deep_researcher/app.py)：依赖组装和 HTTP/SSE
2. [`run_queue.py`](../apps/api/src/deep_researcher/run_queue.py)：队列和租约
3. [`worker.py`](../apps/api/src/deep_researcher/worker.py)：Worker 生命周期
4. [`coordinator.py:171`](../apps/api/src/deep_researcher/coordinator.py:171)：一次运行的主协调流程
5. [`graph.py:271`](../apps/api/src/deep_researcher/graph.py:271)：Graph 拓扑和节点实现

### 第三阶段：证据和上下文

1. [`research_context.py`](../apps/api/src/deep_researcher/research_context.py)
2. [`retrieval.py`](../apps/api/src/deep_researcher/retrieval.py)
3. [`document_processor.py`](../apps/api/src/deep_researcher/document_processor.py)
4. [`source_manifest.py`](../apps/api/src/deep_researcher/source_manifest.py)
5. [`source_map.py`](../apps/api/src/deep_researcher/source_map.py)
6. [`model_gateway.py`](../apps/api/src/deep_researcher/model_gateway.py)
7. [`citation_validator.py`](../apps/api/src/deep_researcher/citation_validator.py)

### 第四阶段：工具和可靠性

1. [`tool_execution.py`](../apps/api/src/deep_researcher/tool_execution.py)
2. [`research_file_space.py`](../apps/api/src/deep_researcher/research_file_space.py)
3. [`sandbox.py`](../apps/api/src/deep_researcher/sandbox.py)
4. [`sandbox_jobs.py`](../apps/api/src/deep_researcher/sandbox_jobs.py)
5. [`event_log.py`](../apps/api/src/deep_researcher/event_log.py)
6. [`run_event_projector.py`](../apps/api/src/deep_researcher/run_event_projector.py)
7. [`task_runtime.py`](../apps/api/src/deep_researcher/task_runtime.py)

### 第五阶段：用测试验证理解

优先看：

- [`tests/test_worker.py`](../apps/api/tests/test_worker.py)
- [`tests/test_task_runtime.py`](../apps/api/tests/test_task_runtime.py)
- [`tests/test_retrieval.py`](../apps/api/tests/test_retrieval.py)
- [`tests/test_citation_validator.py`](../apps/api/tests/test_citation_validator.py)
- [`tests/test_retrieval.py`](../apps/api/tests/test_retrieval.py)（其中包含来源 Map / 检索边界相关覆盖）
- [`tests/test_sandbox_jobs.py`](../apps/api/tests/test_sandbox_jobs.py)
- [`tests/test_run_event_projector.py`](../apps/api/tests/test_run_event_projector.py)
- [`tests/api/test_tool_approvals.py`](../apps/api/tests/api/test_tool_approvals.py)
- [`tests/api/test_research_runs.py`](../apps/api/tests/api/test_research_runs.py)

测试比类名更能说明系统真正承诺的行为，尤其是租约恢复、重复调用、引用边界、检索可见性和取消。

## 12. 修改功能时的定位表

| 需求 | 首先看 | 通常还要看 |
|---|---|---|
| 新增 API 资源 | `app.py` | `models.py`、migration、API tests |
| 修改研究主流程 | `coordinator.py` | `graph.py`、事件投影、Run tests |
| 修改 Graph 节点 | `graph.py` | 对应 `agents/*.py`、checkpoint tests |
| 修改 Agent 输出格式 | `agents/*.py` | `model_gateway.py`、schema tests |
| 增加检索来源 | `retrieval.py` | `document_processor.py`、models、ACL tests |
| 修改长期记忆 | `models.py`、`app.py` | `retrieval.py`、MemoryIndexer、scope tests |
| 新增 MCP 工具 | `tool_execution.py`、`mcp_adapter.py` | grant、approval、ToolRun tests |
| 新增文件能力 | `research_file_space.py` | capability、revision、artifact tests |
| 修改 Python 计算 | `sandbox_jobs.py`、`sandbox.py` | isolation、DerivedEvidence tests |
| 修改取消/恢复 | `run_queue.py`、`worker.py`、`run_control.py` | event log、fencing、worker tests |
| 修改 Task ReAct | `task_runtime.py` | tool registry、observation、budget tests |
| 修改 SSE 事件 | `run_event_projector.py`、`event_log.py` | frontend event store、replay tests |
| 修改数据库结构 | `migrations/versions/` | `models.py`、migration tests |

## 13. 最后形成的心智模型

可以用下面这句话概括项目：

> FastAPI 接收并持久化用户意图，PostgreSQL 队列把 Research Run 交给带租约的 Worker；Coordinator 冻结一个有权限的研究上下文，LangGraph 负责有限的 map、plan、research、verify、write 编排；检索、工具、Sandbox 和模型都通过受控 Adapter 接入；最终所有用户可见结论必须回到领域数据库、不可变来源、证据核验、Citation 和可重放事件。

学习时最需要警惕的三个误区：

1. **Graph checkpoint 不是业务事实源。**
2. **检索命中或模型输出不是证据，只有回读原文并核验后才可能成为证据。**
3. **Task Runtime 的 ReAct 循环已经实现，但当前主协调路径仍以固定 ResearchGraph 为主，不能默认二者已经完全接通。**
