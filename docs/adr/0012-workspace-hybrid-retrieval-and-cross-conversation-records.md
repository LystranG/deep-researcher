---
status: accepted
---

# Workspace 使用混合检索并持久化跨会话研究记录

同一 Workspace 内的检索采用全文匹配与向量召回结合的混合检索，并在候选集合上使用可替换的 Rerank Adapter。第一版使用 PostgreSQL 全文检索、pgvector 精确向量检索和通过 LiteLLM 配置别名接入的 Qwen Rerank；暂不启用近似向量索引。检索必须先施加 Workspace 成员权限、资源作用域、软删除、文档版本和有效期条件，再执行相似度计算。

文档、历史会话片段和已核验研究记录都通过异步索引进入检索范围。索引失败直接记录为失败，不使用静默的词项降级或伪造的检索成功状态。只有索引状态有效的内容才参与检索。

其他会话的原始消息只作为历史会话线索，用于定位相关讨论和回读原始消息；它不能单独作为事实证据，也不会自动成为长期记忆。通过核验的 Claim 与 Evidence Span 自动形成 Workspace Research Record，研究记录采用不可变版本，后续替代或矛盾分别标记为 `superseded` 或 `disputed`。Research Record 与 Long-term Memory 分离，研究结论不会自动写入长期记忆。

检索结果必须回读不可变原文，才能生成 Evidence Span 和 Citation；向量相似度、Rerank 分数、会话摘要和历史消息线索都不能直接作为 Citation。

## Consequences

- 跨会话检索共享 Workspace 知识，但不会把其他会话的完整上下文自动注入当前会话
- 词项检索保留数字、实体名、版本号和精确短语的能力，向量检索补充语义召回，Rerank 负责候选精排
- embedding 或 rerank Provider 失败会暴露为明确的索引/检索失败，需要通过任务重试处理
- 研究记录可以持续积累并保留冲突和历史版本，便于审计和重新核验
- Rerank 与 embedding Provider 通过 Adapter 隔离，未来可以切换 Qwen 的托管方或模型而不改变领域检索 Interface
