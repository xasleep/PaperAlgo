# ADR 0002：SQLite 与单 worker 约束

- 状态：已接受（方向性决策，PR-00 未实现）
- 日期：2026-07-14

## 背景

当前 checkout 没有数据库：settings 位于 `.local/web_settings.json`，任务状态和摘要位于 `runs/<job_id>/`。FastAPI 还在内存中保存活跃子进程句柄，服务重启后不能重新接管进程。

后续若需要可查询的任务元数据，本地单用户场景不需要 PostgreSQL、Redis 或分布式协调。与此同时，多个 Uvicorn worker 会各自持有不一致的进程登记，破坏取消和状态语义。

## 决策

- 后续元数据持久化如被实现，选择仓库本地 SQLite，而不是外部数据库服务。
- FastAPI 始终以单进程、单 worker 运行；SQLite 访问和子进程登记都服从这一约束。
- 大型 artifacts、日志和生成仓库继续保存在 `runs/<job_id>/`，不写入数据库 BLOB。
- API key、完整 Prompt 和完整模型响应不得写入 SQLite；数据库只保存控制面所需的最小、脱敏元数据。
- PR-00 只记录决策，不创建 schema、迁移或数据库代码。

## 后果

该选择符合 Windows 本地部署并降低运维成本，但不支持横向扩展或多 worker 高可用。真正引入 SQLite 时必须用测试定义事务、迁移、崩溃恢复和文件权限行为。
