# ADR 0002：SQLite 与单 worker 约束

- 状态：已接受并已实现（PR-03 持久化控制面与 PR-04A 进程级 Worker）
- 日期：2026-07-22

## 背景

PR-03 之前没有任务数据库：settings 位于 `.local/web_settings.json`，任务状态和摘要位于 `runs/<job_id>/`。legacy runtime 仍在 FastAPI 内存中保存活跃子进程句柄，服务重启后不能重新接管原进程；sqlite runtime 已由独立 Worker 保存控制面和进程身份元数据。

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
- Worker 保存 `worker_id`、`launch_token`、launch state、PID、进程 create time、命令摘要和 heartbeat。launch state 依次表达 `claimed`、`registered`、`identity_unresolved` 和 `exited`；它不是阶段 checkpoint。
- Pipeline 位于独立进程组；取消前必须验证 PID/create time，身份不匹配时不发送终止信号并写入 `process_identity_mismatch`。
- API 取消只写入幂等 job command。Worker 先请求优雅退出，最多等待 10 秒，再终止完整进程树，确认进程树退出后才把任务置为 `canceled`。
- 本地 `Popen` 的自然退出优先于晚到的取消命令：Worker 先 `poll()` 并按真实退出码完成任务，取消命令在同一终态事务中失败为 `already_finished`。取消窗口开始前必须续租；只有命令仍有效、身份匹配且树级退出已确认时才允许写入 `canceled`。
- Windows 强制终止以 `OpenProcess(QUERY_LIMITED_INFORMATION | TERMINATE | SYNCHRONIZE)` 获取句柄，并在同一句柄上校验 create time、确认 `STILL_ACTIVE`、调用 `TerminateProcess` 和有界等待；每个树成员重新打开并校验，PID 已复用则跳过，不使用 `taskkill /T /F`。POSIX 在 `killpg` 前重新校验根身份和 process group。
- 可归因于单个任务的预启动错误统一终结为 `process_launch_failed`，不终止 Worker 循环；数据库、schema、lease fencing、乐观冲突、登记后不安全状态和未知程序错误继续 fail fast。
- running job 缺少任一 PID/create time/process group 时，reconciliation 必须 fail closed：任务保持 running 并进入 `identity_unresolved`，不猜测、扫描或 kill 未知进程，也不启动后续任务。此时新取消请求返回 `process_identity_unresolved`；此前的 pending cancel command 明确失败为同一错误码，而不是伪装成 completed。

## 后果

该选择符合 Windows 本地部署并降低运维成本，但不支持横向扩展或多 worker 高可用。FastAPI 与 Worker 生命周期解耦，因此 API 重启不影响正在运行的 sqlite Pipeline。PR-04A 只提供进程级 reconciliation：queued 保持排队，存活且身份匹配的进程继续监控，已死亡的 running job 以 `process_exited_without_checkpoint` 失败，身份未登记完整的 running job 则隔离并持续占用唯一执行槽。阶段 checkpoint、阶段级恢复和重试仍属于后续工作；`identity_unresolved` 可能需要人工检查并重启本地环境，不能宣称已实现任意崩溃点自动恢复。
