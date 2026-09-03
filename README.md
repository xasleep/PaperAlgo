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
- 当前 WebUI 在任务详情页先读取 REST 权威快照, 再通过同源 `EventSource` 消费 sqlite runtime 的 `GET /api/v1/jobs/{job_id}/events` SSE 事件流；发现 replay gap 时重新读取 REST 快照后继续跟流。SSE 不可用时才进入明确的低频 REST fallback, 不再与 SSE 同时进行无限 2 秒轮询。本 checkout 没有 WebSocket。
- legacy runtime 的活跃进程句柄仍保存在 FastAPI 内存中，FastAPI 重启后不能接管原进程；sqlite runtime 由独立 Worker 保存 launch state、PID、进程 create time、launch token 和 heartbeat，并可在 API 或 Worker 重启后进行进程级 reconciliation。

更细的模块说明见 [docs/info/全仓目录结构与模块说明.md](docs/info/全仓目录结构与模块说明.md)，工程决策见 [docs/adr/](docs/adr/)。

## Windows PowerShell 安装

以下命令都从仓库根目录执行。本项目正式支持 Windows 本地单用户安装；CI 使用 Python 3.11 与 Node.js 22.17.1，Windows 本地开发也可使用已验证的 Python 3.12。依赖不为“更新”而升级：Python 依赖由分层 requirements 文件声明，并由 `constraints.txt` 固定可复现约束；前端依赖由 `web_ui/package-lock.json` 固定。

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt

Set-Location .\web_ui
npm ci
Set-Location ..
```

依赖文件分工如下：

- `requirements-runtime.txt`：运行 Web API、sqlite Worker 与 OpenAI-compatible 远程 Provider 所需的最小 runtime。
- `requirements-dev.txt`：runtime 加 pytest/httpx 等本地测试依赖，是 CI 和日常验证入口。
- `requirements-optional-heavy.txt`：可选重型 Provider 集成依赖，例如 Transformers 和非 Windows 环境下的 vLLM；Windows 本地 Web/API 不安装 vLLM。
- `requirements.txt`：向后兼容入口，包含 runtime 与 optional-heavy。日常 Windows 安装优先使用 `requirements-dev.txt`。

只有在兼容的独立环境中确实要运行本地模型或重型 Provider integration 时，才安装可选重型依赖：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-optional-heavy.txt
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

SQLite 过渡模式可在启动前显式启用。默认本地状态写入 `.local/` 和 `runs/`；干净 checkout 验证或隔离运行时可通过环境变量重定向本地状态、数据库和 runs 目录：

```powershell
$env:JOB_RUNTIME="sqlite"
$env:PAPER2CODE_LOCAL_DIR=Join-Path (Get-Location) ".local"
$env:PAPER2CODE_RUNS_DIR=Join-Path (Get-Location) "runs"
$env:PAPER2CODE_DB_PATH=Join-Path (Get-Location) ".local\paper2code.db"
```

在第二个 PowerShell 窗口使用相同环境变量，通过 `python -m web_api.worker` 模块入口启动独立 Worker（下例使用虚拟环境中的 Python）：

```powershell
$env:JOB_RUNTIME="sqlite"
$env:PAPER2CODE_LOCAL_DIR=Join-Path (Get-Location) ".local"
$env:PAPER2CODE_RUNS_DIR=Join-Path (Get-Location) "runs"
$env:PAPER2CODE_DB_PATH=Join-Path (Get-Location) ".local\paper2code.db"
.\.venv\Scripts\python.exe -m web_api.worker
```

该模式的 `POST /api/v1/jobs` 支持 `Idempotency-Key`，只写入 queued 记录，由持有全局 lease 的 Worker 使用 `BEGIN IMMEDIATE` 原子领取。`worker_id` 仅用于诊断；lease 与所有 Worker 状态写入都由 `worker_id + instance_token` 共同 fencing，同名的旧 Worker 不能继续续租、完成任务或释放新实例的 lease。queued 任务的取消命令由 Worker 直接消费且不会启动 Pipeline；running 任务的取消也是异步幂等命令，不由 FastAPI 直接 kill。Worker 验证 PID 与 create time 后先优雅终止、最多等待 10 秒，再强制终止完整进程树，确认退出后才写入 `canceled`。

PR-07A 起, sqlite runtime 的任务控制命令使用 `POST /api/v1/jobs/{job_id}/commands`, 支持 `approve`, `cancel`, `retry`, `repair`, 且要求 `Idempotency-Key`。命令记录有独立 ID, request status, rejection code 和 result code；重复提交返回同一命令, 不重复执行。PR-07B 起, WebUI 只在当前 job 状态合法时启用对应按钮, 提交后显示 pending/applied/rejected 并禁止重复点击。命令只在当前 job 状态允许时接受, `identity_unresolved` 会稳定拒绝, 不猜测、kill 未知进程或启动冲突操作。旧的 `POST /api/v1/jobs/{job_id}/cancel` 保持兼容, 仍写入同一类持久化 cancel command。

sqlite runtime 还提供 `GET /api/v1/jobs/{job_id}/events` SSE 流。事件 ID 来自 SQLite `job_events.id`, 单调持久化, 支持 `Last-Event-ID` 和 API 重启后的 replay。replay 有服务器上限, 超出时返回 `stream.gap` 并要求客户端通过 REST 状态重新同步。浏览器 `EventSource` 无法手动设置 `Last-Event-ID` header, 因此 endpoint 也接受同源 `last_event_id` query cursor；`eventsource=1` 时 gap 使用 HTTP 200 承载 `stream.gap`, 让浏览器能够读取 resync 指令。SSE 只传递脱敏、有限大小的增量事件；REST job status 仍是权威状态源。

## 升级、迁移、备份与回滚

升级前先停止 FastAPI 和 Worker，避免 SQLite WAL/SHM 或 runs 目录处于写入中。备份本地数据库、settings 和 runs：

```powershell
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$backupRoot = Join-Path (Get-Location) ".local\backups\$stamp"
New-Item -ItemType Directory -Force $backupRoot | Out-Null
Copy-Item .local\paper2code.db* $backupRoot -Force -ErrorAction SilentlyContinue
Copy-Item .local\web_settings.json $backupRoot -Force -ErrorAction SilentlyContinue
Copy-Item runs (Join-Path $backupRoot "runs") -Recurse -Force -ErrorAction SilentlyContinue
```

升级 checkout 后重新安装当前版本声明的依赖并构建前端：

```powershell
git fetch origin --prune
git switch PaperAlgo
git pull --ff-only origin PaperAlgo
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt

Set-Location .\web_ui
npm ci
npm run build
Set-Location ..
```

SQLite schema 迁移由 `web_api.database.initialize_database()` 在 API/Worker 首次打开数据库时幂等执行；不会通过独立迁移命令要求用户手工编辑数据库。迁移前的备份目录保留数据库主文件以及 WAL/SHM 旁路文件，便于回滚。

回滚时同样先停止 FastAPI 和 Worker，然后切回已知可用的提交或分支，按该 checkout 的依赖文件重新安装，并还原同一时间点的 `.local` 与 `runs` 备份。若新版本已经启动过 sqlite runtime，优先使用升级前备份回滚数据库，避免旧代码读取新 schema 或新状态语义。

Worker 监控本地 `Popen` 时先读取真实退出码，再处理晚到的取消命令：exit 0 直接 completed 且不恢复；非零退出在没有取消时先同步已原子落盘的 checkpoint，并与 Worker 重启后确认 registered 进程死亡的场景使用同一恢复资格判断；晚到 cancel 不能把自然退出改写为 canceled，也不能触发恢复，而会原子失败为 `already_finished`。只有续租成功、取消命令仍有效、进程身份匹配且完整进程树已确认退出时才写入 `canceled`。Windows 强制终止使用同一已验证进程句柄校验 create time、终止并有界等待，不依赖无界 `taskkill /T /F`；POSIX 在发送进程组信号前也会重新校验根进程身份和 group id。

缺少本地设置或上传文件、命令参数无效、可预期的 `Popen` 创建失败只会把当前任务标记为 `process_launch_failed`，Worker 会继续处理后续队列。SQLite/持久化错误、lease fencing 失败、登记后无法确认进程已退出以及未知程序错误仍会 fail fast，不会被伪装成单任务失败。

PR-04B 为 sqlite runtime 增加了阶段 checkpoint 与默认最多 1 次的受限恢复；默认 `JOB_RUNTIME=legacy` 的直接执行路径保持原样且不启用 checkpoint。`codes/run_pipeline.py` 仍是执行内核，只有 Worker 启动它时才启用 checkpoint adapter。协议使用固定阶段白名单、连续 sequence、唯一 stage-1 分支和单调无冲突 attempt，在 `runs/<job_id>/checkpoints/` 原子写入限长 JSON；checkpoint 只保存版本、阶段状态和产物的相对路径、字节数、SHA-256，不保存 API key、Prompt、完整模型响应或环境变量。恢复前会验证从可信 Markdown、Planning 输出和原 TaskManifest，一直到配置、分析结果及当前 Manifest repo 成员的完整恢复状态闭包；TaskManifest 的 size/SHA-256 必须始终匹配可信 Planning 边界，而 evaluation/repair 后可合法变化的 repo 文件以最新 completed 边界记录的当前指纹为准。

恢复语义是 stage-boundary at-least-once，而不是 exactly-once：只有连续可信链上、完整状态闭包仍匹配的 `completed` 边界可用于恢复；崩溃时处于 `running`/`failed`/部分写入的当前阶段会从上一个可信 completed 边界所指向的阶段开头重跑，因此 MinerU、planning、analyzing、coding、evaluation 或 repair 可能重复产生外部调用、成本和文件写入。强制终止遗留的旧 `running` attempt 可被同 sequence 的后续连续 attempt 取代，防并发责任仍由 SQLite lease、进程身份和 launch-token fencing 承担。每次恢复使用新的 launch token，并在 SQLite 中保留旧进程身份历史；恢复次数耗尽、checkpoint 缺失或校验失败会形成稳定失败，不会继续启动第二个 Pipeline。

PR-04A 的 fail-closed 进程边界保持不变：已登记且存活、身份匹配的进程继续监控；只有已登记身份被确认死亡的进程才会考虑 checkpoint 恢复。如果任务已经领取但 PID/create time/process group 尚未完整登记，系统不会把“数据库中没有 PID”解释为“进程不存在”，而会进入 `identity_unresolved` 隔离态；该状态不恢复、不猜测、不扫描或 kill 未知进程，并持续阻止后续任务。无合法边界、恢复预算耗尽或 `identity_unresolved` 仍可能需要人工检查并重启本地环境；本实现不宣称覆盖任意崩溃点的自动恢复。

## Provider 配置原则

PR-05 起，所有远程 LLM 调用必须用 `provider_id + model_id` 显式选择，并先通过 `codes/providers.v1.json` 与 `codes/provider_registry.py` 校验。系统不再根据模型名前缀或当前恰好存在的密钥猜 Provider，也不会让 DeepSeek、Qwen 等静默落到 OpenAI 默认 endpoint。Web Settings 保存和新任务入队前都会验证 Provider、模型、API key 与必要 `base_url`；未知或缺失配置返回 422。

Registry 为每个模型记录 `max_n`、context/output 上限、JSON Schema/usage/cache-token 能力、timeout/retry/concurrency 与带生效日期的价格。没有经过验证的 context 或价格显式写为 `null`/`unknown`，PR-06B 成本账本会保持 unknown，不用陈旧表格猜算。当前默认远程模型采用 `max_n=1` 的保守契约；`generated_n > 1` 由应用层拆成独立请求，并继续受 1–32 候选预算和 Registry 并发限制。评测 fallback 只接受同一显式 Provider 下已注册的 model_id；CLI 逗号链按字面值解析，不 trim、不过滤空 token，非法 token 在创建客户端前失败。跨 Provider fallback 必须等未来契约显式携带新的 provider_id，不能再靠模型名推断。

Registry v1 使用封闭 schema：根、Provider、Model、Pricing 和 `request_options` 的未知字段都会被拒绝，所有数值拒绝 bool、NaN、Infinity 和越界值，fallback 必须唯一、非自引用且位于同一 Provider。加载后 Contract（包括嵌套 request options）是只读的；JSON/I/O/schema 错误统一为脱敏的 `invalid_provider_registry`，远程 Provider 的 timeout、429 或 5xx 则继续原样失败。

CLI 仍保留现有 `--reproduce_provider/--reproduce_gpt_version` 与 `--eval_provider/--eval_gpt_version` 迁移面，阶段脚本则接收 `--provider + --gpt_version`。如需维护自定义模型，可复制版本化 Registry 并通过 `PAPER2CODE_PROVIDER_REGISTRY_PATH` 指向它；配置文件只允许保存环境变量名和能力/价格元数据，不得保存密钥。

默认模型清单是 PaperAlgo 当前支持清单，不是官方模型全集镜像。2026-08-06 依据官方文档复核后，OpenAI active 条目只保留 `gpt-4.1-mini`、`gpt-4o-mini`；`o3-mini`、`o4-mini` 已列入 Deprecated，不进入新任务。DeepSeek 仅接受 `deepseek-v4-pro` / `deepseek-v4-flash`，Qwen 接受官方拼写 `qwen3.8-max`、`qwen3.7-max`、`qwen3.7-plus`，Kimi 接受 `kimi-k3`、`kimi-k2.7-code`、`kimi-k2.7-code-highspeed`、`kimi-k2.6`。K3 使用空 `request_options`，最终请求省略官方要求不发送的固定参数。默认条目的 `base_url: null` 是要求本地用户显式选择 endpoint/地域的安全策略，不代表官方地址未知。

CLI Pipeline 会先构造最小系统环境，再只投影当前角色和所选 Provider/Model 的凭据。`REPRODUCE_*` / `EVAL_*` 优先于模型契约声明的 Provider 原生变量；未设置角色变量时可使用例如 `DEEPSEEK_API_KEY` / `DEEPSEEK_BASE_URL`。未选 Provider 的凭据以及另一角色的凭据不会传给阶段子进程。SQLite Worker 会额外投影本地成本账本数据库路径，Pipeline 再为阶段子进程附加 job/stage/attempt/recovery 上下文；这些上下文不包含 prompt、response、API key 或 Authorization。

API key 只应通过本地 Settings 页面或当前 PowerShell 会话的环境变量提供，不应写入 README、命令脚本或测试 fixture。Web settings 保存在被 Git 忽略的 `.local/web_settings.json`；状态接口只返回 `has_api_key`，但本地文件仍是明文 JSON，不等于系统凭据库或磁盘加密。

WebUI 通过只读 `GET /api/v1/providers` 动态获取当前实际 Registry 中可选的 Provider/Model，不维护第二份硬编码清单；响应仅包含版本、ID 和非敏感能力状态，使用 `Cache-Control: no-store`，而 `/api/v1/settings/status` 仍只返回布尔配置状态。无 active model 的 Provider 不可选，Registry 获取失败时 Settings 表单 fail closed。

SQLite 新任务会在入队时保存 reproduce/evaluation Provider、Model、fallback、Registry version 和确定性 Contract SHA-256 的非敏感选择快照，不保存 API key、Authorization、base URL、Prompt、完整响应、完整命令或环境。Worker 可读取当前本地 settings 中轮换后的同一组凭据；若选择、Registry version、指纹不匹配，或历史 queued/running 行缺少快照，则在 `Popen` 前以 `provider_settings_changed` 失败，不猜测或重绑定。相同 `Idempotency-Key` 的 replay 返回原 job 与原快照。该规则不改变 legacy runtime 的同步启动语义。

PR-06B 起，SQLite runtime 为经过 Provider Registry 的真实远程 LLM attempt 写入 append-only `remote_call_ledger`。每个 request、retry、fallback 和 PR-04B recovery 重跑产生的新真实调用都会有独立 `attempt_id`；同一个 `attempt_id` 的重复入账通过唯一约束幂等，不重复计费。账本只保存 job/stage/repair/recovery attempt、provider/model、request/retry/fallback 序号、可获得 usage、pricing contract version/fingerprint、currency、cost status、Decimal 字符串金额和 started/completed/failed/cancelled 时间，不保存 prompt、完整 response、API key、Authorization、本地 credential 或敏感路径。

成本计算只使用 Registry 中已验证的价格和 provider 返回的 usage。未返回 usage 保持 unknown，不记为 0；未验证价格保持 unknown，不猜测；不同 currency 按币种分别汇总，不做汇率合并。失败、取消、timeout 和 provider error 后如果 provider 暴露 partial usage，账本保留已知部分成本并标记为 actual 或 estimated。创建 SQLite job 可选择 `cost_budget_policy="hard"` 并提供 `cost_budget_currency` 与 Decimal 字符串 `cost_budget_amount`；默认 policy 为 `none`。hard budget 在远程调用前用 SQLite `BEGIN IMMEDIATE` 原子检查和预占上界；若价格、输入 token、输出 token 上界或币种无法确定，则按文档化策略 fail closed，不发起远程调用。

当前边界仍是 Windows、本地单用户、`127.0.0.1`、单 FastAPI 与单 Worker；`.local/web_settings.json` 和子进程环境中的 API key 不是加密凭据存储。PR-07B 不实现 WebSocket、多用户、公共互联网、分布式队列、汇率服务、正式 Playwright E2E 框架或真实付费测试调用。

## Evaluation 与 Repair 契约

PR-06A 起，`codes/evaluation_contract.py` 是评测结果和 repair 决策的权威边界。Pipeline 现在区分 `execution_status`、`evaluation_status`、`quality_status` / `quality_verdict` 和 `repair_status`：执行失败会跳过 evaluation；evaluator timeout、Provider protocol error、malformed response、unavailable 和 quorum 不足只表示 evaluation failure，不表示代码质量不合格。

质量结论必须满足 quorum，默认 quorum 为 `floor(generated_n / 2) + 1`。只有 evaluator 成功完成且 quality rejected 时才可能进入 repair。`files_to_fix=[]` 表示不修改任何文件；非空列表必须重新绑定到 Planning 产生的 TaskManifest，并通过目标 repo 路径验证，绝不解释为修复整个仓库或整个 manifest。

Evaluation result、feedback、repo status、SQLite events 和 summaries 不保存 prompt、完整模型响应、API key、Authorization 或原始 Provider payload。成本信息由 PR-06B 的独立 append-only ledger 和 job summary 提供；Evaluation Contract 本身仍不保存账本明细或 WebUI 实时控制。

## MinerU 安装边界

MinerU 是可选的外部 PDF 解析依赖，不在本仓库中安装、封装或测试。请按 MinerU 自身文档在独立环境中安装，避免与 Web/API 环境的依赖冲突。Pipeline 按以下顺序寻找执行文件：

1. 显式传入 `--mineru_executable`；
2. 仓库同级工作区的 `mineru_env\Scripts\mineru.exe`；
3. `PATH` 中的 `mineru`。

已有 Markdown 时可使用 `--skip_mineru --pdf_markdown_path ...`；Web API 会要求该 Markdown 已存在且位于本仓库的 `runs/` 目录下。自动化测试不会启动 MinerU。

## 测试与构建

Python 测试使用 fake provider、fake pipeline 和临时目录，不需要真实 API key：

```powershell
$pytestBase = Join-Path (Get-Location) (".pytest_tmp_" + [guid]::NewGuid().ToString("N"))
.\.venv\Scripts\python.exe -m pytest tests -q -rs -p no:cacheprovider --basetemp $pytestBase
```

前端检查和 fake-provider Playwright E2E：

```powershell
Set-Location .\web_ui
npm ci
npm run typecheck
npm run build
npm run verify:same-origin
npm run smoke
npm run smoke:prod
npm run e2e:fake
Set-Location ..
```

`smoke:prod` 与 `e2e:fake` 需要先完成 `npm run build`。`e2e:fake` 会启动 127.0.0.1 上的临时 FastAPI、fake Provider Registry、临时 SQLite、临时 `.local` 和临时 runs，覆盖 settings 脱敏、Provider/Model discovery、创建任务、SSE/reconnect、artifacts、cancel/retry/repair、cost/budget、浏览器刷新和 REST resync。测试和 CI 不运行真实付费 Provider、MinerU、vLLM 或完整耗时 Pipeline。

GitHub Actions 的正式门禁是 Windows `ci-windows`。workflow 使用 `pull_request` 和 `push`，不使用 `pull_request_target` 执行不可信代码；权限为 `contents: read`；Python/cache key 绑定 `requirements-runtime.txt`、`requirements-dev.txt` 和 `constraints.txt`，npm/cache key 绑定 `web_ui/package-lock.json`。当前没有声明 Linux 完整支持。

## 本地单用户安全边界

这是监听 `127.0.0.1` 的本地单用户工具，不是公网安全系统：

- 没有用户账户、认证、授权、租户隔离或远程部署防护；不要改用 `--host 0.0.0.0` 暴露到局域网或公网。
- CORS 白名单只为本地 Vite 开发服务，不是身份认证机制。
- 只运行一个 FastAPI worker；多 worker 会分裂内存中的进程登记和取消语义。
- `.local/`、`runs/`、`outputs/`、`results/` 和 MinerU 产物可能含论文、日志或模型输出，均应留在本机且不提交。
- API key、完整 Prompt 和完整模型响应不得写入状态接口、SQLite 事件或其他数据库元数据。
