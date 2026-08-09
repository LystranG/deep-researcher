# LangGraph 审批恢复与 MCP Python SDK 调研

日期：2026-08-08

## 1. 范围与版本基线

本笔记只核对以下实现前提：

- LangGraph `interrupt()` / `Command(resume=...)` 的暂停、恢复、重放与副作用语义
- 官方 MCP Python SDK 的 Streamable HTTP、`ClientSession`、`initialize`、`list_tools`、`call_tool` 与错误语义
- 这些语义对 Tool Call、Tool Run、Tool Approval 一次性审批边界的直接约束

仓库当前在 [`pyproject.toml`](../../pyproject.toml) 声明 `langgraph>=1.2,<2`，在 [`uv.lock`](../../uv.lock) 精确锁定 `langgraph==1.2.10`。以下 LangGraph 结论同时核对了当前官方文档和 `1.2.10` 标签源码。

仓库当前**没有**声明或锁定 `mcp` 包。PyPI 显示官方 `mcp==2.0.0` 于 2026-07-28 发布，官方仓库将 v2 标为当前稳定线。因此 MCP 部分以 `2.0.0` 为当前选型基线，但在实现前必须先把版本范围写入依赖并重新锁定，不能把本笔记当作已有锁文件事实。

Context7 当前可定位的版本化 MCP 资料仍主要是 `v1.12.4`。官方 `v2.0.0` 已发生破坏性接口变化，本笔记以官方 `v2.0.0` 标签源码和 v2 文档为准，并在下文单独列出差异。

## 2. 结论摘要

1. `interrupt()` 依赖 checkpointer 和稳定 `thread_id`。恢复必须使用原线程，并把 `Command(resume=...)` 作为下一次 graph 输入
2. 恢复时不是从 Python 调用栈原地继续，而是从包含 `interrupt()` 的节点开头重新执行；中断前代码会再次运行
3. 同一任务内的多个中断按调用顺序匹配 resume 值；并行中断应使用 `{interrupt_id: resume_value}` 映射
4. LangGraph 只提供可恢复执行，不提供外部 Tool 副作用的 exactly-once 保证。审批、参数绑定、执行抢占、幂等键与结果落库必须由业务表和 Adapter 自己保证
5. 当前官方 MCP SDK 稳定线是 v2。`streamable_http_client()` 返回 `(read_stream, write_stream)`，再用 `ClientSession` 建立会话；这与 v1.12.4 的旧 `streamablehttp_client()` 三元组接口不同
6. `initialize()` 完成协议协商并发送 initialized 通知；`list_tools()` 返回分页结果；`call_tool()` 返回业务结果，但协议错误、连接关闭和超时会抛 `MCPError`
7. `CallToolResult.is_error == true` 是“工具执行失败”的正常协议结果，不等同于 JSON-RPC 协议错误；成功结果若声明了 output schema，SDK 会校验 `structured_content`
8. 拒绝、过期、停用和取消必须在进入 `call_tool()` 之前由业务事实源判定。`Command(resume=...)` 里的客户端值不能直接作为执行授权

## 3. LangGraph interrupt 精确语义

### 3.1 暂停与恢复条件

官方文档要求：

- graph 必须使用 checkpointer 编译
- 调用必须带 `configurable.thread_id`
- `interrupt()` payload 必须可 JSON 序列化
- 恢复时必须复用相同 `thread_id`
- 传入 `Command(resume=value)` 的 `value` 会成为节点内 `interrupt()` 的返回值

`thread_id` 是 checkpoint 的持久游标。换一个 `thread_id` 会得到新线程，而不是恢复原审批。

来源：

- [LangGraph Interrupts 官方文档](https://docs.langchain.com/oss/python/langgraph/interrupts)
- [LangGraph 1.2.10 `interrupt()` 源码](https://github.com/langchain-ai/langgraph/blob/1.2.10/libs/langgraph/langgraph/types.py#L811-L930)
- [LangGraph 1.2.10 `Command` 源码](https://github.com/langchain-ai/langgraph/blob/1.2.10/libs/langgraph/langgraph/types.py#L759-L810)

### 3.2 恢复会重跑节点

官方文档和 `1.2.10` 源码都明确说明：恢复时从节点开头重新执行，不是从 `interrupt()` 后一行原地继续。第一次执行中位于 `interrupt()` 之前的读取、写入、网络请求、时间读取和随机值生成都会再次发生，除非它们已经被独立的 checkpoint/task 结果替代。

直接后果：

- 创建 Tool Approval 必须是幂等 upsert 或受唯一约束保护
- 不能在 `interrupt()` 前执行真实高风险工具
- 即使真实调用放在 `interrupt()` 后，Worker 在外部调用完成但业务结果未提交时崩溃，仍可能再次进入该调用
- 恢复路径必须重新查询业务表，不得只相信 checkpoint 中的 approval payload

官方建议工作流保持确定性和幂等性，把副作用或非确定性操作封装为 task；已完成 task 的结果可由 checkpoint 重放，但“已经开始、尚未完成”的 task 仍可能重试，所以副作用本身仍需幂等键或结果核对。

来源：

- [LangGraph Interrupts：Rules of interrupts 与 idempotent side effects](https://docs.langchain.com/oss/python/langgraph/interrupts#rules-of-interrupts)
- [LangGraph Durable execution](https://docs.langchain.com/oss/python/langgraph/durable-execution)
- [LangGraph Functional API：Determinism and idempotency](https://docs.langchain.com/oss/python/langgraph/functional-api)

### 3.3 多个中断与恢复值匹配

`1.2.10` 的 `interrupt()` 使用任务级 scratchpad 记录中断序号，按同一节点中的调用顺序复用 resume 值。因此：

- 不应根据非确定性条件改变同一节点中多个 `interrupt()` 的顺序
- 不应把 `interrupt()` 放进会在恢复时重复扩张的 `while True` 循环
- 并行分支同时暂停时，应读取每个 `Interrupt.id`，一次性传入 `Command(resume={id: value, ...})`
- 当前审批切片宜保持“一个审批节点一次只产生一个中断”，减少顺序和并行映射复杂度

来源：

- [LangGraph Interrupts：Handling multiple interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts#handling-multiple-interrupts)
- [LangGraph 1.2.10 `interrupt()` 的顺序匹配实现](https://github.com/langchain-ai/langgraph/blob/1.2.10/libs/langgraph/langgraph/types.py#L903-L930)

### 3.4 不要捕获 interrupt 异常

`interrupt()` 通过抛出内部 `GraphInterrupt` 暂停运行。用宽泛的 `try/except Exception` 包住它会吞掉暂停信号。审批节点只能捕获明确的业务异常，不能包住 `interrupt()` 本身。

来源：[LangGraph Interrupts：Do not wrap interrupt calls in try/except](https://docs.langchain.com/oss/python/langgraph/interrupts#do-not-wrap-interrupt-calls-in-tryexcept)

## 4. Tool Approval 的 exactly-once 边界

以下是基于上述官方语义得到的项目实现约束，不是 LangGraph 自动提供的能力。

### 4.1 interrupt payload 不是授权事实

建议 `interrupt()` 只暴露安全、可展示的审批摘要：

- `approval_id`
- `tool_run_id`
- 工具显示名与风险级别
- 参数 hash 和脱敏摘要
- 过期时间

不要放入 secret、token、完整 prompt 或可直接执行的未脱敏参数。恢复值只表达用户决策或 approval ID；真正授权必须重新读取 Tool Approval 业务表并校验：

- approval 精确绑定当前 `run_id`
- approval 精确绑定当前 user
- approval 参数 hash 与待执行 Tool Call 的规范化参数 hash 相等
- approval 未过期、未拒绝、未消费
- MCP/Skill/Workspace/Conversation/Agent 的有效授权交集仍然成立
- run 未取消，工具和安装未停用

任一条件不满足都必须在 `call_tool()` 前终止，保证零工具副作用。

### 4.2 执行只允许一个所有者

批准后应通过业务数据库的条件更新抢占执行权，例如把同一 `tool_run_id` 从 `approved` 原子转换为 `executing`。只有成功完成该条件更新的 Worker 才能进入 MCP Adapter。

需要数据库唯一约束或等价 CAS 保证：

- 同一 Tool Call 只有一个 Tool Run 或一个稳定 invocation key
- 同一 Tool Approval 只能消费一次
- Worker lease/checkpoint 恢复不能创建第二个 invocation key
- 重复 approve、重复 resume 和 SSE 重放只返回已有事实，不再次调用工具

### 4.3 外部调用后的崩溃窗口

如果 Worker 已调用远程工具并产生副作用，但在提交 Tool Run 结果前崩溃，仅靠 LangGraph checkpoint 和本地数据库无法判断远端是否已经成功。严格 exactly-once 需要至少一种额外能力：

- 把稳定 `tool_run_id` 作为远端支持的 idempotency key
- 受信本地 MCP Adapter 持久化 invocation key，并对重复请求返回首次结果
- 对不可幂等工具提供可查询的 operation ID 和恢复核对
- 无法提供上述能力时，将状态标记为结果不确定，禁止自动重试高风险副作用

因此项目可以保证“批准只被消费一次、只有一个 Worker 获得调用权”，但不能把不支持幂等的任意第三方 HTTP MCP 调用宣称为端到端 exactly-once。

## 5. MCP Python SDK 当前版本事实

### 5.1 当前稳定线与仓库状态

官方 PyPI 当前版本是 `mcp==2.0.0`，上传时间为 2026-07-28。官方 README 明确称 v2 为当前稳定线，并说明 `pip install mcp` 已安装 2.x；未迁移的 v1 使用者必须显式 `<2`。

本仓库当前没有 `mcp` 依赖，因此实施前应先作明确选择并锁定。按新项目和 Python 3.13 基线，优先评估 v2；如果实现选择 v1，则必须显式锁定 `<2`，并按 v1 API 编写，不能混用 v2 示例。

来源：

- [PyPI `mcp 2.0.0`](https://pypi.org/project/mcp/2.0.0/)
- [MCP Python SDK v2.0.0 README](https://github.com/modelcontextprotocol/python-sdk/blob/v2.0.0/README.md)
- [MCP Python SDK v1 到 v2 迁移指南](https://py.sdk.modelcontextprotocol.io/migration/)

### 5.2 v1.12.4 与 v2.0.0 的 Streamable HTTP 差异

| 版本 | 导入符号 | context manager 返回值 |
| --- | --- | --- |
| v1.12.4 | `streamablehttp_client` | `(read_stream, write_stream, get_session_id)` |
| v2.0.0 | `streamable_http_client` | `(read_stream, write_stream)` |

v2 允许传入预配置的 `httpx2.AsyncClient`，用于统一认证、headers 和 HTTP 配置；context 退出时默认在存在 MCP session ID 的情况下发送 DELETE 终止会话。

来源：

- [v1.12.4 Streamable HTTP 源码](https://github.com/modelcontextprotocol/python-sdk/blob/v1.12.4/src/mcp/client/streamable_http.py#L438-L504)
- [v2.0.0 Streamable HTTP 源码](https://github.com/modelcontextprotocol/python-sdk/blob/v2.0.0/src/mcp/client/streamable_http.py#L640-L710)

## 6. v2 ClientSession 调用流程

低层 `ClientSession` 的标准结构是：

```python
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

async with streamable_http_client(url, http_client=http_client) as (read, write):
    async with ClientSession(read, write, read_timeout_seconds=timeout) as session:
        await session.initialize()
        page = await session.list_tools()
        result = await session.call_tool(tool_name, arguments, read_timeout_seconds=timeout)
```

这段只说明 SDK 生命周期，不代表项目可以直接接受任意 URL 或任意工具。项目 Adapter 仍需在建连前执行 endpoint allowlist、Workspace ACL、安装/启用状态、工具白名单、风险策略和审批校验。

### 6.1 `initialize()`

v2 `initialize()`：

- 发送 initialize request，协商握手协议版本和客户端 capabilities
- 拒绝 SDK 不支持的服务端协议版本
- 安装协商状态
- 发送 initialized notification
- 同一 `ClientSession` 已初始化后再次调用会返回缓存结果

来源：[MCP Python SDK v2.0.0 `ClientSession.initialize`](https://github.com/modelcontextprotocol/python-sdk/blob/v2.0.0/src/mcp/client/session.py#L613-L636)

### 6.2 `list_tools()`

`list_tools()` 返回 `ListToolsResult`，其中 `tools` 是当前页，`next_cursor` 非空时还要继续分页。项目计算“有效工具交集”前必须完成所需分页，不能只取第一页。

SDK 会缓存工具的 output schema，供后续 `call_tool()` 校验成功结果。工具列表和服务端 annotations 只能作为发现输入；项目后端的风险覆盖与启用策略仍是授权事实。

来源：

- [MCP Python SDK v2.0.0 `ClientSession.list_tools`](https://github.com/modelcontextprotocol/python-sdk/blob/v2.0.0/src/mcp/client/session.py#L1234-L1265)
- [MCP Tools 规范：Listing tools](https://modelcontextprotocol.io/specification/2026-07-28/server/tools#listing-tools)

### 6.3 `call_tool()` 与结果校验

v2 `call_tool()` 发送 `tools/call`，默认返回 `CallToolResult`。当结果不是工具错误时，SDK 会用最近 `list_tools()` 中声明的 output schema 校验 `structured_content`：

- 声明 output schema 但未返回 `structured_content`：抛 `RuntimeError`
- `structured_content` 不符合 schema：抛 `RuntimeError`
- output schema 本身非法：抛 `RuntimeError`
- `is_error == true`：作为工具执行失败结果返回，不做成功 output schema 校验

来源：

- [MCP Python SDK v2.0.0 `ClientSession.call_tool`](https://github.com/modelcontextprotocol/python-sdk/blob/v2.0.0/src/mcp/client/session.py#L955-L1097)
- [MCP Tools 规范：Tool result](https://modelcontextprotocol.io/specification/2026-07-28/server/tools#tool-result)

## 7. MCP 错误语义

| 类别 | SDK 表现 | Tool Run 建议结果 |
| --- | --- | --- |
| JSON-RPC 协议错误，如未知工具、非法参数、服务端协议错误 | `MCPError` | `failed_protocol`，不可当作成功 content |
| 请求读取超时、连接关闭 | `MCPError` | `failed_transport` 或 `result_unknown`，按风险决定是否允许重试 |
| 返回对象不符合协商协议模型 | Pydantic `ValidationError` | `failed_protocol` |
| 成功结果缺少或违反 output schema | `RuntimeError` | `failed_validation` |
| 工具自身/API/业务执行失败 | `CallToolResult.is_error == true` | `failed_tool`，保留脱敏 content 摘要 |
| 正常工具结果 | `CallToolResult.is_error == false` | 校验后 `succeeded` |

MCP 规范明确区分两类错误：协议错误使用标准 JSON-RPC error；工具执行错误放在正常 `tools/call` result 中并设置 `isError: true`。客户端还应对敏感操作请求用户确认、在调用前展示工具输入、验证工具结果、设置超时并记录审计日志。

来源：

- [MCP Tools 规范：Error handling 与 Security considerations](https://modelcontextprotocol.io/specification/2026-07-28/server/tools#error-handling)
- [MCP Python SDK v2.0.0 `ClientSession.send_request`](https://github.com/modelcontextprotocol/python-sdk/blob/v2.0.0/src/mcp/client/session.py#L507-L545)

## 8. 对首个 HTTP/SSE 红测试切片的约束

首个公共行为切片至少应覆盖：

1. 高风险 Tool Call 创建待审批 Tool Run 和一次性 Tool Approval，Research Run 进入 `waiting_approval`，SSE 可重放该事实
2. approve 只有在 run、user、参数 hash、过期、启用和取消校验全部通过时才消费 approval
3. reject、expired、disabled、cancelled 均不进入 MCP Adapter，并产生明确终态事件
4. 重复 approve、重复 resume、Worker 恢复只能观察到同一个 Tool Run 结果，Adapter 的稳定 invocation key 只执行一次
5. MCP `is_error`、`MCPError`、结果 schema 失败分别映射到不同 Tool Run 失败语义，研究流程按已确认的 partial failure 规则继续或降级
6. HTTP/SSE 只断言用户可见事实和副作用，不断言 LangGraph 内部节点数量、调用次数或类结构

真实外部 MCP 证据必须与本地 deterministic Adapter 证据分开报告。若远端不支持 idempotency key，本地测试通过也不能宣称任意远程工具已具备端到端 exactly-once。

## 9. 官方来源清单

- LangGraph 官方 Interrupts：<https://docs.langchain.com/oss/python/langgraph/interrupts>
- LangGraph 官方 Durable execution：<https://docs.langchain.com/oss/python/langgraph/durable-execution>
- LangGraph 官方 Functional API：<https://docs.langchain.com/oss/python/langgraph/functional-api>
- LangGraph `1.2.10` 源码：<https://github.com/langchain-ai/langgraph/tree/1.2.10>
- MCP Python SDK v2 文档：<https://py.sdk.modelcontextprotocol.io/>
- MCP Python SDK v2.0.0 源码：<https://github.com/modelcontextprotocol/python-sdk/tree/v2.0.0>
- MCP Python SDK v1 到 v2 迁移：<https://py.sdk.modelcontextprotocol.io/migration/>
- MCP Tools 规范：<https://modelcontextprotocol.io/specification/2026-07-28/server/tools>
- PyPI `mcp 2.0.0`：<https://pypi.org/project/mcp/2.0.0/>
