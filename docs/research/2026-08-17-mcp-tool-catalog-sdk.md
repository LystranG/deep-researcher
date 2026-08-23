# MCP Tool Catalog、目录漂移与 Python SDK 选型调研

日期：2026-08-17

范围：Issue #15 Q5 的决策准备

状态：调研结论，不修改生产 Agent Runtime

## 结论摘要

推荐采用以下组合：

1. **MCP Host 使用官方 `mcp` Python SDK v2.0.x**，并继续隐藏在项目自己的薄 Adapter 后面。新实现优先评估 v2 高层 `Client(mode="auto")`，利用协议协商、缓存和 `subscriptions/listen`；业务层仍自行管理 Tool 身份、目录快照、授权、幂等、恢复和 Observation。官方 v2.0.0 是当前稳定线，支持 MCP `2026-07-28`，同时兼容更早协议。[MCP Python SDK v2.0.0 release](https://github.com/modelcontextprotocol/python-sdk/releases/tag/v2.0.0)
2. **不让 LiteLLM、模型 provider 或 MCP Server 成为 Tool Registry 的事实源**。MCP `tools/list` 只是某次发现结果；模型看到的是本地生成的 `ModelToolAlias`；真正执行必须解析到本地不可变 `ToolDefinitionRef`，再经过当前 Workspace grant、Policy、预算和重复检查。
3. **持久身份、定义版本和模型别名必须分离**：
   - `ToolIdentity`：本地生成、长期稳定，不包含名称或 schema hash
   - `ToolDefinitionRef`：指向某个不可变定义版本，包含精确 schema 与 Adapter binding
   - `ModelToolAlias`：只在某个 Catalog Snapshot 中有意义，满足 provider 名称限制
   - `ToolCatalogSnapshot`：绑定一个 Model Turn，冻结 alias → definition/schema/binding 的映射
4. **MCP 改名不能自动识别为“同一个工具”**。当前 MCP Tool 没有跨改名稳定 ID；若旧名字消失、新名字出现，默认创建新 `ToolIdentity`。只有管理员或可信安装清单提供显式映射时才能延续旧身份，绝不能按相似名称或相似 schema 猜测。
5. **同名工具 schema 变化保留 `ToolIdentity`，产生新的 `ToolDefinitionRef`**。旧 Model Turn 永远解析到旧 Definition；若执行前发现远端已不再提供该精确定义，返回 `DEFINITION_MISMATCH`，不得把旧调用静默交给新 schema。
6. **`tools/list_changed` 只是失效提示，不是版本、diff 或可靠事件日志**。它使发现缓存和未来快照失效，但不改写既有快照。MCP `2026-07-28` 还要求客户端先通过 `subscriptions/listen` 明确订阅；断线重连必须重新订阅。[MCP Tools](https://modelcontextprotocol.io/specification/2026-07-28/server/tools) [MCP Subscriptions](https://modelcontextprotocol.io/specification/2026-07-28/basic/patterns/subscriptions)
7. **Workspace grant 撤销、定义漂移和 Adapter 暂不可用是三种不同事实**：分别映射为不可重试的 Policy 拒绝、需要新快照/新 Model Turn 的定义失配、以及保留原调用身份可退避重试的基础设施故障。
8. **LiteLLM 继续只做 provider 协议归一化**。使用 `tools`、`tool_choice` 和归一化 `tool_calls`，但保持 `drop_params=False`、禁止 prompt 模拟 function calling、禁止静默 provider fallback。LiteLLM 默认会对不支持的参数报错，只有 `drop_params=True` 才会静默删除参数。[LiteLLM unsupported params](https://docs.litellm.ai/docs/completion/drop_params)
9. **FastMCP 3.x 是优秀但非必要的第二层框架**。适合快速开发自有 MCP Server、独立代理或多 Server 应用；不应替换本项目的 Host Registry。它是 Prefect 维护的独立 `fastmcp` 包，不等同于官方 `mcp` 包；其高层 API 曾被纳入官方 SDK v1，但官方 SDK v2 已使用新的 `MCPServer`/`Client` 架构。[FastMCP](https://gofastmcp.com/getting-started/welcome) [MCP SDK v2 migration](https://github.com/modelcontextprotocol/python-sdk/blob/v2.0.0/docs/migration.md)

## 版本边界

本题存在三套容易混淆的版本信息，结论必须以仓库实际锁定版本为准：

| 证据 | 版本 | 结论 |
| --- | --- | --- |
| [`uv.lock`](../../uv.lock) | `mcp==2.0.0`、`mcp-types==2.0.0`、`litellm==1.95.0`、`openai==2.53.0` | 本文的代码级判断基于这些版本 |
| 本地 `mcp.types.LATEST_PROTOCOL_VERSION` | `2026-07-28` | 当前官方 SDK v2 的现代协议，不是交接文档中的 `2025-11-25` |
| Context7 的 `/modelcontextprotocol/python-sdk` | 只列出 `v1.12.4` | 可作旧 API 背景，不能作为 v2.0.0 的最终证据 |
| Context7 的 MCP 规范索引 | `2025-11-25` | `taskSupport` 等旧版事实需按最新规范重新判断 |
| 官方 SDK release | v2.0.0 stable，支持 `2026-07-28` | 最新版本结论以官方 release、规范和锁定源码为准 |

MCP `2025-11-25` 曾在 Tool 上定义 `execution.taskSupport`；`2026-07-28` 已把 Tasks 从核心规范移出，官方 Python SDK v2.0.0 release 也明确把 tasks extension 列为未包含能力。锁定的 `mcp_types.Tool` 仍保留 `execution` 字段用于旧协议兼容，不能据此把 MCP Tasks 当成当前可移植能力。[MCP Python SDK v2.0.0 release](https://github.com/modelcontextprotocol/python-sdk/releases/tag/v2.0.0) [MCP 2025-11-25 Tools](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)

因此本票不依赖 MCP `taskSupport`。远端长任务若未来通过扩展接入，也只能被 Adapter 映射为项目自己的 durable job receipt，不能成为 `ResearchTask`、Tool Call 身份或恢复事实。

## 当前仓库事实

### MCP Adapter

当前 [`mcp_adapter.py`](../../apps/api/src/deep_researcher/mcp_adapter.py) 已经做对了两件事：

- 直接使用官方 `mcp` SDK
- 沿 `next_cursor` 读取全部 `tools/list` 页面

但当前 `McpToolDescriptor` 只保留：

- `name`
- `description`
- `input_schema`

当前实现没有保留 `title`、`outputSchema`、annotations、icons、`_meta`、发现协议版本、`ttlMs`、`cacheScope` 或不可变定义版本；`call_tool()` 还会重新建立 Session、重新发现目录，再按远端名称调用。它可以证明“能连接本地受信 MCP Server”，不能证明 Catalog 的稳定身份和恢复语义。

### ModelGateway

当前 [`model_gateway.py`](../../apps/api/src/deep_researcher/model_gateway.py) 调用 `litellm.acompletion()`，尚未实现 Tool Calling receipt；usage 仍保存在可变 `_last_usage`。因此 Q5 的结果应作为未来 `ToolCallingTurn`/ModelGateway 合同输入，不应在本票顺手改造现有 Gateway。

### 既有 ADR

[`ADR 0007`](../adr/0007-litellm-sdk-adapter-only.md) 已决定首版使用进程内 LiteLLM SDK，不部署 Proxy/Router；[`ADR 0008`](../adr/0008-no-silent-model-fallback.md) 已决定 provider 失败显式暴露。这排除了把 LiteLLM MCP Gateway 作为新的权限和目录事实源。

## MCP 目录的真实能力边界

### Tool 定义没有跨版本稳定身份

MCP `2026-07-28` 的 Tool 定义包含 `name`、可选 `title`/`description`/`icons`、`inputSchema`、可选 `outputSchema`、annotations 和 `_meta`。规范只要求工具名在一个 Server 内唯一；Server 名也不保证跨 Server 唯一，聚合客户端应自行消歧。[MCP Tools：Tool 与 Tool Names](https://modelcontextprotocol.io/specification/2026-07-28/server/tools#tool)

规范没有提供 `toolId`、定义版本或 rename lineage。这意味着：

- `serverInfo.name + tool.name` 不能成为全局身份
- `installation_id + remote_name` 可作首次发现键，但不能证明改名前后是同一工具
- schema hash 是定义指纹，不是持久身份
- title/description 是展示与模型选择信息，不是授权身份

### 当前 Tool 名称比模型 function name 更宽

MCP 当前建议名称长度 1–128，允许 ASCII 字母、数字、下划线、连字符和点，且作用域仅在单个 Server。OpenAI function name 最大 64，只允许字母、数字、下划线和连字符；Anthropic 要求 `^[a-zA-Z0-9_-]{1,64}$`。[MCP Tool Names](https://modelcontextprotocol.io/specification/2026-07-28/server/tools#tool-names) [OpenAI function calling](https://developers.openai.com/api/docs/guides/function-calling) [Anthropic define tools](https://platform.claude.com/docs/en/agents-and-tools/tool-use/define-tools)

Gemini 官方指南建议使用无空格、无特殊字符的描述性函数名，并以 snake_case/camelCase 展示；本文没有找到一个比 provider API schema 更稳定的跨 Gemini API family 正则，因此不扩大字符集，而采用更窄的 ASCII lowercase/number/underscore 交集，并把真实 Gemini deployment 验证列为 live proof。[Gemini function calling](https://ai.google.dev/gemini-api/docs/function-calling)

因此把 MCP 原名直接交给模型至少有三类错误：超长、点字符不兼容、跨 Server 碰撞。模型别名必须是本地生成的投影，不能反向成为授权身份。

### `list_changed` 不足以实现可恢复目录

最新协议的实际语义是：

- Server 在 capabilities 中用 `tools.listChanged` 表示会发送变更通知
- `tools/list` 支持分页和缓存提示
- `notifications/tools/list_changed` 没有 diff、版本或新目录内容
- Server 只是 **SHOULD** 在变化时通知，不是持久消息投递保证
- `2026-07-28` 客户端必须先在 `subscriptions/listen` 请求 `toolsListChanged: true`
- transport 断开后订阅结束；stdio 重连必须重新订阅

来源：[MCP Tools](https://modelcontextprotocol.io/specification/2026-07-28/server/tools) [MCP Subscriptions](https://modelcontextprotocol.io/specification/2026-07-28/basic/patterns/subscriptions)

`ttlMs` 只表示结果在多久内可视为 fresh，`cacheScope` 只区分 public/private 缓存范围；通知会提前使缓存 stale。它们都不是 Catalog revision，更不是历史恢复依据。[MCP Caching](https://modelcontextprotocol.io/specification/2026-07-28/server/utilities/caching)

由此得到本地规则：

1. 每次 refresh 必须完整消费所有分页
2. refresh 结果形成一个本地 discovery revision/digest
3. `list_changed`、TTL 到期、重连、grant 变化都只触发重新发现或未来快照重建
4. 已持久化 Snapshot 永不原地修改
5. 订阅丢失或 Server 不支持通知时，以 TTL 和执行前 freshness gate 补偿

规范没有定义分页期间的一致性快照。如果分页期间收到 `list_changed`，客户端应丢弃本轮结果并重新开始；若 Server 不支持通知，只能把完整结果视为一次观察值，并保留其 observed-at/TTL，不能宣称获得了远端强一致 revision。

### annotations 不能形成 Policy

规范明确要求：除非来自受信 Server，客户端必须把 Tool annotations 视为不可信。`readOnlyHint`、`destructiveHint`、`idempotentHint` 和 `openWorldHint` 只能辅助管理员配置或诊断，不能自动免审批、允许并行或允许重试。[MCP Tool annotations](https://modelcontextprotocol.io/specification/2026-07-28/server/tools#tool)

本项目即使把某个 installation 标记为 trusted，也应由本地 Policy 固化最终风险等级；远端 annotations 变化只能产生新定义版本，不能直接改变已有授权结论。

## SDK 选型

### 推荐：官方 `mcp` Python SDK v2

官方 SDK 与规范同源，锁定版本已提供：

- 高层 `Client`，统一 URL、stdio/in-process transport 与协议协商
- `mode="auto"`：优先现代 `server/discover`，兼容旧 initialize handshake
- response cache，消费 `ttlMs`/`cacheScope`
- `Client.listen()`/subscription 支持和通知驱动 cache eviction
- 完整 Tool 类型、分页、结构化结果和 `outputSchema` 校验
- OAuth、Streamable HTTP、stdio
- 低层 `ClientSession`，允许应用自己处理 `InputRequiredResult` 等多回合状态

来源：[SDK v2 release](https://github.com/modelcontextprotocol/python-sdk/releases/tag/v2.0.0) [v2 Client source](https://github.com/modelcontextprotocol/python-sdk/blob/v2.0.0/src/mcp/client/client.py) [v2 ClientSession source](https://github.com/modelcontextprotocol/python-sdk/blob/v2.0.0/src/mcp/client/session.py)

建议不是让 SDK 接管业务生命周期，而是：

```text
ToolRegistry / ToolPolicy / ToolExecution
              |
       McpToolAdapter
              |
 official mcp Client / ClientSession
```

新 Adapter 可以用高层 `Client` 管理 transport、协商、缓存和订阅；需要持久等待的多回合工具调用不得使用会自动完成 input-required loop 的便利路径，而应通过低层 Session 把状态显式映射为本地 Waiting/receipt。sampling、elicitation、roots 和 Server 发起的其他能力默认拒绝，逐项建立产品策略后再开放。

### 可选：独立 FastMCP 3.x

FastMCP 是 Prefect 维护的独立框架，覆盖 Server、Client、认证、生命周期、代理/组合和交互式 Apps。它的高层 API 是官方 SDK v1 FastMCP API 的来源之一，但今天的 `fastmcp` 与官方 `mcp` 是两个包；官方 SDK v2 已重新设计为 `MCPServer` 和统一 `Client`。[FastMCP welcome](https://gofastmcp.com/getting-started/welcome) [FastMCP repository](https://github.com/PrefectHQ/fastmcp)

适用场景：

- 快速开发本项目自有 MCP Server
- 独立 MCP proxy/composition 服务
- 需要其认证、transform、mount/proxy 能力的边缘集成层

不适合作为当前 Host 核心的原因：

- 会与本项目的 installation、Registry、Policy 和恢复生命周期重叠
- 不能替代稳定 ToolIdentity 或不可变 Snapshot
- 增加第二套框架升级面，但当前官方 SDK 已满足协议层需求

### 不推荐：LiteLLM `experimental_mcp_client`/MCP Gateway

LiteLLM 1.95.0 确实提供 MCP Gateway 和 `experimental_mcp_client`，但它不满足本票的 Catalog 合同：[LiteLLM MCP](https://docs.litellm.ai/docs/mcp)

- Gateway 是 LiteLLM Proxy 的 Key/Team/Organization 权限体系，与 SDK-only ADR 冲突
- `experimental_mcp_client.load_mcp_tools()` 在锁定源码中只调用一次 `session.list_tools()`，不遍历分页
- 转换后的 function name 直接沿用 MCP `tool.name`，没有本地稳定 alias
- OpenAI schema 转换会补 `type`/`properties`/`additionalProperties: false`，这可能改变远端 schema 的接受语义
- helper 直接把模型 tool call name 转为 MCP remote name，没有 Snapshot/DefinitionRef 校验

锁定源码：[LiteLLM v1.95.0 MCP bridge](https://github.com/BerriAI/litellm/blob/v1.95.0/litellm/experimental_mcp_client/tools.py)

这些 helper 可以参考格式转换，但不能进入 Registry/Execution 的权威路径。

### SDK 对比

| 候选 | 优点 | 本项目风险 | 结论 |
| --- | --- | --- | --- |
| 官方 `mcp==2.0.0` | 规范同源；现代 Client；HTTP/stdio；缓存、订阅、OAuth、结果校验 | 业务身份、Policy、快照仍需自建 | **采用** |
| FastMCP 3.x | Server/Client 开发体验好；认证、组合、代理能力丰富 | 与 Registry/installation 生命周期重叠 | **只作自有 Server 或独立边缘层备选** |
| LiteLLM experimental MCP client | 快速转 OpenAI Tool，项目已依赖 LiteLLM | experimental；单页；沿用远端名；改写 schema；无业务快照 | **不采用为核心** |
| LiteLLM MCP Gateway | 多 Server、Proxy 权限、OAuth、统一入口 | 引入 Proxy/DB 和第二权限源，违反现有 ADR | **不采用首版** |
| LangChain/OpenAI Agents 等 Agent 框架 Adapter | 可快速得到框架 Tool/Agent Loop | 引入第二套 Agent Loop/Tool identity，绕开自有 Controller | **不采用核心** |

## LiteLLM 与 provider Tool Calling 约束

### LiteLLM 只提供协议归一化，不提供业务保证

LiteLLM `acompletion()` 接受统一的 `tools`、`tool_choice`、`parallel_tool_calls`，响应以 OpenAI Chat Completions 风格暴露 `message.tool_calls[]`，其中参数仍是 JSON 字符串，需要本地解析和 schema 校验。[LiteLLM function calling](https://docs.litellm.ai/docs/completion/function_call) [LiteLLM input params](https://docs.litellm.ai/docs/completion/input)

锁定的 1.95.0 提供：

- `get_supported_openai_params(model, provider)`：某 provider adapter 能映射哪些 OpenAI 参数
- `supports_function_calling()`
- `supports_parallel_function_calling()`
- `supports_tool_choice()`

来源：[LiteLLM v1.95.0 capability source](https://github.com/BerriAI/litellm/blob/v1.95.0/litellm/litellm_core_utils/get_supported_openai_params.py)

本地无网络检查得到一个重要边界：对 `claude-sonnet-4-20250514` 和 `gemini-2.5-pro`，`get_supported_openai_params()` 包含 `parallel_tool_calls`，但 `supports_parallel_function_calling()` 返回 `False`。前者表示参数转换面，后者来自模型 capability metadata，两者不是同一个保证。

因此启动时应同时检查：

1. provider adapter 是否接受 `tools`/`tool_choice`
2. 模型 capability metadata 是否声明 function/parallel support
3. 当前 deployment 的真实 live contract test 是否通过

任何一项未知都应 fail closed；不得用 `drop_params=True`、prompt 模拟工具调用或静默切模型来伪装能力。

### OpenAI

Chat Completions 返回 assistant `tool_calls[]`，每个调用有 `id`、function `name` 和 JSON 字符串 `arguments`；工具结果用 `role="tool"` 和匹配的 `tool_call_id` 回传。Responses API 则用 `function_call` output item 和 `call_id`，结果用 `function_call_output`/`call_id` 关联；可用 `previous_response_id` 或显式历史继续会话。[OpenAI function calling](https://developers.openai.com/api/docs/guides/function-calling) [OpenAI conversation state](https://developers.openai.com/api/docs/guides/conversation-state?api-mode=responses)

其他约束：

- function name 最大 64，只允许字母、数字、下划线和连字符
- strict schema 要求对象 `additionalProperties: false`，且 properties 中字段都列入 required；可空字段应通过类型表达 optional
- `parallel_tool_calls=false` 时最多产生一个调用

本项目当前使用 Chat Completions 风格 `acompletion()`，不应在 Q5 同时切 Responses。未来增加 Responses Adapter 时，应新增 receipt variant，而不是把 `response.id`、item `id`、`call_id` 混成一个字段。

### Anthropic

Anthropic Messages 返回一个或多个 `tool_use` content block，包含唯一 `id`、`name` 和结构化 `input`，`stop_reason` 为 `tool_use`；下一条 user message 中的 `tool_result.tool_use_id` 必须匹配该 ID。[Anthropic handle tool calls](https://platform.claude.com/docs/en/agents-and-tools/tool-use/handle-tool-calls)

并行调用时，所有 `tool_result` 必须一起放在下一条 user message，先于任何文本；即使某个调用未执行，也应返回匹配的 error result，而不是遗漏。[Anthropic parallel tool use](https://platform.claude.com/docs/en/agents-and-tools/tool-use/parallel-tool-use)

LiteLLM 会把它转换成 Chat Completions 风格，但恢复时仍需保存完整且有界的 assistant tool-call envelope 与顺序，不能只保存工具名和参数。

### Gemini

Gemini 支持并行与组合式 function calling。当前 Interactions API 的调用有 `step.id/name/arguments`，结果用 `function_result.call_id` 和 name 回传，并可通过 `previous_interaction_id` 延续。[Gemini function calling](https://ai.google.dev/gemini-api/docs/function-calling)

对于带 thinking 的 Gemini function calling，官方文档要求把 thought signature 原样带回；并行调用时只有第一项可能携带签名，调用和结果的分组、顺序不能被拆散。[Gemini function calling Markdown](https://ai.google.dev/gemini-api/docs/function-calling.md.txt)

LiteLLM 1.95.0 会把 Gemini thought signature 放入 `provider_specific_fields`，某些路径还会编码进 tool call ID。它说明 receipt 需要 provider-specific allowlist，但不说明可以持久化整个响应对象。[LiteLLM v1.95.0 Gemini adapter](https://github.com/BerriAI/litellm/blob/v1.95.0/litellm/llms/vertex_ai/gemini/vertex_and_google_ai_studio_gemini.py)

### Provider call ID 不是业务身份

LiteLLM 的归一化 `ChatCompletionMessageToolCall` 在上游没有 ID 时会生成 UUID，因此看到非空 `tool_call.id` 也不代表它是 provider 原生稳定 ID。[LiteLLM v1.95.0 response types](https://github.com/BerriAI/litellm/blob/v1.95.0/litellm/types/utils.py)

Provider ID 只用于：

- 同一次 provider 对话中关联 call/result
- 审计 provider receipt
- 恢复同一 Model Turn 的协议历史

它不能用于 ToolIdentity、幂等、重复检测或跨 Turn 去重。逻辑调用身份继续使用已决议的 `task_execution_id + model_turn_sequence + call_ordinal`。

## 推荐领域合同

### `ToolIdentity`

最小字段：

| 字段 | 含义 |
| --- | --- |
| `tool_id` | 本地生成的 UUID/ULID，唯一且永不复用 |
| `origin_kind` | `builtin` 或 `mcp` |
| `origin_owner_ref` | builtin catalog key 或本地 `mcp_installation_id`；不使用 serverInfo name |
| `created_at` | 审计时间 |

关键不变量：

- `tool_id` 不包含 remote name、display name、alias 或 schema hash
- builtin 工具由代码/迁移提供稳定 `tool_id`
- MCP 首次发现时按本地 installation + remote name 建立身份
- remote name 消失又出现时，是否复用身份必须依据明确的 tombstone/installation policy，不能按名称相似度猜测
- 改名默认是旧 identity withdrawn + 新 identity created

### `ToolDefinitionRef`

一个 `ToolIdentity` 可以有多个不可变定义版本。最小字段：

| 字段 | 含义 |
| --- | --- |
| `definition_ref` | 不可变定义记录 ID |
| `tool_id` | 所属持久身份 |
| `definition_version` | 本地单调递增版本；MCP 不提供时由 Registry 生成 |
| `definition_digest` | 规范化完整定义的 SHA-256 |
| `adapter_binding_ref` | 不可变 binding：adapter kind、installation/builtin handler、remote name、binding revision |
| `model_definition_ref` | title/description/input schema 及 provider 编译输入 |
| `result_contract_ref` | output schema、允许结果类型、大小/外置规则 |
| `discovery_receipt_ref` | 协议版本、observed-at、远端目录 digest、TTL/cache scope 等有界证据 |

定义 digest 必须覆盖所有会影响模型选择、参数校验或执行路由的字段，包括：

- remote execution name
- model-visible title/description
- canonical input schema
- optional output schema/result contract
- Adapter kind、remote execution name 与 binding revision
- provider schema projection/compiler version 不直接混入领域定义 digest，而单独形成 `model_definition_digest`

annotations/icons 可以进入展示定义或独立 metadata digest，但 annotations 永远不形成授权。

Policy 风险等级、审批和预算不进入 Definition digest；它们属于可独立演进的 Policy revision，并在生成 Snapshot 和 dispatch 时分别留审计引用与重新裁决。

### `ModelToolAlias`

建议使用所有目标 provider 的安全交集：

```text
^[a-z][a-z0-9_]{0,47}$
```

生成示例：

```text
t_web_search_4q7k2m9d
t_github_search_code_h8p3w6nc
t_file_read_2x9m4v7b
```

建议算法：

1. 固定 `t_` 前缀
2. 从展示 slug 取小写 ASCII 字母、数字和下划线
3. 截断后追加 `tool_id + definition_version + alias_ruleset_version` 的 base32 短摘要
4. Snapshot 内再做唯一性检查；碰撞时增加摘要长度，不追加随机序号

最小字段：

| 字段 | 含义 |
| --- | --- |
| `alias` | 实际交给模型的 function name |
| `provider_profile` | `openai_chat_v1`、`anthropic_via_litellm_v1` 等编译规则 |
| `definition_ref` | 唯一指向精确定义 |
| `model_definition_digest` | 模型实际看到的 name/description/schema 摘要 |

Alias 只在 Snapshot 内解析，禁止全局 `get_tool(alias)`。模型回传远端 MCP 名称、展示名或其他 Snapshot 的 alias 时都应作为未知工具拒绝。

### `ToolCatalogSnapshot`

最小字段：

| 字段 | 含义 |
| --- | --- |
| `snapshot_id` | 不可变快照 ID |
| `bound_turn_ref` | `task_execution_id + model_turn_sequence` |
| `model_route_ref` | model alias、provider、API family 与 capability profile |
| `alias_ruleset_version` | Alias 生成/Schema 编译规则版本 |
| `entries` | 有序 `alias -> definition_ref + model_definition_digest` 映射 |
| `visibility_basis_refs` | 生成时使用的 Workspace grant/Agent/Skill/Policy revision，仅供审计 |
| `catalog_digest` | 对有序 entries 做 digest |
| `created_at` | 快照时间 |

`entries` 应按 alias 确定排序，以稳定模型请求与 prompt cache。Snapshot 的不可变性只保证“当时模型看到了什么”，**不代表它永久授权执行**。dispatch 前必须读取当前 grant/Policy。

恢复时不能重新生成 alias，也不能用最新 Catalog 替换旧 entries。只允许：

1. 读取原 Snapshot
2. 解析原 alias 到原 DefinitionRef
3. 重新检查当前授权
4. 验证 Adapter 当前仍能执行该精确定义
5. 按原逻辑调用身份继续或给出明确失败分类

### `ModelTurnReceipt`

除了规范化提案 `UseTools(calls)`，ModelGateway 应返回有界 receipt：

| 字段 | 用途 |
| --- | --- |
| `provider`、`model`、`api_family` | 确定继续会话的协议 |
| `provider_request_ref`/`response_ref` | 审计和 provider 支持时的续接 |
| `normalized_tool_calls` | call ordinal、alias、raw arguments、provider call ID |
| `assistant_envelope` | 继续会话所需的有界 assistant items/message 与顺序 |
| `continuation_fields` | allowlist 后的 call_id、reasoning item refs、thought signatures 等 |
| `usage`、finish/stop reason | 预算与协议校验 |

禁止把完整 SDK response、任意 `provider_specific_fields`、secret header 或未知大字段直接持久化。每个 Adapter 必须声明 continuation allowlist 与大小上限；未知字段 fail closed 或进入外置诊断 blob，不自动进入下一次 provider 请求。

## Schema 指纹与版本规则

### 推荐方案

1. 接收 MCP Tool 后先按协商协议验证 Tool 结构和 JSON Schema
2. 明确填充协议默认值，例如当前缺少 `$schema` 时按 JSON Schema 2020-12 解释
3. 转换为项目自己的 `NormalizedToolDefinitionV1`
4. 对该对象做 RFC 8785 JCS canonicalization
5. 计算 `sha256("tool-definition-v1\0" + canonical_bytes)`
6. digest 不同就创建新的 `definition_version`，绝不覆盖旧记录

RFC 8785 规定 I-JSON 约束、ECMAScript primitive 序列化、递归属性排序、数组顺序保持和 UTF-8 输出，适合跨语言得到稳定字节。[RFC 8785](https://www.rfc-editor.org/rfc/rfc8785.html)

### 不要声称 schema 语义等价

JCS 只规范 JSON 表示，不理解 JSON Schema 语义。例如 `required` 数组顺序变化可能语义等价，但 JCS 会产生不同 digest。这里宁可产生“安全的多余新版本”，也不要尝试复杂且可能错误的 schema 等价归并。

约束：

- 输入必须先满足 I-JSON；重复属性名、NaN/Infinity、非可安全表达数字应拒绝或进入明确的兼容转换
- 不做 Unicode normalization；JCS 要求保留字符串原值
- 不对数组通用排序
- canonicalization profile 必须版本化
- provider-specific schema 降级单独生成 `model_definition_digest`

如果某 provider 只支持 JSON Schema 子集，compiler 必须：

- 能无损投影时生成 provider schema
- 需要改变接受语义时拒绝该 Tool 对该 provider 可见
- 绝不能像通用 helper 一样静默添加 `additionalProperties: false` 或删除关键约束

实际执行参数始终再按不可变 canonical input schema 验证，不以模型 provider 的 schema adherence 作为授权或正确性保证。

## 漂移、撤销与恢复行为矩阵

| 场景 | 已持久化 Snapshot | dispatch 前结果 | 是否调用 Adapter | 是否可用同一逻辑调用重试 | 后续动作 |
| --- | --- | --- | --- | --- | --- |
| 目录未变化，grant 有效 | 保持不变 | `READY` | 是 | 按幂等策略 | 正常执行 |
| 收到 `tools/list_changed`，尚无调用 | 保持不变 | 未来目录缓存 stale | 否 | 不适用 | refresh 后只生成未来 Snapshot |
| 同名 Tool schema/description/binding 变化 | 旧 ref 保留 | `DEFINITION_MISMATCH` | 否 | 否，不能按新定义重试旧调用 | 新 Model Turn + 新 Snapshot + 新提案 |
| Tool 从目录消失 | 旧 ref 保留 | `DEFINITION_WITHDRAWN` | 否 | 仅等原定义重新可用时可评估；不可改路由 | 告知模型不可用，建立新 Snapshot |
| MCP Tool 改名 | 旧 identity/ref 保留，新名默认新 identity | 旧调用 `WITHDRAWN` | 否 | 否 | 管理员显式映射前视为两个工具 |
| Workspace grant 在模型提案后撤销 | 保持不变，审计仍可读 | `POLICY_DENIED/GRANT_REVOKED` | 否 | 否 | 终止该调用；可让模型在新可见目录下重规划 |
| installation 被禁用/删除 | 保持不变 | `BINDING_REVOKED` | 否 | 否 | 管理操作后才可重新启用 |
| endpoint timeout、DNS、连接断开 | 保持不变 | `ADAPTER_UNAVAILABLE` | 已尝试或健康检查失败 | 是，保留原逻辑调用身份并退避 | 不改 Catalog，不伪装成 Tool 消失 |
| discovery 无法完成 | 保持不变 | 无法证明漂移，分类为 `ADAPTER_UNAVAILABLE` | 否 | 是 | 不能猜测 `WITHDRAWN`/`MISMATCH` |
| 恢复旧 Model Turn，目录 TTL 已过 | 使用原 Snapshot | 先重检 grant，再 refresh/比对精确定义 | 条件满足才调用 | 依状态分类 | 不重新生成 alias |
| provider continuation receipt 缺关键字段 | Snapshot 保持 | `MODEL_PROTOCOL_UNRECOVERABLE` | 否 | 不盲目重发 | 明确失败或从新 Turn 重规划 |

关键判定顺序：

```text
resolve alias in frozen snapshot
  -> validate canonical args
  -> re-check current grant/policy/budget/dedup
  -> ensure adapter binding is administratively enabled
  -> ensure discovery freshness
  -> compare current remote definition with expected definition_ref
  -> dispatch using the frozen remote binding
```

这个顺序防止把权限撤销误报为网络故障，也防止在 refresh 后把旧 alias 路由到新定义。

## 建议接入序列

```text
MCP Client discovery
  -> McpToolAdapter normalizes all pages
  -> ToolRegistry creates/reuses ToolIdentity
  -> ToolRegistry appends immutable ToolDefinitionRef
  -> ToolPolicy intersects system/agent/skill/workspace visibility
  -> Provider compiler creates ModelToolAlias + model schema
  -> persist ToolCatalogSnapshot bound to Model Turn
  -> ModelGateway calls LiteLLM with snapshot entries
  -> persist UseTools + ModelTurnReceipt
  -> validate whole batch and current grant
  -> ToolExecution dispatches exact DefinitionRef
  -> persist Observation
  -> ModelGateway continues using provider-specific receipt
```

Provider 自带 Remote MCP（例如 OpenAI Responses、Gemini Interactions 或 LiteLLM Proxy MCP）首版不使用。它会让 provider 直接发现或执行远端 Tool，使本地 Snapshot、Policy、审批、幂等和 Observation 失去唯一控制点，也无法在 provider 之间保持一致语义。

## 测试建议

测试只验证业务行为，不断言 Registry 条目数、类字段数或 Graph 结构：

- 两个 installation 都有 `search` 时，模型得到无碰撞 alias，执行各自精确 binding
- 同一 Tool 的 schema 从 v1 漂移到 v2 后，旧 Snapshot 调用零副作用并返回 `DEFINITION_MISMATCH`
- remote rename 不会让旧调用落到新名字
- `list_changed` 只影响未来 Snapshot，恢复旧 Turn 仍读取原映射
- grant 在模型提案后撤销，dispatch 前被拒绝且 MCP Server 零调用
- Adapter timeout 保留原逻辑调用身份并可退避重试，不产生新 Tool Call
- 分页目录完整消费；分页中收到 change notification 时丢弃该轮发现结果
- provider 返回未知 alias、非法 JSON 或 canonical schema 不接受的参数时，整批零执行
- Anthropic 并行结果保持同一 user message；Gemini thought signature 和调用顺序在恢复后不丢失
- LiteLLM capability 未知或参数被判 unsupported 时 fail closed，不启用 `drop_params`

## 是否需要 prototype/live spike

不需要为了证明“Python 能连接 MCP 并调用工具”再启动 prototype：当前 Adapter 和官方 SDK 已经证明这一点。

实施前建议做一个**小型、可丢弃的协议 spike**，不是产品原型：

1. 本地 MCP v2 Server 先公开 schema A，再发 `tools/list_changed` 并切到 schema B
2. Host 冻结 Snapshot A，确认旧 alias 在变化后得到 `DEFINITION_MISMATCH`，不会调用 B
3. 验证 `Client(mode="auto")`、cache TTL、`subscriptions/listen`、断线重订阅
4. 对 OpenAI、Anthropic、Gemini 各做一次真实 provider contract test：单调用、两个并行调用、结果回传、进程重启后 receipt 恢复
5. 特别核对 LiteLLM 1.95.0 对 Gemini thought signature、Anthropic tool result 分组、OpenAI Responses call_id 的保留

本调研**没有执行任何真实 provider、远程 MCP、OAuth 或网络故障恢复验证**。本地 capability introspection 不是 live provider 证明，不能据此关闭这些验证项。

## 本票应决定与后续票据

### Issue #15 应决定

- `ToolIdentity`、`ToolDefinitionRef`、`ModelToolAlias`、`ToolCatalogSnapshot` 的语义与最小字段
- alias 只在 Snapshot 内解析
- Definition 漂移时禁止静默路由
- grant、definition、binding、availability 的错误分类
- ModelTurnReceipt 的 provider continuation 边界
- SDK 选型：官方 `mcp` v2 + 自有 Registry/Policy/Execution
- LiteLLM fail-closed capability gate

### 留给后续实现/存储票据

- 具体表名、索引、分区和 FK
- Definition/Snapshot 保留期、压缩和垃圾回收
- installation credential/OAuth token 加密存储
- 多 Worker subscription ownership、leader election 和 refresh debounce
- Server 健康检查、连接池和限流
- JCS 库选型与跨语言测试向量
- provider receipt blob 外置策略和加密
- MCP extensions、sampling、elicitation、roots 的产品策略
- 远端 Tasks extension 的 durable job Adapter

## 一手资料索引

- [MCP 2026-07-28 Tools specification](https://modelcontextprotocol.io/specification/2026-07-28/server/tools)
- [MCP 2026-07-28 Subscriptions](https://modelcontextprotocol.io/specification/2026-07-28/basic/patterns/subscriptions)
- [MCP 2026-07-28 Caching](https://modelcontextprotocol.io/specification/2026-07-28/server/utilities/caching)
- [MCP Python SDK v2.0.0 release](https://github.com/modelcontextprotocol/python-sdk/releases/tag/v2.0.0)
- [MCP Python SDK v2 source](https://github.com/modelcontextprotocol/python-sdk/tree/v2.0.0)
- [FastMCP official docs](https://gofastmcp.com/getting-started/welcome)
- [LiteLLM Function Calling](https://docs.litellm.ai/docs/completion/function_call)
- [LiteLLM Input Params](https://docs.litellm.ai/docs/completion/input)
- [LiteLLM Unsupported Params](https://docs.litellm.ai/docs/completion/drop_params)
- [LiteLLM MCP](https://docs.litellm.ai/docs/mcp)
- [LiteLLM v1.95.0 source](https://github.com/BerriAI/litellm/tree/v1.95.0)
- [OpenAI function calling](https://developers.openai.com/api/docs/guides/function-calling)
- [OpenAI conversation state](https://developers.openai.com/api/docs/guides/conversation-state?api-mode=responses)
- [Anthropic define tools](https://platform.claude.com/docs/en/agents-and-tools/tool-use/define-tools)
- [Anthropic handle tool calls](https://platform.claude.com/docs/en/agents-and-tools/tool-use/handle-tool-calls)
- [Anthropic parallel tool use](https://platform.claude.com/docs/en/agents-and-tools/tool-use/parallel-tool-use)
- [Gemini function calling](https://ai.google.dev/gemini-api/docs/function-calling)
- [RFC 8785 JSON Canonicalization Scheme](https://www.rfc-editor.org/rfc/rfc8785.html)
