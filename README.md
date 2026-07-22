# PaperAlgo / Paper2Code 本地论文到代码 Agent

本仓库面向 Windows 本地单用户场景：用户通过 React WebUI 配置模型、上传论文、启动任务，并通过 FastAPI 查看状态、日志和生成的代码仓库。`codes/run_pipeline.py` 及现有阶段脚本仍是执行内核；Web 层只负责控制和展示，不重新实现论文复现流程。

## 来源与许可

本项目由上游 [going-doer/Paper2Code](https://github.com/going-doer/Paper2Code) 演进而来。上游 PaperCoder 实现对应论文 [Paper2Code: Automating Code Generation from Scientific Papers in Machine Learning](https://arxiv.org/abs/2504.17192)，原作者和论文信息保留在项目历史中。

仓库继续使用上游的 [Apache License 2.0](LICENSE)。`LICENSE` 中保留了原始版权声明 `Copyright 2025 Minju Seo`；本项目没有另行虚构或替换许可证。论文 PDF 和数据集内容仍可能受各自作者、会议或数据源条款约束。

## 当前架构

```text
浏览器
  -> web_ui/（React + Vite，开发端口 5173）
  -> web_api/（FastAPI，127.0.0.1:8000）
  -> legacy: FastAPI subprocess / sqlite: 独立 Worker subprocess
  -> codes/run_pipeline.py
  -> 现有 planning / analyzing / coding / eval / repair 脚本
  -> runs/<job_id>/ 中的状态、日志、结果和生成仓库
```

- `web_ui/` 已实现 settings、创建任务、任务列表、状态/日志/文件查看、取消任务和仓库下载。开发模式直连 FastAPI；构建后的 `web_ui/dist/` 可由 FastAPI 同源托管。
- `web_api/` 是轻量控制面，负责输入校验、本地 settings、任务元数据和 artifacts。legacy runtime 由 FastAPI 启动子进程；sqlite runtime 由独立 Worker 启动子进程。两者都调用 `codes/run_pipeline.py`，不替代执行内核。
- `codes/run_pipeline.py` 负责 MinerU、规划、分析、编码、评测和可选自动修复的阶段编排。
- 默认 `JOB_RUNTIME=legacy` 继续使用 `runs/` 下的 JSON/文件状态并由 FastAPI 直接启动 Pipeline；`JOB_RUNTIME=sqlite` 使用 `.local/paper2code.db` 和独立的单并发 Worker 消费 queued job。
- 当前 WebUI 对运行中任务每 2 秒轮询；当前 checkout **没有** SSE 或 WebSocket 事件接口。
- legacy runtime 的活跃进程句柄仍保存在 FastAPI 内存中，FastAPI 重启后不能接管原进程；sqlite runtime 由独立 Worker 保存 launch state、PID、进程 create time、launch token 和 heartbeat，并可在 API 或 Worker 重启后进行进程级 reconciliation。

更细的模块说明见 [docs/info/全仓目录结构与模块说明.md](docs/info/全仓目录结构与模块说明.md)，工程决策见 [docs/adr/](docs/adr/)。

## Windows PowerShell 安装

以下命令都从仓库根目录执行。建议 Python 3.11 和当前 LTS 版 Node.js/npm。

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install openai fastapi "uvicorn[standard]" python-multipart pytest transformers tiktoken

Set-Location .\web_ui
npm ci
Set-Location ..
```

`requirements.txt` 还包含面向上游本地模型路径的 `vllm`。OpenAI-compatible API + Web 控制面的 Windows 开发不需要 vLLM；只有在兼容的独立环境中确实要运行本地模型时，才使用完整依赖安装：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## 启动开发环境

在第一个 PowerShell 窗口启动 FastAPI：

```powershell
.\.venv\Scripts\python.exe -m uvicorn web_api.main:app --reload --host 127.0.0.1 --port 8000
```

在第二个 PowerShell 窗口启动 WebUI：

```powershell
Set-Location .\web_ui
npm run dev
```

浏览器打开 `http://localhost:5173`。Vite 固定使用 `5173`；端口被占用时会直接失败，以保持与 FastAPI 本地 CORS 白名单一致。当前 Swagger 和 Redoc 页面均已关闭；OpenAPI JSON 位于 `http://127.0.0.1:8000/api/v1/openapi.json`。

如需由 FastAPI 托管生产构建：

```powershell
Set-Location .\web_ui
npm run build
Set-Location ..
.\.venv\Scripts\python.exe -m uvicorn web_api.main:app --host 127.0.0.1 --port 8000
```

随后打开 `http://127.0.0.1:8000`。`web_ui/dist/` 是生成物，不进入版本管理。

SQLite 过渡模式可在启动前显式启用；数据库路径可通过 `PAPER2CODE_DB_PATH` 覆盖：

```powershell
$env:JOB_RUNTIME="sqlite"
$env:PAPER2CODE_DB_PATH=Join-Path (Get-Location) ".local\paper2code.db"
```

在第二个 PowerShell 窗口使用相同环境变量，通过 `python -m web_api.worker` 模块入口启动独立 Worker（下例使用虚拟环境中的 Python）：

```powershell
$env:JOB_RUNTIME="sqlite"
$env:PAPER2CODE_DB_PATH=Join-Path (Get-Location) ".local\paper2code.db"
.\.venv\Scripts\python.exe -m web_api.worker
```

该模式的 `POST /api/v1/jobs` 支持 `Idempotency-Key`，只写入 queued 记录，由持有全局 lease 的 Worker 使用 `BEGIN IMMEDIATE` 原子领取。`worker_id` 仅用于诊断；lease 与所有 Worker 状态写入都由 `worker_id + instance_token` 共同 fencing，同名的旧 Worker 不能继续续租、完成任务或释放新实例的 lease。queued 任务的取消命令由 Worker 直接消费且不会启动 Pipeline；running 任务的取消也是异步幂等命令，不由 FastAPI 直接 kill。Worker 验证 PID 与 create time 后先优雅终止、最多等待 10 秒，再强制终止完整进程树，确认退出后才写入 `canceled`。

Worker 监控本地 `Popen` 时先读取真实退出码，再处理晚到的取消命令：exit 0 直接 completed 且不恢复；非零退出在没有取消时先同步已原子落盘的 checkpoint，并与 Worker 重启后确认 registered 进程死亡的场景使用同一恢复资格判断；晚到 cancel 不能把自然退出改写为 canceled，也不能触发恢复，而会原子失败为 `already_finished`。只有续租成功、取消命令仍有效、进程身份匹配且完整进程树已确认退出时才写入 `canceled`。Windows 强制终止使用同一已验证进程句柄校验 create time、终止并有界等待，不依赖无界 `taskkill /T /F`；POSIX 在发送进程组信号前也会重新校验根进程身份和 group id。

缺少本地设置或上传文件、命令参数无效、可预期的 `Popen` 创建失败只会把当前任务标记为 `process_launch_failed`，Worker 会继续处理后续队列。SQLite/持久化错误、lease fencing 失败、登记后无法确认进程已退出以及未知程序错误仍会 fail fast，不会被伪装成单任务失败。

PR-04B 为 sqlite runtime 增加了阶段 checkpoint 与默认最多 1 次的受限恢复；默认 `JOB_RUNTIME=legacy` 的直接执行路径保持原样且不启用 checkpoint。`codes/run_pipeline.py` 仍是执行内核，只有 Worker 启动它时才启用 checkpoint adapter。协议使用固定阶段白名单、连续 sequence、唯一 stage-1 分支和单调无冲突 attempt，在 `runs/<job_id>/checkpoints/` 原子写入限长 JSON；checkpoint 只保存版本、阶段状态和产物的相对路径、字节数、SHA-256，不保存 API key、Prompt、完整模型响应或环境变量。恢复前会验证从可信 Markdown、Planning 输出和原 TaskManifest，一直到配置、分析结果及当前 Manifest repo 成员的完整恢复状态闭包；TaskManifest 的 size/SHA-256 必须始终匹配可信 Planning 边界，而 evaluation/repair 后可合法变化的 repo 文件以最新 completed 边界记录的当前指纹为准。

恢复语义是 stage-boundary at-least-once，而不是 exactly-once：只有连续可信链上、完整状态闭包仍匹配的 `completed` 边界可用于恢复；崩溃时处于 `running`/`failed`/部分写入的当前阶段会从上一个可信 completed 边界所指向的阶段开头重跑，因此 MinerU、planning、analyzing、coding、evaluation 或 repair 可能重复产生外部调用、成本和文件写入。强制终止遗留的旧 `running` attempt 可被同 sequence 的后续连续 attempt 取代，防并发责任仍由 SQLite lease、进程身份和 launch-token fencing 承担。每次恢复使用新的 launch token，并在 SQLite 中保留旧进程身份历史；恢复次数耗尽、checkpoint 缺失或校验失败会形成稳定失败，不会继续启动第二个 Pipeline。

PR-04A 的 fail-closed 进程边界保持不变：已登记且存活、身份匹配的进程继续监控；只有已登记身份被确认死亡的进程才会考虑 checkpoint 恢复。如果任务已经领取但 PID/create time/process group 尚未完整登记，系统不会把“数据库中没有 PID”解释为“进程不存在”，而会进入 `identity_unresolved` 隔离态；该状态不恢复、不猜测、不扫描或 kill 未知进程，并持续阻止后续任务。无合法边界、恢复预算耗尽或 `identity_unresolved` 仍可能需要人工检查并重启本地环境；本实现不宣称覆盖任意崩溃点的自动恢复。

## Provider 配置原则

WebUI 的 Settings 页面分别配置复现模型和评测模型，当前 schema 支持 `deepseek`、`kimi`、`qwen`、`claude`、`openai`。模型名称必须与实际 Provider 一致；自定义 `base_url` 应指向对应的 OpenAI-compatible API 根地址，而不是 Provider 官网页面。评测模型可以配置 fallback 模型链。

API key 只应通过本地 Settings 页面或当前 PowerShell 会话的环境变量提供，不应写入 README、命令脚本或测试 fixture。Web settings 保存在被 Git 忽略的 `.local/web_settings.json`；状态接口只返回 `has_api_key`，但本地文件仍是明文 JSON，不等于系统凭据库或磁盘加密。

## MinerU 安装边界

MinerU 是可选的外部 PDF 解析依赖，不在本仓库中安装、封装或测试。请按 MinerU 自身文档在独立环境中安装，避免与 Web/API 环境的依赖冲突。Pipeline 按以下顺序寻找执行文件：

1. 显式传入 `--mineru_executable`；
2. 仓库同级工作区的 `mineru_env\Scripts\mineru.exe`；
3. `PATH` 中的 `mineru`。

已有 Markdown 时可使用 `--skip_mineru --pdf_markdown_path ...`；Web API 会要求该 Markdown 已存在且位于本仓库的 `runs/` 目录下。自动化测试不会启动 MinerU。

## 测试与构建

Python 测试使用 fake provider、fake pipeline 和临时目录，不需要真实 API key：

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q
```

前端检查：

```powershell
Set-Location .\web_ui
npm run typecheck
npm run build
npm run verify:same-origin
npm run smoke
npm run smoke:prod
Set-Location ..
```

`smoke:prod` 需要先完成 `npm run build`。测试和构建基线详见 [docs/engineering/baseline.md](docs/engineering/baseline.md)。

## 本地单用户安全边界

这是监听 `127.0.0.1` 的本地单用户工具，不是公网安全系统：

- 没有用户账户、认证、授权、租户隔离或远程部署防护；不要改用 `--host 0.0.0.0` 暴露到局域网或公网。
- CORS 白名单只为本地 Vite 开发服务，不是身份认证机制。
- 只运行一个 FastAPI worker；多 worker 会分裂内存中的进程登记和取消语义。
- `.local/`、`runs/`、`outputs/`、`results/` 和 MinerU 产物可能含论文、日志或模型输出，均应留在本机且不提交。
- API key、完整 Prompt 和完整模型响应不得写入状态接口、SQLite 事件或其他数据库元数据。
