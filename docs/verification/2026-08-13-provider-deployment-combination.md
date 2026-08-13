# Provider 与部署组合真实验证

**Ticket：** #12  
**验证日期：** 2026-08-13（Asia/Shanghai）  
**验证数据库：** 隔离 PostgreSQL 17.10、pgvector 0.8.5，数据库 `deep_researcher_ticket12_final`  
**凭证边界：** 使用本机 `.env`；本文不记录 API key、token 或完整私有端点

## 结果摘要

| 能力 | 状态 | 真实证据与限制 |
| --- | --- | --- |
| OpenAI-compatible 模型 | 通过 | 自定义模型端点完成流式写作，记录 `6537` total tokens；自定义 `api_base` 使用 OpenAI-compatible 路由且不发送不兼容的 `reasoning_effort` |
| Brave Source Discovery | 通过 | 返回 5 个发现结果；3 个候选经正文获取成为 `web_page`，2 个仅保留 `search_snippet`，snippet 未进入 Citation |
| Jina / Web Acquisition | 通过 | IANA 与 IETF 公开页面形成不可变 Snapshot；被选中的获取尝试包含 `jina_reader` |
| Hybrid Retrieval | 通过 | 同一 Run 命中 Workspace 文档并形成 Citation，`retrieval_completed` 记录 token 消耗、余量与结果数；另一个 Workspace 的隐藏验收码没有进入回答或 Citation |
| PostgreSQL checkpoint | 通过 | checkpoint schema 在应用接收请求前初始化，避免运行期 `CREATE INDEX CONCURRENTLY` 与 SSE snapshot 互相阻塞 |
| remote MCP | 环境阻塞 | `DEEP_RESEARCHER_TRUSTED_MCP_URL` 未配置；未以本地 fake 冒充远程通过，未产生真实 Tool Run provenance |
| Docker Sandbox 隔离 | 通过 | Colima Docker daemon 下验证无网络、非 root、只读根文件系统、CPU/内存/PID 限制、宿主文件不可见、Artifact hash/下载与 Workspace ACL；Docker CLI 只继承连接选择和 TLS 环境，不继承应用密钥 |
| Docker Sandbox 取消 | 通过 | 运行中取消后状态为 `cancelled`，Artifact 为空且取消前后 Citation/Artifact 数量不变；当前 schema 没有独立 Derived Evidence 事实表，已单列 follow-up |
| 整页 complete / partial | 确定性通过 | 既有完整与缺失 Chunk 测试进入全量门禁；本次真实问题不是整页综合，不将其描述为真实整页 Provider 证据 |

## 真实组合 Run

- 研究阶段 `planning`、`researching`、`verifying`、`writing` 均完成
- 最终状态为 `completed`，usage 非空，计划角色依次包含 researcher、verifier、writer
- Workspace Citation 精确回到 `TICKET12-VISIBLE-4096` 原文，所有 Citation 均包含 `source_hash`
- 网页 Citation 来自持久化正文 Snapshot；Brave discovery snippet 不具备 Citation 资格
- Research Ledger 的 Citation 数量与实际 Citation 一致，`missing_chunk_ids=[]`
- 验证范围是单次小样本 smoke，不包含生产 SLA、成本基准、跨区域容灾或任意第三方网站兼容性承诺

## 执行记录

- 完整离线确定性 pytest：`141 passed, 13 skipped`
- 真实 OpenAI/Brave/Jina/Hybrid Retrieval/PostgreSQL 组合：`1 passed`，最终复跑 `24.97s`
- Docker Sandbox 隔离、daemon 失败、环境透传、Artifact ACL 与取消：`5 passed`
- Ruff：通过
- Mypy：`37 source files`，通过
- `git diff --check`：通过

## 发现并修复的部署问题

1. 自定义 OpenAI-compatible 模型名无法由 LiteLLM 自动识别：自定义 `api_base` 现在显式使用 `custom_llm_provider="openai"`
2. 自定义模型不支持 `reasoning_effort`：仅原生模型路径发送该参数
3. PostgreSQL checkpointer 在 Run 内重复执行并发索引 DDL：改为应用启动、接收请求前一次性初始化
4. Sandbox Docker CLI 丢弃 `DOCKER_HOST` 后回退到失效 context：只透传 Docker 连接选择与 TLS 变量，同时保持应用凭证隔离
