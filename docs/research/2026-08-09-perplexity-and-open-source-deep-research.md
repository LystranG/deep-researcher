# Perplexity 与开源深度研究产品调研

调研日期：2026-08-09

## 结论

截图里的结果不是一次“深度研究”：当前实现虽然向 Brave 请求了 5 条搜索结果，但只持久化 `results[0]` 的搜索摘要，并将该摘要作为唯一网页证据和引用依据。它不会读取网页正文，也没有根据证据缺口发起第二轮查询。因此右侧当前只能显示搜索摘要和原始链接，不能展示“系统实际阅读的网页数据”。

Perplexity 没有公开完整的内部 Agent 图、排序模型或停止阈值，不能臆测其实现；但其公开 Deep Research 描述的是“数十次搜索、阅读数百个来源、综合报告”，与当前单摘要路径有本质差异。[1]

产品应把 Brave（或其他搜索提供方）定位为 URL 发现层，而不是证据层。最小正确闭环是：生成互补查询 -> 搜索多个候选 URL -> 筛选并抓取多个网页正文 -> 提取可引用片段 -> 识别覆盖/冲突缺口并补搜 -> 仅以已读正文和工作区材料写结论。

## 当前实现核查

`ResearchCoordinator._search_web()` 的实际行为是：

1. 调用 `web_search_gateway.search(query, count=5)`
2. 直接取 `results[0]`
3. 把该结果的 `snippet` 同时写入 `SourceSnapshot.content` 与 `SourceChunk.text`
4. 返回这一条 chunk；其余 4 条结果未持久化、未进入后续上下文

后续 `graph._run_citation_validator()` 又只取 `research_context.sources[0].text`。故即使未来搜索返回更多结果，只要该路径未重构，最终仍退化为单来源引用。

现有 HTTP 引用接口并非不存在：`GET /api/v1/messages/{message_id}/citations` 和 `GET /api/v1/citations/{citation_id}` 可返回 `source_url`、抓取时间、`evidence_text` 与内容 hash。但网页来源的 `evidence_text` 目前就是 Brave snippet；截图右侧的“打开来源网页”只是跳转外站，并不是本地网页正文阅读器。

## Perplexity 可公开确认的行为

### Deep Research、来源与追问

- Perplexity 官方发布文称 Deep Research 会执行数十次搜索、读取数百个来源，再自主生成报告。[1]
- 官方入门文档将可点击引用和自然语言追问作为对话体验的一部分。[2]
- Agent API 的对话状态文档说明，后续追问可通过历史消息重放或 `previous_response_id` 延续已完成的 response。这支持“会话、轮次、已读来源需要应用侧持久化”的设计，而不是把一次搜索当作无状态请求。[3]

### 搜索与正文读取是两个工具边界

- Perplexity Agent API `web_search` 由模型按指令调用；单次 `max_results` 范围为 1--50，结果含 `id`、`url`、`title`、`snippet`、日期等字段。官方特别将 `id` 与 `url` 作为引用的真实依据；回答中的 `[n]` 需由调用方提示模型显式生成。[4]
- `fetch_url` 在已知 URL 后提取网页内容，单次最多 10 页，并返回 URL、标题和提取片段；官方同时说明它不能保证绕过登录、付费墙或反爬，长内容可能截断。[5]
- 旧 Pro Search 工具文档给出了 Search -> Fetch URL -> 再 Search 核验的组合，并以 streaming `reasoning_steps` 表示工具进度；文档提示该能力正在迁移到 Agent API。[6]
- Search API 支持单个或多个 query、结果数、域名、语言、地区和内容提取控制，适合由本项目的受控编排器发起多查询，而不是让搜索 Adapter 暗含所有研究决策。[7]

可借鉴的是工具与用户体验边界，不是冒充 Perplexity 的内部实现：每一步应对用户可见，来源有稳定身份，搜索发现与正文读取分离，失败与内容截断也成为可见状态。

## 成熟开源项目对比

| 项目 | 多查询与迭代 | 网页正文与来源 | UI/引用 | 许可证 | 对本项目的价值 |
| --- | --- | --- | --- | --- | --- |
| Vane（原 Perplexica） | ReAct 循环：Speed 2、Balanced 6、Quality 25 轮；同轮 tool call 可并行 | 搜索工具接收 `queries[]`，去重；Quality 选最多 3 页抓取、分块提取事实 | 来源 block、来源卡片、行内引用 | MIT | 研究过程可视化、来源列表和多质量档 |
| GPT Researcher | breadth/depth 递归；并发子查询，空分支停止 | `visited_urls` 去重；并发 crawler；多抓取策略和来源筛选 | 研究进度、来源卡片、报告引用 | Apache-2.0 | 多查询、抓取、正文筛选、停止条件 |
| LangChain Open Deep Research | supervisor 反思后再派研究单元；可配最大迭代/工具次数 | 支持 Tavily、原生 web search、MCP；长内容压缩 | 主要由 LangGraph Studio/OAP 提供，不是可直接复用的产品 UI | MIT | 有界 Supervisor--Researcher 图与 checkpoint 思路 |

### Vane（原 Perplexica）

旧仓库 `ItzCrazyKns/Perplexica` 已重定向为 Vane，引用时应使用当前仓库。[8]

- `researcher/index.ts` 按模式限制 Agent--tool 循环为 2、6、25 轮，并在结束时聚合去重来源。[9]
- `baseSearch.ts` 接收 `queries[]`，并行搜索并进行相关性/相似性去重；Quality 模式会选择页面再抓取正文。[10]
- `scrapeURL.ts` 支持并发读取最多 3 个 URL，并按 4,000 字符、500 字符 overlap 分块抽取事实。[11]
- 前端的来源组件默认展示部分来源，并让用户展开查看全部；行内引用是独立组件。[12]

不能直接照抄其 TypeScript 模块；应保留其“受控循环 + 可见来源块”的交互思想，以项目自己的 Python Tool Run/Todo CAS 实现。

### GPT Researcher

- `DeepResearchSkill` 生成 breadth 个查询，用 `Semaphore` 和 `asyncio.gather` 控制并发，从当前层提取 learnings、citations、follow-up questions 后在 depth 内递归；同层都失败时停止。[13]
- `ResearchConductor` 收集多个 retriever 的 URL，以 `visited_urls` 去重，批量抓取正文。[14]
- 抓取器支持 browser、BeautifulSoup、PDF、arXiv、Tavily Extract、Firecrawl 等策略，并拒绝过短/空正文。[15]
- GPT Researcher 的官方许可证为 Apache-2.0；若直接复制代码，必须遵守 NOTICE、版权与许可证义务。本项目建议只自行实现等价设计。[16]

### LangChain Open Deep Research

- 其状态机采用 Supervisor--Researcher：Supervisor 找证据缺口并创建研究单元，研究单元执行受控 ReAct，再压缩和汇总结果。[17]
- 默认上限包括并发研究单元 5、Supervisor 研究迭代 6、单 researcher 工具调用 10、正文长度 50,000。这些是可审计的“停止原因”，很适合映射为本项目的 Run 预算与 Todo 状态。[18]
- 官方仓库许可证为 MIT。[19]

## 分阶段落地建议

### Slice A：先消灭单条 snippet 伪证据

目标：每个研究 Todo 的搜索结果都可见、可审计，但还不声称已读网页正文。

1. 将 `search_web` 的输出改为持久化全部候选结果（建议每 query 5--10 条），为每条保存 canonical URL、域名、标题、snippet、排名、查询、发现时间、内容 hash 和 `discovered` 状态
2. 一次 Run 由 planner 生成 3--5 个互补的 Search Todo，而非所有任务复用原问题。例如“官方说明/源码”“架构与运行循环”“限制、替代方案与反例”
3. 按 canonical URL 去重，并保证选择结果跨域名；页面明确区分“搜索摘要，未读正文”
4. SSE 增加 `search_started`、`search_results`、`source_discovered`，显示轮次、查询数、候选数与去重数

验收：针对截图中的问题，用户能看到多个查询、多个结果和为什么选择下一批 URL，而不是只有一个引用。

### Slice B：引入受限的网页正文抓取工具

目标：只把成功提取、持久化的正文片段纳入证据。

1. 新增 `web_page_fetch` Tool Run 和对应 Todo；Agent 根据相关性、来源多样性、预算和未覆盖子问题决定创建，不由 UI 手工写代码触发
2. 使用独立的受限 HTTP 提取 Adapter：仅允许 `http/https`、阻断私网/回环地址、限制重定向、响应大小、MIME、抓取超时和最大正文长度；不传宿主环境变量、Cookie 或凭证
3. 每次提取保存 `SourceSnapshot`（canonical URL、标题、抓取时间、状态、正文/截断标记、hash）和可定位 `SourceChunk`；robots、付费墙、超时、非文本、正文过短都保存为非秘密的失败状态
4. `SourceChunk` 采用 `canonical_url + content_hash + 版本` 的幂等键；Tool Run 用 Todo id 和输入 hash CAS。取消检查必须位于创建请求前、重定向后及写入前，取消后禁止写入新的 chunk、citation 或 Artifact

验收：点击来源可查看“本系统实际抓到的正文/片段、抓取时间、截断与失败原因”，另保留“打开原网页”链接；不能抓取时不编造结论。

### Slice C：有上限的研究循环与可解释停止

目标：研究结果由覆盖度而非第一条结果决定。

1. 每轮汇总每个子问题的可用正文来源数、域名多样性、冲突和空白；只有满足最低覆盖度才允许 writer
2. 未满足时由 Agent 创建 follow-up Search/Fetch Todo，至多 2--3 轮；Run 同时受查询数、抓取数、正文字符数、token/成本/时间预算约束
3. 终止事件必须含结构化原因：`coverage_satisfied`、`budget_exhausted`、`no_new_sources`、`all_fetches_failed` 或 `cancelled`。后四种不得生成伪装为完整研究的结论
4. 每一个事实/回答 span 与一个或多个 `SourceChunk` ID 绑定；Citation Validator 不得再读取 `sources[0]`

验收：用户能看到“第 N 轮，Q 个查询、S 个发现来源、R 页已读、哪些子问题尚未覆盖”，并可追溯每个结论。

### Slice D：来源与研究过程界面

目标：形成接近 Perplexity 的可核验体验，而非只提供外链。

1. 右侧 Evidence 面板按状态显示 `已发现`、`读取中`、`已读取`、`抓取失败` 和 `未采用`
2. 展示前 3 个已采用来源，提供“查看全部来源”；每个来源有标题、域名、抓取时间、正文摘要、被哪些结论引用、原网页外链
3. 点击行内引用打开本地 Source Viewer，显示持久化正文/片段和高亮的 evidence span；不要试图伪装为完整浏览器 DOM
4. 把 Todo 列表转为研究日志：查询、读取、补搜、跳过、失败、取消均有时间线和安全的原因

## 幂等、恢复与安全边界

| 对象 | 推荐幂等键 | 恢复行为 |
| --- | --- | --- |
| Search Tool Run | `todo_id + canonical_input_hash + attempt` | 同一成功 run 只投影一次候选来源，SSE 重放只读事件 |
| URL 发现记录 | `run_id + normalized_query + canonical_url` | 合并排名/查询来源，不重复创建 Fetch Todo |
| Fetch Tool Run | `todo_id + canonical_url + requested_extraction_version` | CAS 领取；已有成功同版本快照直接复用 |
| SourceSnapshot/Chunk | `canonical_url + content_hash + extraction_version` | 内容改变才产生新版本；引用固定到具体 hash |
| Citation | `message_id + answer_span + source_chunk_id` | writer 重试可去重；不依据网页当前内容重算历史引用 |

Worker lease 接管只重放未完成的 Todo/Tool Run；Graph checkpoint 恢复不得依据重复 SSE 再次发起 fetch。取消优先级高于任务派发和落库：已取消 Run 只能记录终止事件，不能产生新来源、Citation、Artifact 或结论。

## 参考资料

1. Perplexity, [Introducing Perplexity Deep Research](https://www.perplexity.ai/hub/blog/introducing-perplexity-deep-research)
2. Perplexity, [Getting started with Perplexity](https://www.perplexity.ai/hub/blog/getting-started-with-perplexity)
3. Perplexity API, [Conversation state](https://docs.perplexity.ai/docs/agent-api/conversation-state)
4. Perplexity API, [Web search tool](https://docs.perplexity.ai/docs/agent-api/tools/web-search)
5. Perplexity API, [Fetch URL content tool](https://docs.perplexity.ai/docs/agent-api/tools/fetch-url-content)
6. Perplexity API, [Pro Search tools](https://docs.perplexity.ai/docs/sonar/pro-search/tools)
7. Perplexity API, [Search API quickstart](https://docs.perplexity.ai/docs/search/quickstart)
8. Vane, [current repository](https://github.com/ItzCrazyKns/Vane) and [MIT license](https://github.com/ItzCrazyKns/Vane/blob/master/LICENSE)
9. Vane, [researcher loop](https://github.com/ItzCrazyKns/Vane/blob/master/src/lib/agents/search/researcher/index.ts)
10. Vane, [base search action](https://github.com/ItzCrazyKns/Vane/blob/master/src/lib/agents/search/researcher/actions/search/baseSearch.ts)
11. Vane, [URL scrape action](https://github.com/ItzCrazyKns/Vane/blob/master/src/lib/agents/search/researcher/actions/scrapeURL.ts)
12. Vane, [source list component](https://github.com/ItzCrazyKns/Vane/blob/master/src/components/MessageSources.tsx) and [citation component](https://github.com/ItzCrazyKns/Vane/blob/master/src/components/MessageRenderer/Citation.tsx)
13. GPT Researcher, [deep research skill](https://github.com/assafelovic/gpt-researcher/blob/main/gpt_researcher/skills/deep_research.py)
14. GPT Researcher, [research conductor](https://github.com/assafelovic/gpt-researcher/blob/main/gpt_researcher/skills/researcher.py)
15. GPT Researcher, [scraper](https://github.com/assafelovic/gpt-researcher/blob/main/gpt_researcher/scraper/scraper.py)
16. GPT Researcher, [Apache-2.0 license](https://github.com/assafelovic/gpt-researcher/blob/main/LICENSE)
17. LangChain, [Open Deep Research state machine](https://github.com/langchain-ai/open_deep_research/blob/main/src/open_deep_research/deep_researcher.py)
18. LangChain, [Open Deep Research configuration](https://github.com/langchain-ai/open_deep_research/blob/main/src/open_deep_research/configuration.py)
19. LangChain, [Open Deep Research MIT license](https://github.com/langchain-ai/open_deep_research/blob/main/LICENSE)
