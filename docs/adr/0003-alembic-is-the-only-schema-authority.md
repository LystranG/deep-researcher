---
status: accepted
---

# Alembic 是唯一数据库 Schema 权威

项目尚无生产数据，允许在 LangGraph/LiteLLM 重构期间将现有 Alembic revision 整理为一个包含新运行时 Schema 的干净初始基线，并重新初始化开发数据库。应用启动不得再调用 `Base.metadata.create_all()`；本地、测试、Compose 和后续生产环境统一通过 `alembic upgrade head` 建库或升级。这样消除 ORM 自动建表与迁移历史形成的双重 Schema 入口，避免旧 Compose volume 已出现的 `DuplicateTable` 问题继续成为运行时技术债。
