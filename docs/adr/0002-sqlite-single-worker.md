# ADR 0002：SQLite 与单 worker 约束

- 状态：已接受（PR-03 持久化控制面与 PR-04A 单 Worker 骨架已实现）
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
- `JOB_RUNTIME=legacy|sqlite` 提供回滚边界：legacy 保持现有 FastAPI 子进程行为；sqlite 由 `python -m web_api.worker` 独立消费 queued job。
- SQLite 状态拆分为 execution、evaluation、quality 三个轴；所有状态更新经统一验证入口并使用 version 乐观锁。
- sqlite runtime 使用单一全局 lease，默认 `max_concurrency=1`；`worker_id` 只用于可读诊断，每个 Worker 实例另生成不经 API 暴露的 `instance_token`。lease 的获取、续租、释放和所有 Worker 写入都在同一 `BEGIN IMMEDIATE` 事务中同时校验 `worker_id`、`instance_token` 与过期时间；升级前没有 token 的 lease 仅能在过期后被接管。
- Worker 领取最早 queued job 前，在同一事务中确认不存在其他 running job，作为 `max_concurrency=1` 的数据库级保护。
- Worker 保存 `worker_id`、`launch_token`、PID、进程 create time、命令摘要和 heartbeat。Pipeline 位于独立进程组；取消前必须验证 PID/create time，身份不匹配时不发送终止信号并写入 `process_identity_mismatch`。
- API 取消只写入幂等 job command。Worker 先请求优雅退出，最多等待 10 秒，再终止完整进程树，确认进程树退出后才把任务置为 `canceled`。

## 后果

该选择符合 Windows 本地部署并降低运维成本，但不支持横向扩展或多 worker 高可用。FastAPI 与 Worker 生命周期解耦，因此 API 重启不影响正在运行的 sqlite Pipeline。PR-04A 只提供进程级 reconciliation：queued 保持排队，存活且身份匹配的进程继续监控，已死亡的 running job 以 `process_exited_without_checkpoint` 失败。阶段 checkpoint、阶段级恢复和重试仍属于后续工作。
