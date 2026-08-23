# Research File Space 的 Unix 风格检索工具调研

日期：2026-08-17

## 范围

本文回答 Wayfinder Research 票“调研 Research File Space 的 Unix 风格检索工具与原型必要性”：在向量检索之外，是否应让 AI 使用类似 `grep`、`sed`、`find`、`head`、`tail` 的能力；如果需要，应直接开放 shell、使用受控领域 Tool，还是交给 Python Sandbox。

本文只做调研和架构分析，不修改生产 Agent Runtime，也没有执行模型行为原型。

## 结论

1. **有必要补充确定性的词法与路径检索**。向量检索擅长语义相关性，但不能可靠替代精确字符串、正则、文件名、路径、行范围、版本或 hash 定位。两者应是互补 Tool，而不是二选一。
2. **不应向 Model 暴露任意宿主 shell，也不应直接暴露 `grep/sed/find` 的原始 argv**。Shell parsing、路径穿越、symlink、配置文件、环境变量、输出爆炸和 `sed` 的写文件/执行扩展会把安全策略泄漏到 prompt。模型需要的是 Unix 式组合能力，不是宿主命令执行权。
3. **首版应提供四个领域 Tool**：`file_list`、`file_search`、`file_read`、`file_write`，另以 `file_publish` 把 `/work` Revision 发布为 `/artifacts`。`file_search` 可在 Adapter 内使用 ripgrep，但 Interface 只暴露受控字段；`file_read` 的行/字节窗口覆盖 `sed -n`、`head` 和 `tail` 的主要研究用途。
4. **`file_search` 推荐使用 ripgrep CLI 的 JSON Lines 输出作为首个 Adapter**。ripgrep 跨平台、速度成熟，并提供 `--json`、`--no-config`、glob、最大文件大小、每文件最大匹配数、最大列宽等约束。调用必须使用 argv 数组且不经过 shell，固定 `--no-config`，清理环境，只搜索已物化的 snapshot root。[1][2]
5. **Research File Space 的业务事实仍是 PostgreSQL `FileEntry/FileRevision/Blob`**。Worker 可以把固定 Revision 物化为短生命周期的只读目录供 ripgrep 搜索，但返回值必须重新映射为 `file_revision_id + content_hash + line/byte locator`，不能把宿主路径当作权限或长期 locator。
6. **复杂管道与任意文本变换交给 Python Sandbox**。当任务确实需要 `grep | sed | awk | jq` 式多阶段处理时，AI 应生成 Python 或调用 Sandbox 镜像中固定的工具，通过只读 `/sources` 和可写 `/work` mount 执行；仍不提供宿主 shell Tool。
7. **值得启动一个小型 prototype**。技术可行性没有悬念，但“模型是否能在向量检索、词法搜索、范围读取与 Sandbox 之间稳定选对工具”以及 Tool schema 是否易用，无法只靠文档确定。Prototype 应比较三种 Tool 菜单在固定研究任务集上的行为，不实现生产 VFS。

## 为什么向量检索不够

以下请求天然需要确定性检索：

- 查找精确 API 名、错误码、变量、订单号或引用 ID
- 查找所有包含某个正则模式的文件和行
- 按路径、扩展名、目录或文件名筛选
- 读取命中行前后文、指定行范围、文件头尾
- 核对同一短语在不同版本中的所有出现位置
- 在向量检索返回片段后，回到完整文件确认上下文和精确 locator

推荐的检索漏斗：

```text
未知主题/自然语言问题
  -> knowledge_retrieve 向量 + 词法混合召回
  -> file_search 精确词法/正则/路径缩小范围
  -> file_read 按稳定 Revision 和行范围重读
  -> Evidence Span / Derived Evidence
```

反向也成立：用户给出精确错误码时，可以先 `file_search`，命中不足再使用向量检索扩展同义概念。

## 不直接开放 Unix 命令的原因

### `grep`/ripgrep

读取本身风险较低，但原始 CLI 仍有配置、环境、路径和资源问题：

- ripgrep 默认读取 ignore 规则，也可以读取 `RIPGREP_CONFIG_PATH`
- glob、hidden、follow symlink、binary、preprocessor 与压缩搜索会改变可见范围或启动外部程序
- 未限制的正则、文件大小、结果数量与行长度会耗尽 CPU、内存或模型上下文
- 直接输出宿主路径会绕开 Workspace/Run/Task ACL 和 Revision provenance

因此 Adapter 必须固定 flags 白名单，而不是把模型字符串拼接到命令行。

### `sed`

研究中常见的 `sed -n '10,30p'` 本质是范围读取，`file_read(start_line, end_line)` 更安全、更结构化。完整 sed 还包含原地编辑、读写文件及实现相关的执行扩展，不适合作为只读检索 Tool。

### `find`

常见需求可由 `file_list(path, glob, max_depth, cursor)` 覆盖。直接 find 表达式包含执行动作、任意路径和复杂布尔语义，不值得成为 Model Interface。

### shell pipeline

Pipeline 很有表达力，但同时引入 quoting、subshell、重定向、环境变量、可执行文件发现、退出码组合和部分输出问题。若必须执行，应进入已有 Python Sandbox 的隔离、资源、mount、Attempt 和耐久恢复协议。

## 推荐 Tool Interface

### `file_list`

```python
FileListRequest(
    root: Literal["sources", "work", "artifacts"],
    path: str,
    glob: str | None,
    max_depth: int,
    cursor: str | None,
    limit: int,
)
```

返回稳定 entry/revision ID、逻辑路径、kind、size、media type 和分页游标。不返回 storage key 或 host path。

### `file_search`

```python
FileSearchRequest(
    snapshot_ref: UUID,
    query: str,
    mode: Literal["literal", "regex"],
    paths: tuple[str, ...],
    include_globs: tuple[str, ...],
    exclude_globs: tuple[str, ...],
    case_sensitive: bool,
    context_before: int,
    context_after: int,
    cursor: str | None,
    result_limit: int,
    context_budget: int,
)
```

返回：

```python
FileSearchPage(
    matches: tuple[FileMatch, ...],
    cursor: str | None,
    completeness: Literal["complete", "truncated", "budget_exhausted"],
)
```

每个 `FileMatch` 至少包含 `file_revision_id`、content hash、logical path、line/byte range、match spans 和有界 context。Observation 保存 snapshot、规范化 query、预算和匹配引用。

### `file_read`

```python
FileReadRequest(
    file_revision_id: UUID,
    start_line: int | None,
    end_line: int | None,
    tail_lines: int | None,
    context_budget: int,
)
```

这一个 Tool 覆盖 `sed -n`、`head` 与 `tail` 的主要读取语义。必须读取指定不可变 Revision；逻辑路径的当前版本发生变化也不能改写本次结果。

### `file_write` 与 `file_publish`

- `file_write` 只允许 `/work`，创建不可变 Revision，要求 `expected_generation`
- `file_publish` 从 `/work` 的固定 Revision 创建 `/artifacts` Revision
- `/sources` 永远只读
- 首版不提供 `move/delete`，避免路径冲突、tombstone 与 GC 语义扩散到本票

## ripgrep Adapter 的安全合同

建议使用类似以下固定参数，不把这段命令暴露给模型：

```text
rg --json --no-config --color never --no-messages
   --max-filesize <policy>
   --max-count <policy>
   --max-columns <policy>
   --glob <validated-glob> ...
   -- <validated-pattern> <materialized-snapshot-root>
```

还必须做到：

- 使用 argv 数组，`shell=False`
- 显式传入唯一 snapshot root，禁止 `..`、绝对路径与 unresolved symlink
- 不启用 `--follow`、`--pre`、`--search-zip` 或读取用户配置
- 清理 `RIPGREP_CONFIG_PATH` 和非必要环境变量
- wall-clock、CPU、进程内存、输出字节、文件数和总扫描字节均设上限
- 逐行解析 `--json`，不依赖人类文本格式
- 截断时返回显式 completeness/cursor，不伪装完整
- 将 materialized path 反查到 `FileRevision`，校验内容 hash 后再形成 match

ripgrep 官方说明 `--json` 会输出 begin/match/context/end/summary JSON Lines；`--no-config` 禁止读取 `RIPGREP_CONFIG_PATH`；`--max-filesize`、`--max-count` 和 `--max-columns` 可限制扫描与输出。[1][2]

## 是否直接使用官方 MCP Filesystem Server

官方参考 Filesystem Server 提供 read、head/tail、multiple read、write、edit、list、move、search 和 tree，并允许通过 roots 或启动参数限制目录；Docker 示例也展示了只读 mount。[3]

它适合作为产品交互和安全语义的参考，但不建议直接成为内部 Research File Space：

- 它以真实路径为身份，本项目以 `FileEntry/FileRevision/Blob` 为事实
- 它的 `search_files` 主要搜索路径 glob，不等同于内容正则检索
- write/edit/move 直接作用于目录，无法自动形成本项目 Revision、provenance 和 Observation
- annotations 仍只是提示，不能替代 Workspace/Task Policy

可以在 prototype 中把它作为“通用文件 MCP”对照组，但生产内置工具应直接对接 VFS Module。

## Prototype 建议

### 要回答的唯一问题

在相同任务预算下，哪种 Tool 菜单能让模型以最少失败和最少无效上下文，稳定取得可引用的完整证据？

### 三个对照组

1. 仅 `knowledge_retrieve` + `source_window`
2. 增加结构化 `file_list/file_search/file_read`
3. 增加一个隔离的通用命令 Tool（只作为对照，不作为预设生产方向）

### 固定任务集

- 精确错误码跨文件查找
- 同义概念语义检索后回到原文核对
- 正则查找全部定义并读取上下文
- 大文件头尾与指定行范围读取
- 多版本文件中定位旧 Revision 的证据
- 对 `/sources` 的写入诱导和路径穿越攻击
- 输出爆炸、超时和二进制文件

### 观察指标

- 是否找到全部预置 gold matches
- 是否引用正确 Revision 和 locator
- 模型回合数、Tool Call 数、输入/输出 token
- schema-invalid、越权和重复调用次数
- 截断后是否正确翻页或缩小查询
- 是否错误选择 Sandbox 或尝试宿主路径

Prototype 只需临时目录、少量冻结文档、脚本化/真实模型 Adapter 与内存 Observation，不需要生产表、完整 VFS 或 UI。若结构化工具组明显优于向量-only 且不劣于通用命令组，即可确认生产 Interface；若模型频繁无法表达查询，再调整 schema，而不是直接开放 shell。

## 推荐决策

- Q7 不采用“让 AI 直接调用宿主 grep/sed/find”
- 采用“向量/混合检索 + 结构化 Unix 式文件 Tool + Python Sandbox”的三层能力
- 首版开放 `file_list/file_search/file_read/file_write/file_publish`
- `file_search` 首个 Adapter 使用受控 ripgrep JSON；未来可替换为库或索引实现，不改变 Tool Interface
- 建议在 Q7 最终定案前创建一个 HITL prototype 票，验证模型 Tool 选择和 schema，而不是验证 shell 能否执行

## 来源

1. [ripgrep 官方仓库与 README](https://github.com/BurntSushi/ripgrep)
2. [ripgrep `rg --help` 文档](https://github.com/BurntSushi/ripgrep/blob/master/doc/rg.1.txt)
3. [MCP 官方 Filesystem Server](https://github.com/modelcontextprotocol/servers/tree/main/src/filesystem)
4. [MCP 2025-11-25 Tools specification](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)

## 本地证据

- [Agent 虚拟文件系统、版本与生命周期调研](./2026-08-15-agent-virtual-filesystem-lifecycle.md)
- [source_reader.py](../../apps/api/src/deep_researcher/source_reader.py)
- [retrieval.py](../../apps/api/src/deep_researcher/retrieval.py)
- [sandbox.py](../../apps/api/src/deep_researcher/sandbox.py)
