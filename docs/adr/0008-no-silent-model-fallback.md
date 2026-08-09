---
status: accepted
---

# LiteLLM provider 失败时直接暴露失败状态

当运行明确选择 LiteLLM provider 后，timeout、限流、provider 错误或结构化输出失败直接映射为任务/Run 的失败或部分失败状态，不自动静默切换到 Extractive Adapter。Extractive Adapter 仅作为无凭证开发模式下显式选择的模型 Adapter。这样避免把模型不可用伪装成看似成功的回答，也保持失败、重试和审计语义简单可见。
