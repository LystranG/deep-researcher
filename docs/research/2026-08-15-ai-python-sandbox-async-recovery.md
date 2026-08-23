# AI Python Sandbox 的异步执行、隔离与恢复模式

日期：2026-08-15

## 1. 研究问题与结论

本笔记回答：当 Python Sandbox 只作为 AI 的 Tool、而不是用户直接提交代码的入口时，系统应如何处理耐久排队、隔离、输入挂载、输出收集、取消、超时、崩溃恢复、幂等和 Tool Observation 回流；哪些能力可交给容器运行时，哪些必须由业务系统实现。

结论是：**把 Sandbox 建成异步、耐久、至少一次执行的 Tool Job；把“结果发布”做成业务数据库中的幂等、至多一次提交。** Docker 只是一次 Attempt 的隔离执行器，不是任务队列、工作流引擎或事实存储。研究 Worker 不应同步阻塞在 `docker run` 上，而应在提交 Tool Call 后暂停；专用 Sandbox Worker 完成执行并持久化 Observation 后，再把对应 Research Task 唤醒。

推荐的主链路是：

```text
ReAct Subagent
  -> Tool Policy / ACL / Budget
  -> transaction: ToolCall + SandboxJob(queued) + RunEvent
  -> Research Task 暂停，只保留稳定 ID

Sandbox Worker
  -> lease claim + heartbeat
  -> resolve immutable inputs
  -> create/start isolated Attempt
  -> wait/reconcile/cancel/timeout
  -> validate and promote outputs
  -> transaction: terminal Attempt + ToolObservation + Artifact refs

Run Queue
  -> enqueue/resume owning Research Task
  -> ReAct Subagent reads ToolObservation and decides next action
```

这套设计不承诺容器代码 exactly-once：Worker 或节点在不确定窗口崩溃时，同一个逻辑 Tool Call 可能产生多个 Attempt。系统应承诺的是：

- 一个 `invocation_key` 只对应一个逻辑 Tool Call
- 每个 Attempt 使用独立输入/输出目录和稳定执行身份
- 只有一个终态 Observation 能成为该 Tool Call 的有效业务结果
- 取消后、过期后或失去租约的 Attempt 不能发布 Artifact、Derived Evidence 或支持性结论

Kubernetes Job 官方文档明确提醒，即使 `parallelism=1`、`completions=1`、`restartPolicy=Never`，同一程序也可能启动两次，应用必须容忍重复执行；这正是本设计采用“至少一次执行、幂等发布”的依据，而不是假设调度器能提供 exactly-once。[Kubernetes Jobs](https://kubernetes.io/docs/concepts/workloads/controllers/job/#handling-pod-and-container-failures)

## 2. 一手资料给出的能力边界

### 2.1 Docker 能提供一次 Attempt 的生命周期与隔离原语

Docker Engine 官方 SDK 示例把生命周期拆成 `ContainerCreate -> ContainerStart -> ContainerWait -> ContainerLogs`。这允许调用方保存 container ID，在自身进程重启后重新 inspect/wait/read logs；比一个阻塞的 `docker run --rm` 更适合耐久 Worker。[Docker Engine SDK examples](https://docs.docker.com/reference/api/engine/sdk/examples/)

Docker 还直接提供以下原语：

- `--network none` 让容器只有 loopback 网络设备；它能阻断默认外网访问，但不能替代业务层的工具权限和数据分级。[None network driver](https://docs.docker.com/engine/network/drivers/none/)
- `--read-only` 可把根文件系统设为只读；只读 bind mount 可投放输入，tmpfs 或单独输出挂载可提供受控写空间。[docker container run](https://docs.docker.com/reference/cli/docker/container/run/)、[Bind mounts](https://docs.docker.com/engine/storage/bind-mounts/)、[tmpfs mounts](https://docs.docker.com/engine/storage/tmpfs/)
- cgroups 可限制 CPU、内存和进程等资源；Docker 默认不给容器设置资源上限，所以 Sandbox 必须显式配置。这些仍只是单 Attempt 的资源上限，不是业务 deadline、排队超时或重试预算。[Resource constraints](https://docs.docker.com/engine/containers/resource_constraints/)
- namespaces、cgroups 和 Linux capabilities 构成 Docker 的主要隔离机制。Docker 官方同时强调 daemon 的高权限攻击面以及宿主目录共享的风险；Sandbox 内不得获得 Docker socket、任意宿主路径、设备或可由 AI 控制的运行参数。[Docker Engine security](https://docs.docker.com/engine/security/)
- `docker stop` 先发送终止信号，宽限期后使用 `SIGKILL`；调用方仍需决定何时取消、宽限多久以及何种业务状态才算取消完成。[docker container stop](https://docs.docker.com/reference/cli/docker/container/stop/)

Docker **不**提供本系统所需的 Workspace ACL、Tool policy、耐久业务队列、lease、业务幂等键、Artifact provenance、Observation 回流或最终结果发布事务。容器的 restart policy 也不等于 Tool Job 重试：它只会用原参数重启同一容器进程，无法重新执行输入授权、预算、取消和发布资格校验。[Start containers automatically](https://docs.docker.com/engine/containers/start-containers-automatically/)

`--rm` 会在容器退出后删除容器及其匿名卷，而 `--restart` 管理容器退出后的重启，两者不是本场景的恢复组合。[docker container run](https://docs.docker.com/reference/cli/docker/container/run/) 当前实现的随机容器名与 `--rm` 会抹掉可重连身份和终态元数据，因此适合短命同步调用，不适合崩溃接管。

Docker `live-restore` 只能在 daemon 暂时不可用时让 Linux 容器继续运行，并有版本兼容、配置变化和日志 FIFO 堵塞等限制；它不能恢复已经死亡的宿主机，也不能告诉业务系统某次 Tool Call 是否已合法发布。[Live restore](https://docs.docker.com/engine/daemon/live-restore/)

OCI Runtime Spec 也只定义 `creating/created/running/stopped` 以及 create/start/kill/delete/state 等运行时操作；错误状态约束不能扩展为业务 exactly-once。它恰好说明 runtime state 与 Tool Job state 应分层建模。[OCI Runtime lifecycle](https://github.com/opencontainers/runtime-spec/blob/v1.3.0/runtime.md#lifecycle)、[OCI Runtime errors](https://github.com/opencontainers/runtime-spec/blob/v1.3.0/runtime.md#errors)

### 2.2 代表性 AI Sandbox 平台也把“身份、生命周期、文件、命令”显式分开

E2B 使用稳定 `sandboxId`，公开 Running、Paused、Killed 状态和 connect/pause/kill 操作；pause 可保存文件系统和内存，也可选择只保留文件系统后冷启动，kill 后不可恢复。说明“可重连执行身份”和“持久文件状态”应是显式模型，不能隐含在一次客户端调用栈中。[E2B Sandbox lifecycle](https://e2b.dev/docs/sandbox)、[E2B Sandbox persistence](https://e2b.dev/docs/sandbox/persistence)

E2B 后台命令会返回 PID handle，可用 `sandboxId + pid` 重新 connect/wait；但官方明确指出 stdout/stderr 事件流只发送给最初启动该后台命令的客户端，跨进程恢复应把输出重定向到文件。这说明“远程进程仍在运行”不等于“输出已经耐久”，本项目必须主动固化日志或 result manifest。[E2B Background commands](https://e2b.dev/docs/commands/background)

E2B 的文件 API 将 read/write、upload/download 与命令执行分开，snapshot 也把可复用状态作为独立资源；这支持本项目采用“稳定 File/Artifact 引用进入 Observation”，而不是把完整文件塞进模型消息或 checkpoint。[E2B Filesystem](https://e2b.dev/docs/filesystem)、[E2B Sandbox snapshots](https://e2b.dev/docs/sandbox/snapshots)

Modal Sandbox 同样提供 `Sandbox.create`、稳定 Sandbox ID 与 `Sandbox.from_id`、异步 `.aio` 调用、`detach`、`exec`、stdout/stderr、timeout/idle timeout 和 `terminate`；文件则通过 Sandbox filesystem API、只读 Volume mount 或 snapshot 单独管理。[Modal Sandboxes](https://modal.com/docs/guide/sandboxes)、[Modal Filesystem Access](https://modal.com/docs/guide/sandbox-files)、[Modal Volumes](https://modal.com/docs/guide/volumes#read-only-mounts)

这些平台负责的是远程执行环境的生命周期和文件传输。即使选用托管 Sandbox，应用仍需保存自己的 Tool Call、调用者、Workspace、预算、输入版本、Observation、引用和取消事实；供应商 sandbox ID 只是 Attempt 的 executor reference，不能成为本项目的业务主键。

### 2.3 耐久队列仍是业务系统责任

PostgreSQL 官方文档说明 `SKIP LOCKED` 会跳过暂时无法加锁的行，视图并不一致，但适合多个消费者访问 queue-like table。它适合这里的领取竞争，不适合被误解为任务完成或 exactly-once 保证。[PostgreSQL `SELECT ... FOR UPDATE SKIP LOCKED`](https://www.postgresql.org/docs/current/sql-select.html#SQL-FOR-UPDATE-SHARE)

因此队列表必须另外记录状态、lease owner、lease expiry、heartbeat、attempt、retry/backoff、deadline 和 terminal reason；领取事务只能决定“谁现在有权尝试”，不能决定外部容器副作用是否只发生一次。

## 3. 当前实现核对

当前 `DockerSandbox.execute` 已使用无网络、只读根、非 root UID/GID、`cap-drop=ALL`、`no-new-privileges`、PID/内存/CPU 上限、只读输入和独立 `/output`，隔离基线方向正确。[sandbox.py](../../apps/api/src/deep_researcher/sandbox.py#L63)

但执行耐久性存在以下缺口：

1. `SandboxExecution` 在数据库中置为 `queued` 并 commit 后，通过 API 进程内 `ThreadPoolExecutor` 调用；commit 与 `submit` 之间、排队期间或 API 进程重启时都可能永久丢失工作。[app.py](../../apps/api/src/deep_researcher/app.py#L778)、[app.py](../../apps/api/src/deep_researcher/app.py#L3487)
2. 容器使用随机名称和 `docker run --rm`，执行线程通过阻塞 `subprocess.run` 等待。Worker 进程死亡后，数据库没有 executor node/container ID/attempt lease 可供接管，容器退出后也没有终态 metadata 可重新读取。[sandbox.py](../../apps/api/src/deep_researcher/sandbox.py#L63)
3. 取消依赖同一 Python 进程内的 `_active_containers` 字典。换进程或重启后，业务记录仍在，但无法定位已启动容器；`docker rm -f` 返回成功后也缺少独立 reconciler 核对最终状态。[sandbox.py](../../apps/api/src/deep_researcher/sandbox.py#L158)、[app.py](../../apps/api/src/deep_researcher/app.py#L3563)
4. stdout/stderr 和成功 Artifact/DerivedEvidence 会落库，但不存在一等 Tool Observation，结果也不回到 ReAct Agent。现有 Research Run 已有 PostgreSQL lease queue，而 Sandbox 没复用同等级的 claim/heartbeat/recovery seam。[run_queue.py](../../apps/api/src/deep_researcher/run_queue.py#L10)、[worker.py](../../apps/api/src/deep_researcher/worker.py#L8)
5. Attachment 会以只读文件挂载，Evidence Span ID 当前只用于授权和 provenance，并未把正文物化为容器输入。未来 Tool contract 必须只接受业务文件版本 ID，由服务端解析挂载，不能接受 AI 提供的宿主路径或 mount flags。[app.py](../../apps/api/src/deep_researcher/app.py#L3324)
6. 用户当前可以通过 `POST /api/v1/runs/{run_id}/sandbox-executions` 直接提交 `purpose`、Python code 和 inputs。这与已经确认的目标边界冲突：新版本应只允许 Agent 经 Tool Registry 创建；用户可以查看、下载或取消，但不能直接发起任意代码执行。[app.py](../../apps/api/src/deep_researcher/app.py#L3487)

## 4. 推荐的业务模型与状态机

### 4.1 逻辑调用与物理 Attempt 分离

至少要区分三个事实：

| 事实 | 身份 | 作用 |
| --- | --- | --- |
| `ToolCall` | 稳定 `invocation_key` | 一次 ReAct 决策产生的逻辑调用；保存工具名、规范化参数 hash、Task/Run/Workspace、预算与策略版本 |
| `SandboxJob` | 与 `ToolCall` 一对一 | 耐久排队、取消、deadline 和最终结果归属 |
| `SandboxAttempt` | 每次领取一个新 ID | 保存 lease、executor node、container/provider ID、image digest、输入 manifest、开始/结束时间、exit code、资源统计和失败类别 |

`invocation_key` 应由 `run_id + research_task_id + agent_iteration + tool_call_id` 等稳定决策身份产生，不应只用 code hash：Agent 可能有意在不同轮次执行相同代码。数据库应约束同一个 key 不能携带不同参数；重复提交相同 key 返回既有 Job/Observation。

建议状态为：

```text
queued -> leased -> starting -> running
                         |         |
                         v         v
                       failed <- stopping
                         |         |
                         v         v
                    retry_wait   cancelled

running -> succeeded -> publishing -> completed
running -> timed_out
running -> unknown       # executor/node 丢失，等待 reconcile 或新 Attempt
```

`cancel_requested_at` 和 `deadline_at` 应是正交事实，不能只编码在一个状态字符串里。终态至少区分 `completed`、`failed`、`timed_out`、`cancelled`；`unknown` 不是成功或失败，只表示旧 Attempt 的结果暂不可判定。

### 4.2 耐久领取

1. Agent Tool policy 通过后，在同一事务中插入 `ToolCall`、`SandboxJob(status=queued)` 和用户可见 `RunEvent`；事务提交前绝不启动容器。
2. 专用 Sandbox Worker 使用 `FOR UPDATE SKIP LOCKED` 领取最早的 queued/retryable 或 lease 已过期 Job，写入 owner、lease expiry、heartbeat 和 attempt number。
3. Worker 在任何外部副作用前重新检查 Run/Task 取消、Tool Call 是否已有有效 Observation、deadline、预算和输入 ACL。
4. Heartbeat 只延长当前 Attempt 的执行权。Worker 一旦失去 lease，不得发布结果；reconciler 决定终止、接管或等待旧 Attempt。
5. 可重试错误使用有界指数 backoff 和最大 Attempt 数；确定性代码错误、权限失败、超时、取消和输出违规默认不重试。超过上限进入 terminal failed/dead-letter，形成失败 Observation，而不是让 Agent 永久等待。

### 4.3 容器创建与崩溃窗口

Docker backend 应使用 Engine API 的 detached `create/start/inspect/wait/logs/remove` 生命周期，不使用阻塞 `docker run --rm`。容器名或 labels 至少包含 `sandbox_attempt_id`，并持久化 `executor_node_id + container_id`。

| 崩溃窗口 | 恢复动作 |
| --- | --- |
| 领取后、create 前 | lease 过期后创建新 Attempt |
| create 后、container ID 落库前 | 在原 executor node 按稳定 name/label 查找；找到则补记并 inspect，找不到才重试 |
| start 后、wait 前 | inspect 现有容器；running 则继续 wait，exited 则读取 exit code/logs |
| 容器完成、业务发布前 | 保留容器 metadata 和 attempt output staging；恢复后重新校验并发布 |
| Observation 已发布、容器未清理 | cleanup sweeper 按 labels 删除；不得重新执行 |
| 整个 executor node 丢失 | 将旧 Attempt 标为 lost/unknown；从外部不可变输入创建新 Attempt。不能声称旧 Attempt 未执行过 |

如果 Worker 可能换宿主接管，就必须把 Docker 封装成有稳定 node identity 的 Sandbox Executor service，或使用提供稳定 sandbox ID 的远程平台。把一个宿主本地 container ID 交给任意 Worker 无法恢复。

## 5. 输入、输出与 Observation

### 5.1 输入 manifest

AI 可见的 Tool schema 建议只暴露：

```text
python_execute(
  purpose,
  code,
  input_file_refs[],
  timeout_seconds
)
```

AI 不能设置宿主路径、Docker image、UID/GID、mount mode、网络、capability、环境变量或 Docker 参数。服务端根据 `input_file_refs`：

- 在调用创建和 Attempt 启动时各做一次 Workspace/Run ACL 与版本存在性校验
- 固化 `file_version_id + sha256 + logical_path + size` 的 immutable manifest
- 把文件复制或解析到每个 Attempt 独立 staging，按只读方式挂入 `/sources` 或 `/inputs`
- 对 `/work` 使用 Attempt 私有临时空间；只有 `/output` 是候选正式产物出口
- 将 image 使用不可变 digest 固定，并记录 runtime/policy version

用户上传原件和 `/sources` 永远只读；AI 不得通过 Sandbox 覆盖用户源文件。若未来 Research File Space 支持 `/work`，也应在 Attempt 结束后以显式 File version/Artifact 提交，而不是让容器直接写最终对象路径。

### 5.2 输出两阶段发布

容器输出先落在 `attempt_id` 独立 staging。Worker 在发布前检查：

- exit code 与终止原因
- stdout/stderr 单项和总字节上限，并记录 `truncated`，不能静默截断后冒充完整结果
- 文件数量、单文件和总大小、规范化相对路径、symlink/特殊文件、media type 与 SHA-256
- 当前 lease、Run/Task/Job 取消、deadline 和 Tool Call 是否已有 Observation

通过后，先用确定性对象 key 把文件固化到 Blob/Object Store，再在一个数据库事务中创建 Artifact/File version、Derived Evidence（若允许）和 Tool Observation。对象写入与数据库事务无法天然原子时，使用 staging + finalize/sweeper；重复 finalize 必须按 hash 和 key 幂等。

失败、超时和取消也要产生终态 Observation，但不能产生支持性 Derived Evidence 或 Citation。可按 retention policy 暂存失败日志用于诊断，不能把未校验 output 当作 AI 可引用证据。

### 5.3 Observation 是业务事实，不是 checkpoint 正文

推荐返回给 ReAct Subagent 的结构为：

```json
{
  "observation_id": "...",
  "tool_call_id": "...",
  "status": "completed|failed|timed_out|cancelled",
  "attempt_count": 1,
  "exit_code": 0,
  "stdout_preview": "...",
  "stderr_preview": "...",
  "stdout_truncated": false,
  "stderr_truncated": false,
  "artifacts": [
    {
      "file_id": "...",
      "path": "/artifacts/result.csv",
      "media_type": "text/csv",
      "size_bytes": 123,
      "sha256": "..."
    }
  ],
  "timing": {
    "queued_at": "...",
    "started_at": "...",
    "completed_at": "...",
    "duration_ms": 1000
  },
  "error": null
}
```

大型 stdout/stderr、完整文件和 provenance 通过稳定 ID 引用，不进入 LangGraph checkpoint 或模型消息。Checkpoint 只保存 `tool_call_id`、`observation_id` 和必要路由状态；`ToolObservation`、Artifact、Derived Evidence、RunEvent 才是可审计、可恢复的业务事实。

Sandbox Worker 发布 Observation 后，应以幂等方式把 owning Research Task 重新放入 Run Queue。Research Worker 恢复时先按 ID读取 Observation，再将其作为 Tool message 输入同一个 ReAct Subagent；不需要用户调用 resume，也不应通过内存 callback 唤醒。

## 6. 取消与超时

### 6.1 取消

取消请求先在数据库写 `cancel_requested_at`，再由 Sandbox Worker/Executor 执行容器 stop/kill。应遵守：

1. queued Job 直接转 cancelled，不创建 Attempt
2. leased/starting Job 在 create/start 前再次检查并停止
3. running Job 先发 stop，短宽限后 kill，再 inspect/wait 确认 terminal
4. executor 暂不可达时保持 `cancel_requested`/`unknown`，不能因为一次 kill RPC 失败就宣称 cancelled
5. 结果发布事务再次检查取消；一旦取消事实先提交，迟到成功结果不得成为 Artifact/Derived Evidence/Observation completed
6. Run 取消应扇出到所有未终态 Tool Jobs，但每个 Job 独立确认终止

用户可拥有“取消 AI 发起的 Sandbox Tool”权限，但不再拥有“创建任意 Python execution”权限。

### 6.2 超时

需要区分：

- `overall_deadline_at`：从 Tool Call 创建起计算，覆盖排队、重试和执行
- `attempt_timeout_seconds`：容器真正开始后计算的 wall-clock 上限
- CPU/内存/PID/输出上限：由 runtime 和业务校验共同执行

Docker cgroup 资源上限不提供业务 wall-clock deadline。Worker/reconciler 必须持久化 deadline；重启后按绝对时间继续判断，而不是重新给完整 timeout。达到 deadline 时执行 stop/kill，记录 timed_out Observation，不再重试，除非 Planner 明确创建新的逻辑 Tool Call。

## 7. 运行时责任与业务系统责任

| 能力 | 容器/Sandbox runtime | deep-researcher 业务系统 |
| --- | --- | --- |
| 进程隔离 | namespaces/cgroups/capabilities/seccomp、容器或 microVM 生命周期 | 选择和验证安全 backend；专用 executor host；禁止 socket/设备/特权参数 |
| 网络 | 执行 `network none` 或供应商网络策略 | 决定工具是否允许网络、域名/凭证/数据外发策略；AI 不可改策略 |
| 文件挂载 | 执行只读 mount、tmpfs、输出 mount 或 provider file API | ACL、不可变版本、逻辑路径、hash、大小、provenance、最终 Artifact/File version |
| 资源 | 执行 CPU/内存/PID 等限制并给出 exit/OOM 信号 | 套餐/Workspace/Run 预算、并发配额、overall deadline、输出上限与错误分类 |
| 执行生命周期 | create/start/inspect/wait/logs/stop/kill | 耐久 Job、lease/heartbeat、attempt、retry/backoff、dead-letter、reconcile |
| 取消 | 终止具体 container/sandbox | 保存取消事实、传播、竞态优先级、终态发布资格 |
| 恢复 | 同一 daemon/provider 内按 container/sandbox ID 查询 | 稳定 invocation/attempt identity、executor routing、节点丢失重试、未知结果处理 |
| 幂等 | name/label/ID 可辅助查重 | 唯一约束、参数 hash、CAS、只发布一个有效 Observation |
| 输出 | exit code、stdout/stderr、文件 API | 限额、hash、Blob 固化、Artifact/Derived Evidence、ACL 和 retention |
| Agent 回流 | 无 | 持久 Tool Observation、RunEvent 投影、Research Task 重新入队和 ReAct resume |

一句话边界：**runtime 保证“这个 Attempt 在什么隔离环境中发生并如何终止”，业务系统保证“为什么可以发生、属于谁、是否仍有资格发布、如何被 Agent 继续消费”。**

## 8. 隔离部署建议

当前 Docker flags 应保留，并补充部署级约束：

- Sandbox Worker 与 API/Research Worker 分进程、最好分主机或至少使用专用 rootless Docker daemon；启动时验证 cgroup/resource limit 能力，失败时 fail closed，不回退宿主 Python
- 使用固定 allowlist 镜像和 image digest；镜像内非 root，根只读，默认 seccomp/AppArmor/SELinux 不得由 Tool 参数关闭
- 不挂 Docker socket、宿主根目录、密钥目录或 Workspace 通用目录；只挂服务端生成的 per-attempt staging
- 默认无网络；以后需要联网时应是另一个风险级别和受控 egress policy，而不是 `python_execute` 的布尔开关
- executor API 使用服务身份认证，不能让浏览器或 Agent 直接访问 Docker daemon/provider credential
- 多租户威胁模型若要求强于共享内核容器，应保持 `SandboxExecutor` 深模块接口可替换，以便切换到 microVM/托管 Sandbox；不能把 Docker CLI 细节泄漏到 Tool schema 和 Agent loop

E2B 和 Modal 的一手资料都把 Sandbox 暴露为稳定远程资源而非本地 Python 子进程，说明 executor backend 可替换是现实需要，不应把业务状态绑定到某一供应商 ID。[E2B Docs](https://e2b.dev/docs)、[Modal Sandboxes](https://modal.com/docs/guide/sandboxes)

## 9. 行为验收矩阵

测试应验证业务行为，而不是 Graph 节点或方法调用次数：

1. **commit-submit 崩溃**：Tool Call 事务提交后进程退出，新 Sandbox Worker 能从队列领取并最终回流 Observation
2. **create-persist 崩溃**：容器已创建但 container ID 尚未落库，reconciler 按 attempt label 找回，不启动第二个并行 Attempt
3. **finish-publish 崩溃**：容器已退出、输出已写入但 DB 未发布，恢复后只创建一组 Artifact 和一个有效 Observation
4. **重复领取**：lease 过期导致新 Worker 接管时，旧 Worker 的迟到结果因 lease/CAS 失败而不能发布
5. **相同调用重放**：同一 `invocation_key` 重复提交返回已有状态/结果；不同轮次有意执行相同 code 仍可创建新 Tool Call
6. **取消竞态**：取消先提交时，迟到的成功结果不能产生 Artifact、Derived Evidence 或 completed Observation
7. **超时恢复**：Worker 重启不会重置 absolute deadline；过期 Attempt 被终止并返回 timed_out
8. **输入不可变**：源文件版本在排队后发生变化或失去 ACL，Attempt 不会读取替代内容，且返回明确失败 Observation
9. **输出违规**：超文件数/大小、非法路径或 symlink 不会进入正式文件空间，也不会成为证据
10. **Observation 再决策**：ReAct Subagent 收到失败/成功 Observation 后能分别修正代码重试或消费 Artifact 继续任务；用户无需调用 Sandbox 或 resume
11. **节点永久丢失**：旧 Attempt 标为 lost/unknown，新 Attempt 允许执行，但最终只有一个 Observation 能发布
12. **隔离基线**：容器内无外网、UID 非 0、根不可写、输入只读、只有指定 work/output 可写，CPU/内存/PID/时间上限均产生可区分终态

## 10. 对后续决策票的约束

- Python Sandbox 应实现成专用异步 Tool Job，不在 Research Worker 或 API 进程内同步执行 Docker
- Tool Observation 必须是数据库业务事实；LangGraph checkpoint 只保存稳定 ID
- 同一个 Research Task 在等待 Sandbox 时暂停，Observation 发布后由 Run Queue 自动恢复，不引入用户 resume
- Sandbox 的执行语义是 at-least-once；业务发布通过唯一约束、lease fencing 和 CAS 实现单一有效结果
- 用户不再拥有创建 Python execution 的接口，只保留查看、下载和取消 AI Tool Job 的能力
- Docker 隔离实现可以复用，但必须替换随机 `--rm` 阻塞调用和进程内取消映射，加入持久 attempt/executor identity 与 reconcile
- Sandbox 输入输出必须接入统一 Research File Space 的稳定文件版本和 Artifact 引用，不能暴露宿主路径或让容器直接修改正式文件
