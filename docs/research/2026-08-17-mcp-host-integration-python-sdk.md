# MCP Host 接入架构与 Python SDK 选型调研

日期：2026-08-17

## 范围

本文回答 Wayfinder Research 票“调研 MCP Host 接入架构与 Python SDK 选择”。目标不是让 MCP SDK 接管 Agent Loop，而是确定 MCP Server 如何作为外部 Tool Provider 接入已确定的 `ToolCallingTurn`、`ToolRegistry`、`ToolPolicy` 与 `ToolExecution`。

本文只使用 MCP 规范、官方 Python SDK、候选 SDK 官方文档与源码，以及仓库当前实现。没有连接真实远程 MCP Server，也没有修改生产实现。

## 结论

1. **首选并继续使用官方 `mcp` Python SDK v2**。仓库已声明 `mcp>=2.0,<3`、锁定 `2.0.0`，当前 `LocalTrustedHttpMcpAdapter` 也直接使用 `ClientSession` 与 `streamable_http_client`。官方 SDK 覆盖初始化、Streamable HTTP/stdio、协议结果类型、OAuth 与 schema/result 原语；分页循环、目录快照、`list_changed` 缓存策略和业务错误归一化仍由 Host 自己负责。不需要再引入一个框架来获得基本协议能力。[1][2]
2. **MCP 只位于 Provider/Adapter seam**。官方 SDK 返回的 `Tool`、`CallToolResult`、异常、session ID 或 task ID 不得成为业务 Tool 身份、授权或恢复事实。应用层必须生成自己的 installation identity、catalog snapshot、Tool Call、Tool Run、Tool Observation 与 wait reference。
3. **`ToolRegistry` 应快照 MCP 目录，而不是每次模型调用都直连发现**。完整消费 `tools/list` 分页结果，净化并验证 schema，为每个 Tool 固化 server installation、remote name、schema hash、definition revision 与本地 alias。收到 `tools/list_changed` 后只使未来快照失效，不能原地改写已持久化的旧快照。[1][2]
4. **MCP annotations 只是不可信提示**。`readOnlyHint`、`destructiveHint`、`idempotentHint`、`openWorldHint` 缺失时都有保守默认值，规范还要求客户端不要信任不可信服务器提供的 annotations。本地 `ToolPolicy` 必须拥有最终风险、审批、并发和重试裁决。[3]
5. **MCP `outputSchema` 与 SDK 校验值得复用，但不能替代本地结果合同**。SDK v2 能针对已发现工具的 output schema 校验 `structuredContent`；应用仍需做大小限制、secret 清理、Evidence/File/Artifact 引用转换，并由 Observation writer 形成业务事实。[1][3]
6. **MCP task-augmented execution 目前只能视为未来/实验性远端扩展**。2025-11-25 规范中的 Tasks 仍是实验性能力，而官方 Python SDK v2 migration 明确移除了实验性 Tasks 支持。因此不能宣称当前 `mcp>=2,<3` 提供 task execution。若未来选定具体协议版本和维护中的 SDK/Server 组合，Adapter 才可把远端 task ID 包进内部 `DurableJobReceipt`；任务轮询、租约、取消、重试和单一终态 Observation 仍由本项目的 ToolExecution/Worker 管理。[3][4]
7. **官方 SDK v2 的 `MCPServer` 与第三方 Prefect FastMCP 必须区分**。后者可用于编写测试 Server 或独立集成层，但不应成为首版 Host 核心依赖；其 Server 配置、命名前缀、认证和 session lifecycle 不能替代本项目的 Tool Registry、installation、Policy 和 Worker 生命周期。[5]
8. **不采用 LiteLLM MCP Gateway、LangChain MCP Adapters 或 OpenAI Agents SDK MCP 层作为核心 Host**。三者分别把 MCP 绑定到 Proxy 权限体系、LangChain Tool/Agent 抽象或 Agents SDK Agent Loop。本项目已经决定进程内 LiteLLM SDK、业务层 ToolPolicy 和自持可恢复 Controller，引入这些层会产生第二套目录、权限或运行时事实。[5][6][7]

## 当前仓库事实

- `pyproject.toml` 声明 `mcp>=2.0,<3`，`uv.lock` 锁定 `mcp==2.0.0`
- `LocalTrustedHttpMcpAdapter` 使用官方 SDK 的 `ClientSession`、`streamable_http_client` 和 `MCPError`
- 当前 Adapter 只连接配置中的单个 localhost Streamable HTTP endpoint
- `_list_all_tools()` 已处理 `next_cursor` 分页，但只投影 `name/description/input_schema`
- `call_tool()` 只返回最多 2000 字符的 summary，没有保留 `structuredContent`、output schema、content blocks 或 provider receipt
- 当前参数在进入 MCP SDK 前没有按远端 `inputSchema` 做本地校验
- 当前 `ToolExecutionService` 只识别一个固定 MCP Tool，不是通用 Registry

这些事实说明应扩展现有的薄 Adapter seam，而不是替换成新的 Agent 框架。

## 推荐接入分层

```text
ReActTaskController.advance(TaskClaim)
  -> ToolCallingTurn.run(TaskTurnPermit)
       -> ToolRegistry.snapshot/prepare
            -> McpToolProvider.list_definitions
       -> ToolPolicy.decide
       -> ToolExecution.dispatch
            -> McpToolAdapter.call
                 -> official mcp Python SDK
```

### `McpInstallation`

一个 Workspace 启用的 MCP Server 应先成为本地 installation 事实，至少包含：

- 稳定 `installation_id`
- server endpoint/transport reference
- 凭据引用，不保存明文 token
- Workspace grant 与 enabled/revoked 状态
- 允许的协议版本和 server identity
- endpoint 网络策略、TLS/OAuth 配置与超时
- 最近一次成功 discovery 的时间、etag/schema digest 或错误状态

模型不得提供 endpoint、workspace ID、credential、installation ID 或 transport 参数。

### `McpToolProvider`

Provider 负责协议发现和规范化，不负责授权：

```python
class McpToolProvider(Protocol):
    async def list_definitions(
        self, installation: McpInstallationRef
    ) -> tuple[McpToolDefinitionDraft, ...]: ...
```

实现要求：

1. 建立受策略约束的 SDK Client/session 并完成 initialize
2. 遍历所有 `tools/list` 页面，禁止只保存第一页
3. 校验 tool name、input schema 与可选 output schema
4. 保存远端原名与 schema hash，不直接使用远端名作为模型 alias
5. annotations、icons、title 等保留为诊断或显示数据，不形成权限事实
6. 若 Server 广告 `tools.listChanged`，订阅 `notifications/tools/list_changed` 并使当前快照失效；未广告时使用 TTL、显式刷新和错误恢复策略。通知是失效信号，不是新目录本身

### Catalog 身份

建议分离三种身份：

```text
业务身份       mcp:<installation_id>:<remote_name>@<schema_hash>
模型别名       github_search_code_ab12
Provider call  远端或模型返回的短期 call ID
```

- 业务身份稳定且可审计
- 模型别名只需满足当前模型供应商的命名限制，在单次快照中唯一
- Provider call ID 只用于本次 Tool Calling 协议关联，不参与幂等身份

### 调用与结果

调用前的确定顺序：

1. 用 catalog snapshot 将模型 alias 解析为业务 Tool definition
2. 按冻结的 input schema 校验并规范化参数
3. 计算 canonical args 与重复指纹
4. `ToolPolicy` 依据系统、Task、Skill、Workspace grant、风险、预算和审批裁决
5. 原子创建本地 Tool Call/Tool Run/outbox
6. Adapter 才可调用 `ClientSession.call_tool`

Adapter 返回统一草稿：

```python
ToolAdapterReceipt = ImmediateToolResult | DurableJobReceipt
```

`ImmediateToolResult` 可以携带有界 structured content、content block 摘要、资源引用和诊断引用；完整二进制、大文本或秘密必须外置。SDK/协议层的 `isError=true` 是工具执行结果，不是 transport exception，应先规范化为失败 Observation 再交给 Controller。[3]

## Transport 与认证建议

### Streamable HTTP

作为远程与平台托管 Server 的默认 transport。SDK v2 允许注入预配置 HTTP client，适合集中设置 OAuth、TLS、proxy、超时、重定向和 headers。[1][2]

必须在建连前执行：

- endpoint allowlist 与 SSRF 防护
- DNS/redirect 后地址复核
- 禁止访问 metadata、loopback、私网等未授权地址
- Workspace installation 与 credential scope 校验
- 请求和响应大小、连接数、read timeout 限制

### stdio

只用于用户明确安装、由 Plugin/Sandbox supervisor 启动的本地 Server。API/Worker 不得根据模型参数直接执行 `npx`、`uvx` 或任意 command。安装解析、版本固定、环境变量、工作目录和资源限制都应先成为受控 installation 配置。

### OAuth

官方 SDK v2 提供 `OAuthClientProvider`，可以管理 discovery、授权码/PKCE、token 与 client registration。[1] 生产实现需要持久化且加密的 token storage，并把 user/workspace/token scope 绑定到 installation；示例中的内存存储和交互式 `input()` 不能直接用于 Worker。

## SDK 候选比较

| 候选 | 优点 | 与本项目的冲突 | 结论 |
| --- | --- | --- | --- |
| 官方 `mcp` Python SDK v2 | 规范同源；ClientSession/高层 Client；HTTP/stdio；分页、通知、OAuth、schema 校验 | 仍需自己实现 installation、Policy、持久化、恢复 | **采用** |
| Prefect FastMCP 3.x | 多 Server 配置、前缀、认证、复用 session，Server 开发体验好 | 会与 Registry/installation 生命周期重叠；额外依赖与升级面 | **可选，用于自有 Server 或独立适配层，不作为首版核心** |
| LiteLLM MCP Gateway | Proxy 统一多 Server、Key/Team 权限和 OAuth | 需要 LiteLLM Proxy/DB，复制本项目 ToolPolicy；违反 SDK-only ADR | **不采用** |
| LangChain MCP Adapters | 快速转成 LangChain tools，支持多 Server | 引入 LangChain Agent/Tool 语义，绕开业务 Tool Call/Observation | **不采用** |
| OpenAI Agents SDK MCP | hosted/local MCP、Agent Loop、审批和 tracing 集成完整 | 绑定另一套 Agent Loop；本项目使用 LiteLLM 且 Controller 自持恢复与审批 | **不采用** |

## 需要明确拒绝的捷径

- 不把 MCP tool name 直接放入全局 Registry 并相信其唯一
- 不信任远端 annotations 自动免审批、自动并行或自动重试
- 不在 catalog 更新后把旧 alias 静默指向新 schema
- 不让 ModelGateway 直接调用 MCP
- 不让 MCP SDK result 直接成为 Tool Observation
- 不把 `isError=false` 等同于结果已经满足本地 provenance/result contract
- 不让 Server sampling、elicitation、roots 或 logging callback 默认开启；这些能力必须分别建立产品策略和用户交互后才可启用
- 不把当前 SDK 未支持的远端 MCP task ID 当作本地 Job/Research Task 身份；未来启用时也只能作为不透明 provider metadata

## 测试建议

行为测试穿过 `ReActTaskController.advance(TaskClaim)`，至少覆盖：

- 多页目录被完整快照，模型 alias 冲突得到稳定消歧
- 远端 schema 漂移不会改变旧 catalog snapshot
- 非法参数在零 MCP 副作用前被拒绝
- annotations 声称 read-only，但本地 Policy 仍要求审批
- grant 在模型提案后被撤销，dispatch 前复核并拒绝
- `structuredContent` 不符合 output schema 时形成安全失败 Observation
- `isError=true` 与 transport/protocol error 被分类为不同事实
- 重放同一 logical Tool Call 只产生一次外部副作用
- 远端 task/断线恢复只发布一个 fenced 终态 Observation

真实集成需要单独验证 Streamable HTTP、OAuth、stdio supervisor、目录变更通知、schema mismatch 和 `isError`；远端 task 还需要先锁定目标协议版本并确认 SDK 实现。本研究没有完成这些 live proof。

## 是否需要 prototype

MCP Host 的核心可行性已经由当前 Adapter、官方 SDK 与既有审批测试证明，不需要为了“能否连接并调用”再做原型。实施前应做一个很小的兼容性 spike：本地 MCP Server 动态修改 schema 并触发目录变更，验证 catalog 快照不会漂移、结果 schema mismatch 与 `isError` 分类正确；远端 task 不应作为当前 SDK v2 的验收前提。

## 来源

1. [MCP Python SDK v2 文档](https://py.sdk.modelcontextprotocol.io/v2/)
2. [MCP Python SDK v2 migration](https://py.sdk.modelcontextprotocol.io/v2/migration/)
3. [MCP 2025-11-25 Tools specification](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)
4. [MCP Tasks 2025-11-25 与 Tasks extension](https://modelcontextprotocol.io/specification/2025-11-25/basic/utilities/tasks)
5. [官方 SDK v2 migration 与 Prefect FastMCP 官方仓库](https://py.sdk.modelcontextprotocol.io/v2/migration/)，[FastMCP](https://github.com/PrefectHQ/fastmcp)
6. [LiteLLM MCP Gateway 官方文档](https://docs.litellm.ai/docs/mcp)
7. [LangChain MCP Adapters 官方仓库](https://github.com/langchain-ai/langchain-mcp-adapters)
8. [OpenAI Agents SDK MCP integrations](https://openai.github.io/openai-agents-python/mcp/)

## 本地证据

- [pyproject.toml](../../pyproject.toml)
- [uv.lock](../../uv.lock)
- [mcp_adapter.py](../../apps/api/src/deep_researcher/mcp_adapter.py)
- [tool_execution.py](../../apps/api/src/deep_researcher/tool_execution.py)
- [既有 LangGraph 审批恢复与 MCP Python SDK 调研](./2026-08-08-langgraph-interrupt-and-mcp-python-sdk.md)
