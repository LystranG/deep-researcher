---
status: accepted
---

# 首版 Agent 共享受控模型别名

Planner、Researcher、Verifier 和 Writer 首版共享同一个由服务端策略解析的 `model_alias`，但分别保留独立的 Agent Module、输入输出 schema、工具集合、权限、预算和生命周期。Planner/Verifier 使用结构化输出，Writer 使用 streaming；未来按 AgentRole 拆分模型只需扩展策略映射，不改变顶层 Graph 或领域接口。
