---
status: accepted
---

# 首版使用 LiteLLM SDK，不部署 Proxy 或 Router

模型接入采用进程内 LiteLLM SDK Adapter，由领域层先解析 Organization/Workspace/Agent role 的模型 allowlist 和内部 `model_alias`，再由 Adapter 注入 provider secret 并调用 LiteLLM。首版不部署 LiteLLM Proxy 或 Router，以避免重复的密钥、路由、配额和故障切换事实源；出现多个真实 provider、负载均衡或组织级路由需求时，再在同一个 Gateway seam 后增加 Router Adapter。
