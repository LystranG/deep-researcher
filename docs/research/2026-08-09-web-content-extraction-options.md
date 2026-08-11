# 网页正文提取能力与 Adapter 选型

调研日期：2026-08-09

## 范围与直接结论

本文只补充以下两份既有调研尚未展开的正文提取选型，不重复研究循环、覆盖裁决和引用账本设计：

- [`2026-08-09-perplexity-and-open-source-deep-research.md`](./2026-08-09-perplexity-and-open-source-deep-research.md)
- [`2026-08-09-recoverable-research-loop-and-evidence-ledger.md`](./2026-08-09-recoverable-research-loop-and-evidence-ledger.md)

直接结论：

1. **Brave Web Search 不返回网页详细正文**。其 Web Search response 的正文相关字段是 `description` 和可选的最多 5 个 `extra_snippets`；`fetched_content_timestamp` 只是时间元数据，不包含页面正文。[1]
2. **Brave LLM Context 返回真实页面中与查询相关的预提取内容，但不是完整网页快照**。它按一次 query 搜索并返回多个 URL 的 relevance-ranked smart chunks，可限制 URL、snippet 和 token 数；适合 grounding，不适合替代“按指定 URL 抓取、版本化、重放”的 Web Page Reader。[2]
3. **Brave Answers 是托管研究与答案产品，不是正文抓取接口**。Research mode 会迭代搜索并输出 queries、progress、blindspots、最终带引用答案和 usage，但不会把每个采用页面的完整正文作为稳定结果交给本项目。[3]
4. **当前第一纵向切片应该引入第二个正文提取 Adapter，但只作为 fallback**：保留现有受限 `HttpWebPageGateway` 处理静态 HTML；增加可选的 Jina Reader URL Reader Adapter 处理 JS 动态页和 PDF。不要让 Brave Answers、Firecrawl Agent 或其他托管 Agent 接管本项目的研究循环。
5. **暂不在第一切片自托管 Firecrawl 或 Crawl4AI**。两者能力更强，但会同时引入浏览器基础设施、代理、资源治理和更大的安全面。先记录 native reader 的失败类型和 fallback 命中率；只有动态页、扫描 PDF 或高摩擦站点成为主要覆盖缺口时再升级。

## 1. Brave 三种产品的准确边界

### 1.1 Web Search：URL 发现和摘要层

Brave 官方 Web Search skill 的 response schema 包含标题、URL、`description`、年龄/发布时间、语言、publisher、schema.org 数据、thumbnail 和可选 `extra_snippets`。其中 `extra_snippets=true` 也只是每个结果最多增加 5 个摘录；没有完整 HTML、完整正文或按页面定位的内容结构。[1]

因此当前 `BraveWebSearchGateway` 将 `web.results[].description` 映射为 `snippet` 是符合接口语义的。问题不在于 Brave 漏返回了一个“正文”字段，而在于应用若把 snippet 持久化成已读正文，就错误地提升了证据等级。

**可采纳决策**：Web Search 结果只能创建 `Source Discovery` 或 `search_snippet`，不得创建 `web_page` Evidence Span。`include_fetch_metadata`、`page_age` 和 rich schema 只增强候选排序与时效判断，不改变这个边界。

### 1.2 LLM Context：一次查询的相关正文片段层

Brave LLM Context 官方说明它不同于传统搜索摘要：会提取实际页面中的 text chunks、tables、code blocks 和 structured data，并按 URL 返回 `grounding.generic[].snippets`。[2]

它同时有明确的 query-oriented 边界：

- 每个请求只有一个 `q`
- `count` 表示参与候选的搜索结果数
- `maximum_number_of_urls`、总 token、每 URL token、总 snippets 和每 URL snippets 都有上限
- `context_threshold_mode` 决定相关性过滤强度
- 官方对比表把它定义为 single search；Answers 才是 multi-search

所以它返回的是“针对当前问题选出的正文片段集合”，不是“给定 URL 的可重放完整网页版本”。同一 URL 在不同 query、threshold 或 token budget 下可能得到不同片段。

**可采纳决策**：未来可以增加 `BraveLlmContextAdapter` 作为 query grounding Adapter，但其 canonical input hash 必须包含 query、Goggles、语言、threshold 和所有 token/URL 上限。返回内容仍要保存本地 SourceSnapshot/Chunk 和 hash；不能只保存 Provider URL。

### 1.3 Answers：托管答案与托管研究层

Brave Answers 使用 OpenAI-compatible `/res/v1/chat/completions`，分为：[3]

- 默认 single-search：由 Brave 生成答案，streaming 时可启用 citation tags
- `enable_research=true`：最多 1--5 次迭代、1--50 个总查询、1--300 秒时间预算，必须 streaming

Research mode 的流中可出现 `<queries>`、`<progress>`、`<blindspots>`、`<answer>` 和 `<usage>`；最终 answer 自带引用，usage 给出 requests、queries、input/output token 和成本分项。它很适合做外部对照评测，但查询规划、页面选择、停止和答案写作已经由托管层完成。

**可采纳决策**：Answers 不放入首版 Researcher 主路径。否则本项目无法保证所有搜索轮次、页面快照、Evidence Gap、停止原因和 Claim--Evidence 绑定都来自本地事实账本。可以把它作为独立 Benchmark Adapter，比较结果质量、耗时和成本，不能把其答案当作本地 Writer 已验证输出。

## 2. 当前本地正文 Reader 的能力与缺口

当前 [`web_page.py`](../../apps/api/src/deep_researcher/web_page.py) 已经是一个安全范围较清晰的静态 Reader：

- 只接受 `http/https`，阻断私网、回环、直接私网 IP，并在 Fake IP 环境用公共 DNS 二次校验
- 每次重定向后重新校验地址，最多处理有限重定向
- 只接受 `text/html` 和 `text/plain`
- 响应上限 1 MB、正文上限 50,000 字符、固定超时
- 用标准库 HTML parser 去除 script、style、noscript、SVG 和 template 文本

它的 locality 和安全性很好，适合静态文章、官方文档和简单页面；但不会执行 JavaScript，不读取 PDF，也没有 Readability/正文区域识别。对于 SPA、懒加载页面或复杂导航页，它可能只得到 shell、菜单和模板噪声。

现有 `FetchedWebPage` 还只有 `title/content/truncated`，不够支撑跨 Adapter 审计。无论引入哪个第三方，都应先把共同结果扩展为：

```text
requested_url, final_url, adapter_id, adapter_version, extraction_mode,
canonical_input_hash, fetched_at, http_status, content_type,
cache_state, warnings, error_code, retryable, truncated,
raw_content_hash, extracted_content_hash, title, content
```

合规允许时可以另外保存 raw HTML/PDF/MHTML Artifact；Citation 始终固定到本地 SourceSnapshot/Chunk，而不是第三方当前返回值。

## 3. 第三方正文提取候选对比

| 维度 | Firecrawl | Jina Reader | Crawl4AI |
| --- | --- | --- | --- |
| 静态 HTML | `/scrape` 输出 Markdown、clean HTML、raw HTML、JSON 等[4] | `curl-impersonate` 轻量读取，或 auto 选择 curl/browser[8] | 有不执行 JS 的 HTTP strategy，也有浏览器路径[11] |
| JS 动态页 | Cloud 处理 JS blocked 内容；支持 wait/click/write/scroll/actions[5] | Puppeteer/Chrome，支持 browser 强制、selector wait、timeout 和脚本注入[7][8] | Playwright，支持 JS、wait、session、滚动和多步交互[11] |
| PDF/文档 | URL 自动识别 PDF/DOCX；PDF 按页计 credit；Cloud 还提供 OCR 路径[4][5] | PDF.js；Office 经 LibreOffice；支持 URL 或文件上传，不是 OCR[7][8] | 专用 PDF strategy 提取文本、元数据、图片；官方明确扫描件默认无 OCR，复杂/加密 PDF 有局限[13] |
| 反爬 | Cloud 有代理与 enhanced 模式；默认自托管不含高级 Fire-engine[5][6] | Hosted 可切 browser、内部或自带 proxy；官方承认仍可能失败[7] | 自管 stealth、proxy、fallback，控制最多；成功率和代理质量也由部署者负责[11] |
| 付费墙/登录 | 可保留合法登录 profile，但没有绕过付费墙保证[5] | 可转发 cookie；带 cookie 的请求不缓存[7] | 支持持久 session、cookies 和表单交互[11] |
| 部署 | Cloud 最省运维；self-host 需要 PostgreSQL、Redis、RabbitMQ 等，Cloud 与 OSS 能力有差异[6] | Hosted 最低接入；OSS 提供单 Docker/无状态或 S3 cache，SaaS MongoDB 层不开放[7][8] | Python library 或 Docker，自行管理浏览器池、队列、认证、监控和资源[11][12] |
| 成本 | Cloud 基础 scrape 1 credit/page，PDF 1 credit/PDF page；enhanced、JSON 等额外计费[4] | Hosted 受 rate/token 计量策略约束；self-host 主要是 Chrome、LibreOffice 和存储成本[7][8] | 开源版无项目方按次费用；承担 CPU/RAM、浏览器、代理、可选 LLM 和运维成本[11] |
| 许可证 | 核心仓库 AGPL-3.0，自托管与修改须做许可证评估[9] | Apache-2.0；部分运行资产不可再分发，需要按官方脚本取得[7][10] | LICENSE 含 Apache 2.0 文本，并追加显著 attribution 要求，不能只按标准 Apache 标签理解[14] |
| 可审计性 | 可返回 source/final URL、status、raw HTML、cache metadata；Cloud 内部抓取路径仍由 Provider 执行[4] | JSON/Markdown 可保留 URL、title、content、warning，且可固定 engine/selector/timing；Hosted 内部执行仍是 Provider 事实[7][8] | 可捕获 raw/clean content、headers、错误、MHTML、网络与 console，控制和可观测性最强[11] |

### 3.1 Firecrawl

Firecrawl Cloud 是三者中最完整的托管网页数据产品：官方 Scrape 文档明确处理静态、JS-rendered、PDF 和图片，可输出 Markdown、raw HTML、screenshot 与结构化数据；actions 还能在提取前点击、滚动和等待。[4][5]

但其 Cloud 与 self-host 并非同一能力包。官方 self-host 指南明确：快速 Compose 基线没有生产级认证、TLS、持久化或 HA；默认 stack 有 fetch/Playwright，但高级 anti-bot Fire-engine、screenshots/actions 和一些 Cloud 功能需要额外服务或不可用。[6]

Firecrawl Cloud 的计费也最容易进入 Tool Budget：基础 scrape 通常为 1 credit/page，PDF 每页计费；enhanced proxy、JSON、PII redaction 等会增加 credits。[4] 核心仓库为 AGPL-3.0；若未来修改并以网络服务方式自托管，需要单独完成许可证义务评估。[9]

**适合场景**：动态站点和常规 Reader 持续失败、但团队暂时不愿维护浏览器/代理基础设施时，作为高摩擦 fallback。第一切片不应直接接 Firecrawl Agent，只考虑 URL scrape endpoint。

### 3.2 Jina Reader

Jina Reader 的 interface 与本项目所需 `WebPageGateway.fetch(url)` 最接近：给 `r.jina.ai` 一个 URL，返回 Markdown/HTML/text/JSON。官方开源分支同时公开了 headless Chrome、curl-impersonate、PDF.js 和 LibreOffice 路径；可以用 `x-engine`、selector、wait、timeout、token guardrail、cache policy 和 Markdown chunking 控制结果。[7][8]

它有 hosted 和 OSS Docker 两条路线。OSS 分支为 Apache-2.0，可无状态运行或使用 S3-compatible cache；SaaS 的 MongoDB 索引、rate limit 和部分内部 Provider 不在开源分支中。官方还注明镜像/构建依赖若干不可再分发资产，需要按脚本单独下载。[7][8][10]

Jina Hosted 支持内部/自带 proxy 和 cookie forwarding，但这不应成为本项目首版能力。转发用户 cookie 会扩大数据与授权边界；官方也没有承诺任何站点或付费墙必然可读。

**适合场景**：用最小开发量补上公开 JS 页面和公开 PDF，验证第二个 Reader Adapter 的真实 seam。上线前应重新核对实时 rate limit/定价；Hosted 只发送公开 URL，禁止 cookie、Authorization、Workspace 文档和私有网络地址。

### 3.3 Crawl4AI

Crawl4AI 提供最高的本地控制力：HTTP 或 Playwright 路径、JS、wait、session、proxy、Markdown filtering、原始内容、网络/console 记录和 PDF strategy 都可以应用侧配置，并能以 Python library 或 Docker 运行。[11][13]

但浏览器爬虫是高风险运行面。项目在 2026 年的安全版本中修复过 Docker API 的 RCE、SSRF、auth bypass、任意文件写入、XSS 和硬编码 JWT secret；v0.9 才转为 auth-on、loopback bind、声明式请求和 deny-by-default CORS。[12] 这说明它当前已有较认真安全设计，也说明不能把 crawler 直接嵌入 API/Worker 进程或暴露旧版默认配置。

LICENSE 文件在 Apache 2.0 文本后追加了显著 attribution 要求，采用前应按实际分发/公开使用方式评估，而不是只读取仓库 SPDX 标签。[14]

**适合场景**：私有部署、较高抓取量、必须保存浏览器级审计数据，且团队愿意运营独立受限 crawler 服务时。第一切片直接引入它会把研究循环与浏览器平台建设绑在一起，降低 locality。

## 4. 反爬、付费墙与安全边界

### 4.1 产品边界

三类工具都不能被表述为“保证绕过反爬或付费墙”。代理、browser、stealth、cookie 和 click 只能提高合法公开内容或用户已获授权内容的可读取率。

首版建议固定策略：

- 不接受或转发用户 cookie、Authorization header、浏览器 profile
- 不尝试绕过 CAPTCHA、登录、付费墙或 robots 明示的限制
- 将这些情况保存为 `access_restricted`、`bot_challenge` 或 `authentication_required`，不把失败解释成“页面没有信息”
- 只有未来建立明确的 Credential/Approval seam 后，才讨论用户授权内容读取

### 4.2 SSRF 与浏览器隔离

无论 Provider 自称有 SSRF 防护，本项目在创建 Tool Run 前仍要执行现有 public URL 校验，并在每次 redirect/final URL 后复核。Hosted Adapter 只允许公共 URL，不能发送包含本地 hostname、signed URL、token query 或用户身份信息的地址。

若未来自托管 browser reader：

- 独立容器/服务运行，不与 API、Worker 或 Python Sandbox 共进程
- deny-by-default egress，只开放解析后的公网目标和必要 DNS
- 非 root、只读根文件系统、临时 profile、CPU/RAM/PID/响应大小/时间限制
- 禁止 `file://`、下载落宿主路径、任意 JS/hook、任意 proxy 和客户端自带 launch args
- 网页内容按不可信数据处理，不允许页面文本直接变成 Tool 指令或权限变更

### 4.3 可审计结果契约

第三方 response 不是事实源。统一 Extractor Adapter 应把 Provider 结果转换成 Result Envelope，再由研究账本落不可变 SourceSnapshot：

```text
Fetch Tool Run
  -> requested/final URL + redirect chain
  -> adapter/version + canonical parameters
  -> status/MIME/cache/warning/error/retryability
  -> compliant raw artifact hash + extracted content hash
  -> SourceSnapshot/Chunk
  -> Evidence Span
```

相同 URL 经不同 Adapter 或版本读取时保留不同 snapshot，不能覆盖旧证据。Coverage Evaluator 只计算成功、未被 challenge/error warning 污染且可定位的 SourceChunk。

## 5. 当前第一纵向切片的明确建议

**建议采用“Native first，Jina Reader fallback”，暂不引入重型自托管 crawler。**

具体顺序：

1. 保留 `HttpWebPageGateway` 作为静态 HTML/text 默认 Adapter，并补齐 final URL、status、MIME、warning/error 和 extraction version
2. 将现有 interface 深化为统一 Extractor Adapter/Result Envelope；Provider-specific 参数留在 Adapter 后面
3. 增加可选 `JinaReaderWebPageAdapter`，只读取公开 URL，不传 cookie/proxy/任意脚本；仅在 native 返回正文过短、检测到 JS shell 或 MIME 为 PDF 时 fallback
4. Jina 结果必须落本地 SourceSnapshot/Chunk 和 content hash；Provider 缓存、warning、engine 与截断信息进入 Tool Run 审计字段
5. Brave Web Search 继续只做 URL discovery；Brave LLM Context 可在后续作为 query grounding 对照，不替代指定 URL Reader；Brave Answers 只做 benchmark
6. 记录 `native_success`、`fallback_success`、`dynamic_required`、`pdf_required`、`challenge`、`restricted` 和成本/延迟。若高摩擦失败已成为关键 Coverage Gap，再评估 Firecrawl Cloud URL scrape；若隐私和抓取量要求自托管，再单独建设 Crawl4AI/Reader OSS browser 服务

这个选择让第一切片真正形成两个 Adapter 的 seam，同时把研究循环、停止裁决、来源版本和 Citation 保留在本项目内部。它补足当前最明显的 JS/PDF 缺口，但不把一次 Agent 架构优化扩大成浏览器平台重构。

## 参考资料

1. Brave Search, [`web-search` 官方 skill，固定提交 `3e088af`](https://github.com/brave/brave-search-skills/blob/3e088af66eb61f1c207c22b2be0278ca8744d1d1/skills/web-search/SKILL.md)
2. Brave Search, [`llm-context` 官方 skill，固定提交 `3e088af`](https://github.com/brave/brave-search-skills/blob/3e088af66eb61f1c207c22b2be0278ca8744d1d1/skills/llm-context/SKILL.md)
3. Brave Search, [`answers` 官方 skill，固定提交 `3e088af`](https://github.com/brave/brave-search-skills/blob/3e088af66eb61f1c207c22b2be0278ca8744d1d1/skills/answers/SKILL.md)
4. Firecrawl Docs `7877f4c`, [Scrape 与计费](https://github.com/firecrawl/firecrawl-docs/blob/7877f4cf689e1c1f48c8dc9f2c15fa895d93c410/features/scrape.mdx)
5. Firecrawl Docs `7877f4c`, [Advanced scraping](https://github.com/firecrawl/firecrawl-docs/blob/7877f4cf689e1c1f48c8dc9f2c15fa895d93c410/advanced-scraping-guide.mdx)；[Enhanced mode](https://github.com/firecrawl/firecrawl-docs/blob/7877f4cf689e1c1f48c8dc9f2c15fa895d93c410/features/enhanced-mode.mdx)
6. Firecrawl Docs `7877f4c`, [Self-hosting](https://github.com/firecrawl/firecrawl-docs/blob/7877f4cf689e1c1f48c8dc9f2c15fa895d93c410/contributing/self-host.mdx)
7. Jina AI Reader `1574bfd`, [README 与 Reader API/自托管说明](https://github.com/jina-ai/reader/blob/1574bfd380d249c86c82db4dace0d9c8fe17e2b1/README.md)
8. Jina AI Reader `1574bfd`, [Architecture](https://github.com/jina-ai/reader/blob/1574bfd380d249c86c82db4dace0d9c8fe17e2b1/architecture.md)；[Cookbooks](https://github.com/jina-ai/reader/blob/1574bfd380d249c86c82db4dace0d9c8fe17e2b1/cookbooks.md)
9. Firecrawl `448ef4b`, [AGPL-3.0 LICENSE](https://github.com/firecrawl/firecrawl/blob/448ef4bf815d8df798d1a676f0303285e54cabdb/LICENSE)
10. Jina AI Reader `1574bfd`, [Apache-2.0 LICENSE](https://github.com/jina-ai/reader/blob/1574bfd380d249c86c82db4dace0d9c8fe17e2b1/LICENSE)
11. Crawl4AI `7e80152`, [README 与能力/部署](https://github.com/unclecode/crawl4ai/blob/7e801521428ee12509994d39151006f64055ebe3/README.md)；[HTTP strategy](https://github.com/unclecode/crawl4ai/blob/7e801521428ee12509994d39151006f64055ebe3/docs/md_v2/assets/llm.txt/txt/http_based_crawler_strategy.txt)；[Page interaction](https://github.com/unclecode/crawl4ai/blob/7e801521428ee12509994d39151006f64055ebe3/docs/md_v2/core/page-interaction.md)
12. Crawl4AI `7e80152`, [v0.9 secure-by-default Docker release](https://github.com/unclecode/crawl4ai/blob/7e801521428ee12509994d39151006f64055ebe3/docs/blog/release-v0.9.0.md)；[v0.8.7 security hardening](https://github.com/unclecode/crawl4ai/blob/7e801521428ee12509994d39151006f64055ebe3/docs/blog/release-v0.8.7.md)
13. Crawl4AI `7e80152`, [PDF processing strategies](https://github.com/unclecode/crawl4ai/blob/7e801521428ee12509994d39151006f64055ebe3/docs/md_v2/advanced/pdf-parsing.md)
14. Crawl4AI `7e80152`, [LICENSE 与追加 attribution](https://github.com/unclecode/crawl4ai/blob/7e801521428ee12509994d39151006f64055ebe3/LICENSE)
