# ADR 0002：SQLite 与单 worker 约束

- 状态：已接受并已实现（PR-03 持久化控制面、PR-04A 进程级 Worker 与 PR-04B 受限阶段恢复）
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
- 本地 `Popen` 的自然退出优先于晚到的取消命令：Worker 先 `poll()`；exit 0 直接 completed，非零退出在无取消时先同步 checkpoint 并进入与重启后 confirmed-missing 相同的恢复资格判断，晚到 cancel 则失败为 `already_finished`、不得写 canceled 或启动恢复。取消窗口开始前必须续租；只有命令仍有效、身份匹配且树级退出已确认时才允许写入 `canceled`。
- Windows 强制终止以 `OpenProcess(QUERY_LIMITED_INFORMATION | TERMINATE | SYNCHRONIZE)` 获取句柄，并在同一句柄上校验 create time、确认 `STILL_ACTIVE`、调用 `TerminateProcess` 和有界等待；每个树成员重新打开并校验，PID 已复用则跳过，不使用 `taskkill /T /F`。POSIX 在 `killpg` 前重新校验根身份和 process group。
- 可归因于单个任务的预启动错误统一终结为 `process_launch_failed`，不终止 Worker 循环；数据库、schema、lease fencing、乐观冲突、登记后不安全状态和未知程序错误继续 fail fast。
- running job 缺少任一 PID/create time/process group 时，reconciliation 必须 fail closed：任务保持 running 并进入 `identity_unresolved`，不猜测、扫描或 kill 未知进程，也不启动后续任务。此时新取消请求返回 `process_identity_unresolved`；此前的 pending cancel command 明确失败为同一错误码，而不是伪装成 completed。
- sqlite Worker 启动 `codes/run_pipeline.py` 时启用可选 checkpoint adapter；legacy runtime 不传入 checkpoint 参数，既有直接执行和重启后不可接管语义不变。
- checkpoint 文件位于 `runs/<job_id>/checkpoints/`，使用固定版本、固定字段、固定阶段白名单、单调 sequence/attempt、64 KiB 上限和原子替换。SQLite 只记录阶段、attempt、恢复状态和 checkpoint 相对路径等最小元数据，不存储 artifacts、Prompt 或模型响应正文。
- 每个 completed checkpoint 记录从下一阶段继续所需的完整当前状态闭包：可信 Markdown、Planning 产物与原 TaskManifest、配置、分析结果，以及 coding/evaluation/repair 时当前全部 Manifest repo 成员和必要状态结果。TaskManifest 必须与 Planning 边界的 size/SHA-256 一致；允许合法变化的 repo 文件只复核最新边界的当前指纹，不复核已过时的历史 repo 版本。
- 读取时拒绝未知字段/版本/阶段、sequence 跳跃、冲突 stage、双 stage-1 分支、attempt/resume 不一致、无效 UTF-8/JSON、路径穿越、绝对路径、符号链接、junction/reparse point、硬链接、缺失文件、闭包不完整和指纹不匹配。仅 completed 边界可用于恢复；running、failed 或部分写入的当前阶段不能作为已完成证据，而是从上一个可信 completed 边界指向的阶段开头重跑。因此恢复是 stage-boundary at-least-once，可能重复 Provider 调用、MinerU、成本累计和阶段文件写入。
- 在线本地非零退出与 Worker 重启后发现 registered 进程确认死亡共用同一资格契约：只有 PID/create time/process group 已登记、当前 lease/fencing 有效、job 仍 running、原 launch token/launch state 匹配、无 pending/claimed cancel、完整闭包有效且预算可用时才恢复。默认恢复预算为 1；恢复计数在 SQLite 事务中持久化，每次恢复使用新 launch token，旧进程身份和真实可得的 exit code 复制到历史表。`identity_unresolved`、pending cancel、无合法 checkpoint、校验失败或预算耗尽都不会启动恢复进程。
- reconciliation 已按 PID/create time 确认登记进程死亡且已有 pending cancel 时，取消优先阻止恢复并在不发送 kill 的情况下完成 `canceled`；若取消在恢复事务完成但新 `Popen` 之前到达，Worker 会在启动前消费命令并安全取消该尚未 spawn 的新 attempt。
- 恢复准备、stage 元数据、进程历史和新 launch identity 更新均在 `BEGIN IMMEDIATE` 中验证当前 `worker_id + instance_token` lease 与原 launch token；旧 Worker 不能消费 checkpoint 或增加恢复次数。

## 后果

该选择符合 Windows 本地部署并降低运维成本，但不支持横向扩展或多 worker 高可用。FastAPI 与 Worker 生命周期解耦，因此 API 重启不影响正在运行的 sqlite Pipeline。PR-04B 只能从严格校验的 completed 阶段边界恢复一次，并不消除操作系统 spawn 与身份持久化窗口，也不提供 exactly-once、任意语句级恢复、自动重放或多 Worker 容错。checkpoint 可能因后续合法写入而失效，模型/外部工具调用也可能重复计费。`identity_unresolved`、恢复预算耗尽或非法/缺失 checkpoint 仍可能需要人工检查并重启本地环境。
