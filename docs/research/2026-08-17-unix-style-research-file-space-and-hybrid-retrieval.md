# Research File Space 的 Unix 风格检索与混合召回调研

调研日期：2026-08-17

## 调研范围

本文为 Issue 15 的 Q7 提供决策准备，只研究 Research File Space 首版 Tool Interface，不修改 Agent Runtime，也不提前决定 FileEntry、FileRevision、Blob、目录项和垃圾回收的具体表结构。

本文复用而不重开以下既有结论：

- `Research File Space` 是代理访问只读知识来源、当前 Research Run 工作文件和持久研究产物的统一视图，不是宿主目录或对象存储的别名。[正式术语](../../CONTEXT.md)
- 路径、可变目录项、不可变 Revision 和 Blob 必须分离；`/sources`、`/work`、`/artifacts` 的生命周期、ACL、版本和 provenance 以 Issue 22 调研为基线。[Issue 22 调研](2026-08-15-agent-virtual-filesystem-lifecycle.md)
- Python Sandbox 只能消费服务端解析并固定的 Revision，输出先进入 staging，再摄取为 Work Revision/Artifact 和 Tool Observation。[Issue 24 调研](2026-08-15-ai-python-sandbox-async-recovery.md)
- Workspace 检索已经确定为 ACL 前置的 PostgreSQL FTS/BM25 + pgvector 精确向量召回，经 RRF、Rerank 和 token budget 后回读不可变原文；相似度和 rerank 分数本身不能成为 Citation。[检索 Spec](../specs/research-ledger-and-workspace-retrieval.md)、[ADR 0012](../adr/0012-workspace-hybrid-retrieval-and-cross-conversation-records.md)

本次新增回答：是否应让 AI 像 Unix 一样使用 `grep`、`sed`、`find`、`head`、`tail`；这些能力应表现为原生 shell、受限命令 DSL，还是结构化文件 Tool；它们如何与向量/关键词召回互补；首版是否需要 `move/delete`；以及是否需要独立 prototype。

## 结论摘要

1. **需要 Unix 风格的能力，但不应把 shell 或宿主命令行作为 Research File Space 的模型合同。** 首版提供结构化 `file_list/file_stat/file_read/file_grep/file_write/file_publish`，并把既有混合检索作为独立 `file_search`/Retrieval Tool 与它们组合。`grep/find/head/tail` 的常用研究行为均可由这些 Tool 覆盖；`sed` 的只读切片由 `file_read` 覆盖，内容变换交给产生新 Revision 的 `file_write` 或 Python Sandbox。
2. **AgentScope 2.0 的正式发布版给出了很强的设计证据。** v2.0.6 的 Workspace 同时装配 `Bash/Edit/Glob/Grep/Read/Write`，但 Bash 的模型说明明确要求优先使用专用 Tool，不要用 `find/grep/cat/head/tail/sed/awk`；其 `Grep` 使用 argv 直接调用 ripgrep，不经 shell，并设置 30 秒超时、500 列上限、默认 250 条结果和 offset。[AgentScope Workspace](https://github.com/agentscope-ai/agentscope/blob/29b592358c2e983a0d10dd5227316b7a02d8c23a/src/agentscope/workspace/_base.py#L362-L390)、[Bash](https://github.com/agentscope-ai/agentscope/blob/29b592358c2e983a0d10dd5227316b7a02d8c23a/src/agentscope/tool/_builtin/_bash.py#L27-L48)、[Grep](https://github.com/agentscope-ai/agentscope/blob/29b592358c2e983a0d10dd5227316b7a02d8c23a/src/agentscope/tool/_builtin/_grep.py#L27-L55)、[Grep 执行](https://github.com/agentscope-ai/agentscope/blob/29b592358c2e983a0d10dd5227316b7a02d8c23a/src/agentscope/tool/_builtin/_grep.py#L276-L300)
3. **AgentScope 的模式可借鉴，业务模型不可照搬。** AgentScope 的 Tool 面向真实/沙箱文件系统路径；deep-researcher 必须额外保存 Workspace/Run/Task capability、固定 Revision、content hash、稳定 locator 和 provenance，不能让容器路径成为授权或 Citation 身份。
4. **首版不向模型暴露 `move/delete`。** `/sources` 永远不可改；`/work` 的过期清理由系统生命周期负责；`/artifacts` 的删除属于用户/策略治理。研究代理通过写新 Revision、换新路径或 publish 已足够完成首版工作。`move/delete` 会提前引入双目录并发、冲突、tombstone/restore、审计和 GC 语义，收益不足。
5. **建议启动一个独立 `$prototype` 会话，但只验证模型行为和 Tool schema，不实现生产 File Space。** 技术上执行 ripgrep 没有悬念；真正未知的是模型是否会在概念查询、精确字符串和混合问题间正确选择 `file_search` 与 `file_grep/file_read`，以及结构化 Tool 是否覆盖真实研究中的 Unix 管道习惯。

## 一手资料核对

### 1. AgentScope 2.0：保留 Bash，但把文件操作做成专用 Tool

截至调研日，AgentScope 最新正式发布版为 **v2.0.6（2026-08-07，commit `29b5923`）**。[Release](https://github.com/agentscope-ai/agentscope/releases/tag/v2.0.6)

它的 `WorkspaceBase.list_tools()` 为同一 Workspace backend 装配六个 Tool：`Bash`、`Edit`、`Glob`、`Grep`、`Read`、`Write`。因此 Local、Docker、E2B 等 backend 可以复用同一模型 Tool surface。[Workspace source](https://github.com/agentscope-ai/agentscope/blob/29b592358c2e983a0d10dd5227316b7a02d8c23a/src/agentscope/workspace/_base.py#L362-L390)

但 AgentScope 并没有鼓励模型把所有文件行为拼成 Bash：

- Bash 的 tool description 要求文件名查找用 `Glob`，内容查找用 `Grep`，读取用 `Read`，编辑用 `Edit`，写入用 `Write`；只有专用 Tool 无法完成或用户明确要求时才退回 Bash。[Bash source](https://github.com/agentscope-ai/agentscope/blob/29b592358c2e983a0d10dd5227316b7a02d8c23a/src/agentscope/tool/_builtin/_bash.py#L27-L48)
- `Grep` 是独立、只读、并发安全的 Tool，schema 明确表达 pattern、path、输出模式、glob/type、上下文、大小写、multiline、limit 和 offset。[Grep schema](https://github.com/agentscope-ai/agentscope/blob/29b592358c2e983a0d10dd5227316b7a02d8c23a/src/agentscope/tool/_builtin/_grep.py#L39-L151)
- `Grep` 构造 `['rg', *args, search_path]` argv，经 backend 直接执行而不启动 shell；固定 30 秒 timeout，并在调用侧加入 `--max-columns 500`、默认 250 条结果和 offset。[Grep execution](https://github.com/agentscope-ai/agentscope/blob/29b592358c2e983a0d10dd5227316b7a02d8c23a/src/agentscope/tool/_builtin/_grep.py#L259-L300)、[Grep limits](https://github.com/agentscope-ai/agentscope/blob/29b592358c2e983a0d10dd5227316b7a02d8c23a/src/agentscope/tool/_builtin/_grep.py#L358-L485)
- `Read` 使用 offset/limit 读取，默认和最大均为 2000 行，并限制单行进入模型的字符数；这相当于结构化 `head/tail/sed -n`，无需让模型构造命令管道。[Read source](https://github.com/agentscope-ai/agentscope/blob/29b592358c2e983a0d10dd5227316b7a02d8c23a/src/agentscope/tool/_builtin/_read.py#L27-L95)

这支持本项目采用“**通用执行环境可以存在，但核心文件检索优先暴露专用结构化 Tool**”的方向。AgentScope 仍把绝对文件路径交给模型，结果也不是本项目的 `file_revision_id + content_hash + locator`，所以只能参考交互和执行方式，不能直接成为 Research File Space 的业务合同。

### 2. AgentScope Runtime：旧 FilesystemSandbox 不是内容 grep

AgentScope Runtime main 在调研日固定于 commit `22072fd`（2026-06-04）。其 `FilesystemSandbox` 暴露 read/write/edit/list/tree/move/search/info 等方法，同时继承 BaseSandbox 的 `run_shell_command`。[FilesystemSandbox](https://github.com/agentscope-ai/agentscope-runtime/blob/22072fd7075ce0c6f43cb39509d6a14b0e60ddb5/src/agentscope_runtime/sandbox/box/filesystem/filesystem_sandbox.py)、[BaseSandbox](https://github.com/agentscope-ai/agentscope-runtime/blob/22072fd7075ce0c6f43cb39509d6a14b0e60ddb5/src/agentscope_runtime/sandbox/box/base/base_sandbox.py)

需要注意版本边界：该镜像固定 `@modelcontextprotocol/server-filesystem@2025.3.28`，只把 `/workspace` 配为允许目录。[AgentScope Runtime MCP config](https://github.com/agentscope-ai/agentscope-runtime/blob/22072fd7075ce0c6f43cb39509d6a14b0e60ddb5/src/agentscope_runtime/sandbox/box/filesystem/box/mcp_server_configs.json)

这个固定版本的 `search_files` 是对**文件和目录名**做递归、大小写不敏感的子串匹配，并返回完整路径，不搜索文件内容，也没有分页/输出上限；`read_file` 则一次返回完整 UTF-8 文本。[MCP filesystem 2025.3.28 source](https://github.com/modelcontextprotocol/servers/blob/e5ed5bff23c01fab9976b7e3cc9446411b9a23ce/src/filesystem/index.ts#L57-L90)、[search implementation](https://github.com/modelcontextprotocol/servers/blob/e5ed5bff23c01fab9976b7e3cc9446411b9a23ce/src/filesystem/index.ts#L186-L224)、[tool list](https://github.com/modelcontextprotocol/servers/blob/e5ed5bff23c01fab9976b7e3cc9446411b9a23ce/src/filesystem/index.ts#L336-L432)

当前 MCP Filesystem Server main 已增加 `read_text_file(head|tail)`、media read、Tool annotations 和 MCP Roots 动态目录，但 `search_files` 仍主要是路径 glob/名称搜索，而不是内容 grep。[当前 README，commit `76d64c8`](https://github.com/modelcontextprotocol/servers/blob/76d64c822f5125032f89eb71dbdb94e42b434821/src/filesystem/README.md)

因此不能把“AgentScope 有 FilesystemSandbox”推导成“它已经提供了带版本、provenance、分页和内容 grep 的 Research File Space”。它证明的只是：通用 shell和结构化文件 API 可以在隔离 backend 中并存。

### 3. E2B：Sandbox SDK 同样把命令与文件 API 分开

E2B Python SDK main 快照 `f5d702a`（2026-08-13）同时提供：

- `commands.run(cmd, background, envs, user, cwd, stdout/stderr callback, timeout)`；`cmd` 是字符串，适合在 sandbox 内执行任意命令。[Commands source](https://github.com/e2b-dev/e2b/blob/f5d702a520de52ac0e5d4dda3ca0d5fca01d7993/packages/python-sdk/e2b/sandbox_sync/commands/command.py#L182-L210)
- `files.read/write/list/get_info/rename/remove` 等独立文件 API，并为传输提供 request timeout/streaming 能力。[Filesystem source](https://github.com/e2b-dev/e2b/blob/f5d702a520de52ac0e5d4dda3ca0d5fca01d7993/packages/python-sdk/e2b/sandbox_sync/filesystem/filesystem.py)

这说明成熟 Sandbox SDK 通常允许宿主应用选择“任意命令”或“结构化文件操作”。但 SDK 解决的是隔离环境 I/O，不会替 deep-researcher 保存 Workspace ACL、Run mount generation、Revision/hash、Evidence Span 或 publish provenance；E2B sandbox path/ID 不能成为本项目的文件业务主键。

### 4. ripgrep 与 shell 的实现约束

ripgrep 15.2.0（2026-07-15）是递归、面向行的正则内容搜索工具，默认跳过 hidden、ignore 和检测到 NUL 的 binary 文件；默认 regex engine 基于有限自动机，PCRE2 是显式 opt-in。[ripgrep README](https://github.com/BurntSushi/ripgrep/blob/15.2.0/README.md)、[Guide](https://github.com/BurntSushi/ripgrep/blob/15.2.0/GUIDE.md#automatic-filtering)

若使用 ripgrep 作为 `file_grep` 的内部 Adapter，首版必须固定默认 engine，不允许模型启用 PCRE2、preprocessor、压缩解码、任意 type definition 或用户配置文件；只传经过 schema 校验的 argv。Python 官方 `subprocess` 文档也明确指出：不启动 shell 时，shell metacharacter 可以作为普通参数安全传给子进程；显式 `shell=True` 后，应用必须自行正确引用空白和 metacharacter 以避免 shell injection。[Python 3.14 subprocess security](https://docs.python.org/3.14/library/subprocess.html#security-considerations)

## 三种暴露方式比较

| 方案 | 表达力 | 权限与审计 | 输出与恢复 | 结论 |
| --- | --- | --- | --- | --- |
| 原生 shell command string | 最高，可用管道、重定向、命令替换和任意二进制 | 很难从字符串可靠判断真实读写集合；路径和命令注入面大 | stdout/stderr 缺少稳定 File/Revision locator；截断后难分页恢复 | 不作为 File Space Tool；仅保留在单独、耐久、隔离的 Python/Shell Sandbox 风险域 |
| 受限 Unix 命令 DSL | 可模仿 `grep/find/head/tail/sed` | 如果支持通用 pipe、重定向、短路、变量或命令替换，很快变成另一个 shell parser | 可以结构化，但 DSL 版本、组合爆炸和错误语义成本高 | 不定义通用 mini-shell；只把常用意图做成固定字段的 Tool schema |
| 结构化文件 Tool | 能覆盖研究检索、精读、写 Work 和发布 Artifact | Registry 可标记 read-only/side effect；Policy 可按 scope、operation、Revision 和预算检查 | 返回 typed Observation、cursor、Revision/hash/locator，容易恢复和 Citation 回读 | **首版推荐** |

关键不是“后端绝不运行命令”，而是“**模型不提交命令语言**”。`file_grep` 的 Adapter 可以在受控 Worker 内以 argv 运行固定 digest 的 ripgrep，也可以以后替换为数据库/库内实现；Tool schema 和业务 Observation 不应泄漏该实现。

## 首版推荐 Tool Interface

### 1. 检索与读取

| Tool | 核心输入 | 核心输出 | 允许范围 | 分类 |
| --- | --- | --- | --- | --- |
| `file_list` | `scope`、相对逻辑前缀、`glob?`、`recursive?`、`cursor?`、`limit` | `entry_id`、当前 `revision_id`、逻辑路径、media type、size、hash、next cursor | `/sources`、当前 Task `/work`、`/artifacts` | 短同步、只读、可并行 |
| `file_stat` | `entry_id` 或 `revision_id` | 类型、size、media type、hash、current/stale 状态、provenance 摘要 | 同上 | 短同步、只读、可并行 |
| `file_read` | **固定 `revision_id`**；`start_line + line_count` 或 `tail_lines`，二者互斥 | 带行/字节 locator 的文本块、hash、truncated、next cursor | 同上；必须通过当前 capability | 短同步、只读、可并行 |
| `file_grep` | `scope/revision_ids`、`pattern`、`literal|regex`、case、glob/media type、before/after、cursor、limit | 结构化 match：`revision_id + hash + line/byte locator + preview` | 同上；调用开始时冻结候选 Revision manifest | 有界只读、可并行 |
| `file_search` | 自然语言 query、scope、filters、top-k/token budget | 混合召回候选：固定 Revision/chunk locator、分数与检索 receipt | 已通过 Workspace/Run ACL 且已索引的知识来源/发布产物；mutable work 默认不进入长期索引 | 有界只读；复用既有 Retrieval seam 的合同 |

这里的 `scope` 是模型可选的业务范围，不是授权。`workspace_id/run_id/task_id/caller/tool grants` 必须由 `TaskClaim`/执行上下文注入，模型不得提交或覆盖。逻辑路径只用于选择；真正读取和返回均以服务端解析出的 `entry_id/revision_id/hash` 为准。

Unix 常用操作映射如下：

| Unix 意图 | 首版 Tool 表达 |
| --- | --- |
| `find /sources -name '*.md'` | `file_list(scope='sources', glob='**/*.md', recursive=true)` |
| `grep -n -C 3 PATTERN ...` | `file_grep(pattern, before=3, after=3, ...)` |
| `grep ... \| head -n 20` | `file_grep(..., limit=20)` |
| `head -n N` | `file_read(revision_id, start_line=1, line_count=N)` |
| `tail -n N` | `file_read(revision_id, tail_lines=N)` |
| `sed -n 'M,Np'` | `file_read(revision_id, start_line=M, line_count=N-M+1)` |
| `sed -i`/复杂 awk 变换 | 读取固定 Revision 后以 `file_write` 产生新 Work Revision，或交给 Python Sandbox |

不提供通用 pipe。模型以多个 Tool Call 组合；每一步都形成可审计 Observation，后一步显式消费前一步的稳定 ID/cursor。

### 2. 写入与发布

| Tool | 核心合同 | 范围与副作用 |
| --- | --- | --- |
| `file_write` | 输入 Task 内相对逻辑路径、`content|blob_ref`、`expected_revision_id` 或 `create_only`；成功总是创建新不可变 Revision并返回 hash | **只允许当前 Task 的 `/work/tasks/{task_id}`**；写操作、串行、幂等 key + optimistic CAS；不得覆盖 `/sources` 或直接写 `/artifacts` |
| `file_publish` | 输入稳定 `work_revision_id`、Artifact 目标名/路径、`expected_artifact_revision_id?`；创建 Artifact Revision 并记录 `wasDerivedFrom` | `/work` -> `/artifacts`；引用同一 Blob 而非目录移动；失败不删除 Work Revision，可按同一逻辑 Tool Call 幂等重试 |

`file_write` 的“更新”语义是同一 Entry 上追加 Revision，不是原地改 Blob。并发冲突必须返回当前 revision/generation，让 Agent 重新读后决定换路径、合并或放弃，不能 last-write-wins。

`file_publish` 是业务发布而不是 `mv`：源 Work Revision 保持可恢复，Artifact 获得独立身份和 provenance；数据库事务或 staging finalize 失败后可以安全重试。这与 Issue 22 已确定的路径/Revision/Blob 分离一致。

## `/sources`、`/work`、`/artifacts` 首版权限矩阵

| 操作 | `/sources` | 当前 Task `/work` | `/artifacts` |
| --- | --- | --- | --- |
| list/stat/read | 允许，按 Run mount 固定 Revision | 允许，仅 Task owner | 允许，按 Workspace ACL |
| grep/search | 允许，仅冻结的来源 Revision | 允许，仅本 Task 已提交 Revision | 允许，按 Workspace ACL |
| write/new revision | 拒绝 `read_only_source` | 允许，必须 CAS | 不直接允许 |
| publish | 不能作为目标；source Revision 可作为 Python 输入 | 允许作为 publish 源 | 仅 `file_publish` 可创建/产生新 Artifact Revision |
| move | 首版不暴露 | 首版不暴露 | 首版不暴露 |
| delete | 永远不允许 Agent 删除来源 | 首版不暴露；Run 生命周期清理 | 首版不暴露；由用户/策略治理 |

`/work/shared` 不在首版模型写入面内。Subagent 共享通过 Research Ledger、明确 publish/提交的 Revision 或 Artifact 完成，避免并发 Task 互相读取 mutable scratch。

## 为什么首版不提供 `move/delete`

### `move`

研究任务需要的是“以新名字保存结果”和“发布产物”，分别可由 `file_write(new_path, ...)` 与 `file_publish(...)` 完成。真正的 move 需要同时处理源 Entry、目标父目录、名称冲突、跨 scope 限制、多 Entry 锁顺序、历史路径展示和恢复语义。它不会增加证据检索能力，却会扩大写入合同。

### `delete`

- `/sources` 的内容可能是 Citation/审计依据，Agent 永远无删除权。
- `/work` 是 Run 内 scratch，过期和 GC 属于系统生命周期，不需要模型逐文件清理。
- `/artifacts` 是用户可见持久产物；删除、恢复和保留策略属于用户/Workspace governance，不应让研究模型在首版自行决定。

后续只有出现真实、重复的研究行为证据时才单独增加 `file_move` 或 `file_soft_delete`，并沿用 Issue 22 的 generation/CAS、tombstone/restore 和 provenance pin 合同。物理 Blob 删除永远不是 Agent Tool。

## 命令与检索安全语义

若 `file_grep` 内部采用 ripgrep Adapter，至少执行以下合同：

1. **不经 shell**：固定 executable/digest，以 argv 传 pattern 和路径；模型不能提交 flags 或完整 command string。文件名以 `-` 开头也不会变成 flag，Adapter 在固定参数后使用明确的 pattern 参数和 `--`/受控根。
2. **固定 Revision manifest**：执行前按注入 capability 解析候选，生成 `logical_path -> revision_id/hash/materialized_path` manifest；只把这些 Revision 物化到一次调用的只读临时目录。返回时反向映射为稳定业务 ID，绝不返回 host/container path。
3. **路径边界**：逻辑路径做 Unicode/分隔符规范化，拒绝 NUL、`.`/`..` 穿越、绝对宿主路径、symlink/hardlink、device/socket/FIFO；知道路径、Blob key 或 Revision ID 均不构成授权。
4. **regex 边界**：默认 `literal`；显式 `regex` 只使用 ripgrep 默认 engine，限制 pattern bytes、捕获/多行规模，不启用 PCRE2、preprocessor、用户配置、任意 `--type-add` 或压缩外部程序。
5. **资源边界**：限制候选文件数、单文件/总输入字节、max file size、wall-clock/CPU/memory、match 数、context 行、单行字符、Observation 总字节；超限返回 typed `truncated/resource_limit` 和 cursor，而不是静默伪装完整结果。
6. **文本与二进制**：首版 grep 只搜索允许 media type 的规范化文本/解析文本 Revision；PDF、Office、图片等先由受控 parser 形成可定位文本快照。原始二进制返回 `unsupported_media_type`，不允许 `--text` 把任意 bytes 塞入模型。
7. **无外网、最小权限**：Worker/容器非 root、root filesystem 只读、无网络、固定镜像、无宿主 socket/credential；`/sources` materialization 只读，临时空间调用结束后可清理。
8. **结构化输出**：Adapter 应消费 JSON/机器可解析输出或库 API，按 manifest 重建 `revision_id/hash/line|byte locator/preview`；stderr 和 exit code 只进入有界 receipt，不成为知识证据。

## 与混合检索的互补调用序列

### 概念问题

```text
file_search(自然语言问题)
  -> ACL 前置的 FTS + vector exact
  -> RRF + rerank + token budget
  -> 候选 Revision/chunk locator
  -> file_read(固定 Revision 的精确邻域)
  -> Evidence Span / Citation
```

向量/FTS 擅长同义表达、主题关联和“不知道关键词在哪”的导航。最终仍必须回读不可变原文。

### 已知精确 token、日期、错误码或原句

```text
file_grep(literal/受限 regex, scope 或候选 Revision)
  -> 结构化 matches + cursor
  -> file_read(命中上下文)
  -> Evidence Span / Citation
```

grep 擅长可解释、低歧义的精确定位，尤其适合标识符、错误文本、版本号、日期和查漏。grep preview 仍只是 navigation lead；只有固定 Revision 上回读出的精确 span 才能进入 Claim/Citation。

### 混合问题

优先 `file_search` 找概念相关文件，再在候选 Revision 上 `file_grep` 验证专有词、否定语句或数值；如果用户已给出精确 quote，则顺序可反转。不要规定一条永远固定的管线，应该由 Tool description 提示模型按 query 类型选择，同时由 prototype 验证选择质量。

## 与 Registry / Policy / Execution 的接合

- **Registry**：保存稳定 Tool identity、schema/version、read-only/side-effect/parallel-safe、结果 schema 和 Adapter binding。`file_grep` 的 Tool identity 不应因底层从 ripgrep 换成数据库实现而改变；语义改变才产生新 definition version。
- **Policy**：从 TaskClaim 注入 Workspace/Run/Task/caller grants；检查 scope、operation、Revision 可见性、预算、文件数/字节和风险级别。模型参数中的 path/scope 只能缩小授权，不能扩大授权。
- **Execution**：冻结 Revision manifest、执行有界 Adapter、持久化 Tool Observation 和 receipt；写操作使用 idempotency key + expected revision/generation；大型输出外置，仅把 preview、cursor 和稳定引用送回模型。

只读且候选 Revision 不重叠也无共享写的 `file_list/stat/read/grep/search` 可受控并行；`file_write/publish` 串行并在执行前整批完成 schema、grant、预算和冲突预检。

## 是否需要 prototype

**建议需要，且应创建独立 prototype 会话/票据；本次不实现。**

### 要验证的最小假设

1. 模型在概念、精确和混合查询中能稳定选择 `file_search`、`file_grep`、`file_read`，不会为了熟悉度反复尝试不存在的 shell。
2. `file_list + file_grep + file_read` 的 schema 足以覆盖真实研究中的 `find/grep/head/tail/sed -n`，无需首版通用 pipe DSL。
3. 结构化 Observation 的 cursor、truncated、Revision/hash/locator 足以让模型从截断恢复，并形成正确 Evidence Span。
4. 将 grep 限定为 fixed argv、只读 materialization 和稳定 Revision 映射后，仍能达到可接受的检索延迟与 token 成本。

### 最小实验

- 使用一个一次性 fixture corpus，不接生产数据库：至少覆盖纯文本、长行、大文件、重复文件名、Unicode 路径、二进制、被撤销 Revision 和路径穿越样本。
- 准备 24 个任务：8 个精确 token/日期/错误码，8 个概念/同义表达，8 个“先语义找文件、再精确核对”的混合任务。
- A 组仅混合检索；B 组混合检索 + 结构化 `list/grep/read`；C 组可在完全只读、无网络 sandbox 内把 raw shell 作为**评估上界**，但它不是候选生产接口。
- 使用计划中的至少两个实际 provider/model；不要用单一模型结果锁定通用 schema。

### 建议验收指标

| 指标 | 建议门槛 |
| --- | --- |
| 精确/混合任务 evidence-locator 成功率 | B 组至少 90%，且比 A 组提高至少 10 个百分点 |
| 纯概念任务成功率 | B 组相对 A 组下降不超过 5 个百分点 |
| Revision/hash/line locator 可复现 | 100% 命中实际固定 Revision，路径 rename 后仍可解析 |
| 越权、`..`、symlink、binary 强制文本、mutation 尝试 | 100% 被拒绝并返回正确 typed error |
| 截断恢复 | 所有超限 case 都给 cursor/truncated，模型能继续读取而不丢命中 |
| Tool 选择 | 至少 85% 的任务首个检索 Tool 与题型匹配；记录无效调用和修正轮数 |
| 成本 | 记录 tool calls、模型输入/输出 token、Observation bytes、p50/p95 latency；B 不应靠无限输出换取成功率 |

这些数值是 prototype 的**建议产品门槛**，不是外部资料中的行业标准。若 B 未达到门槛，应先调整 tool descriptions、schema 和结果形状；只有出现结构化 Tool 无法表达的重复任务，才考虑扩大 DSL，不应直接开放 shell。

## 版本边界与未验证项

- AgentScope 结论基于 v2.0.6 源码（2026-08-07）；AgentScope Runtime 是另一仓库，核对的是 main commit `22072fd`（2026-06-04）及其固定 MCP Filesystem Server `2025.3.28`。两者的 Tool surface 不应混为同一版本。
- MCP Filesystem Server 当前 main commit `76d64c8`（2026-07-29）相较 AgentScope Runtime 的固定版本已有 head/tail、media、annotations、Roots 等漂移；本文没有建议直接接入该 MCP server。
- E2B 只核对 SDK 源码，没有创建真实 sandbox，也没有验证供应商多租户隔离或传输性能。
- 没有运行 AgentScope、ripgrep 或任何真实 provider/model；模型 Tool 选择、token 成本和 corpus 性能必须由建议的 prototype 验证。
- 既有 Retrieval seam 已验证 Workspace 文档/记录的混合召回方向，不等于所有 FileRevision 已接入索引；`/sources`/发布 Artifact 的索引生命周期和 mutable `/work` 是否仅用 grep，仍需后续 File Space 设计决定。
- PDF/Office/图片 OCR 的 locator 与原始 Revision 映射、超大文件分片、具体 cursor 编码、配额和保留期仍属于后续 File Space 设计票。
- `file_grep` 最终由受控 ripgrep materialization、数据库正则/FTS，还是独立搜索服务实现尚未决定；首版应先固定业务 Tool contract，再由 prototype 比较 Adapter。

## 对 Q7 的推荐决议草案

1. Research File Space 首版采用结构化 Tool，不向模型暴露宿主 shell、原生 `grep/sed/find` 或通用 Unix pipe DSL。
2. 首版文件 Tool 为 `file_list`、`file_stat`、`file_read`、`file_grep`、`file_write`、`file_publish`；既有混合 Retrieval 以独立 `file_search` 与其组合。
3. `/sources` 只读；`file_write` 仅能写当前 Task 的 `/work` 并产生新 Revision；`file_publish` 以稳定 Work Revision 创建 Artifact Revision 和 provenance，不做目录移动。
4. 首版不暴露 `move/delete/mkdir/edit`；目录可由逻辑路径隐式表达，编辑通过读固定 Revision 后写新 Revision，复杂变换交给 Python Sandbox。
5. grep Adapter 若使用 ripgrep，必须 fixed argv/no-shell、默认 regex engine、只读 Revision materialization、无网络和资源/输出上限，返回稳定 Revision/hash/locator/cursor。
6. grep/search 只负责导航；最终 Evidence Span/Citation 必须回读不可变 Revision 原文。
7. 在锁定 schema 前启动独立 prototype，按本文假设、对照组和指标验证至少两个实际模型；prototype 不修改生产 Runtime。
