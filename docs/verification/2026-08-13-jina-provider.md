# Jina Reader Provider 真实验证

**Ticket：** #10  
**验证日期：** 2026-08-13（Asia/Shanghai）  
**目标环境：** 开发机直连 Jina Hosted Reader 与 `https://example.com/`  
**凭证状态：** 未配置 `DEEP_RESEARCHER_JINA_READER_API_KEY`，使用 Jina 当前允许的匿名访问；未记录 token、cookie 或其他秘密

## 验证结论

- 真实 Jina 成功路径通过：公开网页正文先持久化为本地不可变 Source Snapshot/Source Chunk，再由 Citation 引用；Citation 没有直接引用 Provider response
- 真实 Provider failure 与 Local HTTP fallback 通过：把 Jina 客户端连接超时收紧到 `0.000001` 秒后，系统依次记录 `jina_reader=provider_unavailable` 与 `local_http=success`，最终 Citation 指向 Local HTTP 产生的 Snapshot
- 真实超预算路径通过：`X-Token-Budget: 1` 稳定返回 HTTP 409 `BudgetExceededError`，Adapter 归一为 `constraint_exhausted`，Web Acquisition 不执行 Local HTTP fallback
- 当前真实 response 与 Adapter 假设兼容，无需修改生产 Adapter；新增默认跳过的 live 集成测试，确定性 fake/contract 测试仍单独保留

本次结果只证明下述公开网页、请求模式和验证时点的契约，不代表所有站点、动态页面、PDF、区域网络或生产可靠性均已验证

## 实际契约

### 成功响应

- endpoint 类型：Jina Hosted Reader URL-prefix GET endpoint
- 请求模式：`GET https://r.jina.ai/https://example.com/`
- 关键请求头：`Accept: application/json`、`X-Preset: research`、`X-Markdown-Chunking: h3`、链接/图片/媒体保留策略；未发送 Authorization
- HTTP 状态：200
- response envelope：顶层 `code`、`status`、`data`、`meta`；`data` 是对象，包含 `content`、`title`、`url`、`chunks`、`usage`、`warning` 等字段
- 观察到的差异：本次 `status` 为 `200`，而官方示例可能显示 `20000`；本次 `data.contentType` 缺失，Adapter 使用 `text/markdown` 默认值
- 持久化证明：live Research Run 返回的 acquisition attempt 为 `jina_reader/success/selected_for_snapshot=true`，attempt content hash 等于 Source Snapshot content hash；Citation 的 `source_url` 为 `https://example.com/`，证据文本来自该 Snapshot

### Provider failure 与 Local HTTP fallback

- 真实失败方式：使用真实 Jina endpoint，但把 Jina `httpx.Client` timeout 设置为 `0.000001` 秒，使真实网络请求稳定触发超时
- 规范化类别：`provider_unavailable`，`retryable=true`，允许安全 fallback
- fallback 请求：Local HTTP Adapter 直接 `GET https://example.com/`
- 尝试顺序：`jina_reader=failed`，随后 `local_http=success`
- provenance：两次尝试分别持久化；只有 Local HTTP attempt 的 `selected_for_snapshot=true`，其 content hash 等于 Snapshot content hash，Citation 指向该本地 Snapshot

### 超预算与安全边界

- 请求模式：在成功请求基础上增加 `X-Token-Budget: 1`
- 真实响应：HTTP 409；envelope 含 `code=409`、`status=40904`、`name=BudgetExceededError`、`message`、`readableMessage`，`data=null`
- 规范化类别：`constraint_exhausted`
- 安全语义：只记录 Jina attempt，Local HTTP 未运行；规范化错误没有绕过运行约束
- 取消与 unsafe/disallowed URL：本 ticket 沿用确定性业务回归覆盖，因为真实 Provider 调用不能安全、稳定地制造这些本地域事件；它们同样属于 must-stop 类别，不能触发 fallback

## 可重复命令

```bash
DEEP_RESEARCHER_RUN_JINA_LIVE=1 uv run pytest \
  apps/api/tests/integration/test_jina_provider.py -q
```

2026-08-13 执行结果：Tests run `3`，passed `3`，failed `0`，skipped `0`

未设置 `DEEP_RESEARCHER_RUN_JINA_LIVE=1` 时，这三个测试默认跳过，避免把外部网络可用性混入确定性全量门禁

## 环境限制

- 没有真实 Jina token，因此未验证带 `Authorization: Bearer ...` 的有凭证配额、账户限流或付费能力
- 只验证一个公开静态 HTML 页面；没有验证登录页、付费墙、Cookie/Authorization 转发、浏览器自动化或 PDF
- 超时场景是客户端主动设置极短 timeout，不是对 Jina 服务可用性的判断
- 没有执行可靠性、吞吐、区域可用性或站点兼容性基准
