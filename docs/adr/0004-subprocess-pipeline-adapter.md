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
- 子进程失败必须保留非零退出和明确失败状态，不能通过捕获所有异常或返回空结果掩盖。

## 后果

CLI、legacy Web runtime 与 SQLite Worker 共享同一执行内核，降低行为漂移。legacy 的进程登记仍只存在于当前 FastAPI 进程中，FastAPI 重启后只能把旧任务显示为 detached/orphaned，不能重新接管原进程。

FastAPI 与 SQLite Worker 生命周期解耦，因此 API 重启不会终止正在运行的 SQLite Pipeline。Worker 重启只执行进程级 reconciliation：身份已登记且仍存活的进程可以继续监控，已登记但死亡的进程明确失败；身份未完整登记的 running job 进入 `identity_unresolved` 并阻止新任务启动。PR-04A 没有阶段 checkpoint 或阶段恢复，也不保证任意崩溃点的自动恢复。
