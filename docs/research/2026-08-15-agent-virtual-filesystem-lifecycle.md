# Agent 虚拟文件系统、版本与生命周期调研

调研日期：2026-08-15

## 范围与结论

本文回答 Wayfinder Research 票“如何为 Agent 提供统一路径视图，同时保持只读来源、Run 内工作文件、持久产物、Blob 版本、ACL、并发、软删除、垃圾回收和 provenance 的明确语义”。调研只核对当前实现与一手资料，不修改生产实现。

结论如下：

1. **统一路径视图应是应用层虚拟命名空间，而不是把对象存储直接挂载成 POSIX 文件系统**。S3 的 prefix 只是对象 key 前缀，并不是真目录；目录、重命名、版本指针、ACL 和软删除都应由 PostgreSQL 中的命名空间元数据定义。[1]
2. **路径、文件版本和字节对象必须分离**：`FileEntry` 表示可重命名的路径身份，`FileRevision` 表示不可变内容版本，`Blob` 表示按摘要寻址的不可变字节。对象存储的原生 Version ID 可作为底层防护信息，但不能成为产品的版本身份。[2]
3. **Agent 看到三个语义不同的根**：`/sources` 是当前 Run 冻结的只读来源投影；`/work` 是 Run 内、按 Task 隔离的临时写层；`/artifacts` 是 Workspace 级、跨 Run 持久且用户可见的正式产物。Linux OverlayFS 与 AgentFS 都证明了“只读 lower/base + 可写 upper/delta + merged view”的可行语义，但本项目应在应用层实现它，而不是让数据库事实依赖某台 Worker 的挂载状态。[3][4]
4. **Sandbox 文件系统只能是执行介质，不能是持久事实源**。OpenAI 明确要求把容器视为临时资源，容器过期后文件不可恢复；E2B 同样把 timeout、pause、resume、snapshot 作为 Sandbox 生命周期，而非业务 Artifact 生命周期。因此 Sandbox 成功后必须把输出固化为本项目的 Blob、Revision、Observation 和 provenance，再允许 Worker 清理执行目录。[5][6]
5. **写操作采用不可变 Revision + 乐观并发控制**。Agent 必须携带 `expected_revision` 或 `expected_generation`；不匹配时返回可重试冲突，不能静默 last-write-wins。S3 的 `If-Match`/ETag 条件写和 PostgreSQL `FOR UPDATE` 分别提供底层与元数据层的并发原语。[7][8]
6. **软删除与物理 GC 必须分成两步**。删除路径只创建 tombstone；GC 在保留期之后重新计算 Blob 可达性，并保护仍被 DocumentVersion、SourceSnapshot、Artifact Revision、Evidence、Tool Observation、运行中 Job 或 provenance 引用的内容。对象存储 Lifecycle 只能作为已经判定为可回收对象的后备清理机制，不能替代业务可达性判断。[9]
7. **provenance 应直接使用不可变 Revision ID 建图**。W3C PROV 把固定版本内容视为 Entity，把上传、抓取、Agent 写入、Sandbox 执行和发布视为 Activity，把用户、Agent、模型与 Worker 视为 Agent；`used`、`wasGeneratedBy`、`wasDerivedFrom` 足以覆盖输入、输出、修订和派生关系。[10]

## 1. 当前实现审计

### 1.1 三套互相孤立的存储

| 类型 | 当前字节或正文位置 | 当前元数据 | 已有能力 | 主要缺口 |
| --- | --- | --- | --- | --- |
| 用户附件 | `var/objects/workspaces/{workspace_id}/attachments/{attachment_id}/source` | `Attachment`、`Document`、`DocumentVersion`、`SourceChunk` | UUID 派生 key、SHA-256、附件软删除、文档版本 | 没有目录树或统一文件操作；附件晋升只是复用同一 `storage_key`；版本写入缺少显式并发前置条件 |
| 网页来源 | PostgreSQL 的 `SourceSnapshot.content` 与 `SourceChunk.text` | `SourceSnapshot`、`SourceChunk`、Evidence/Citation | Run 绑定、内容 hash、抓取时间、失效时间 | 不在任何文件路径下；无法与附件或 Agent 文档用同一 `list/read/stat` 接口浏览 |
| Sandbox 产物 | `var/sandbox-output/{workspace_id}/{execution_id}/...` | `Artifact`、`DerivedEvidence` | SHA-256、大小、媒体类型、Workspace 下载 ACL、软删除字段 | 与附件对象存储分根；只有 Sandbox 能创建；没有版本、移动、恢复、发布和 GC 协议 |

当前 `LocalObjectStore` 只支持 `put_attachment` 和把 key 解析为本地路径，先写 `.part` 再用 `os.replace` 原子替换；它不是通用 Blob Store，也没有 `get/exists/delete` 或条件写接口。[L1] `Attachment`、`DocumentVersion` 和 `Artifact` 都各自保存 `storage_key + sha256 + size`，但没有统一 Blob 身份。[L2][L3]

附件晋升为 Workspace 文档时，新 `DocumentVersion` 直接复用附件的 `storage_key`；更新版本时通过 `document.current_version + 1` 计算下一版本，虽然数据库有 `(document_id, version)` 唯一约束，却没有调用方传入的 base version 或显式行锁。并发更新最好的结果是其中一次唯一约束失败，调用者也无法区分“内容冲突”与普通服务错误。[L4]

Sandbox 已正确把附件只读挂载到 `/inputs/{filename}`，只让 `/output` 可写，并启用只读根文件系统、无网络、非 root、cap-drop、no-new-privileges、PID/内存/CPU/超时限制。[L5] 但执行完成后，产物仍停留在 `sandbox-output` 根目录，随后逐个生成 `Artifact` 和 `DerivedEvidence`；不存在统一 File Revision，也没有把输出发布到一个可由 Agent 后续 `read/move/edit` 的工作空间。[L6]

### 1.2 可以保留的基础

以下现有事实有复用价值，不应被虚拟文件系统重新发明：

- `WorkspaceMember` 是 Workspace ACL 的事实源
- `DocumentVersion` 和 `SourceSnapshot` 已表达“某个来源在某一时刻的固定版本”
- `SourceChunk`、`EvidenceSpan`、`Citation` 已承担可引用内容定位
- `SandboxExecution`、`DerivedEvidence` 已记录代码 hash、输入 ID、stdout hash 和结果 hash
- `Artifact.deleted_at`、`Attachment.deleted_at`、`Document.deleted_at` 已采用软删除方向

新的文件空间应为这些领域对象提供统一投影和字节版本层，而不是把所有领域语义压成一张通用 files 表。

## 2. 一手实现与规范带来的边界

### 2.1 Agent/代码执行平台：执行文件系统天然是短生命周期

OpenAI Code Interpreter 允许模型迭代运行 Python、接收上传文件、创建文件并通过 `container_file_citation` 暴露生成文件；但官方明确说明容器闲置 20 分钟后过期，关联数据被丢弃且不可恢复，应用必须在容器有效时下载需要的文件。[5]

E2B 的公开 SDK 把 Sandbox timeout、pause、resume、connect 和 snapshot 作为独立生命周期操作，并为 Sandbox 提供 POSIX 风格读写接口。[6] 这类平台说明 Agent 确实需要熟悉的路径和文件操作，但也说明不能把容器 ID 或容器路径当作长期 Artifact ID。

**对本项目的约束**：

- Python Tool 的输入必须是本地稳定 `FileRevision ID`，不能只保存 Worker host path
- 执行前物化或挂载输入，执行后立即摄取输出
- Tool Observation 返回 `revision_id + sha256 + logical_path`，而不是返回 `/output/foo.csv` 作为长期 locator
- Worker 恢复时从业务状态重建 Sandbox；不尝试把旧容器本身当作恢复机制

### 2.2 OverlayFS 与 AgentFS：统一视图和事实存储是两件事

Linux OverlayFS 把 `lowerdir` 和 `upperdir` 合并为一个 merged directory；同名 upper 对象遮蔽 lower 对象，删除 lower 对象时通过 upper 中的 whiteout 表示，而不修改 lower 文件。[3]

AgentFS 的官方规范也将 namespace（dentry）与 inode/data 分离，并给出只读 base + 可写 delta + whiteout 的 overlay lookup 规则；其 README 强调文件、工具调用和状态可在 SQLite 中审计和快照。[4] 但 AgentFS 当前仍标记为 Beta，而且规范把 ACL、版本历史、快照、内容去重和 checksum 列为扩展点，不是已经具备的完整生产语义。[4]

**可借鉴而不直接照搬的部分**：

- 只读 base 与可写 delta 的查找优先级
- namespace 与 content 分离
- 多步文件操作放在数据库事务中
- 工具调用采用追加式审计记录
- 删除只读 lower 时使用逻辑 tombstone/whiteout

**不直接采用的部分**：

- 不允许 Agent 创建 symlink、hard link、device、FIFO 或 socket；首版只支持普通文件和目录，避免越权路径解析
- 不把大 Blob 存进 PostgreSQL 或单个 SQLite 文件
- 不把 Unix uid/gid/mode 当成产品 ACL；权限仍由 Workspace/Run/Task capability 决定
- 不依赖 FUSE/NFS mount 作为 API、Worker 与恢复协议的事实源

### 2.3 对象存储：适合不可变 Blob，不适合产品目录树

AWS 明确说明 S3 prefix 不是目录，slash 也只是常用 delimiter。[1] S3 Versioning 会为覆盖写生成新 Version ID，为普通删除生成 delete marker；每个版本是完整对象而非 diff。[2] 这适合灾难恢复，却不足以表达：

- 同一 Blob 在多个逻辑路径或 Artifact 版本中复用
- 目录 rename/move 的原子性
- 当前 Revision 指针和 base revision 冲突
- Workspace/Run/Task ACL
- 输入、工具活动和输出之间的 provenance
- 本地对象存储与 S3 之间一致的产品合同

因此对象存储只负责不可变字节。产品版本、路径和生命周期必须由数据库定义；如底层开启 S3 Versioning，应把 provider Version ID 和 ETag 保存为运维字段，而不是暴露为 Agent API 的版本号。

## 3. 推荐的逻辑模型

精确表名与字段属于后续设计票；本节只固定必须分开的身份和关系。

```text
Workspace
  └─ FileEntry                    # 稳定路径身份，可 rename/move/soft-delete
       ├─ current_revision_id ───────┐
       └─ FileRevision[]              │ # 不可变内容与元数据版本
              ├─ blob_id ────────────┼─ Blob # 按 sha256 寻址的不可变字节
              ├─ generated_by_id     │
              └─ ProvenanceEdge[] ───┘

ResearchRun
  └─ RunFileMount[]               # 将固定来源版本投影到 /sources
       └─ source_ref + revision/content hash

ToolCall / SandboxExecution
  ├─ uses FileRevision[]
  └─ generates FileRevision[]
```

### 3.1 `FileEntry`：可变命名空间

`FileEntry` 至少需要：

- 稳定 `entry_id`
- `workspace_id`
- `scope`：`source_mount | run_work | artifact`
- `run_id`、`task_id`（仅相应 scope）
- `parent_entry_id + name + normalized_name`
- `kind`：首版只允许 `directory | file`
- `current_revision_id`
- `generation`：每次 rename、move、current revision 或删除状态变化时递增
- `deleted_at/deleted_by/delete_reason`

同一 scope 和 parent 下只允许一个未删除的 `normalized_name`。路径是 dentry 链的投影，不是 Blob key；rename/move 只修改元数据，不能复制或改写字节。

### 3.2 `FileRevision`：不可变产品版本

每一次有意义的内容写入都创建新 Revision，不原地修改旧 Revision：

- `revision_id` 与单调 `revision_number`
- `entry_id + blob_id`
- `media_type + size_bytes + sha256`
- `created_at + created_by_kind + created_by_id`
- `run_id + task_id + tool_call_id + sandbox_execution_id`（按来源可空）
- `base_revision_id`：表达这次修改基于哪个版本
- `origin_kind`：`user_upload | web_capture | agent_write | sandbox_output | copy | publish`

路径可以变化，Revision locator 不变化。Citation、Evidence、Tool Observation、checkpoint 和下载记录都应引用 `revision_id`，展示时再解析当前或历史路径。

### 3.3 `Blob`：不可变物理对象

Blob key 建议由摘要派生，例如 `blobs/sha256/{prefix}/{digest}`，写入遵循：

1. 流式计算大小和 SHA-256
2. 写临时对象
3. 以摘要 key 条件提交；相同摘要复用已有 Blob
4. 校验对象元数据后创建 `Blob` 记录
5. 在同一数据库事务内创建 Revision 并更新 Entry 指针

Blob 只保存物理事实：digest、size、provider、storage key、provider version/ETag、创建时间、完整性状态。不要把路径、用户可见文件名或 ACL 编码进 Blob key。

### 3.4 `RunFileMount`：冻结的来源投影

Run 创建或重规划采纳新来源时，保存显式 mount manifest：

- `run_id`
- Agent 可见路径
- 来源类型与稳定来源 ID（DocumentVersion、Attachment、SourceSnapshot、Artifact Revision）
- 精确内容 hash/Revision ID
- mount generation 与加入时间
- 只读 capability

恢复时按 manifest 重建同一个 `/sources`，不能重新解析“当前文档版本”。重规划可以追加新的 mount generation，但旧 Observation 继续引用当时版本。

## 4. 三个根目录的明确语义

| 根目录 | 底层 scope | 生命周期 | Agent 权限 | 并发模型 | 删除语义 |
| --- | --- | --- | --- | --- | --- |
| `/sources` | RunFileMount 的只读投影 | 至少覆盖 Run、引用和审计保留期 | `list/stat/read/search` | 内容冻结；重规划只追加新 generation | Agent 永远不能删；底层来源删除后按保留/合规策略显示已撤销状态 |
| `/work` | `run_work` | Run 终态后进入 grace period；已发布或被证据引用的 Revision 被 pin | Task 自己目录可增删改查；共享区需 CAS | `/work/tasks/{task_id}` 单写者；共享写带 expected generation | tombstone；Run 清理后可物理 GC 未引用 Blob |
| `/artifacts` | Workspace 级 `artifact` | 跨 Run 持久，直到用户/策略删除并过保留期 | Agent 可创建、发布、产生新版本和 rename；删除为可恢复软删除 | 每次写必须基于 expected revision/generation | tombstone + restore；物理删除延迟且受 provenance pin 保护 |

为保持 ReAct Subagent 的私有工作状态，默认工作目录应是 `/work/tasks/{task_id}/`。其他 Subagent 只读取已经提交到 Research Ledger、`/work/shared` 或 `/artifacts` 的稳定 Revision，不能读取另一个 Task 尚未提交的 mutable scratch 文件。

### 4.1 首版文件工具

建议对模型暴露少量正交工具，而不是完整 shell 文件权限：

- `file_list(path, cursor, limit)`
- `file_stat(path)`
- `file_read(path, revision_id?, byte_range?)`
- `file_search(path, query, limit)`
- `file_write(path, content|blob_ref, expected_revision?)`
- `file_mkdir(path, expected_parent_generation)`
- `file_move(source, target, expected_entry_generation, expected_target_parent_generation)`
- `file_delete(path, expected_entry_generation)`
- `file_restore(entry_id, target_path?, expected_parent_generation)`
- `artifact_publish(work_revision_id, artifact_path, expected_revision?)`

工具层必须统一做绝对路径规范化、拒绝 `..`、拒绝 NUL、限制名称与深度、禁止 symlink/hardlink，并在返回值中带 `entry_id/revision_id/generation/sha256`。Storage key 和 host path 永不进入模型上下文。

`artifact_publish` 不复制字节：它创建或更新 `/artifacts` 的 Entry，生成一个 `origin_kind=publish` 的新 Revision，引用同一 Blob，并记录 `wasDerivedFrom(work_revision)`。这样用户看到的是一次可审计发布，而不是难以解释的目录移动。

## 5. Python Sandbox 与虚拟文件空间的桥接

推荐流程：

```text
ReAct Subagent
  -> python_execute(code, purpose, input_paths, output_contract)
  -> FileSpace.resolve_and_pin(input_paths)
  -> Sandbox Worker 物化只读 /inputs + 空的可写 /output
  -> 执行 Python
  -> 扫描 /output，拒绝 symlink/特殊文件并应用数量与大小限额
  -> ingest 为 Blob + /work FileRevision
  -> 持久化 Tool Observation + provenance
  -> ReAct Subagent 根据 Observation 再决策
```

关键边界：

1. `input_paths` 在 ToolCall 创建时解析为精确 Revision ID 并 pin；排队期间同名文件产生新版本也不改变这次执行输入
2. 输入以只读方式挂载，输出目录是唯一可写持久化候选；容器 `/tmp` 仍是易失空间
3. 每个输出先摄取 Blob 和 Revision，数据库提交成功后 Observation 才可见
4. Observation 保存结构化输出清单，不内联大型文件：`path/revision_id/media_type/size/sha256`
5. 重试使用 ToolRun idempotency key；若相同执行结果已经固化，恢复时复用，不能重复发布同一路径的新版本
6. 用户上传原件永远不会因 Agent 或 Sandbox 操作被覆盖、rename 或删除

## 6. ACL 与能力边界

S3 官方建议现代用例关闭对象 ACL，使用 bucket/IAM policy 集中控制权限。[11] 对本项目而言，底层 Bucket 或本地对象根应只允许服务身份访问；产品 ACL 必须在解析逻辑路径和签发下载响应之前完成。

每次 Agent 文件调用至少校验：

1. 当前 Run 属于当前 Workspace
2. 当前 ToolCall 持有相应 scope/prefix/operation capability
3. `/work/tasks/{task_id}` 的 task owner 与调用者一致
4. `/sources` 的 revision 位于当前 Run mount manifest
5. `/artifacts` 的 Workspace 成员权限允许该操作
6. Entry、Revision、Blob、来源对象的 `workspace_id` 链一致

推荐 capability 示例：

```json
{
  "workspace_id": "...",
  "run_id": "...",
  "task_id": "...",
  "grants": [
    {"prefix": "/sources", "ops": ["list", "stat", "read", "search"]},
    {"prefix": "/work/tasks/<task-id>", "ops": ["list", "stat", "read", "write", "move", "delete"]},
    {"prefix": "/artifacts", "ops": ["list", "stat", "read", "publish", "new_version", "move", "soft_delete"]}
  ]
}
```

Blob digest、storage key、presigned URL 或“知道路径”都不构成授权。下载链接应在 Workspace ACL 检查后短时签发，或继续由 API 流式返回。

## 7. 并发与事务

### 7.1 乐观并发是公共合同

所有会改变 Entry 的工具都携带调用者读到的 generation：

- `file_write`：`expected_revision_id`
- `file_move/delete/restore`：`expected_entry_generation`
- 在目录中创建或移动目标：`expected_parent_generation`

服务在数据库事务中锁定相关 Entry/parent，比较 generation，然后创建不可变 Revision 或修改 dentry。PostgreSQL `FOR UPDATE` 会阻止其他事务修改或删除同一行直到事务结束；多个 Entry 操作必须按稳定 ID 排序取锁，减少 deadlock。[8]

冲突返回结构化 `revision_conflict`，包含当前 `revision_id/generation`，由 ReAct Subagent 重新读取后决定 merge、换名或放弃。不能自动覆盖用户或另一个 Task 的新版本。

### 7.2 底层条件写是第二道防线

S3 条件写可以用 `If-None-Match` 防止同 key 已存在时覆盖，或用 `If-Match`/ETag 确保对象未变化。[7] 本地 Adapter 可用临时文件 + `os.replace` 完成单对象发布，但仍要由数据库 generation 决定“这次写是否有权成为当前版本”。

推荐提交顺序：

1. 以 idempotency key 领取文件 ToolRun
2. 上传/复用不可变 Blob
3. 数据库事务锁 Entry，验证 expected generation
4. 创建 Revision、provenance edge、更新 current pointer 和 generation
5. 提交 Observation/RunEvent
6. 事务失败留下的无引用 Blob 交给 grace-period GC

## 8. 软删除、保留与垃圾回收

### 8.1 软删除

`file_delete` 只把 Entry 变为 tombstoned，并保存 `deleted_at/deleted_by/delete_reason/deletion_generation`。默认 list/read 隐藏 tombstone；用户或有权限的 Agent 可在保留期内 restore。若原路径已被占用，restore 必须显式选择新路径，不能覆盖当前文件。

`/sources` 的 Agent 删除始终返回 `read_only_source`。底层用户文档被删除时，不应改写历史 Run mount；可以把 mount 标记为 `revoked` 并依据合规策略禁止新读取，但已有 Citation/审计记录仍保存不可变 hash 和来源身份。

### 8.2 Blob GC

不要只维护一个可被崩溃和并发写破坏的 refcount。采用“标记候选 + 再验证 + 异步清除”：

1. 找出超过 grace period、没有 live Entry/Revision 的 Blob
2. 把候选标记为 `purge_pending`
3. 在删除前重新检查所有 pin：
   - 当前和保留期内的历史 FileRevision
   - Attachment/DocumentVersion/SourceSnapshot/Artifact
   - EvidenceSpan/Citation/ResearchRecord
   - Tool Observation/DerivedEvidence/provenance edge
   - 未终态 Run、Sandbox/Tool Job、上传 lease
   - 法务保留或管理员 pin
4. 删除物理对象，记录幂等 purge event
5. 最后把 Blob 标记为 purged；失败可重试

S3 Lifecycle 对 versioned bucket 的普通 Expiration 只给当前版本加 delete marker，不会自动删除 noncurrent 版本；永久清理需要单独的 `NoncurrentVersionExpiration`，且删除后不可恢复。[9] 因此 Lifecycle 规则必须晚于产品保留期，并只作用于已经由业务 GC 判定可回收的专用 prefix/tag。

### 8.3 建议生命周期

| 对象 | 建议保留语义 |
| --- | --- |
| Run mount manifest | 至少与 Run、Citation、审计记录同寿命 |
| `/work` live Entry | Run 终态后进入可配置 grace period |
| 被 Observation/Evidence 引用的 work Revision | 引用存在期间 pin；可隐藏路径但不删 Blob |
| `/artifacts` 当前 Revision | 直到用户软删除并过保留期 |
| `/artifacts` 历史 Revision | 按 Workspace 版本保留策略；provenance/citation pin 优先 |
| 上传中临时 Blob | lease 过期且无 Revision 后清理 |
| Sandbox host 目录 | 输出成功摄取并校验后即可清理；它不是保留机制 |

## 9. Provenance

W3C PROV 的三个核心概念可以直接映射：[10]

| PROV | 本项目对象 | 关键关系 |
| --- | --- | --- |
| Entity | FileRevision、DocumentVersion、SourceSnapshot、EvidenceSpan | 固定 hash 和稳定 ID |
| Activity | user upload、web capture、Agent write、SandboxExecution、artifact publish | `started_at/completed_at/tool_call_id/code_hash` |
| Agent | 用户、ReAct Subagent、模型版本、Worker/service identity | 对 Activity 的 responsibility |

每个新 Revision 至少记录：

- `wasGeneratedBy(activity_id)`
- Activity 的 `used(input_revision_id/source_snapshot_id/evidence_span_id)`
- 内容变换时的 `wasDerivedFrom(output_revision_id, input_revision_id)`
- `wasAssociatedWith(user|agent|worker)`
- Sandbox 的 code hash、镜像 digest、Tool schema/version、输入顺序、stdout/stderr hash

同 Blob 的 `artifact_publish` 仍创建一个新的 Artifact Revision 和 derivation edge，因为“正式发布”是新的业务事实。反过来，rename/move 只产生 Entry audit event，不创建内容 Revision。

最终回答若链接一个 Agent 生成文档，应引用 `artifact_revision_id`；文档内部 Citation 继续引用不可变 SourceSnapshot/DocumentVersion/EvidenceSpan。这样路径改名、产生新版本或软删除都不会让历史回答悄悄指向不同内容。

## 10. 推荐决策与不推荐方案

### 推荐锁定

1. PostgreSQL 管 namespace、版本指针、ACL、生命周期和 provenance；对象存储只管不可变 Blob
2. `/sources`、`/work`、`/artifacts` 是同一 FileSpace Module 下的不同 scope，不是三个任意本地目录
3. `/work` 按 Task 隔离；共享只发生在显式提交的 Revision 上
4. Sandbox 输入解析并 pin 到 Revision，输出先摄取再返回 Observation
5. 所有写操作有 idempotency key 和 expected generation/revision
6. 删除先 tombstone，GC 延迟且以可达性为准
7. Citation、Observation、checkpoint 只保存稳定 ID/hash，不保存 host path

### 不推荐

- 直接把 S3 prefix、`var/objects` 或 `var/sandbox-output` 暴露给 Agent
- 让 Sandbox 容器或 Worker 磁盘成为 Artifact 的事实源
- 一个 Workspace 共享完全可写的长期目录
- 用 S3 Version ID 代替产品 Revision ID
- 让 Agent 覆盖或删除用户上传原件
- last-write-wins，或只靠数据库唯一约束表达并发冲突
- 删除 Entry 时同步物理删 Blob
- 只靠 refcount 或对象存储 Lifecycle 做 GC
- 允许 symlink/hardlink 穿过 scope/ACL 边界

## 11. 行为验收建议

测试应验证用户和 Agent 可观察的业务行为，而不是断言表、字段或 Graph 节点数量：

| 场景 | 必须证明的行为 |
| --- | --- |
| 只读来源 | Agent 对 `/sources/x` 写、移、删均被拒绝，原始上传与 hash 不变 |
| Run 恢复 | Worker 重启后同一 logical path 仍解析到原 mount generation 和 Revision |
| Sandbox 往返 | Agent 读取 source、运行 Python、得到 work Revision，再次读取并解释；Observation 带稳定 ID/hash |
| 幂等恢复 | Sandbox 成功后在 Observation 提交边界崩溃，接管 Worker 不产生第二个 Artifact Revision |
| 并发写 | 两个调用基于同一 base revision 写同一路径，只有一个成为当前版本，另一个收到可重试 conflict |
| Task 隔离 | 一个 Subagent 不能读取另一个 Task 未提交的 scratch 文件 |
| 发布 | publish 不复制字节，但生成可下载 Artifact Revision 和完整 derivation |
| rename | 路径改变后历史 Observation/Citation 仍解析到同一 Revision |
| 软删除与恢复 | Artifact 删除后默认不可见，保留期内可恢复且内容 hash 不变 |
| GC pin | work 路径已过期但 Revision 仍被 Evidence/Observation 引用时，Blob 不被清理 |
| ACL | 猜到 entry/revision/blob ID 的其他 Workspace 用户仍无法读取 |
| 原始内容保护 | Agent 创建同名 Artifact 或新版本不会覆盖 Attachment/DocumentVersion 的原始 Blob |

本地 `LocalObjectStore` 与真实 S3/兼容对象存储 Adapter 应分别验证。前者证明确定性领域行为；后者额外证明 conditional write、provider ETag/version、Lifecycle tag 和失败重试，不能用 mock 结果声称生产对象存储语义已经验证。

## 12. 留给后续决策票的具体参数

本 Research 票可以锁定上述语义，但以下参数仍需在 File Space 设计票中确定：

- 精确 Python interface 与表名
- `/work` grace period、Artifact 历史版本保留数和 Workspace quota
- 大文件分片上传与完整性校验协议
- web SourceSnapshot 正文继续存 PostgreSQL，还是大正文迁到 Blob 后保留数据库摘要
- `/artifacts` 的用户角色矩阵，以及 Agent 是否可对已发布 Artifact 执行 soft delete
- 文件浏览、版本历史、恢复和 provenance 的 UI 形态

## 参考资料

1. AWS, [Organizing objects using prefixes](https://docs.aws.amazon.com/AmazonS3/latest/userguide/using-prefixes.html)
2. AWS, [Retaining multiple versions of objects with S3 Versioning](https://docs.aws.amazon.com/AmazonS3/latest/userguide/Versioning.html)
3. Linux Kernel, [Overlay Filesystem](https://www.kernel.org/doc/html/latest/filesystems/overlayfs.html)
4. Turso AgentFS, 固定提交 `0a014ebd`, [README](https://github.com/tursodatabase/agentfs/blob/0a014ebd4918615baff589ed17486e557e7c6a23/README.md)；[Agent Filesystem Specification 0.4](https://github.com/tursodatabase/agentfs/blob/0a014ebd4918615baff589ed17486e557e7c6a23/SPEC.md)
5. OpenAI, [Code Interpreter](https://developers.openai.com/api/docs/guides/tools-code-interpreter)
6. E2B, 固定提交 `f5d702a5`, [`SandboxOpts` 与生命周期源码](https://github.com/e2b-dev/e2b/blob/f5d702a520de52ac0e5d4dda3ca0d5fca01d7993/packages/js-sdk/src/sandbox/sandboxApi.ts#L450-L550)；[`pause`、snapshot 与 timeout](https://github.com/e2b-dev/e2b/blob/f5d702a520de52ac0e5d4dda3ca0d5fca01d7993/packages/js-sdk/src/sandbox/sandboxApi.ts#L1187-L1337)
7. AWS, [Add preconditions to S3 operations with conditional requests](https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-requests.html)
8. PostgreSQL, [Explicit Locking — Row-Level Locks](https://www.postgresql.org/docs/current/explicit-locking.html#LOCKING-ROWS)
9. AWS, [Expiring objects](https://docs.aws.amazon.com/AmazonS3/latest/userguide/lifecycle-expire-general-considerations.html)
10. W3C, [PROV-DM: The PROV Data Model](https://www.w3.org/TR/prov-dm/)
11. AWS, [Controlling ownership of objects and disabling ACLs for your bucket](https://docs.aws.amazon.com/AmazonS3/latest/userguide/about-object-ownership.html)

## 本仓库核对位置

- [L1] [`storage.py`](../../apps/api/src/deep_researcher/storage.py#L12-L51)
- [L2] [`Attachment` 与 `DocumentVersion`](../../apps/api/src/deep_researcher/models.py#L400-L454)
- [L3] [`Artifact`](../../apps/api/src/deep_researcher/models.py#L974-L993)
- [L4] [`promote_attachment` 与文档版本更新](../../apps/api/src/deep_researcher/app.py#L2173-L2242)；[`next_version_number`](../../apps/api/src/deep_researcher/app.py#L2353-L2442)
- [L5] [`DockerSandbox.execute`](../../apps/api/src/deep_researcher/sandbox.py#L63-L149)
- [L6] [`run_sandbox_execution` 输出固化](../../apps/api/src/deep_researcher/app.py#L3325-L3454)
- [L7] [`SourceSnapshot`](../../apps/api/src/deep_researcher/models.py#L722-L740)
