# 研究账本与 Workspace 混合检索

## Problem Statement

当前研究运行可以产生消息、网页来源、引用、记忆和 Python Sandbox 结果，但这些事实缺少一个持续、可恢复、可审计的研究工作流。单次运行结束后，已核验的研究结论无法稳定地进入同一 Workspace 的后续会话；不同会话之间也没有统一的 ACL 感知检索、长文分段、证据定位和结果版本治理。

用户需要一个类似 Perplexity 的研究助手：Agent 能持续搜索网页、读取正文、上传和检索文档、按需执行 Python 计算、补充研究记录和记忆，并在同一 Workspace 的不同会话中复用已核验知识，同时不把未经核验的历史回答或模型摘要伪装成证据。

## Solution

建立以 Research Run 为生命周期边界的研究账本，并在 Workspace 内提供统一的混合 Retrieval module。

研究账本保存单次研究运行的目标、子任务、来源快照、Evidence Span、Claim、Coverage Snapshot、Evidence Gap、Stop Decision、工具执行和取消状态。研究账本不跨运行共享；通过核验的 Claim 与 Evidence Span 自动提升为不可变版本的 Workspace Research Record，供同一空间后续会话检索。长期记忆单独治理，只保存稳定偏好、空间规则、约束和事实。

Retrieval module 对已通过 ACL 和资源作用域过滤的候选执行全文/BM25 召回、pgvector 精确向量召回、融合和 Qwen Rerank，再按 token budget 组装少量上下文。历史会话原始消息只能作为低信任的历史会话线索，命中后回读原始消息或来源；最终 Citation 必须回到不可变 SourceSnapshot/SourceChunk 的精确 Evidence Span。

网页正文使用 Jina Reader 作为主 Adapter，本地 HttpWebPageGateway 作为读取失败时的 Adapter。完整正文持久化后先生成 Source Manifest 和 Chunk Descriptor，按需读取局部 Chunk 或邻近窗口；整页综合使用 map-reduce，不把全文自动放入模型上下文。

Python Sandbox 由 Agent 自主判断是否需要调用，不逐次进入 Tool Approval。执行环境负责隔离：无网络、非 root、只读根文件系统、显式输入、无宿主凭证、资源和时间上限。代码、用途、输入、结果和哈希持久化为可审计事实；结果属于 Derived Evidence，不能伪装成原始来源。

## User Stories

1. As a researcher, I want each question to create a recoverable Research Run, so that an interrupted investigation can resume without losing its ledger.
2. As a researcher, I want the ledger to record goals and sub-tasks, so that I can see what the Agent has actually investigated.
3. As a researcher, I want each Claim to link to supporting or contradicting Evidence Span records, so that conclusions remain auditable.
4. As a researcher, I want the system to distinguish complete results from partial results with Evidence Gaps, so that missing coverage is visible.
5. As a researcher, I want the Stop Decision to be deterministic from coverage and constraints, so that the model cannot declare an incomplete investigation complete.
6. As a researcher, I want web search results to be treated as discovery only, so that a Brave snippet cannot become unsupported evidence.
7. As a researcher, I want Jina Reader to retrieve readable web content, so that search results can be checked against page text.
8. As a researcher, I want a local page reader to handle Jina failures, so that static pages remain readable.
9. As a researcher, I want long pages stored as immutable snapshots and chunks, so that the Agent can inspect only the relevant context.
10. As a researcher, I want the Agent to retrieve neighboring chunks when needed, so that evidence is not cut away from its heading or explanation.
11. As a researcher, I want whole-document synthesis to use bounded map-reduce, so that large documents do not overflow the model context.
12. As a researcher, I want to upload a document and have it asynchronously parsed, chunked, and indexed, so that large uploads do not block the HTTP request.
13. As a researcher, I want document indexing to expose `processing`, `ready`, and `failed` states, so that I know whether a document is searchable.
14. As a researcher, I want embedding failures to be explicit failures, so that the system never silently presents lexical-only results as a successful vector index.
15. As a researcher, I want Workspace Documents to be searchable from every conversation in that Workspace, so that I do not need to upload the same file again.
16. As a researcher, I want ordinary conversation attachments to remain private until explicitly promoted, so that one conversation cannot accidentally expose its files to another.
17. As a researcher, I want different Workspaces to be hard-isolated before vector or lexical search, so that similarly named content cannot cross the ACL seam.
18. As a researcher, I want workspace-scoped and user-scoped Long-term Memory to work across conversations, so that stable preferences and constraints persist.
19. As a researcher, I want conversation-scoped Memory to remain private to its conversation, so that temporary context does not leak into later work.
20. As a researcher, I want previous conversations to be searchable as low-trust history leads, so that I can recover earlier discussions without treating old answers as proof.
21. As a researcher, I want historical messages split deterministically into bounded segments, so that retrieval can locate a discussion without injecting an entire transcript.
22. As a researcher, I want verified Claims and Evidence Spans to become Workspace Research Records automatically, so that useful research accumulates across sessions.
23. As a researcher, I want Research Records to remain immutable, so that old citations remain auditable after later research.
24. As a researcher, I want superseded records and disputed records to be distinguishable, so that newer evidence does not erase unresolved conflicts.
25. As a researcher, I want Research Records and Long-term Memory to remain separate, so that a temporary research conclusion does not become a permanent user fact.
26. As a researcher, I want retrieval to combine exact terms with semantic similarity, so that identifiers, versions, numbers, and synonyms are all searchable.
27. As a researcher, I want a Qwen Rerank Adapter after initial recall, so that the final context is selected by relevance rather than raw vector distance.
28. As a researcher, I want embedding and rerank Providers configured behind replaceable Adapters, so that the Provider can change without changing the Retrieval module.
29. As a researcher, I want exact pgvector search first, so that initial recall is complete before performance evidence justifies an approximate index.
30. As a researcher, I want retrieval results to be limited by a token budget, so that the Writer receives a small, stable context.
31. As a researcher, I want every final Citation to point to exact source offsets and hashes, so that I can inspect the source text that supports the answer.
32. As a researcher, I want Python calculations to run without per-execution approval when isolated, so that simple analysis and argument checking remain fluid.
33. As a researcher, I want Python execution to be recorded as Derived Evidence with its inputs and hash, so that computed claims remain reproducible.
34. As a researcher, I want cancellation to prevent new Derived Evidence, Citation, or Artifact records, so that a stopped run cannot publish new results.
35. As a researcher, I want failed tools and incomplete branches to be visible in the final result, so that partial work is never presented as complete research.

## Implementation Decisions

- The Research Ledger belongs to one Research Run and is separate from LangGraph checkpoint state. Business facts remain in PostgreSQL-backed domain records; checkpoints are only execution recovery data.
- The deep external seam is a Retrieval module. Its callers provide a query, Workspace/user scope, current conversation scope, candidate limits, and token budget; the module owns ACL-aware candidate filtering, lexical/vector fusion, reranking, deduplication, and context assembly.
- Retrieval uses PostgreSQL full-text search plus pgvector. The first vector search is exact; HNSW/IVFFlat is deferred until measured scale or latency requires it.
- Embedding generation is exposed through an `EmbeddingGateway` Adapter. Reranking is exposed through a `RerankGateway` Adapter. LiteLLM is the Provider Adapter; the default Rerank model is a configurable Qwen model alias, initially mapped to DashScope `qwen3-rerank`.
- Embedding and Rerank Provider failures are explicit indexing or retrieval failures. There is no silent lexical fallback after a configured Provider fails.
- Documents, deterministic conversation segments, and verified Research Records are indexed asynchronously. An item participates in retrieval only after its index state is `ready`.
- A SourceChunk stores content hash, offsets, embedding model/version metadata, dimensions, index state, failure reason, and indexed timestamp. Embedding dimensions must be validated at runtime because the first schema does not fix a single model dimension.
- A Conversation Segment stores a bounded range of messages and their IDs, content hash, visibility scope, and index metadata. It is a history navigation source, not a Citation source.
- A Research Record stores a verified Claim, its Workspace and originating Run, immutable version, content hash, status, and references to evidence. Versions are never overwritten; later versions use `superseded` and conflicts use `disputed`.
- Research Records are automatically created only from verified Claim/Evidence results. Memory Candidates are separately extracted and governed; Research Records do not automatically become Long-term Memory.
- Workspace ACL, resource scope, soft deletion, current document version, attachment readiness, memory status/expiry, and run cancellation are applied before retrieval results are exposed to the Graph.
- Historical conversation messages are low-trust history leads. The Agent may use them to locate prior discussions and then re-read original messages or sources, but they cannot alone support a factual Citation.
- Web content follows Jina Reader primary and local HTTP reader fallback. Search snippets remain Source Discovery only.
- Progressive disclosure is mandatory for long content: snapshot/manifest/chunk descriptor first, local chunk window on demand, Evidence Span only after source re-read. Whole-page analysis uses bounded map-reduce.
- Python Sandbox execution is autonomous within the isolated runtime and does not require per-call approval. It is no-network, non-root, read-only-root, credential-free, resource-bounded execution; outputs are Derived Evidence.
- The deterministic Stop Policy owns final completion semantics. It uses Coverage Snapshot, Citation completeness, cancellation state, and run constraints. Partial results must explicitly report gaps; no verified Claim means no research conclusion.

## Testing Decisions

- Tests verify external business behavior through the highest useful seam. They do not assert field counts, enum counts, method counts, or merely that a dependency was called.
- The primary integration seam is the Research Run execution path: create a Workspace and Conversation, upload/promote data, run a query, and inspect the answer, Citation, Research Record, or failure status.
- Existing retrieval citation tests are the prior art for promotion visibility, cross-Workspace isolation, exact PDF page evidence, and unknown citation rejection.
- Add Retrieval module behavior tests for lexical/vector candidate fusion, Qwen Rerank ordering, duplicate elimination, token-budget truncation, and explicit Provider failure.
- Add indexing behavior tests for asynchronous document and conversation-segment states, successful embedding persistence, failure persistence, and retry without losing document readability.
- Add cross-session tests for Workspace Document visibility, workspace/user Memory visibility, conversation Memory isolation, historical-message low-trust handling, and Research Record automatic promotion.
- Add Research Record tests for immutable versions, superseded records, disputed records, conflict visibility, and preservation of old Citation hashes.
- Add long-page tests proving that only bounded chunk windows enter model context while final Evidence Span offsets resolve against the immutable snapshot.
- Add Sandbox tests proving successful execution creates Derived Evidence with provenance and cancelled execution creates no new Derived Evidence, Citation, or Artifact.
- Run SQLite migration upgrade/downgrade tests for local deterministic behavior and PostgreSQL migration/offline SQL checks for pgvector extension and full-text indexes. Real PostgreSQL query plans remain a deployment verification concern.

## Out of Scope

- A standalone Qdrant, Milvus, or other independent vector database
- HNSW/IVFFlat approximate indexing before measured scale or latency evidence
- Silent lexical fallback after a configured embedding or Rerank Provider fails
- Treating Brave snippets, Rerank scores, vector distances, summaries, or historical messages as direct Citation evidence
- Automatically making every Research Record a Long-term Memory
- Automatically sharing ordinary conversation attachments or raw unverified web snapshots across conversations
- Cross-Workspace retrieval or ACL bypasses
- Full transcript injection into the model context
- Browser JavaScript rendering, arbitrary remote code execution, or network-enabled Python Sandbox execution
- Per-execution Tool Approval for the isolated Python Sandbox
- Persisting chain-of-thought, complete prompts, secrets, or Provider tokens

## Further Notes

- The domain glossary defines Research Ledger, Research Record, Historical Conversation Lead, Evidence Span, Claim, Derived Evidence, Coverage Snapshot, Stop Decision, and Memory scopes. The glossary intentionally does not contain implementation-specific names such as PostgreSQL, LiteLLM, or Qwen.
- ADR-0011 records the isolated Python Sandbox approval decision. ADR-0012 records the Workspace hybrid retrieval and cross-conversation Research Record decision. Existing runtime ADRs for PostgreSQL queue, Alembic authority, LiteLLM Adapter, and business-state ownership remain in force.
- The current worktree contains an in-progress schema and Retrieval seam plus focused module tests. It has not been committed. The next implementation pass must review those changes against this spec before extending Coordinator, indexing workers, and Research Record lifecycle.
