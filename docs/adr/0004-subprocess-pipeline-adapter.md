# ADR 0004：使用子进程适配现有 Pipeline

- 状态：已接受并已实现
- 日期：2026-07-14

## 背景

`codes/run_pipeline.py` 和现有阶段脚本已经定义论文解析、规划、分析、编码、评测和修复流程。Web 控制面需要启动、观察和取消任务，但不应复制或替换执行内核。

## 决策

- `web_api/job_service.py` 通过 `subprocess.Popen` 调用 `codes/run_pipeline.py`。
- FastAPI 负责校验输入、构造参数和最小环境、保存上传、记录进程与公开 artifacts；Pipeline 继续负责阶段编排和运行状态文件。
- 每个任务使用 `runs/<job_id>/` 隔离状态、日志、结果和生成仓库。
- Windows 使用新进程组并按进程树取消任务；不引入 Celery、Redis、LangGraph 或远程 worker。
- 子进程失败必须保留非零退出和明确失败状态，不能通过捕获所有异常或返回空结果掩盖。

## 后果

CLI 与 Web 共享同一执行路径，降低行为漂移。代价是活跃进程登记只存在于当前 FastAPI 进程中，服务重启后只能把旧进程标记为 detached/orphaned，不能重新接管。
