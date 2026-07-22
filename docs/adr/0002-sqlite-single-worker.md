# ADR 0002：SQLite 与单 worker 约束

- 状态：已接受（PR-03 已实现持久化控制面，完整 Worker 尚未实现）
- 日期：2026-07-14

## 背景

当前 checkout 没有数据库：settings 位于 `.local/web_settings.json`，任务状态和摘要位于 `runs/<job_id>/`。FastAPI 还在内存中保存活跃子进程句柄，服务重启后不能重新接管进程。

后续若需要可查询的任务元数据，本地单用户场景不需要 PostgreSQL、Redis 或分布式协调。与此同时，多个 Uvicorn worker 会各自持有不一致的进程登记，破坏取消和状态语义。

## 决策

- 控制面元数据使用仓库本地 SQLite，而不是外部数据库服务；默认路径为 `.local/paper2code.db`，可由 `PAPER2CODE_DB_PATH` 显式覆盖。
- FastAPI 始终以单进程、单 worker 运行；SQLite 访问和子进程登记都服从这一约束。
- 大型 artifacts、日志和生成仓库继续保存在 `runs/<job_id>/`，不写入数据库 BLOB。
- API key、完整 Prompt 和完整模型响应不得写入 SQLite；数据库只保存控制面所需的最小、脱敏元数据。
- 使用标准库 `sqlite3`、编号 migration、WAL、foreign keys 和 busy timeout，不引入 ORM。
- `JOB_RUNTIME=legacy|sqlite` 提供过渡边界：legacy 保持现有子进程行为；sqlite 只创建 queued job，不直接启动 Pipeline。
- SQLite 状态拆分为 execution、evaluation、quality 三个轴；所有状态更新经统一验证入口并使用 version 乐观锁。

## 后果

该选择符合 Windows 本地部署并降低运维成本，但不支持横向扩展或多 worker 高可用。PR-03 仅建立持久化事实源、幂等创建和未来 Worker 所需的表；lease 获取、命令消费、阶段执行、恢复与完整 Worker 生命周期仍属于后续工作。
