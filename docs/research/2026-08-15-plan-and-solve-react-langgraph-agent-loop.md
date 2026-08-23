# Plan-and-Solve、ReAct 与 LangGraph Agent Loop 能力边界调研

日期：2026-08-15

## 1. 调研问题与版本基线

本笔记回答：在当前依赖版本下，LangGraph 对动态计划、有界 ReAct 工具循环、`Send` / `Command`、`interrupt` / resume、checkpoint、结构化 Tool Calling 与并行任务提供了哪些可靠语义；哪些能力可以直接复用，哪些必须由本项目的业务 Module 自己承担。

仓库声明 `langgraph>=1.2,<2` 和 `langgraph-checkpoint-postgres>=3.1,<4`，锁文件中的实际版本是：

- `langgraph==1.2.10`
- `langgraph-prebuilt==1.1.0`
- `langgraph-checkpoint==4.1.1`
- `langgraph-checkpoint-postgres==3.1.1`
- `langchain-core==1.5.3`

截至调研日，LangGraph 最新正式版是 `1.2.11`。其发布说明相对 `1.2.10` 主要包含 trace policy、依赖升级和 checkpoint 修复，没有改变本文涉及的 Graph、`Send`、`Command`、ReAct loop 与 interrupt 公共模型。本文仍以仓库实际锁定的 `1.2.10` 标签源码为精确实现基线，并用当前官方文档核对公开语义。不能把最新文档中未来新增的便利接口自动视为本仓库已经可用。

来源：

- 本仓库 [`pyproject.toml`](../../pyproject.toml) 与 [`uv.lock`](../../uv.lock)
- [LangGraph `1.2.10` release](https://github.com/langchain-ai/langgraph/releases/tag/1.2.10)
- [LangGraph `1.2.11` release](https://github.com/langchain-ai/langgraph/releases/tag/1.2.11)

## 2. 结论摘要

1. 推荐的总体形态确实是 **Plan-and-Solve 外层 + 每个 Research Task 内部一个有界 ReAct Agent**，但两者都不是 LangGraph 自动提供的业务能力。LangGraph 提供执行图、动态 fan-out、循环、暂停和恢复；计划内容、依赖、预算、完成条件与重规划必须由项目定义
2. `StateGraph` 的节点和可达目标在 `compile()` 前确定。`Send` 可以在运行时按数据动态创建多个“对同一个已注册节点的调用”，`Command` 可以同时更新状态并路由到已存在节点；二者都不能替代持久化 Research Task DAG 或在运行时注册新节点
3. `langgraph.prebuilt.create_react_agent` 在 LangGraph v1 已弃用。官方替代是 `langchain.agents.create_agent`，但本仓库没有安装 `langchain`，当前 LiteLLM `ModelGateway` 也不是它要求的 LangChain chat model。不要把弃用的 prebuilt 直接写入新架构
4. 推荐复用 LangGraph 核心原语构建项目自己的 `ReActTaskModule` 子图：Model 决策 → 受控 Tool 调用 → Tool Observation → Model 再决策。Tool schema 解析和 `ToolMessage` 配对可借鉴或选择性复用 `ToolNode`，但工具授权、幂等、耐久 Job、Observation 落库和预算不能交给 `ToolNode`
5. `create_agent` / prebuilt loop 的默认停止条件只是“模型不再返回 tool calls”；Graph 的 `recursion_limit` 只是 super-step 上限。真实的有界执行必须另有 `max_model_turns`、`max_tool_calls`、token / cost / wall-clock 预算、取消检查、重复调用检测和任务完成条件
6. `Send` fan-out 在同一个 super-step 并行执行，写同一状态键时必须配置 reducer。Graph 级 `max_concurrency` 可以限制调度并发；直接把多条调用作为一个 `ToolNode` 输入时，其异步实现使用 `asyncio.gather`，内部没有独立并发上限。因此工具是否允许并行仍需项目策略决定
7. checkpoint 在每个 super-step 保存状态，并持久化已完成节点的 pending writes，能避免同一 super-step 中已成功的兄弟节点在恢复时重跑。但它不提供外部工具 exactly-once，也不替代 `ResearchRun`、`ResearchTask`、`ToolCall`、`ToolObservation`、Evidence 和 Artifact 等业务事实
8. `interrupt()` 能可靠暂停并以同一 `thread_id` 恢复，但恢复会从包含 interrupt 的节点开头重新执行。它适合作为“等待审批或外部耐久 Job”的运行时暂停原语，不是授权系统、Job Queue 或幂等协议
9. 结构化 Tool Calling 的框架合同是：模型产生带稳定 call ID 的 tool call，执行层返回对应 `ToolMessage`，再把 Observation 送回模型。模型供应商兼容、参数规范化、工具白名单、风险分级、结果大小与 Blob 引用仍属于项目的 `ModelGateway` / `ToolRegistry` / `ToolExecutionService`

## 3. Plan-and-Solve 与 ReAct 应如何组合

### 3.1 两个概念解决不同层级的问题

Plan-and-Solve 原论文的核心是先把整体问题拆成子任务，再按计划执行，以减少 Zero-shot CoT 的漏步骤问题。它是一种 prompting 方法，不定义持久化 DAG、Worker claim、失败恢复、重规划或任务预算。

ReAct 原论文的核心是把推理决策和动作 / 环境 Observation 交错执行，使模型可以根据外部信息更新后续动作。它也不定义生产系统所需的权限、审计、幂等、恢复和停止政策。

因此项目应采用两层结构：

```text
Global Planner / Replanner
  -> 产生有界、带依赖和完成条件的 Research Plan
  -> Task Scheduler 选择 ready tasks，并用 Send 有界 fan-out
       -> 每个 Research Task 运行独立的 ReActTaskModule
            Model Turn
              -> Tool Call(s)
              -> Tool Policy / durable Tool Execution
              -> persisted Tool Observation
              -> 下一次 Model Turn
            -> Task Result Proposal / Evidence Gap
  -> Coverage Verifier
       -> gap/conflict/failure: 受控重规划
       -> sufficient: Writer -> Citation Validator
```

这里的“ReAct”表示可审计的 **Model Decision → Tool Call → Observation** 循环，不要求保存或展示模型的私有思维链。需要持久化的是结构化决策、工具调用和 Observation，不是隐式 reasoning trace。

来源：

- [Plan-and-Solve Prompting 原论文](https://arxiv.org/abs/2305.04091)
- [Plan-and-Solve 官方代码](https://github.com/AGI-Edgerunners/Plan-and-Solve-Prompting)
- [ReAct 原论文](https://arxiv.org/abs/2210.03629)
- [ReAct 官方项目页](https://react-lm.github.io/)

### 3.2 为什么不是“每个 Graph 节点都是 Agent”

LangGraph 官方明确说明 node 只是函数，既可以包含模型，也可以是普通代码。计划校验、ready-task 选择、预算结算、权限判断、证据覆盖和最终提交需要确定性语义，不应伪装成自主 Agent。真正需要 ReAct 的边界是单个 Research Task 内部，因为这里需要模型根据最新 Observation 决定下一项工具动作。

来源：[LangGraph Graph API：Graphs、Nodes 与 Edges](https://docs.langchain.com/oss/python/langgraph/graph-api#graphs)

## 4. 动态计划、Send 与 Command

### 4.1 LangGraph 能可靠提供的能力

`StateGraph` 使用静态注册的 node 和 edge 构建后再编译。条件边可以根据当前 state 选择下一个已注册节点。`Send(node, state)` 允许条件路由在运行时返回任意数量的调用，每个调用可携带不同的局部 state，官方将其定位为 orchestrator-worker / map-reduce 的动态 fan-out。

`Command(update=..., goto=...)` 可以让一个 node 在同一返回值中更新 state 并选择下一个已注册节点；`goto` 也可以携带一个或多个 `Send`。这很适合表达：

- Planner 完成后进入 Scheduler
- Scheduler 为当前 ready tasks 发出有限 `Send`
- Task 结果汇合后进入 Verifier
- Verifier 发现 Evidence Gap 时返回 `Command(update=gap, goto="planner")`

来源：

- [LangGraph Graph API：conditional edges、Send 与 Command](https://docs.langchain.com/oss/python/langgraph/graph-api)
- [LangGraph 官方 orchestrator-worker 示例](https://docs.langchain.com/oss/python/langgraph/workflows-agents#orchestrator-worker)
- [LangGraph 1.2.10 `Send` / `Command` 源码](https://github.com/langchain-ai/langgraph/blob/1.2.10/libs/langgraph/langgraph/types.py#L664-L810)

### 4.2 LangGraph 不会替项目完成的部分

`Send` 创建的是下一 super-step 的运行时 task，不是本项目的 `ResearchTask` 业务记录。它不会自动处理：

- task ID、plan version、依赖边和 ready frontier
- task claim、lease、attempt、retry / backoff 和 dead-letter
- 每个 task 的目标、成功标准、allowed tools 与预算
- Evidence Gap 是否足以触发重规划
- 重规划后旧任务的取消、保留、替换与结果继承
- 并行任务提交结果时的业务幂等和冲突合并

运行中也不能用 `Send` 或 `Command` 注册一种全新的 node 类型；目标必须已存在于编译后的 Graph。动态的是数据和调用数量，不是代码拓扑。正确做法是保持拓扑固定，把 `ResearchPlan` 和 `ResearchTask` 当作业务数据，由 Scheduler 反复选择 ready tasks。

这是由 `StateGraph` 的 compile 模型以及 `Send` / `Command` 的目标为 node name 的合同直接得到的限制。

### 4.3 并行合并语义

LangGraph 以 Pregel 风格 super-step 执行；同一 super-step 的多个 node 可以并行。多个并行结果写同一 state channel 时，必须为该 channel 定义 reducer，否则更新无法安全合并。业务结果不要只靠 list append reducer 去重，应以稳定 `task_id + attempt` 在业务层幂等提交，Graph state 只保存结果 ID 或轻量引用。

Graph executor 会读取 `config["max_concurrency"]` 并用 semaphore 限制异步任务并发。本仓库已经传入这一配置，但它只是进程内 Graph 调度上限，不等同于 Workspace 配额、外部 API 并发、Sandbox 容量或数据库 Worker lease。

来源：

- [LangGraph Graph API：super-step 与 reducer](https://docs.langchain.com/oss/python/langgraph/graph-api#graphs)
- [LangGraph 1.2.10 async executor 的 `max_concurrency`](https://github.com/langchain-ai/langgraph/blob/1.2.10/libs/langgraph/langgraph/pregel/_executor.py#L131-L166)

## 5. 有界 ReAct 工具循环

### 5.1 官方预置循环的真实语义

锁定版本中的 `create_react_agent` 构造一个 Model node 和 Tool node 循环：最后一条 `AIMessage` 没有 tool calls 时结束；有 tool calls 时进入工具执行，再返回 Model。默认 v2 会把一条模型消息中的多条 tool calls 分别转换成 `Send("tools", ...)`。

但 LangGraph v1 已正式弃用 `langgraph.prebuilt.create_react_agent`，官方替代是 `langchain.agents.create_agent`。新 factory 仍运行在 LangGraph 上，并用 middleware 扩展 prompt、工具政策、重试、限制和 HITL。

来源：

- [LangGraph v1 migration：`create_react_agent` → `create_agent`](https://docs.langchain.com/oss/python/migrate/langgraph-v1#create_react_agent-create_agent)
- [LangGraph 1.2.10 prebuilt loop 源码](https://github.com/langchain-ai/langgraph/blob/1.2.10/libs/prebuilt/langgraph/prebuilt/chat_agent_executor.py#L830-L989)
- [LangChain Agents 官方文档](https://docs.langchain.com/oss/python/langchain/agents)

### 5.2 为什么当前仓库不能直接替换成 `create_agent`

本仓库只安装了 `langchain-core`，没有 `langchain` 包。当前 `ModelGateway` 是项目自定义 Protocol；真实实现直接调用 LiteLLM `acompletion()` 并返回文本流或 JSON object，不是 `create_agent` 要求的 `BaseChatModel` / LangChain model runnable，也没有“返回标准化 `AIMessage.tool_calls`”的接口。

因此有两个可行方向：

1. **推荐：自定义 `ReActTaskModule` 子图**。扩展现有 `ModelGateway`，返回项目自己的规范化 `ModelTurn`（text、tool calls、usage、finish reason）；用固定 StateGraph loop 和业务 ToolExecution seam 驱动。这样不会让 LangChain message state 成为业务事实，也容易插入预算、取消和 durable Sandbox Job
2. 引入 `langchain` 并编写 LiteLLM-compatible `BaseChatModel` adapter，再把 `create_agent` 当子图。它能复用更多 middleware，但增加依赖和消息模型耦合，且仍不能替代业务 Tool / Observation / Task Controller

无论选择哪条，都不要在新实现中依赖已弃用的 `langgraph.prebuilt.create_react_agent`。可以把其 `1.2.10` 源码作为 loop 行为参考或 throwaway prototype 基线。

### 5.3 “有界”不能只靠 recursion limit

LangGraph `recursion_limit` 限制的是一次 Graph 执行最多经历多少个 super-steps，达到上限会抛 `GraphRecursionError`；它不是工具调用预算，也不表达业务上的任务完成。当前 Python 官方文档说明 1.0.6 起默认值为 1000，更不能依赖默认值约束成本。

prebuilt 的 `remaining_steps` 只在接近上限且模型仍请求工具时返回兜底消息。生产任务仍需 Task Controller 明确检查：

- `max_model_turns`
- `max_tool_calls`，以及每种工具的独立上限
- input / output token、金额和 wall-clock deadline
- 连续相同 tool + canonical args 的重复检测
- Tool Observation 总上下文大小
- run / task cancellation
- 任务成功标准是否满足
- 失败、拒绝、超时和预算耗尽时应形成何种 `TaskResult`

建议显式设置一个比业务 turn 上限略高的 `recursion_limit` 作为最后保险；正常停止必须由 Task Controller 形成可解释结果，不能把 `GraphRecursionError` 当成功路径。

来源：

- [LangGraph Graph API：recursion limit](https://docs.langchain.com/oss/python/langgraph/graph-api#recursion-limit)
- [LangGraph 1.2.10 `GraphRecursionError`](https://github.com/langchain-ai/langgraph/blob/1.2.10/libs/langgraph/langgraph/errors.py#L67-L87)
- [LangGraph 1.2.10 prebuilt `remaining_steps` 判断](https://github.com/langchain-ai/langgraph/blob/1.2.10/libs/prebuilt/langgraph/prebuilt/chat_agent_executor.py#L620-L719)

## 6. 结构化 Tool Calling 与 ToolNode

### 6.1 可复用的标准消息协议

LangGraph / LangChain prebuilt 的可靠合同是：

1. Model 返回 `AIMessage.tool_calls[]`，每项至少含 call ID、tool name 和结构化 args
2. Tool executor 对每个 call 产生带同一 `tool_call_id` 的 `ToolMessage`
3. Message reducer 把 Model turn 和 Tool Observation 纳入该 Agent 的局部历史
4. Model 在下一 turn 读取 Observation，决定继续调用工具或结束

Prebuilt 会校验每个 AI tool call 都有对应 ToolMessage。可选 `response_format` 是 loop 结束后的结构化最终输出，而且 prebuilt 会再调用一次模型；它不应与中间 tool-call schema 混为一谈。

来源：

- [LangGraph 1.2.10 chat history 校验与 structured response](https://github.com/langchain-ai/langgraph/blob/1.2.10/libs/prebuilt/langgraph/prebuilt/chat_agent_executor.py#L242-L394)
- [LangChain structured output 官方文档](https://docs.langchain.com/oss/python/langchain/structured-output)

### 6.2 ToolNode 可以复用到什么程度

`ToolNode` 已提供工具 schema 封装、tool name 路由、参数校验、`ToolMessage` 构造、错误转消息、state / store / runtime 注入和 `Command` 工具结果。它适合做 ReAct 子图中的协议适配层。

但项目工具不能把真正副作用直接写成任意普通函数后交给默认 ToolNode。每个 Tool implementation 必须薄薄地委托到项目 `ToolRegistry` / `ToolExecutionService`，由业务层完成授权、幂等、风险、Job 创建、Observation 持久化与结果引用。

ToolNode 的异步批量路径对输入中的所有 tool calls 使用 `asyncio.gather()`，没有内部独立 semaphore；prebuilt v2 则把每条 call 变成独立 `Send`，可受 Graph `max_concurrency` 约束。项目仍应根据工具元数据决定：只允许单调用、可安全并行，还是必须串行；不能把模型一次返回多条调用等同于“允许并行”。

来源：

- [LangGraph 1.2.10 `ToolNode` 合同](https://github.com/langchain-ai/langgraph/blob/1.2.10/libs/prebuilt/langgraph/prebuilt/tool_node.py#L622-L739)
- [LangGraph 1.2.10 `ToolNode._afunc()` 并行实现](https://github.com/langchain-ai/langgraph/blob/1.2.10/libs/prebuilt/langgraph/prebuilt/tool_node.py#L828-L860)
- [LangGraph 1.2.10 prebuilt v2 per-call `Send`](https://github.com/langchain-ai/langgraph/blob/1.2.10/libs/prebuilt/langgraph/prebuilt/chat_agent_executor.py#L830-L859)

### 6.3 项目需要新增的 ModelGateway 合同

当前 `acomplete_structured()` 只支持一次性 JSON schema 输出；Writer 支持文本 streaming，但都没有标准化 tool calls。后续接口票至少要决定：

- `acomplete_turn(context, tools, tool_choice, ...) -> ModelTurn`
- `ModelTurn.tool_calls[]` 的稳定 call ID、name、canonical args
- provider 不支持并行 tool calls、strict schema 或 streaming tool args 时的归一化行为
- usage、finish reason、provider request ID 和可重试错误
- Tool Observation 如何裁剪为模型可读内容，以及完整正文如何通过稳定对象 ID 外置

这属于项目 Adapter 合同，不是 LangGraph state 自动推导出来的能力。

## 7. interrupt、resume 与 checkpoint

### 7.1 interrupt 的可靠语义

`interrupt(value)` 要求 graph 使用 checkpointer，并以稳定 `thread_id` 调用。它保存当前执行状态，外部随后以同一个 thread ID 和 `Command(resume=...)` 恢复。并行分支产生多个 interrupt 时，可以按每个 interrupt ID 提交 resume map。

关键限制是：恢复会从包含 `interrupt()` 的 node 开头重跑，interrupt 前的代码也会再次执行。因此所有 interrupt 前副作用必须幂等；真正工具调用若可能在返回前崩溃，也必须有业务 idempotency key 或“结果不确定”状态。

来源：

- [LangGraph Interrupts 官方文档](https://docs.langchain.com/oss/python/langgraph/interrupts)
- [LangGraph 1.2.10 `interrupt()` 源码](https://github.com/langchain-ai/langgraph/blob/1.2.10/libs/langgraph/langgraph/types.py#L811-L934)

### 7.2 对异步 Sandbox Tool 的含义

可以复用 interrupt 表达“Agent 当前等待外部结果”，但必须由业务 Sandbox Job 提供真实耐久性：

1. Tool call 幂等创建 `SandboxJob`，写入稳定 job ID
2. Graph 只保存 call / job / observation ID，然后暂停或结束当前 Worker 占用
3. 专用 Sandbox Worker claim job、执行、心跳并持久化结果
4. 完成事件把 Research Run 重新入队
5. Research Worker 以同一 thread ID 恢复，重新读取 Job 业务事实

resume payload 不是 Sandbox 结果事实，interrupt 也不会自动唤醒 Worker、执行容器、续租或去重。究竟使用 interrupt 还是“node 返回 waiting 状态、下一次重新 invoke”应由后续 Sandbox / Task Controller 接口票决定；无论哪种方式，Job 表才是事实源。

### 7.3 checkpoint 的恢复边界

官方 checkpointer 在每个 super-step 边界保存完整 state snapshot，并在 node / task 完成时记录 pending writes。同一 super-step 中某个并行 node 失败后，恢复可以复用其他已成功 node 的 writes，而不重跑成功兄弟。

这提供的是 Graph 计算恢复，不是业务事务：

- 外部 API / Sandbox / MCP side effect 不获得 exactly-once
- checkpoint 不负责 Workspace ACL、最终 Run 状态和用户可见审计
- replay / time travel 会重新执行目标 checkpoint 之后的 LLM 与外部请求
- checkpoint state 应只存稳定 ID、有限消息和结构化引用，不能塞入 secret、大型文档、完整 Artifact 或长期 Memory
- checkpoint 需要 retention；官方文档明确提醒长期运行会无限增长

来源：

- [LangGraph Checkpointers：super-step、pending writes 与 fault tolerance](https://docs.langchain.com/oss/python/langgraph/checkpointers)
- [LangGraph Persistence：checkpointer 与 Store 的边界](https://docs.langchain.com/oss/python/langgraph/persistence)
- [LangGraph Time Travel：checkpoint 后节点会重新执行](https://docs.langchain.com/oss/python/langgraph/use-time-travel)

## 8. 对当前实现的差距判断

当前实现已经正确复用了几项核心基础：

- Flat `StateGraph`，而非自由递归 Supervisor
- `Send` 用于有限 Research Brief fan-out
- `thread_id=run:{run_id}` 和 PostgreSQL checkpointer
- Graph `max_concurrency`
- `interrupt()` 用于高风险工具审批恢复
- `research_results` / `map_results` 使用 reducer 汇合并行结果

但它还不是目标中的 Plan-and-Solve + ReAct：

- [`agents/planner.py`](../../apps/api/src/deep_researcher/agents/planner.py) 是规则函数，固定生成 researcher / verifier / writer 和两个 brief，不生成带依赖、成功标准或 plan version 的动态计划
- [`agents/researcher.py`](../../apps/api/src/deep_researcher/agents/researcher.py) 默认 Researcher 只返回“已准备研究分支”和已有 source IDs，不调用模型，也不根据 Observation 决定工具
- [`graph.py`](../../apps/api/src/deep_researcher/graph.py) 的工具处理发生在 researcher fan-out 前，最多是一条审批工具路径，不存在每个 Research Task 内的 Model ↔ Tool 循环
- [`model_gateway.py`](../../apps/api/src/deep_researcher/model_gateway.py) 能流式写答案和一次性生成 structured JSON，但没有规范化 Tool Calling turn
- 当前环境没有 `langchain`，所以官方新 `create_agent` 不可直接导入

这意味着后续不是简单地“在现有 researcher 中再加一次模型调用”，而是要确定一组新的深模块接口：Research Plan、Task Scheduler、ReAct Task Controller、Model Turn、Tool Registry、Tool Observation 和任务结果裁决。

## 9. 直接复用与业务自持矩阵

| 能力 | LangGraph / LangChain 可复用部分 | 必须由本项目业务 Module 承担 |
| --- | --- | --- |
| 总体拓扑 | `StateGraph`、固定 node、条件 edge、loop | Planner / Researcher / Verifier / Writer 的职责与接口 |
| 动态任务 | `Send` 对已注册 worker 做运行时 fan-out | Research Plan、task ID、依赖 DAG、ready frontier、plan version、重规划提交 |
| 动态路由 | `Command(update, goto)` | 哪些 gap / conflict / failure 允许重规划及其预算 |
| ReAct loop | Model → tools → Model 的 Graph 模式；可参考 `create_agent` | `ReActTaskModule`、Task Result Proposal、确定性完成裁决 |
| Tool Calling | 标准 tool schema、AI call ID、`ToolMessage`、可选择性复用 `ToolNode` | Tool Registry、allowlist、风险、参数规范化、幂等、审计、异步 Job |
| 并行 | super-step、`Send`、reducer、`max_concurrency` | 每类工具可否并行、Workspace / provider / Sandbox 配额、公平调度 |
| 停止 | conditional edge、`remaining_steps`、`recursion_limit` 最后保险 | turn / call / token / cost / time 预算、重复检测、取消、成功标准 |
| 暂停恢复 | checkpointer、`interrupt()`、`Command(resume)` | 审批事实、Job 状态、唤醒入队、恢复授权与幂等 |
| 故障恢复 | super-step checkpoint、pending writes | 外部 side effect exactly-once、Tool Observation / TaskResult 业务提交 |
| 状态与记忆 | Graph state、thread-scoped checkpoint；可选 Store | `ResearchRun`、Task、Evidence、Citation、Memory、Artifact、ACL 的事实源 |
| 结构化输出 | model structured output / agent `response_format` | schema 版本、业务校验、非法输出修复、供应商差异 |

## 10. 推荐给后续决策票的约束

1. 顶层继续使用 Flat `StateGraph`，不要引入自由递归 Supervisor
2. 把计划建模为持久化业务数据；Graph Scheduler 只读取 plan 并用 `Send` 调度当前 ready tasks
3. 每个 Research Task 运行隔离的 ReAct loop；不共享可变 scratchpad，只读取稳定 Evidence / Observation / Artifact 引用
4. 首选自定义 `ReActTaskModule` 子图并延续项目 `ModelGateway`；只有在 prototype 证明 middleware 收益明显且 LiteLLM adapter 边界清晰时，才引入 `langchain.agents.create_agent`
5. 不使用已弃用的 `langgraph.prebuilt.create_react_agent` 作为生产新依赖
6. `ToolNode` 若复用，只做协议适配；真实执行必须穿过项目 Tool policy 和 durable execution seam
7. 每个 Tool Call、Tool Observation、Task Result 都先落业务事实，再把稳定 ID 写入 Graph state
8. 显式定义任务预算和停止原因；`recursion_limit` 仅作无法到达正常终止时的保险
9. Planner 只能创建满足全局上限的任务；ReAct Subagent 只能提交 Evidence Gap / follow-up proposal，不能自行递归启动 Agent
10. Verifier 决定是否需要受控重规划；模型只能提出完成建议，Task Controller 负责可验证的终态
11. 对模型一次返回的多条 tool calls 默认不要无条件并行；根据 tool metadata 和预算确定串行或有限并行
12. Graph checkpoint 保持内部恢复用途，不能成为 SSE、ACL、长期 Memory、Evidence、Artifact 或最终 Run 状态的事实源

## 11. 一手来源索引

### 论文与概念原点

- Wang et al., [Plan-and-Solve Prompting](https://arxiv.org/abs/2305.04091)
- Yao et al., [ReAct](https://arxiv.org/abs/2210.03629)

### 当前官方文档

- [LangGraph Graph API](https://docs.langchain.com/oss/python/langgraph/graph-api)
- [LangGraph Workflows and Agents](https://docs.langchain.com/oss/python/langgraph/workflows-agents)
- [LangGraph Checkpointers](https://docs.langchain.com/oss/python/langgraph/checkpointers)
- [LangGraph Persistence](https://docs.langchain.com/oss/python/langgraph/persistence)
- [LangGraph Interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts)
- [LangGraph Time Travel](https://docs.langchain.com/oss/python/langgraph/use-time-travel)
- [LangGraph v1 migration](https://docs.langchain.com/oss/python/migrate/langgraph-v1)
- [LangChain Agents](https://docs.langchain.com/oss/python/langchain/agents)
- [LangChain Tools](https://docs.langchain.com/oss/python/langchain/tools)
- [LangChain Structured Output](https://docs.langchain.com/oss/python/langchain/structured-output)

### 锁定版本官方源码

- [`Send`、`Command`、`interrupt`](https://github.com/langchain-ai/langgraph/blob/1.2.10/libs/langgraph/langgraph/types.py)
- [prebuilt ReAct agent loop](https://github.com/langchain-ai/langgraph/blob/1.2.10/libs/prebuilt/langgraph/prebuilt/chat_agent_executor.py)
- [`ToolNode`](https://github.com/langchain-ai/langgraph/blob/1.2.10/libs/prebuilt/langgraph/prebuilt/tool_node.py)
- [async executor 与 `max_concurrency`](https://github.com/langchain-ai/langgraph/blob/1.2.10/libs/langgraph/langgraph/pregel/_executor.py)
- [Graph errors 与 recursion limit](https://github.com/langchain-ai/langgraph/blob/1.2.10/libs/langgraph/langgraph/errors.py)
