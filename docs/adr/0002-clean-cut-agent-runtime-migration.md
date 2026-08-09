---
status: accepted
---

# Agent Runtime 采用开发期 clean cut 迁移

当前系统仍处于未被真实用户使用的开发阶段，现有数据库内容和旧的 queued/running Research Run 不要求保留、续跑或转换。LangGraph/LiteLLM 重构采用 clean cut：保留已经验证的公共 HTTP、SSE、Workspace ACL、Citation、Memory 和 Skill 行为，但不建立 Legacy Runner、旧新双轨、execution-kind 路由或旧 checkpoint 兼容层；测试数据和开发数据库可在实施阶段按明确命令重新初始化。这样减少只为一次性迁移存在的 Adapter、字段和分支，避免长期维护两套运行时。
