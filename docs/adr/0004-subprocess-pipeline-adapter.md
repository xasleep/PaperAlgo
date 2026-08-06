# ADR 0004：使用子进程适配现有 Pipeline

- 状态：已接受并已实现
- 日期：2026-07-22

## 背景

`codes/run_pipeline.py` 和现有阶段脚本已经定义论文解析、规划、分析、编码、评测和修复流程。Web 控制面需要启动、观察和取消任务，但不应复制或替换执行内核。

## 决策

- `JOB_RUNTIME=legacy`（默认）由 `web_api/job_service.py` 在 FastAPI 进程中通过 `subprocess.Popen` 调用 `codes/run_pipeline.py`；活跃进程句柄只存在于该 FastAPI 实例的内存中。
- `JOB_RUNTIME=sqlite` 由显式启动的 `python -m web_api.worker` 独立进程领取任务并通过 `subprocess.Popen` 调用同一个执行内核；FastAPI 只写控制面记录和幂等取消命令，不持有或终止 SQLite Pipeline。
- FastAPI 负责校验 Web 输入、保存上传和公开 artifacts；legacy launcher 或 SQLite Worker 负责构造参数与最小环境。Pipeline 继续负责阶段编排和运行状态文件。
- 每个任务使用 `runs/<job_id>/` 隔离状态、日志、结果和生成仓库。
- 两种 runtime 都把 Pipeline 放入独立进程组。SQLite Worker 持久化 launch state、PID、create time、process group、命令摘要和 heartbeat，并在终止前验证进程身份；不引入 Celery、Redis、LangGraph 或远程 worker。
- PR-04B 只在 SQLite Worker 构造的命令中显式启用 `run_pipeline.py` checkpoint 参数。CLI 与默认 legacy launcher 不传这些参数，原 Prompt、生成算法、阶段脚本和直接执行语义保持不变。
- 子进程失败必须保留非零退出和明确失败状态，不能通过捕获所有异常或返回空结果掩盖。SQLite Worker 对在线非零退出与重启后确认死亡的 registered 进程使用同一恢复契约；exit 0 不恢复，晚到 cancel 不得覆盖自然退出或启动恢复。

## 后果

CLI、legacy Web runtime 与 SQLite Worker 共享同一执行内核，降低行为漂移。legacy 的进程登记仍只存在于当前 FastAPI 进程中，FastAPI 重启后只能把旧任务显示为 detached/orphaned，不能重新接管原进程。

FastAPI 与 SQLite Worker 生命周期解耦，因此 API 重启不会终止正在运行的 SQLite Pipeline。Worker 重启先执行进程级 reconciliation：身份已登记且仍存活的进程继续监控；身份未完整登记的 running job 进入 `identity_unresolved` 并阻止新任务启动。只有身份完整登记、原进程确认死亡且包含可信 Markdown、Planning Manifest/输出、配置、分析结果及当前 repo 成员的 completed 状态闭包通过严格校验时，PR-04B 才允许默认一次的阶段边界恢复。TaskManifest 必须与 Planning 边界指纹一致；该恢复是 stage-boundary at-least-once，不保证任意语句级崩溃点或 exactly-once；legacy 重启后仍不能接管原进程，也不启用 checkpoint。
