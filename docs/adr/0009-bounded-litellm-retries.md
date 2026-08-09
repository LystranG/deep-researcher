---
status: accepted
---

# LiteLLM 只启用有界暂时性错误重试

LiteLLM Adapter 对 timeout、429 和暂时性 5xx 启用最多两次的有限 retry；不启用 provider fallback，不重试取消、权限拒绝或结构化输出校验失败。首个用户可见 streaming delta 产生后不透明重放请求，避免重复回答和 Citation；每次 attempt 记录安全的 provider/model/错误类别元数据，业务取消和预算策略优先于 LiteLLM retry。
