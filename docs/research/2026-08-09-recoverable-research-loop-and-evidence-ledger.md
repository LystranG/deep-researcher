# 可恢复深度研究循环与证据账本补充调研

调研日期：2026-08-09

## 范围与结论

本文只补充 [`2026-08-09-perplexity-and-open-source-deep-research.md`](./2026-08-09-perplexity-and-open-source-deep-research.md) 尚未覆盖的内容，重点核对：长时间研究运行的恢复、任务与工具执行幂等、证据覆盖评估、停止条件和引用溯源。

Perplexity 没有公开 Deep Research 的内部 Agent 图、恢复协议、幂等键或覆盖阈值。本文不会从产品表现反推其私有实现；Perplexity 官方公开资料只能作为能力与交互基线，不能作为内部架构证据。[1]

新增的一手资料支持以下结论：

1. **恢复分为控制流恢复、事件流恢复和副作用恢复**。OpenAI Background Responses 可以轮询、取消和按 `sequence_number` 续流，但这不等于搜索、抓取、Python 执行或本地落库 exactly-once。[2]
2. **最值得建立的是深的“研究账本 Module”**。它统一管理 Research Task 领取、Tool Run 结果提交、Knowledge Source 版本、Evidence Span、Coverage Snapshot、停止裁决和 Claim--Evidence 绑定；搜索、网页读取、MCP 和 Python Sandbox 都只是其后的 Adapter。
3. **覆盖评估不能只是提示词或模型自报“研究完成”**。硬预算和语义覆盖应分别计算，最终由持久化裁决决定是否进入 Writer。
4. **引用不是报告末尾的 URL 列表**。稳定 Source ID、不可变 SourceSnapshot、可定位 Evidence Span、Claim 与回答 span 的关系必须在 Writer 之前存在，Writer 只能引用已经进入账本的证据。[3][16]
5. **开源实现适合借鉴受限循环，不适合直接继承其恢复和引用语义**。GPT Researcher 的内存 `visited_urls` 会把“见过 URL”和“成功读取”混在一起；LangChain Open Deep Research 的覆盖停止和引用保真仍主要依赖提示词。[7][8]

## 1. 官方公开能力对恢复边界的启示

### 1.1 OpenAI Background Responses：可恢复交付，不是副作用事务

OpenAI Background mode 为长时间运行的 response 提供了明确的公共生命周期：[2]

- response 可处于 `queued`、`in_progress`，随后进入终态，调用方可按 response ID 轮询
- 进行中的 response 可以取消；官方明确说明重复取消是幂等的，后续调用返回最终 Response
- `background=true` 与 `stream=true` 可同时使用；连接中断后，可记录事件的 `sequence_number`，再用 `starting_after` 恢复事件流
- Deep Research 官方指南建议长任务使用 background mode，并可通过 webhook 接收完成通知；同时允许用 `max_tool_calls` 限制工具调用总数，从而控制成本和延迟[4]

这些事实可以直接借鉴为产品体验：Research Run 具有稳定 ID、可轮询状态、可取消、可从事件 cursor 恢复 UI 投影。但需要保留一个重要区分：

| 能力 | 官方接口能证明什么 | 本项目仍需负责什么 |
| --- | --- | --- |
| response 轮询 | 远端运行可在断线后继续查询状态 | 本地 Research Run、Research Task、Tool Run 状态投影 |
| `starting_after` | 事件交付可从 cursor 继续 | 事件去重、SSE 重放、投影幂等 |
| 重复 cancel | 对同一远端 Response 的取消调用幂等 | 取消后不得新建 Research Task、SourceSnapshot、Citation 或 Artifact |
| `max_tool_calls` | 提供硬工具次数上限 | 时间、成本、正文字符、来源数、覆盖度和停止原因 |

**可采纳决策**：将 Provider Run ID 视为 Tool Run 的远端关联 ID，而不是 Research Run 的事实源。恢复时先读取本地成功结果；只有没有可复用结果且仍持有有效 lease 的 Tool Run 才能发起外部调用。事件 cursor 只恢复展示，不触发工具副作用。

### 1.2 OpenAI Conversations 与 Agents SDK：会话续接和研究事实分层

OpenAI Conversation state 文档区分了三种续接方式：[5]

- 应用自行重放完整历史
- 使用 `previous_response_id` 串联 response
- 使用具有稳定 ID 的 Conversation 对象跨 session、device 或 job 保存消息、工具调用和工具输出

Agents SDK 的结果文档也把 `history`、`last_response_id`、`interruptions` 和可序列化 `state` 分为不同的续接表面；被审批中断的运行返回 resumable state，而不是伪装成 final output。[6]

这说明“对话连续性”和“研究事实持久化”应是两个 seam：

- Conversation Context 负责让下一轮模型理解近期对话
- 研究账本负责保存可恢复、可审计、可引用的 Research Task、Tool Run、Knowledge Source、Evidence Span 和停止裁决

**可采纳决策**：不能只保存模型历史或 Graph state 后声称研究可恢复。即便底层 Provider 支持 Conversation/Response continuation，本地仍要按 Workspace ACL 重新解析可见的 Workspace Document、Conversation Attachment、Long-term Memory 和工具权限；Provider conversation ID 不应扩大数据可见范围。

### 1.3 OpenAI Deep Research 与 Web Search：工具记录是运行轨迹，不是完整证据账本

OpenAI Deep Research 官方输出可包含 `web_search_call`、`code_interpreter_call`、`mcp_tool_call`、`file_search_call` 和最终 `message`。Web Search 的 action 还可细分为 `search`、`open_page` 和 `find_in_page`；最终消息的 `url_citation` annotation 带 URL、标题和答案字符范围。[3][4]

这一公共结构说明，成熟研究体验至少要保留“搜索、打开页面、页内定位、代码执行、最终回答”这些不同动作，而不是只保存一个搜索摘要。但是 URL annotation 仍不足以表达本项目所需的历史可重放证据：网页内容会变化，同一 URL 也可能有多个抓取版本。

**可采纳决策**：借鉴 typed output item，但把它映射为本地不可变事实：

```text
Search Round
  -> Source Discovery
  -> SourceSnapshot(content_hash, fetched_at, extraction_version)
  -> SourceChunk
  -> Evidence Span(locator)
  -> Claim
  -> Citation(answer_span)
```

Provider 返回的 citation annotation 只能作为 Citation Candidate；Citation Validator 必须确认其 Source ID、locator 和回答 span 都能解析到本地已保存证据。

## 2. Brave Search 官方资料新增的工具分层

Brave 官方维护的 Search API skills 将能力分成至少三层：[9][10]

- Web Search 返回排序后的 URL、snippet 和丰富元数据，支持分页、freshness、语言、SafeSearch、结果类型和 Goggles 重排
- LLM Context 返回按 URL 组织的预提取正文片段、表格或代码，可控制最大 URL 数、总 token、每 URL token 和相关性阈值
- Answers 承担托管式多次搜索与答案生成；官方 skill 将 LLM Context 描述为单次搜索，将 Answers 描述为 multi-search

LLM Context 比普通 snippet 更接近 Evidence，但它仍是一次 API 返回值，不天然具备本项目要求的不可变版本、内容 hash、抓取状态、失败重试和历史引用定位。

**可采纳决策**：

1. 保留 `WebSearchAdapter` 作为 URL 发现 Adapter；需要低延迟正文时可增加 `BraveLlmContextAdapter`，但两者都输出统一 Result Envelope
2. Result Envelope 至少包含 `tool_run_id`、查询 hash、Provider、Provider request ID、状态、候选 URL、正文片段、来源元数据、重试性和原始响应 hash
3. `freshness`、语言、Goggles/域名规则属于 Search Task 的显式输入，必须进入 canonical input hash，不能藏在 Adapter 默认值里
4. 即使采用 LLM Context，仍须持久化 SourceSnapshot 和 SourceChunk；Provider 的 `age` 或 `fetched_content_timestamp` 只是元数据，不能替代本地 `fetched_at + content_hash`
5. 不采用 Answers 作为核心研究编排器，因为它会把查询规划、循环和停止决策移到不可审计的托管层；它可以作为未来独立 Adapter 或对照评测对象

## 3. 开源源码揭示的恢复与停止缺口

### 3.1 GPT Researcher：`visited_urls` 不是幂等账本

在固定提交 `5d84d2f5553e70a2765a8ff3a0d2672d60437ce8` 中，`ResearchConductor._get_new_urls()` 在抓取前就把 URL 加入内存 `visited_urls`，之后才把新 URL 交给正文抓取。如果抓取失败，同一进程后续再次看到该 URL 也会被视为已访问并跳过。[7]

这会混淆以下不同事实：

- 搜索发现过 URL
- Worker 已领取抓取任务
- 抓取成功并保存了正文
- 抓取失败但可以重试
- 抓取永久失败

**可采纳决策**：至少分别持久化 `discovered`、`fetch_claimed`、`fetched`、`failed_retryable`、`failed_terminal`。只有成功的具体 SourceSnapshot 版本可以满足覆盖度；URL 去重不能抑制失败重试，Tool Run 领取与完成分别使用 CAS。

GPT Researcher 的递归停止同样主要是结构性预算：成功分支在 `depth > 1` 时继续递归，显式终止主要来自零查询、当前层全部失败或 depth 耗尽，而不是已验证的证据覆盖、冲突消解或信息增益。[11]

**可采纳决策**：`depth/breadth` 只映射为硬预算，不映射为 `coverage_satisfied`。模型生成的 follow-up question 为空、连续无新 URL 或连续无新 Claim 时，应产生明确的语义停止候选。

### 3.2 GPT Researcher：单 URL 学习映射不足以支持引用溯源

同一固定提交中，DeepResearchSkill 将引用解析为“learning 文本 -> 单个 source URL”的内存字典，并在报告上下文中拼接 `[Source: URL]`。它没有稳定 Claim ID、SourceSnapshot 版本或 Evidence Span，也不能自然表达同一 Claim 的多个支持来源、反证和网页版本。[12]

**可采纳决策**：Claim 与 Evidence Span 建立多对多关系；Evidence Span 固定到 `snapshot_id + content_hash + locator`。Writer 只消费已绑定 Claim，不能从 raw notes 或 URL 字符串临时恢复引用。

### 3.3 LangChain Open Deep Research：提示词软停止不等于可审计裁决

在固定提交 `20aaa0d422bd290c83f93574810ef1244e8d5955` 中，研究提示词要求研究者在拥有 3 个以上来源、连续两次搜索得到相似信息或可以完整回答时停止；但代码真正强制的停止主要是最大迭代、没有 tool call 或模型调用 `ResearchComplete`。[8][13]

因此“来源数足够”“信息已收敛”和“可以完整回答”仍是模型的软判断，而不是持久化、可回放的 Coverage Decision。

此外，Open Deep Research 会把工具异常转换为普通字符串 ToolMessage，再将 Tool/AI Message 拼成 `raw_notes` 交给压缩和最终写作提示词。提示词要求保留来源，但源码没有强类型 Evidence Ledger 来约束压缩和引用关系。[14]

**可采纳决策**：

- Supervisor 只能创建下一批 Research Task，不能单独拥有完成权
- 模型的 `ResearchComplete` 只记录为 `completion_candidate`
- 独立 Coverage Evaluator 计算 Coverage Snapshot，确定性 Stop Policy 再产出公共 `stop_reason`
- 工具异常使用结构化 Result Envelope，至少记录 `error_code`、`retryable`、`attempt` 和 `provider_request_id`
- `raw_notes` 只用于展示或模型上下文，不能成为 Citation 的 provenance source of truth

## 4. 证据覆盖与停止裁决的推荐形态

### 4.1 Coverage Snapshot

每一轮结束后，为每个 Research Goal 生成持久化 Coverage Snapshot。建议首版只保存可解释的离散状态和计数，不追求一个虚假的万能总分：

| 维度 | 首版可审计字段 | 满足条件示例 |
| --- | --- | --- |
| 直接支持 | `supporting_claim_count`、`supporting_domain_count` | 核心 Claim 至少有一个直接 Evidence Span |
| 反例与冲突 | `counterevidence_count`、`conflict_status` | 已搜索反例；存在冲突时报告能并列呈现 |
| 来源质量 | `primary_source_count`、`source_quality_flags` | 用户要求官方资料时至少一个一手来源 |
| 来源多样性 | `independent_domain_count` | 非同域转载或同一内容镜像 |
| 时效性 | `freshness_status`、`oldest/newest_source_at` | 快速变化主题满足指定时间窗 |
| 新信息增益 | `new_snapshot_count`、`new_claim_count` | 本轮产生新的成功快照或新的可引用 Claim |
| 可引用性 | `uncited_claim_count`、`invalid_citation_count` | Writer 前所有必要 Claim 都有有效绑定 |

模型可负责分类、发现冲突和提出 Evidence Gap；确定性代码负责计数、阈值、预算和状态转换。OpenAI 的引用指南同样建议使用跨运行稳定、可人工检查且粒度适中的 citable unit，并把来源多样性、可信度和冲突观点作为 grounding 要求。[16] 这样既保留模型对语义的 leverage，也保证停止原因可以重放和测试。

### 4.2 硬预算与语义停止分离

推荐的终止原因：

| `stop_reason` | 判定来源 | 是否可生成完整报告 |
| --- | --- | --- |
| `coverage_satisfied` | Coverage Snapshot 通过全部必要 Goal | 可以 |
| `budget_exhausted` | 查询、抓取、token、成本或工具次数上限 | 只能生成带缺口声明的部分报告 |
| `time_exhausted` | Research Run deadline | 只能生成带缺口声明的部分报告 |
| `no_new_evidence` | 连续 N 轮无新成功快照且无新 Claim | 只能生成带缺口声明的部分报告 |
| `contradiction_unresolved` | 核心 Claim 存在未解决冲突且无预算继续 | 只能并列呈现冲突，不能给确定结论 |
| `all_fetches_failed` | 必要来源均无法提取 | 不生成伪完整结论 |
| `tool_unavailable` | 必要 Tool/Adapter 不可用 | 不生成伪完整结论 |
| `cancelled` | 持久取消事实 | 不得新增报告、Citation 或 Artifact |

取消在提交任何新副作用前拥有最高优先级。`coverage_satisfied` 只在 Citation Validator 通过且提交 Writer Task 前仍未取消时成立。

## 5. 推荐的研究账本 Module

### 5.1 Interface

研究账本 Module 应隐藏 lease、CAS、重复执行、来源版本、覆盖投影和引用关系的复杂度，对编排层只暴露少量高 leverage 操作：

1. 领取或恢复一个 Research Task
2. 以 canonical input hash 领取 Tool Run
3. 提交结构化工具结果并生成或复用 SourceSnapshot
4. 从 SourceChunk 登记 Evidence Span 与 Claim 关系
5. 计算并保存 Coverage Snapshot
6. 裁决继续研究、部分完成或停止
7. 为 Writer 提供只读、已验证的 Claim 集合

搜索、网页抓取、Brave LLM Context、MCP、File Search 和 Python Sandbox 都放在 Adapter 后面。增加第二个搜索或抓取 Adapter 时，不应改变编排循环、覆盖评估或 Citation Validator，这才是一个真实 seam。

### 5.2 建议幂等键与恢复语义

| 对象 | 建议稳定身份 | 重试/接管规则 |
| --- | --- | --- |
| Research Task | `run_id + goal_id + task_kind + generation` | lease 超时可接管；成功结果只投影一次 |
| Search Round | `run_id + normalized_query_hash + search_policy_version` | 相同输入复用成功轮次；参数变化生成新轮次 |
| Tool Run | `task_id + adapter + canonical_input_hash + tool_version` | CAS 领取；失败按 `retryable` 和 attempt policy 重试 |
| Source Discovery | `run_id + search_round_id + canonical_url` | 合并排名和发现查询，不等于抓取成功 |
| SourceSnapshot | `canonical_url + content_hash + extraction_version` | 内容或提取版本变化才新增版本 |
| Evidence Span | `snapshot_id + locator + span_hash` | 永远指向不可变 snapshot |
| Claim | `run_id + normalized_claim_hash + claim_kind` | 保留支持、反驳和未知状态，不用文本 set 无序去重 |
| Citation | `message_id + answer_span_hash + claim_id + evidence_span_id` | Writer 重试可去重；Validator 只解析本地 ID |
| Run Event | `run_id + event_kind + aggregate_id + aggregate_version` | SSE 重放只读；cursor 不触发业务副作用 |

已有 LangGraph 调研已经确认 checkpoint/interrupt 重放不保证外部副作用 exactly-once，本文不再重复展开。[15] 本次开源源码核查进一步说明：仅有内存 `visited_urls`、模型历史或 Graph state 都不足以替代这个 Module。

## 6. 可直接采纳的第一纵向切片

先实现“可恢复的两轮研究 + Coverage Snapshot”，不要同时扩展所有抓取器：

1. Planner 创建有限 Research Goal 与初始 Search Task
2. Search Adapter 保存完整 Source Discovery，不把 snippet 当已读证据
3. Fetch/LLM Context Adapter 通过 Tool Run CAS 读取候选来源并保存 SourceSnapshot/Chunk
4. Evidence Extractor 为必要 Goal 生成 Claim 与 Evidence Span 绑定
5. Coverage Evaluator 输出缺口、冲突、新信息增益和 citation readiness
6. 未满足且预算允许时生成补搜 Search Task；相同输入恢复时复用已有成功 Tool Run
7. 达到覆盖、预算、连续无新证据、工具失败或取消时保存结构化 Stop Decision
8. Writer 只消费已验证 Claim；Citation Validator 拒绝任何无法解析到 Evidence Span 的答案 span

公共验收应验证：断开并恢复 SSE、Worker lease 接管或 Graph checkpoint 恢复后，同一 Search/Fetch/Python Tool Run 不重复产生计费副作用；用户能看到每轮目标、查询、成功来源、Evidence Gap、停止原因和逐项引用。

## 参考资料

1. Perplexity, [Introducing Perplexity Deep Research](https://www.perplexity.ai/hub/blog/introducing-perplexity-deep-research)
2. OpenAI, [Background mode](https://developers.openai.com/api/docs/guides/background)
3. OpenAI, [Web search](https://developers.openai.com/api/docs/guides/tools-web-search)
4. OpenAI, [Deep research](https://developers.openai.com/api/docs/guides/deep-research)
5. OpenAI, [Conversation state](https://developers.openai.com/api/docs/guides/conversation-state)
6. OpenAI Agents SDK, [Results and state](https://developers.openai.com/api/docs/guides/agents/results)
7. GPT Researcher `5d84d2f`, [`_get_new_urls` 与抓取调用路径](https://github.com/assafelovic/gpt-researcher/blob/5d84d2f5553e70a2765a8ff3a0d2672d60437ce8/gpt_researcher/skills/researcher.py#L241-L257)；[`_get_new_urls`](https://github.com/assafelovic/gpt-researcher/blob/5d84d2f5553e70a2765a8ff3a0d2672d60437ce8/gpt_researcher/skills/researcher.py#L801-L822)
8. LangChain Open Deep Research `20aaa0d`, [研究停止提示词](https://github.com/langchain-ai/open_deep_research/blob/20aaa0d422bd290c83f93574810ef1244e8d5955/src/open_deep_research/prompts.py#L138-L183)；[Supervisor 运行逻辑](https://github.com/langchain-ai/open_deep_research/blob/20aaa0d422bd290c83f93574810ef1244e8d5955/src/open_deep_research/deep_researcher.py#L225-L358)
9. Brave Search, [`llm-context` 官方 skill，固定提交 `3e088af`](https://github.com/brave/brave-search-skills/blob/3e088af66eb61f1c207c22b2be0278ca8744d1d1/skills/llm-context/SKILL.md)
10. Brave Search, [`web-search` 官方 skill，固定提交 `3e088af`](https://github.com/brave/brave-search-skills/blob/3e088af66eb61f1c207c22b2be0278ca8744d1d1/skills/web-search/SKILL.md)
11. GPT Researcher `5d84d2f`, [DeepResearchSkill 递归与停止条件](https://github.com/assafelovic/gpt-researcher/blob/5d84d2f5553e70a2765a8ff3a0d2672d60437ce8/gpt_researcher/skills/deep_research.py#L373-L538)
12. GPT Researcher `5d84d2f`, [learning 与 URL 映射](https://github.com/assafelovic/gpt-researcher/blob/5d84d2f5553e70a2765a8ff3a0d2672d60437ce8/gpt_researcher/skills/deep_research.py#L143-L204)；[报告上下文拼接](https://github.com/assafelovic/gpt-researcher/blob/5d84d2f5553e70a2765a8ff3a0d2672d60437ce8/gpt_researcher/skills/deep_research.py#L590-L638)
13. LangChain Open Deep Research `20aaa0d`, [Researcher 工具循环和硬停止](https://github.com/langchain-ai/open_deep_research/blob/20aaa0d422bd290c83f93574810ef1244e8d5955/src/open_deep_research/deep_researcher.py#L421-L475)
14. LangChain Open Deep Research `20aaa0d`, [工具错误与 raw notes 压缩](https://github.com/langchain-ai/open_deep_research/blob/20aaa0d422bd290c83f93574810ef1244e8d5955/src/open_deep_research/deep_researcher.py#L421-L560)；[引用保留提示](https://github.com/langchain-ai/open_deep_research/blob/20aaa0d422bd290c83f93574810ef1244e8d5955/src/open_deep_research/prompts.py#L186-L220)
15. LangGraph, [Functional API and durable execution](https://docs.langchain.com/oss/python/langgraph/functional-api)；本仓库既有调研 [`2026-08-08-langgraph-checkpoint-and-docker-sandbox.md`](./2026-08-08-langgraph-checkpoint-and-docker-sandbox.md)
16. OpenAI, [Citation Formatting](https://developers.openai.com/api/docs/guides/citation-formatting)
