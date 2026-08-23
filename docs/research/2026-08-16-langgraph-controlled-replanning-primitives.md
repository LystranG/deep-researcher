# LangGraph 受控重规划原语补充调研

日期：2026-08-16

## 1. 范围与版本基线

本文是 [`2026-08-15-plan-and-solve-react-langgraph-agent-loop.md`](./2026-08-15-plan-and-solve-react-langgraph-agent-loop.md) 的增量补充，不重复其中已经确认的结论：Graph 拓扑在单次运行内是固定的，`Send` 只动态产生对已注册 node 的调用，checkpoint 不提供外部副作用 exactly-once，计划版本、依赖、Evidence Gap、证据冲突和重规划预算都属于业务协议。

仓库锁定 `langgraph==1.2.10`；截至 2026-08-16，官方最新正式版仍是 `1.2.11`，其相对 `1.2.10` 的发布说明没有改变本文涉及的控制流与恢复模型。因此本文以 `1.2.10` 源码核对可用 API，并用当前官方文档核对语义。

来源：

- 本仓库 [`pyproject.toml`](../../pyproject.toml) 与 [`uv.lock`](../../uv.lock)
- [LangGraph 1.2.11 release](https://github.com/langchain-ai/langgraph/releases/tag/1.2.11)

## 2. 增量结论

1. `RetryPolicy` 与 node-level `error_handler` 可以把“节点抛异常且重试耗尽”规范化为一次 Graph 内路由；`error_handler` 可以读取 `NodeError(node, error)` 并返回 `Command`。这是失败进入 Verifier 的合适框架钩子，但它不会替业务定义失败分类、计划版本或是否值得重规划
2. Evidence Gap 与证据冲突不应伪装成异常。它们是成功完成一次研究/验证后得出的领域结果，应作为结构化 `TaskOutcome` / `VerificationOutcome` 返回。只有真正的执行异常走 retry / error handler
3. Graph node 和 `@task` 的重放粒度不同：恢复时受影响的 node 从函数开头重跑；node 内已经完成的 `@task` 结果可以从 checkpointer 恢复而跳过执行，但未完成的 task 仍可能重跑。因此 LLM、搜索、Sandbox 与业务写入仍要有稳定幂等键
4. 当前官方文档明确说明，已有 thread 恢复时执行的是**当前重新编译的 Graph**，LangGraph 不替应用固定 graph revision。运行中的业务计划必须独立带 `plan_version` / `graph_contract_version`；部署兼容性不能依赖 checkpoint 暗中固定旧拓扑
5. `Command` 与普通 edge 会叠加，不是覆盖关系。Verifier 若用 `Command` 在 Writer 和 Replanner 之间二选一，就不能再从同一 node 配普通 edge，否则两条路径都可能执行
6. `recursion_limit` 只是 Pregel super-step 的最后保险，`max_concurrency` 只是当前 Graph executor 的调度上限；二者都不是 `max_replans`、计划宽度、工具并发、供应商限流或全局 Worker 配额
7. `durability="sync"` 可保证下一 super-step 启动前 checkpoint 已写入；默认 `"async"` 仍有进程崩溃导致最近 checkpoint 未落盘的小窗口。两者都不能把 checkpoint 与业务数据库提交变成同一事务
8. `interrupt()` 只适合确实需要外部输入的暂停点，例如人工批准一个已持久化的重规划候选；纯自动的 failure / gap / conflict 路由应使用 conditional edge 或 `Command`，不应制造无人的 interrupt

## 3. 原语如何拼成“仅三类触发”的控制面

建议顶层保持固定控制流，把动态性全部放进版本化计划数据：

```text
Scheduler
  -> bounded Send(research_task, task_ref)
  -> Gather durable TaskOutcome refs
  -> Verifier
       SUCCESS / sufficient ----------------------> Writer
       FAILURE / EVIDENCE_GAP / EVIDENCE_CONFLICT -> ReplanGate
  -> ReplanGate
       budget + policy allow -> Replanner -> ValidateAndCommitPlan -> Scheduler
       budget/policy deny    -> terminal partial/failure outcome
```

### 3.1 Scheduler：`Send` 只负责有限 fan-out

`Send(node, arg, timeout=...)` 在 `1.2.10` 中可以为每个动态调用传不同 state，并可带 per-dispatch timeout。它适合把已经由业务 Scheduler 判定为 ready 的 task refs 发到统一 `research_task` node；它不会计算 DAG frontier，也没有内建 fan-out 数量上限。

因此 Scheduler 在返回 `Send[]` 前必须从业务计划读取并验证：

- `plan_id`、`plan_version` 与 task 的稳定 ID
- 所有依赖是否处于允许的终态
- 本批次最大 task 数、run 级预算和工具/provider 配额
- 已完成或正在运行的 `task_id + attempt` 是否需要去重

Graph state 只携带稳定引用。Worker 只能提交不可变 outcome / follow-up proposal，不能直接改共享计划，也不能自行 `goto="replanner"`。

来源：

- [Graph API：`Send`](https://docs.langchain.com/oss/python/langgraph/graph-api#send)
- [LangGraph 1.2.10 `Send` 源码](https://github.com/langchain-ai/langgraph/blob/1.2.10/libs/langgraph/langgraph/types.py#L664-L751)

### 3.2 Verifier：用一个 `Command` 同时记录裁决并路由

Verifier 应输出封闭、可审计的领域裁决，而不是自由文本布尔值：

```text
SUFFICIENT
FAILURE
EVIDENCE_GAP
EVIDENCE_CONFLICT
```

当 node 既要写入 `verification_outcome_ref` / `replan_trigger`，又要在 Writer 与 ReplanGate 之间路由时，`Command(update=..., goto=...)` 比 conditional edge 更直接；如果 router 只读状态、不更新状态，则使用 conditional edge 即可。

必须为 Verifier 选择**一种**出边机制。官方 Graph API 明确警告：同一 node 同时存在普通 edge 和 dynamic routing 时，两种路径都会生效。`Command` 的类型标注只帮助图渲染和静态可见性，不会验证业务触发是否合法。

来源：

- [Graph API：Edges 的单一路由机制警告](https://docs.langchain.com/oss/python/langgraph/graph-api#edges)
- [Graph API：`Command`](https://docs.langchain.com/oss/python/langgraph/graph-api#command)
- [LangGraph 1.2.10 `Command` 源码](https://github.com/langchain-ai/langgraph/blob/1.2.10/libs/langgraph/langgraph/types.py#L758-L810)

### 3.3 ReplanGate：业务协议承担真正的限制

LangGraph 没有“重规划”类型。ReplanGate 必须确定性检查：

- trigger 必须精确属于 `FAILURE | EVIDENCE_GAP | EVIDENCE_CONFLICT`
- `replan_count < max_replans`，且未超过 token、cost、wall-clock 与 task-count 预算
- 同一 `trigger_fingerprint` 没有产生过等价 plan，避免原地循环
- 新计划只能新增/替换允许的 task；已接受 Evidence 的 provenance 不得静默改写
- 新 plan 通过 schema、DAG 无环、依赖存在、工具 allowlist 和预算校验后，才原子提交新的 `plan_version`
- commit 使用 `(run_id, expected_plan_version)` 乐观并发控制，恢复或并发 Verifier 不能重复推进版本

`recursion_limit` 仍应设置成高于业务正常上限的保护值，但不能代替 `max_replans`。达到 `GraphRecursionError` 表示控制面没有在预期条件终止，应被当作故障而非一次合法的“预算耗尽”结果。

来源：

- [Graph API：recursion limit](https://docs.langchain.com/oss/python/langgraph/graph-api#recursion-limit)
- [LangGraph 1.2.10 super-step limit 与 `GraphRecursionError`](https://github.com/langchain-ai/langgraph/blob/1.2.10/libs/langgraph/langgraph/pregel/main.py#L2997-L3012)

## 4. 失败、重试与重规划不是同一个概念

### 4.1 `RetryPolicy` 先处理可重试的执行异常

`RetryPolicy` 支持最大尝试次数、指数退避、jitter 和按异常类型/谓词决定是否重试。`max_attempts` 包含第一次执行。默认 `retry_on` 有自己的异常分类，不能直接等同于本项目的 provider / tool retry policy；项目应显式映射 timeout、429、5xx、授权拒绝、参数错误与取消。

只有异常仍未被处理且 retry 耗尽时，node-level `error_handler` 才运行。`error_handler` 获得 `NodeError.node` 和原始异常，可以返回 `Command`，把失败规范化为持久化 `TaskOutcome(kind=FAILURE, ...)` 的引用并回到 Gather / Verifier。

这里的边界是：

- retry：同一 task attempt 内对暂态执行故障的技术恢复
- failure outcome：retry 耗尽后的业务事实
- replan：Verifier / ReplanGate 评估 failure outcome 后做出的新计划决策

不要在 error handler 中直接生成新计划，否则异常处理、预算策略和计划版本提交会耦合在一个不易审计的路径里。

来源：

- [Fault tolerance：retry、timeout、error handler 的固定顺序](https://docs.langchain.com/oss/python/langgraph/fault-tolerance)
- [LangGraph 1.2.10 `RetryPolicy` 源码](https://github.com/langchain-ai/langgraph/blob/1.2.10/libs/langgraph/langgraph/types.py#L416-L435)
- [LangGraph 1.2.10 `NodeError` 源码](https://github.com/langchain-ai/langgraph/blob/1.2.10/libs/langgraph/langgraph/errors.py#L148-L166)
- [LangGraph 1.2.10 `StateGraph.add_node(error_handler=...)`](https://github.com/langchain-ai/langgraph/blob/1.2.10/libs/langgraph/langgraph/graph/state.py#L662-L716)

### 4.2 `interrupt()` 不进入 retry / error handler

官方 fault-tolerance 文档明确指出，`interrupt()` 使用 `GraphBubbleUp` 暂停运行，绕过 retry policy 与 error handler。它不是一种“特殊失败”，也不能用来自动触发 Replanner。

如果未来要求人工批准重规划，正确顺序是：先幂等生成并持久化 plan candidate，再 `interrupt()` 暴露 candidate ID；恢复后重新读取审批事实并进行 compare-and-swap commit。因为恢复从包含 interrupt 的 node 开头执行，interrupt 前写入必须可重复。

来源：

- [Fault tolerance：Behavior with `interrupt()`](https://docs.langchain.com/oss/python/langgraph/fault-tolerance#behavior-with-interrupt)
- [Interrupts：恢复与幂等规则](https://docs.langchain.com/oss/python/langgraph/interrupts)

## 5. Durable execution 与 task replay 的精确边界

### 5.1 Graph node 会重跑；完成的 `@task` 才能在 node 内跳过

当前 Graph API 文档区分了两层恢复：

- checkpoint 保存于 super-step 边界；受影响的 Graph node 恢复时从函数开头运行
- node 内若调用 `@task`，已完成 task 的结果可从 checkpointer 恢复，从而跳过该段工作
- 已经开始但未完成的 task 仍可能在恢复时重新执行

这意味着“把整段 Research Task 写成一个 node”并不会自动得到细粒度 replay。若一个 node 内依次做 LLM、搜索、抓取和落库，可以把每个耐久操作拆成 node 或 `@task`；无论哪种方式，外部调用仍需要 `(run_id, task_id, attempt, operation_id)` 幂等键以及可查询的结果事实。

task replay 还要求稳定的调用顺序。恢复前若改变 node 内 `@task` / `interrupt()` 的先后，已缓存结果或 resume value 可能与新的调用错配。因此部署不能把 checkpoint 当作代码版本兼容层。

来源：

- [Graph API：Re-execution and idempotency / Using tasks in nodes](https://docs.langchain.com/oss/python/langgraph/graph-api#re-execution-and-idempotency)
- [Functional API：Determinism 与 Idempotency](https://docs.langchain.com/oss/python/langgraph/functional-api#determinism)

### 5.2 pending writes 只避免成功兄弟 node 重跑

同一 super-step 中并行 node 的输出会以 task writes 写入 checkpointer。如果一个 node 失败，其他已成功 node 的 pending writes 会在恢复时复用，因此不必重跑成功兄弟。

这不会提供以下保证：

- 失败 node 内已经发出的外部请求不会重复
- 成功写入业务数据库但尚未返回的 node 不会再次执行
- 多个 worker 对同一个 plan/task 的业务提交自动去重
- checkpoint state 与业务数据库在同一个原子事务提交

因此 Gather / Verifier 应读取业务表中的稳定 outcome refs，而不是把 reducer 里的内存 list 当成计划完成事实。

来源：[Checkpointers：super-step、task writes 与 pending writes](https://docs.langchain.com/oss/python/langgraph/checkpointers#super-steps)

### 5.3 durability mode 是 checkpoint 风险选择，不是业务一致性协议

`1.2.10` 支持：

| 模式 | 官方语义 | 对受控重规划的含义 |
| --- | --- | --- |
| `sync` | 下一步开始前同步持久化 | 最小化 Graph 内部 plan transition 的恢复窗口，但增加延迟 |
| `async` | 下一步执行时异步持久化，且为默认值 | 性能较好；进程在窗口内崩溃时最近 checkpoint 可能丢失 |
| `exit` | 仅退出时持久化 | 长运行 Research Run 不应依赖它进行中途恢复 |

即使选择 `sync`，新 `plan_version` 仍必须先以业务事务幂等提交；checkpoint 丢失或重放时，根据 stable ID 重新读取并识别已经提交的版本。

来源：

- [Checkpointers：Durability modes](https://docs.langchain.com/oss/python/langgraph/checkpointers#durability-modes)
- [LangGraph 1.2.10 默认 `async` 与三种模式](https://github.com/langchain-ai/langgraph/blob/1.2.10/libs/langgraph/langgraph/pregel/main.py#L2602-L2603)

### 5.4 time travel / fork 不是生产重规划

从旧 checkpoint replay 或 fork 会重新执行该点之后的 node，包括 LLM 和 API 请求。它适合调试或显式创建替代轨迹，但不是业务 Replanner：它不自动解释为何重规划、扣减预算、废止旧 task、继承 Evidence 或产生新 `plan_version`。

另外，当前官方 Graph API 明确说明 thread 恢复使用当前编译的 Graph。应用若在部署后改变 node/edge，旧 checkpoint 会沿新 Graph 继续；LangGraph 不提供业务 `graph_contract_version` 固定和迁移策略。因此 Research Run 至少应保存兼容性版本，并在 Worker claim 时拒绝或迁移不兼容的运行。

来源：

- [Time travel：replay 与 fork](https://docs.langchain.com/oss/python/langgraph/use-time-travel)
- [Graph API：Graph changes on resume](https://docs.langchain.com/oss/python/langgraph/graph-api#re-execution-and-idempotency)

## 6. 并行与递归限制

`max_concurrency` 在 `1.2.10` 的 async executor 中通过 semaphore 限制当前 Graph 调用所提交的异步任务。它适用于 `Send` 形成的并行 Graph tasks，但不认识以下业务约束：

- Workspace / tenant 并发与公平性
- 同一 provider、MCP server 或 Sandbox pool 的独立配额
- 一个 Research Plan 最多允许多少 active tasks
- task 内部工具实现自行创建的协程、线程或远端 jobs
- 重规划最多增加多少 task

`Send[]` 的长度也没有内建业务上限。Scheduler 必须先截断到 plan policy 允许的 batch，再返回 `Send`。对于 task 内递归，继续采用固定 node loop + 显式 turn/tool/replan budget；禁止 Researcher 自行创建任意层级子 Agent。

来源：

- [LangGraph 1.2.10 async executor 的 `max_concurrency` semaphore](https://github.com/langchain-ai/langgraph/blob/1.2.10/libs/langgraph/langgraph/pregel/_executor.py#L131-L166)
- [LangGraph 1.2.10 `max_concurrency` 配置说明](https://github.com/langchain-ai/langgraph/blob/1.2.10/libs/langgraph/langgraph/_internal/_config.py#L194-L233)

## 7. 能力边界与最小业务协议

| 需求 | LangGraph 可直接提供 | 业务层必须提供 |
| --- | --- | --- |
| 动态执行计划 | `Send` 动态调用既有 worker；conditional edge / `Command` 路由 | 计划 schema、DAG、ready frontier、plan version、任务替换/继承规则 |
| 失败进入裁决 | `RetryPolicy`、timeout、`error_handler(NodeError)` | 异常分类、稳定 failure outcome、retry 与 replan 的分界 |
| Gap / conflict 重规划 | `Command` 跳转 ReplanGate | Evidence coverage、冲突判定、trigger fingerprint、是否值得重规划 |
| 有界重规划 | `recursion_limit` 仅作最后保险 | `max_replans`、新增 task 上限、token/cost/time 预算、去循环 |
| 并行研究 | `Send`、reducer、pending writes、`max_concurrency` | 业务 fan-out 上限、配额、公平调度、outcome 幂等合并 |
| 恢复 | checkpoint、task replay、interrupt/resume、durability mode | 外部副作用幂等、业务事实事务、唤醒/claim、代码/图契约版本 |
| 计划审批 | `interrupt()` / `Command(resume=...)` | candidate 与 approval 事实、ACL、过期/撤销、CAS commit |

建议最小计划协议至少含：

```text
ResearchPlan(plan_id, run_id, version, parent_version, status, created_by_trigger)
ResearchTask(task_id, plan_version, dependencies, goal, success_criteria, allowed_tools, budget)
TaskOutcome(task_id, attempt, kind, evidence_refs, failure_ref, proposal_refs)
VerificationOutcome(kind, evidence_gap_refs, conflict_refs, trigger_fingerprint)
ReplanPolicy(max_replans, max_total_tasks, max_cost, deadline, duplicate_trigger_policy)
```

最终边界可以概括为：**LangGraph 负责可恢复地执行“是否去 Replanner”的控制流；业务计划协议负责证明“为什么可以去、还能去几次、旧计划如何演进、结果如何幂等”。**

## 8. 一手来源索引

- [LangGraph Graph API](https://docs.langchain.com/oss/python/langgraph/graph-api)
- [LangGraph Fault tolerance](https://docs.langchain.com/oss/python/langgraph/fault-tolerance)
- [LangGraph Checkpointers](https://docs.langchain.com/oss/python/langgraph/checkpointers)
- [LangGraph Functional API](https://docs.langchain.com/oss/python/langgraph/functional-api)
- [LangGraph Interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts)
- [LangGraph Time travel](https://docs.langchain.com/oss/python/langgraph/use-time-travel)
- [LangGraph 1.2.10 `StateGraph` 源码](https://github.com/langchain-ai/langgraph/blob/1.2.10/libs/langgraph/langgraph/graph/state.py)
- [LangGraph 1.2.10 control types 源码](https://github.com/langchain-ai/langgraph/blob/1.2.10/libs/langgraph/langgraph/types.py)
- [LangGraph 1.2.10 Pregel runtime 源码](https://github.com/langchain-ai/langgraph/tree/1.2.10/libs/langgraph/langgraph/pregel)
