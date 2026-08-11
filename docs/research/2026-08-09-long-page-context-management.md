# 超长网页的上下文管理与证据定位

调研日期：2026-08-09

## 范围与直接结论

本文接受已经确认的产品决策：**Jina Reader 是网页正文读取的 primary Adapter，本地 `HttpWebPageGateway` 是 fallback Adapter**。这项决定覆盖 [`2026-08-09-web-content-extraction-options.md`](./2026-08-09-web-content-extraction-options.md) 第 5 节中相反的调用顺序；本文不重新比较抓取器，只回答“超长网页如何不撑爆模型上下文，同时保持 Evidence Span 可审计”。

直接结论：

1. **完整正文应进入 Research Ledger，不应自动进入模型消息**。Jina 输出先持久化为不可变 `SourceSnapshot` 和可定位 `SourceChunk`；Graph state、Planner、Researcher 和 Writer 只保存稳定 ID 与少量、按需读取的内容。
2. **Jina 已提供有用的提取侧能力，但没有替应用完成上下文管理**：`x-markdown-chunking` 可按标题或结构返回 chunks，`x-target-selector` 可读取已知 CSS 区域，`x-max-tokens` 会截断结果，`x-token-budget` 会在超限时拒绝请求。[1][2][3] 这些能力不能替代本地检索、重排、分页展开与 Evidence Span。
3. **SSE 不是长文分页方案**。固定源码只承诺 `Accept: text/event-stream` 产生多条 JSON event，尤其用于 compound response format；它没有提供 chunk cursor、断点随机读取或“只推送相关段落”的语义。[4] 即使流式接收，若把所有 event 内容累计进模型消息，仍会撑爆上下文。
4. **采用 progressive disclosure**：模型先看 Source Manifest，再检索少量 `Chunk Descriptor`，最后显式读取选中的原文窗口。LangChain Deep Agents 的官方 RAG 指南也把检索结果写到持久 backend，只向 orchestrator 返回路径，并要求不要把完整 chunk 粘贴进 orchestrator 消息。[5]
5. **摘要只用于导航和压缩，不能成为最终证据**。`Chunk Digest`、`Source Brief` 和 map--reduce 输出可以进入 Planner/Reducer 上下文，但 Citation 必须回到不可变 SourceSnapshot 中的精确 Evidence Span。LlamaIndex 的 `CitationQueryEngine` 也将已检索 node 再切成更细的 citation chunk，说明“检索粒度”和“引用粒度”应分开。[6]
6. **第一版不需要另建一套长文 Agent**。把“保存全部、按需读取、精确引用”收进 Research Ledger module，对 Graph 暴露两个小 interface：`search_source_chunks` 与 `read_source_chunks`。这能集中复杂度并通过 deletion test：删掉该 module 会迫使每个 Researcher、Verifier 和 Writer 各自处理全文、预算、定位和恢复。

## 1. Jina Reader 能解决什么，不能解决什么

### 1.1 当前固定源码中的能力

Jina Reader 固定提交 `1574bfd` 的 README、Cookbooks、DTO 和测试共同定义了以下表面：[1][2][3][4]

| 能力 | 官方语义 | 本项目正确用法 | 不能据此声称 |
| --- | --- | --- | --- |
| `x-markdown-chunking: h1..h5` | 按指定标题层级切 Markdown | 标题可靠的文章默认用 `h3` | 已经只返回“相关”chunk |
| `x-markdown-chunking: s1..s5` | 标题不可靠时按 block 结构切分 | chunk 分布异常时改用 `s3` | 已经完成 query-aware retrieval |
| `x-preset: research` | `markdown+frontmatter`、`h3` chunking，并保留 links/images/media | 可以作为起点，但显式覆盖无用媒体和链接选项 | preset 会自动控制下游模型上下文 |
| `x-target-selector` | 只返回匹配 CSS selector 的区域 | 已知站点模板或二次定向读取 | 对任意网页都能自动发现正确 selector |
| `x-remove-selector` | 删除指定 selector | 去除确定的导航、弹窗和页脚噪声 | 可以取代正文识别与内容验证 |
| `x-max-tokens` | 将结果截断到最大 token 数，最小值 500 | 仅作 preview 或显式的 partial snapshot | 保存了完整网页，或可以判断网页“没有提到”某内容 |
| `x-token-budget` | 若结果超过预算则以 `BudgetExceededError` 拒绝；搜索端忽略 | 防止异常响应被误接收为成功 | 会自动把超长网页分页返回 |
| `Accept: text/event-stream` | 返回多条 SSE JSON event | 边接收边落临时对象或展示进度 | event 是可独立引用的正文分页 |

Jina `research` preset 保留所有链接、图片和媒体，而本项目另存 canonical page URL，首版通常不需要为每个图片/链接向研究模型支付 token。建议显式覆盖：

```text
Accept: application/json
X-Preset: research
X-Markdown-Chunking: h3      # 标题稀疏或分布异常时改为 s3
X-Retain-Images: alt
X-Retain-Media: none
X-Retain-Links: text
```

如果 JSON 同时包含 `content` 与 `chunks`，客户端不得把两份重复正文拼进 ToolMessage。接收端只把它们转为持久化事实，并向模型返回 manifest。若后续需要降低应用进程峰值内存，可以采用 Jina 文档说明的纯文本 record-separator chunk 输出，逐块写入对象存储；这属于传输优化，不改变账本 interface。[1]

### 1.2 为什么不能把 `x-max-tokens` 当最终方案

`x-max-tokens` 的明确语义是 trim，不是 relevance selection。[1][3] 因此它适合快速 preview，但会造成三个审计问题：

- 页面后半部分无法检索，可能产生系统性遗漏
- 不能区分“原网页没有信息”和“信息位于截断点之后”
- 只保存截断内容时，Coverage Snapshot 不能把该来源记为完整读取

如果确实使用，`SourceSnapshot` 必须记录 `truncated=true`、Provider 报告的 token/usage、实际 content hash 与截断策略。截断片段中的精确原文仍可支持局部、肯定性的 Claim；它不能支持全页否定性 Claim、完整清单或“已覆盖全文”的判断。

`x-token-budget` 更严格：固定测试证明极小预算返回 HTTP 409 `BudgetExceededError`，而不是 partial content。[3] 这应映射为 `failed_retryable` 或 `oversized` Tool Run，不能创建伪成功 snapshot。

### 1.3 SSE 只负责传输

Jina OpenAPI 接受 `text/event-stream`，固定 E2E 测试验证 compound `markdown+html`、`markdown+text` 会产生多个可解析 JSON event。[4] 这有利于避免等待完整 response 后才开始处理，但当前公开 interface 没有定义：

- 任意 HTML 页面的 chunk cursor
- 按 chunk ID 随机重读
- 中断后从某个正文 offset 续传
- 只返回与 Research Goal 相关的 event

所以本项目需要在本地生成稳定的 `SourceChunk.id`、ordinal、heading path 和 locator。SSE event 不能直接成为 Citation，也不能代替 Tool Run 的幂等提交。

## 2. 正确的数据流：全文在账本，少量证据进模型

```text
Brave Source Discovery
  -> Jina Reader Tool Run (primary)
       -> 失败时 Local HTTP Reader Tool Run (fallback)
  -> immutable SourceSnapshot / extracted artifact
  -> structural SourceChunk[]
  -> Source Manifest
       -> search_source_chunks(goal/query, cursor, max_tokens)
       -> rerank descriptors
       -> read_source_chunks(chunk_ids, cursor, max_tokens)
  -> exact Evidence Span
  -> Claim--Evidence relation
  -> Coverage Snapshot / Stop Decision
  -> Writer 只读已核验 Claim + Evidence Span
```

关键分层：

| 持久化事实 | 是否默认进入模型 | 是否可直接支持 Citation | 用途 |
| --- | --- | --- | --- |
| Search title/snippet | Planner 可少量读取 | 否 | URL discovery |
| 完整 `SourceSnapshot` | 否 | 不能直接引用整页 | 历史版本、重放和审计 |
| `Source Manifest` | 是，受小预算约束 | 否 | 标题、URL、heading tree、chunk 数、完整性状态 |
| `Chunk Descriptor` | 是，检索后返回少量 | 否 | chunk ID、heading path、token 数、短 preview、score |
| `SourceChunk.text` | 仅显式按需读取 | 只能作为 Evidence Candidate | 证据抽取、局部分析 |
| `Chunk Digest` / `Source Brief` | 可以 | 否 | 导航、map--reduce、问题分解 |
| `Evidence Span` 原文 | 是，Verifier/Writer 按 Claim 读取 | 是 | 最终支持或反驳 Claim |
| Python Derived Evidence | 按已确认规则读取 | 是，但必须保留输入 spans、代码与执行哈希 | 计算验证 |

这里的“完整持久化”指本系统保存 Jina 实际返回的、经规范化的完整 extracted representation。若因传输上限、Provider budget、站点限制或取消而只保存了部分内容，必须用 snapshot completeness state 明示，不能把 partial 当 full。

## 3. Research Ledger module 的最小 interface

### 3.1 Source Manifest

Source Manifest 是确定性投影，不需要新表。它从 snapshot/chunks 计算：

```text
snapshot_id, title, canonical_url, fetched_at,
adapter_id, extraction_version, content_hash,
completeness, warnings, chunk_count, total_tokens,
heading_tree[{heading_path, first_chunk_id, chunk_count}]
```

默认只向模型返回 manifest，不返回全文或所有 chunk preview。这样 Agent 先知道“页面有什么区域”，再决定是否检索或展开。

### 3.2 两个读取 interface

```text
search_source_chunks(
  run_id, goal_id, snapshot_ids, query,
  cursor?, max_results?, max_tokens?
) -> ChunkDescriptorPage

read_source_chunks(
  run_id, chunk_ids,
  cursor?, max_tokens?, neighbor_window?
) -> SourceTextPage
```

共同约束：

- Research Ledger 重新校验 Run、Workspace 和来源可见性，不信任模型传来的 scope
- `max_tokens` 是本次模型输入预算，不是 Research Run 总预算
- 只能在完整 chunk/句子窗口边界停止，不能静默剪掉半句
- 达到预算时返回 `next_cursor`、`omitted_chunk_ids` 和 `remaining_token_estimate`
- 返回文本始终带稳定 `snapshot_id/chunk_id/locator/content_hash`
- ToolMessage 只保存结果引用和简短统计；原文继续留在账本
- 相同参数与相同 snapshot version 使用 canonical input hash 幂等复用

第一版可以复用现有词项/全文排名，先不引入新的向量数据库。Anthropic 官方 Contextual Retrieval 给出的成熟路线是 lexical BM25 与 embeddings 召回、去重/融合，再对较大的候选集 rerank，只把 top-K 交给生成模型；它也明确指出加入更多 chunk 会增加命中机会，但过多信息会干扰模型。[7] 等首版公共验收证明词项检索的 recall 不足，再在同一 interface 后增加 embedding/rerank Adapter。

### 3.3 Chunk locator

Jina 的 Provider chunk ordinal 不能单独成为历史 locator，因为重新提取或切分策略变化会改变序号。建议每个 `SourceChunk` 至少保存：

```text
snapshot_id, ordinal, heading_path,
start_char, end_char, text_hash, token_count,
provider_chunking_strategy, extraction_version
```

Evidence Span 再保存：

```text
source_chunk_id, start_char_in_chunk, end_char_in_chunk,
exact_text, span_hash
```

LlamaIndex 固定版本的 CitationQueryEngine 默认用 512-token、20-token overlap 的 SentenceSplitter 将已检索 node 再切成更细 citation node，并复制原 node 的 metadata/score。[6] 本项目可以借鉴“引用二次细分”，但必须额外保存精确 offset/hash；只给模型编号 `Source 1` 不足以在重新执行后稳定定位。

## 4. 上下文预算不是运行次数限制

用户已经明确不需要给 Research Run 或 Python Sandbox 增加简单次数上限；这与单次模型调用必须遵守有限 context window 不冲突。

每次模型调用动态计算：

```text
evidence_input_budget =
  model_context_window
  - system_and_policy_tokens
  - compact_conversation_tokens
  - tool_schema_tokens
  - requested_output_reserve
  - safety_margin
```

建议规则：

1. 先为 Writer/Verifier 的结构化输出保留明确 token，不用“把输入塞满再祈祷输出成功”
2. tool 只消费 `evidence_input_budget`；不足时返回 cursor，让 Agent 在下一次调用继续读
3. static prompt、近期对话和工具 schema 都计入预算；不能只统计网页正文
4. 一轮结束后把大 ToolMessage 替换为 Ledger reference、Claim、Evidence Gap 和 Stop Decision，原始结果不随消息历史反复重放
5. 预算统计值、使用的 chunk IDs 和省略原因写入 Tool Run 审计字段

Deep Agents 固定源码采用“达到上下文 fraction 后摘要旧消息、保留近期消息，并把完整旧历史 offload 到 backend”的做法；默认示例是 trigger 0.85、keep 0.10。[8] 本项目不必照搬阈值，但应借鉴事实分层：**摘要留在短期上下文，完整历史留在可按需读取的持久层**。

## 5. Progressive disclosure、重排与 map--reduce

### 5.1 普通问答路径

```text
Manifest
  -> query 检索候选 chunks
  -> lexical/semantic fusion（首版可只有 lexical）
  -> rerank
  -> 读取 top chunks + 必要邻居窗口
  -> 抽取 Candidate Claim + exact Evidence Span
  -> Verifier 回读原文
```

Anthropic 的 Contextual Retrieval 为每个 chunk 增加约 50--100 token 的文档内定位上下文，再用于 embedding 和 BM25；官方实验还采用“先召回较多候选，再 rerank 到较少 top-K”的结构。[7] 对本项目最小版本，`heading_path + title + 相邻标题` 已能提供一部分确定性上下文；不要首版就为每个 chunk 调模型生成 contextual prefix。

### 5.2 必须综合整页时

不能靠 top-K 回答“列出全文所有条目”“比较各章节”之类具有全量语义的问题。此时 Agent 可以创建 map--reduce work：

```text
Map(snapshot chunk groups)
  -> ChunkDigest {
       topics,
       candidate_claims,
       candidate_span_locators,
       unresolved_questions
     }
Reduce(all digest refs)
  -> SourceBrief / Claim candidates / coverage gaps
Verify(candidate spans against immutable snapshot)
  -> Evidence Span + Claim relation
```

Map 输出必须包含原始 chunk IDs 与候选 quote locators。Reducer 只合并结构化 digest，不接收全部正文；最终 Citation Validator 再读取原始 span。LangGraph 官方 orchestrator--worker pattern允许 orchestrator 动态创建 worker 并汇总输出；Deep Agents RAG 指南则展示了“每个持久 chunk 单独分析，orchestrator 只拿短 summary”的具体做法。[5][9]

LangChain Open Deep Research 固定源码提供了一个有用的反例边界：它把 `raw_content[:max_content_length]` 交给摘要模型，摘要失败时返回原文；研究结束又把累积 messages 交给 `compress_research`，token overflow 时删除较老消息。[10] 这能临时降低上下文压力，但没有不可变 snapshot、精确 span 或可恢复的分块 map 状态，所以不能直接作为本项目的 Citation provenance。

### 5.3 哪些摘要可以进模型

- **可以**：确定性 heading manifest、短 preview、带 chunk IDs 的 Chunk Digest、带来源覆盖列表的 Source Brief
- **可以但要降级标识**：模型生成的摘要、Reducer 综合结果；只能指导下一步读取和形成 Claim Candidate
- **不能成为证据**：搜索 snippet、未保存原文的摘要、丢失 chunk IDs 的 agent notes、Writer 自己生成的归纳
- **最终可引用**：不可变 snapshot 上的 exact Evidence Span，或符合既定规则的 Python Derived Evidence

摘要若声称某事实但找不到可回读的 exact span，Coverage Snapshot 应记录 `unsupported_claim`，而不是让 Writer引用摘要。

## 6. 失败、取消与恢复语义

### 6.1 Fetch 与 fallback

```text
Jina claimed
  -> success: persist snapshot/chunks once
  -> retryable failure: retry policy or Local Reader fallback
  -> terminal restricted/challenge: record failure, do not infer absence
  -> oversized/budget rejected: record incomplete status, create targeted work
```

Jina 和 Local Reader 得到不同正文时，必须保留不同 `adapter_id + extraction_version + content_hash` 的 snapshot，不能覆盖。fallback 成功也不修改先前失败 Tool Run，只创建关联的后继 Tool Run。

### 6.2 Chunk map/reduce 恢复

建议幂等键：

| Work | canonical key |
| --- | --- |
| fetch | `canonical_url + adapter/version + normalized options` |
| chunk persist | `snapshot_id + ordinal + text_hash` |
| map | `run_id + goal_id + sorted chunk_hashes + prompt_version` |
| reduce | `run_id + goal_id + sorted map_output_hashes + prompt_version` |
| Evidence Span | `snapshot_id + locator + span_hash` |

Worker 重启后只领取尚未成功的 map work；已成功 chunk、digest、Evidence Span 按 hash 复用。Reduce 只有在要求的 map work 到达终态后运行；如果允许 partial reduce，必须把 `missing_chunk_ids` 写入结果和 Coverage Snapshot。

### 6.3 取消

取消检查位于：发起 Jina/Local 请求前、接收完成准备提交前、创建 map work 前、提交 Evidence Span/Claim 前。取消后可以保留已原子提交的历史 snapshot/chunk，但不得继续产生新 Evidence、Claim、Citation 或 Artifact。

Jina SSE 中途断开时，当前公开 interface 没有正文 cursor 恢复保证。[4] 所以未完成的流不能标记为成功 snapshot；重试必须产生新的 Tool Run attempt，只有完成并通过内容 hash 校验后才能提交。

## 7. 最小实现顺序

第一条纵向切片只需：

1. 将 Jina Reader 设为 primary、Local Reader 设为 fallback，统一 Result Envelope
2. 使用 Jina heading/structured chunking，把完整 extracted response 落 `SourceSnapshot/SourceChunk`，ToolMessage 只返回 IDs 与统计
3. 为 chunk 增加 heading path、offset、hash 和 token count；为 snapshot 增加 completeness/warning/extraction version
4. 实现 `Source Manifest`、`search_source_chunks`、`read_source_chunks`
5. Evidence Extractor 从按需读取的原文创建独立 Evidence Span；Writer 只读已绑定 Claim
6. 增加动态单次调用 context budget 和 cursor，不增加 Research Run 总执行次数限制
7. 普通路径通过后，再增加“整页综合”map--reduce work；首版不用新增向量数据库或 Source Brief 表

不建议首版做：

- 把 Jina 全文直接塞进 Researcher ToolMessage
- 用 `x-max-tokens` 截掉全文后假装抓取完成
- 把 Provider chunk ordinal 当永久 Citation ID
- 让模型摘要直接创建 Citation
- 同时引入新的 crawler、向量数据库、全局知识图谱和复杂 multi-agent supervisor

## 8. 公共验收

以下验收验证业务行为，不验证类、字段或调用次数：

1. **超长页不撑爆上下文**：给定正文显著大于目标模型可用输入的测试页，Research Run 仍能完成；模型调用记录显示输入不超过动态预算，且没有任何一次 ToolMessage 包含完整 snapshot
2. **尾部信息可发现**：答案所需事实只放在长页末尾；系统不得依赖 `x-max-tokens` 前缀，必须通过 chunk 检索或 map work 找到并形成 Evidence Span
3. **精确引用**：点击 Citation 可以打开保存的 snapshot，并定位到 exact Evidence Span；span hash 与 snapshot 对应范围一致
4. **摘要不可越权**：故意让 Chunk Digest 包含原文不存在的结论，Citation Validator 必须拒绝，Coverage 显示 `unsupported_claim`
5. **partial 不冒充 full**：Jina token budget 拒绝、流中断或响应上限触发时，来源显示 incomplete/oversized；不得据此生成全页否定性 Claim
6. **fallback 可见**：Jina 失败、本地 Reader 成功时，来源详情显示两个 Tool Run、实际采用的 snapshot adapter/version 以及失败原因
7. **恢复不重复副作用**：在部分 map work 完成后重启 Worker，已完成 work 和 snapshot 被复用，只执行剩余 work；最终 Citation 与未重启执行一致
8. **取消边界**：取消后不再新增 Evidence Span、Claim、Citation 或 Artifact；已提交的来源事实仍可审计
9. **全文综合诚实**：全页清单类问题缺少部分 map work 时，只能产生带 `missing_chunk_ids` 的部分结果，不能标记 `coverage_satisfied`
10. **Provider chunking 变化可追溯**：同一 URL 从 `h3` 改为 `s3` 后保留不同 extraction version；历史 Citation 仍解析到原 snapshot/span

## 参考资料

1. Jina AI Reader `1574bfd`, [README：request headers、token guardrail、Markdown chunking 与 preset](https://github.com/jina-ai/reader/blob/1574bfd380d249c86c82db4dace0d9c8fe17e2b1/README.md)；[Cookbooks：index/research preset 与 chunking](https://github.com/jina-ai/reader/blob/1574bfd380d249c86c82db4dace0d9c8fe17e2b1/cookbooks.md)
2. Jina AI Reader `1574bfd`, [`CrawlerOptions`：preset、selector、token 与 chunking 字段](https://github.com/jina-ai/reader/blob/1574bfd380d249c86c82db4dace0d9c8fe17e2b1/src/dto/crawler-options.ts)
3. Jina AI Reader `1574bfd`, [token budget E2E](https://github.com/jina-ai/reader/blob/1574bfd380d249c86c82db4dace0d9c8fe17e2b1/tests/e2e/token-budget.test.ts)；[Markdown chunking E2E](https://github.com/jina-ai/reader/blob/1574bfd380d249c86c82db4dace0d9c8fe17e2b1/tests/e2e/markdown-chunking.test.ts)
4. Jina AI Reader `1574bfd`, [SSE output E2E](https://github.com/jina-ai/reader/blob/1574bfd380d249c86c82db4dace0d9c8fe17e2b1/tests/e2e/stream-output.test.ts)；[live OpenAPI](https://r.jina.ai/openapi.json)
5. LangChain 官方 Docs `768a41e`, [Deep Agents RAG：retrieve、offload、delegate 与分页](https://github.com/langchain-ai/docs/blob/768a41ed767d8c18b39a07f50f5a8f7f7700aa59/src/oss/deepagents/rag.mdx)
6. LlamaIndex `v0.14.6`, [`CitationQueryEngine`：检索 node 与 citation chunk 分层](https://github.com/run-llama/llama_index/blob/v0.14.6/llama-index-core/llama_index/core/query_engine/citation_query_engine.py)；[官方示例](https://github.com/run-llama/llama_index/blob/v0.14.6/docs/examples/query_engine/citation_query_engine.ipynb)
7. Anthropic, [Contextual Retrieval：chunk、BM25/embedding、rerank 与上下文取舍](https://www.anthropic.com/news/contextual-retrieval)
8. LangChain Deep Agents `6a5d93f`, [Summarization middleware：按上下文比例压缩并 offload 完整历史](https://github.com/langchain-ai/deepagents/blob/6a5d93f9ba7391189f65e6b999b724253577085d/libs/deepagents/deepagents/middleware/summarization.py)
9. LangChain 官方 Docs `768a41e`, [LangGraph orchestrator--worker pattern](https://github.com/langchain-ai/docs/blob/768a41ed767d8c18b39a07f50f5a8f7f7700aa59/src/oss/langgraph/workflows-agents.mdx)
10. LangChain Open Deep Research `20aaa0d`, [网页截断/摘要](https://github.com/langchain-ai/open_deep_research/blob/20aaa0d422bd290c83f93574810ef1244e8d5955/src/open_deep_research/utils.py)；[研究消息压缩与 token overflow 处理](https://github.com/langchain-ai/open_deep_research/blob/20aaa0d422bd290c83f93574810ef1244e8d5955/src/open_deep_research/deep_researcher.py)
