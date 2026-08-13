# Hybrid Retrieval 真实验证

**Ticket：** #11  
**验证日期：** 2026-08-13（Asia/Shanghai）  
**验证数据库：** 隔离 PostgreSQL 17.10，`pgvector 0.8.5`，数据库 `deep_researcher_ticket11`，不是历史开发库  
**凭证状态：** 使用本机 `.env` 和运行时环境变量；文档不记录 token、cookie 或 API key

## 验证结论

- 当前 Alembic head `p3q4r5s6t7` 在 pgvector 镜像上完整升级成功
- `vector` extension、SourceChunk/ConversationSegment/ResearchRecord 三个 FTS GIN 索引和 exact cosine distance 均可用
- LiteLLM embedding 真实成功：`qwen3-embedding-8b` 返回 `4096` 维向量，向量以 `ready` 状态持久化并保留模型名/维度
- Voyage rerank 真实成功：`voyageai/rerank-2.5-lite` 返回候选 `index` 和 `relevance_score`，排序结果进入统一 `RetrievalPage`
- Workspace ACL 在 lexical/vector 召回前生效；隐藏 Workspace 的候选不影响可见结果
- 真实 `RetrievalPage` 通过分页返回 `consumed_tokens`、`remaining_tokens`、`cursor`、`omitted_ids` 和 `completeness`；重复 content hash 在精排前去重
- 真实 embedding Provider 失败会把附件标记为 `failed`，保留文件名和 hash，不伪装成 lexical-only 成功
- 真实 rerank Provider 失败以 LiteLLM `ServiceUnavailableError` 暴露，`last_metadata` 保持为空，不返回 lexical-only 成功

## 实际 Provider 契约

### Embedding

- API base：本机 `.env` 配置的 OpenAI-compatible `/v1` endpoint
- LiteLLM model：`qwen3-embedding-8b`
- Adapter 路由：自定义 `api_base` 时显式使用 `custom_llm_provider="openai"`
- 观察结果：返回模型标识 `Qwen/Qwen3-Embedding-8B`，单次两/三条输入均返回等维 `4096` 向量

### Rerank

- LiteLLM model：`voyageai/rerank-2.5-lite`
- Adapter 路由：自定义 endpoint 使用 `custom_llm_provider="litellm_proxy"`，并将 `/v1` suffix 归一为服务根地址；LiteLLM 自动请求 `/v1/rerank`
- 观察结果：真实返回 `document`、`index`、`relevance_score`，语义相关候选排名高于无关候选
- Provider metadata：本次响应的 `id` 和 `meta` 为 `null`；系统只依赖候选下标和分数，不把 rerank metadata 当 Citation

## 数据库与规模

- PostgreSQL：`17.10 (Debian 17.10-1.pgdg12+1)`，ARM64
- pgvector：`0.8.5`
- Migration：`p3q4r5s6t7`
- 每次 Hybrid Retrieval 成功 live 用例：2 个 Workspace、4 个 SourceChunk（其中 1 个跨 Workspace 隐藏、2 个相同 content hash）
- 另有成功/失败各 1 条附件索引 live 用例，分别验证真实向量持久化和显式失败状态
- 验证库累计重跑数据：16 个 Workspace、16 个 Conversation、21 个 SourceChunk；不作为性能样本

## 确定性回归

- Retrieval 与迁移聚焦：`15 passed`（含自定义 LiteLLM embedding/rerank 路由行为）
- 真实 Hybrid Retrieval：`4 passed`
- Ruff：通过
- Mypy：`37 source files`，通过
- SQLite Alembic upgrade：通过
- 前端 Vitest：`8 passed`
- 前端 TypeScript typecheck 与 Vite build：通过

## 限制与未验证项

- 当前 Provider 端点没有可用的 `qwen3-reranker-8b` 路由；本 ticket 使用用户指定的 Voyage Rerank 模型，因此不能把结果描述为 Qwen Rerank 证据
- 只验证小样本、静态文本和单个隔离数据库；没有生产规模吞吐、延迟、成本或 query plan 基准承诺
- 没有验证 HNSW/IVFFlat、独立向量数据库、远程 PostgreSQL 网络故障或 Provider 限流恢复
- 旧 `infra-postgres-1` 历史 volume 仍缺少 Alembic version 且使用旧无 pgvector 镜像，未修改、未 stamp、未清理；验证使用独立容器
