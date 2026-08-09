# LangGraph checkpoint 恢复与 Docker Python Sandbox 官方资料

日期：2026-08-08

本笔记只记录 LangGraph 和 Docker 的官方一手资料，并把结论映射到“Todo 驱动的 Agent Python Sandbox”第一版。引用链接均指向项目维护方的文档或源码；本笔记不把本机 Docker 的一次测试当作生产隔离证明。

## 1. 结论摘要

1. LangGraph `interrupt()` 必须配合持久化 checkpointer 和稳定的 `configurable.thread_id`。恢复时再次调用同一线程，并传入 `Command(resume=value)`；`value` 才会成为 `interrupt()` 的返回值。
2. 恢复会从包含 `interrupt()` 的节点开头重放。中断前的数据库写入、网络请求和其他副作用可能再次执行；这些副作用必须幂等，或拆到中断之后的独立节点。
3. Checkpoint 只保证图状态和节点 writes 的持久化/恢复，不保证 Sandbox 容器副作用 exactly-once。Todo、Tool Run、Sandbox Execution 仍需稳定幂等键、CAS 领取和结果核对。
4. `--network none` 只保留容器内 loopback；`--read-only` 将根文件系统设为只读；`--user` 以指定非 root UID/GID 运行；`--cap-drop=ALL` 与 `no-new-privileges` 降低权限提升面。需要写入时只能显式挂载 tmpfs 或独立输出目录。
5. Docker 的 `--memory`、`--cpus`、`--pids-limit` 提供 cgroup 资源上限，但 rootless 模式依赖 cgroup v2 和 systemd 委派。Worker 必须在启动前检查能力，不能把“不支持限制”静默当成已隔离。
6. 运行超时和取消是 Worker 的生命周期控制：Docker 资源参数不替代应用层 deadline。取消一旦被观察到，Worker 不应启动新容器、提交新结论或创建 Artifact。

## 2. LangGraph interrupt、Command 和 checkpoint

### 2.1 暂停与恢复

官方 `interrupts` 文档规定，图必须使用 checkpointer 编译并提供 `configurable.thread_id`。`interrupt(value)` 会持久化可 JSON 序列化的 payload 并暂停；恢复时复用同一 `thread_id`，以 `graph.invoke/stream(Command(resume=value), config)` 继续，`value` 即节点中 `interrupt()` 的返回值。`Command(update/goto/graph)` 是节点返回值的控制命令，不应拿来替代外部恢复输入。

来源：

- [Interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts)
- [Command 类型（1.2.x 源码）](https://github.com/langchain-ai/langgraph/blob/1.2.10/libs/langgraph/langgraph/types.py#L759-L810)

对本项目的约束：中断 payload 只能包含脱敏的 Todo/Sandbox 摘要（ID、状态、参数 hash、过期时间），不能放凭证、完整 prompt 或隐式思维链。恢复端点按 `run_id + thread_id` 读取业务事实，再决定是否允许领取 Todo；不能直接信任客户端 resume 值。

### 2.2 重放和副作用

恢复不是从 Python 调用栈中的 `interrupt()` 下一行继续，而是从该节点开头重新执行。因此中断前的 Todo 创建、Tool Run 插入、容器启动或外部 API 请求会有重复风险。官方建议把副作用移到中断之后，或以幂等 upsert/稳定 key 包住；不要用宽泛 `try/except` 捕获 `interrupt()`。

来源：

- [Interrupt rules and idempotent side effects](https://docs.langchain.com/oss/python/langgraph/interrupts#rules-of-interrupts)
- [Durable execution](https://docs.langchain.com/oss/python/langgraph/durable-execution)
- [Functional API：determinism and idempotency](https://docs.langchain.com/oss/python/langgraph/functional-api)

同一节点的多个中断按稳定调用顺序匹配 resume；并行中断使用 `{interrupt_id: resume_value}` 映射。Todo 节点宜“一次只产生一个中断”，避免条件分支或不确定循环改变顺序。

### 2.3 Checkpoint 粒度、恢复和耐久性

官方 checkpointer 文档说明每个 super-step 生成 checkpoint，并按 node/task 保存 writes。一个并行节点失败时，同一 super-step 中已经成功的 pending writes 在恢复时不会重跑；这可用于 Worker lease 接管，但不能替代业务 CAS。`thread_id` 是恢复游标，`checkpoint_ns` 区分子图，`checkpoint_id` 可用于历史读取/回放；换线程不会恢复旧执行。

指定 `checkpoint_id` 回放时，checkpoint 之前的节点跳过，之后的 LLM/API/interrupt 会再次执行，外部副作用必须去重。`StateSnapshot.tasks` 暴露任务 ID、节点名、错误和中断信息，可作为审计投影输入。`update_state` 创建新 checkpoint，不修改旧快照。

官方 durability 模式为 `sync`、`async`、`exit`：`sync` 在进入下一步前持久化，最适合 Sandbox 关键步骤；`async` 有进程崩溃时丢失最新 checkpoint 的窗口；`exit` 只在退出/错误/interrupt 时保存，不适合作为 Worker 中途接管的唯一保障。

来源：

- [Checkpointers](https://docs.langchain.com/oss/python/langgraph/checkpointers)
- [Persistence](https://docs.langchain.com/oss/python/langgraph/persistence)
- [Time travel](https://docs.langchain.com/oss/python/langgraph/use-time-travel)

## 3. Docker 隔离能力及限制

### 3.1 网络、文件系统和用户

Docker 官方 `none` 网络驱动示例显示容器内只有 `lo`，没有外部网络设备。因此 Sandbox 启动必须显式使用 `--network none`，并在应用层拒绝允许改变网络模式的用户输入。

`--read-only` 将容器根文件系统挂为只读。需要临时写入（例如 `/tmp`）时使用显式 `--tmpfs /tmp`；用户附件以只读 bind mount（`:ro`）挂载，独立输出目录是唯一持久可写挂载。只读根并不等于宿主文件系统只读，任何 bind mount 仍需由 Workspace ACL 和 Conversation 范围校验后生成。

`--user <uid[:gid]>` 让进程以非 root 身份运行。镜像本身也应创建专用非 root 用户；仅依赖运行时 `--user` 时要确认该 UID 对输入挂载可读、对输出目录可写。官方安全示例同时使用 `--cap-drop=ALL`、`--read-only` 和 `--tmpfs`；项目设计还应加 `no-new-privileges`。

来源：

- [None network driver](https://docs.docker.com/engine/network/drivers/none/)
- [docker run reference：`--read-only`、`--user`](https://docs.docker.com/reference/cli/docker/container/run/)
- [tmpfs mounts](https://docs.docker.com/engine/storage/tmpfs/)
- [Bind mounts（只读选项）](https://docs.docker.com/engine/storage/bind-mounts/)
- [Build best practices：非 root 用户](https://docs.docker.com/build/building/best-practices/)
- [Docker 官方安全示例（非 root、cap-drop、read-only）](https://docs.docker.com/guides/dhi-backstage/)
- [Compose 安全选项（`no-new-privileges`、`cap_drop`、`read_only`、`network_mode`、`pids_limit`）](https://docs.docker.com/reference/compose-file/services/)

### 3.2 资源上限和超时

官方 `docker run` 文档提供 `--memory` 和 `--cpus` 示例；`--pids-limit` 限制容器可创建的进程数。三者属于 Docker/cgroup 约束，必须与 Sandbox 的代码执行预算同时传入，而不是由 Python 代码自律。

Rootless 文档特别指出：资源限制通常要求 cgroup v2 + systemd；若 `docker info` 的 cgroup driver 为 `none`，rootless 会忽略这些 flags。Worker 应在执行前检查 `docker info` 和 daemon 能力，无法确认时返回“沙箱不可用/未完成集成”，不得回退宿主 Python。

Docker 资源 flags 不提供业务级 wall-clock deadline。Worker 需要自己的 deadline/取消 token：超时或取消先阻止后续 Todo/Artifact 提交，再终止并清理容器；容器退出后的 stdout/stderr 仍需受字节上限约束，超限应标记 Sandbox Execution 失败而不是截断后伪造结论。

来源：

- [Resource constraints](https://docs.docker.com/engine/containers/resource_constraints/)
- [docker run reference：`--memory`、`--cpus`、`--pids-limit`](https://docs.docker.com/reference/cli/docker/container/run/)
- [Rootless mode resource limits](https://docs.docker.com/engine/security/rootless/tips/)
- [Docker Engine API：容器资源配置](https://docs.docker.com/reference/api/engine/)

## 4. 面向 Todo/Sandbox 的幂等和取消边界

建议把一次研究计算拆成以下稳定事实：

| 实体 | 稳定身份/幂等边界 | 允许的副作用 |
| --- | --- | --- |
| Todo | `run_id + logical_key` 唯一；状态更新使用版本/CAS | 只写 Todo 状态和事件投影 |
| Research Task | `run_id + task_key` 唯一；可被 lease 接管 | 编排 Agent 步骤，不直接启动容器 |
| Tool Run | `run_id + invocation_key` 唯一；单 Worker CAS 领取 | 记录工具调用意图、状态和结果 |
| Sandbox Execution | `tool_run_id` 唯一；容器 ID 为执行器内部字段 | 启动一次受限容器，写入受控 Artifact |

LangGraph checkpoint/replay、重复 SSE 或 lease 接管只能重新发起“读取这些事实”的流程。真正启动 Sandbox 前必须原子地把 Tool Run/Todo 从 `pending`/`approved` 转为 `running`，并检查 run 取消标记；失败或不确定结果只能进入 `failed`/`cancelled`，禁止生成支持性 Citation 或研究结论。取消在任何新容器启动、Artifact 提交、answer 写入前再次检查，确保取消优先。

## 5. 验证清单

- 记录 `docker info` 的 rootless/cgroup 能力；在不满足资源限制时测试必须标记 skipped 或不可用
- 运行容器检查无默认网卡、当前 UID 非 0、根目录不可写、仅允许的 tmpfs/输出目录可写
- 用超出 CPU/内存/PID/输出/时间预算的代码验证得到明确 `failed`，且没有 Artifact/Citation
- 模拟 Worker 崩溃、checkpoint replay、重复 SSE 和 lease 接管，确认同一 `tool_run_id` 不启动第二个 Sandbox 副作用
- 模拟取消竞态，确认取消后不会产生新结论、Citation 或 Artifact
