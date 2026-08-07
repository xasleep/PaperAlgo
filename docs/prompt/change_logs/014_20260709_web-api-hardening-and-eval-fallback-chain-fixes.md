# 014 web-api-hardening-and-eval-fallback-chain-fixes

修改时间：2026-07-09

## 本次更新概述

- 加固 FastAPI 作业接口的路由顺序、路径校验、上传校验、任务状态读取和 artifact/log 访问边界。
- 加固 Web settings 与状态类 JSON 文件：写入使用临时文件 + 原子替换，读取损坏/空 JSON 时返回可解释状态而不是 500。
- 明确 API key 的存储与回显语义：`.local/web_settings.json` 仍为本地明文存储，但 `/settings`、`/settings/status`、任务状态、命令和日志不回显 API key。
- 强化 Windows/本地单用户进程生命周期：Web API 启动 pipeline 时使用受控 cwd/env/log 句柄，取消任务时尽量终止整棵进程树。
- 修复 eval fallback 在 auto-refine 多轮中的链路延续：首轮 fallback 生效后，后续轮次以生效模型为当前模型，并继续携带剩余 fallback 链。
- 补充针对路径穿越、PDF 内容伪造、坏 JSON、进程状态、repo 文件边界、并发下载、fallback 链路和文档 UTF-8 的 pytest 覆盖。
- 当前 `docs/info/全仓目录结构与模块说明.md` 和部分历史 change log 可被 UTF-8 读取，但文本显示仍存在乱码痕迹；本条 change log 以当前代码真实行为为准，用于后续同步文档。

## 修复明细

### 作业生命周期与取消语义

- 问题背景：
  - Web API 启动的 pipeline 是长生命周期子进程；在 Windows 下如果只终止父进程，MinerU、Python stage 或下游子进程可能残留。
  - FastAPI 服务重启后，内存中的 `ACTIVE_PROCESSES` 会丢失，旧任务不能继续被当作当前实例可取消任务。
- 修复内容：
  - `web_api/job_service.py` 在启动 pipeline 时使用 `cwd=str(REPO_ROOT)`、受控环境变量、UTF-8 日志文件句柄和平台相关进程组参数。
  - Windows 下使用 `subprocess.CREATE_NEW_PROCESS_GROUP` 启动；取消时调用 `taskkill /PID <pid> /T /F`。
  - POSIX 下使用 `start_new_session=True`，取消时先 `SIGTERM` process group，必要时再 `SIGKILL`。
  - `run_status.json` 记录 `pid`、`process_pid`、`process_group_id`、`process_state`、`process_active`、`cancelable`、`process_kill_strategy`。
  - `get_job_status()` 会根据内存进程表、状态文件和 PID 存活情况区分 `active`、`finished`、`detached`、`orphaned`。
- 修复后的实际行为：
  - 当前 FastAPI 实例仍持有且进程存活的任务为 `active`，可取消。
  - terminal 状态 `completed`、`failed`、`canceled` 会标记为 `finished`。
  - 服务重启后仍存活但不在当前内存进程表里的任务为 `detached`，不可取消。
  - 状态仍像运行中但 PID 不存在的任务为 `orphaned`，不可取消。
  - `cancel_job()` 对 `detached/orphaned` 返回失败信息，API 层转为 409。
- 影响文件：
  - `web_api/job_service.py`
  - `web_api/main.py`
- 测试覆盖：
  - `tests/test_process_lifecycle.py`
    - 覆盖取消任务时终止子进程树。
    - 覆盖 `active`、`detached`、`orphaned`、`finished` 状态语义。
  - `tests/test_environment_security.py`
    - 覆盖启动任务时 API key 不进入状态文件、命令行和 launcher log。
- 兼容性/注意事项：
  - 服务重启后不会重新接管 detached 进程，只会识别并展示其状态。
  - Windows 取消依赖系统 `taskkill`。

### JSON 状态文件与配置文件可靠性

- 问题背景：
  - `run_status.json`、`run_summary.json`、`repo_status.json`、`web_settings.json` 都是运行时状态文件，半写、空文件或损坏 JSON 会影响 Web API 查询。
  - 早期 `web_settings.json` 使用普通 `json.load()` / `json.dump()`，损坏时会导致 `/settings/status` 和 `/jobs` 返回 500。
- 修复内容：
  - `web_api/json_io.py` 提供 `read_json_file()` 和 `write_json_file_atomic()`。
  - `web_api/job_service.py` 的 Web 状态写入通过 `write_json_file_atomic()`。
  - `codes/utils.py` 的 pipeline 状态写入同样使用临时文件、`fsync` 和 `os.replace()`。
  - `web_api/settings_store.py` 已改为原子写入 `.local/web_settings.json`，读取时对空文件、坏 JSON、非对象 JSON 和 schema 校验失败做容错。
- 修复后的实际行为：
  - `run_status.json` 或 `run_summary.json` 损坏时，`/jobs`、`/jobs/{job_id}`、`/jobs/{job_id}/summary` 返回 best-effort 状态，不返回 500。
  - `web_settings.json` 不存在、为空、损坏或结构非法时，`/settings/status` 返回 `configured=false`。
  - `web_settings.json` 不存在或不可用时，`POST /jobs` 返回 400：`API settings are not configured.`。
  - 原子写失败时旧文件不被破坏，临时文件会尽量清理。
- 影响文件：
  - `web_api/json_io.py`
  - `web_api/settings_store.py`
  - `web_api/job_service.py`
  - `web_api/artifact_service.py`
  - `codes/utils.py`
- 测试覆盖：
  - `tests/test_json_state_resilience.py`
    - 覆盖坏/空 `run_status.json`、`run_summary.json` 的读取容错。
    - 覆盖 Web 状态 JSON 原子写失败保留旧文件。
    - 覆盖 `codes/utils.py` 状态 JSON 容错读取和原子写。
  - `tests/test_settings_store_resilience.py`
    - 覆盖 settings 文件不存在、损坏、空文件、非法结构。
    - 覆盖 settings 原子写失败不破坏旧文件。
- 兼容性/注意事项：
  - 损坏的 settings 文件当前被视为“未配置”，接口不额外返回损坏详情字段。
  - `.local/web_settings.json` 仍是本地明文 JSON 文件；接口脱敏不等于磁盘加密。

### 路径安全与文件读取边界

- 问题背景：
  - WebUI 需要读取历史 job、repo 文件、日志和下载 zip；这些接口如果不限制路径，容易出现 job_id 或相对路径穿越。
  - 代码仓库文件和日志可能很大、可能是二进制或非 UTF-8。
- 修复内容：
  - `web_api/path_security.py` 的 `validate_job_id()` 拒绝 `..`、斜杠、反斜杠、Windows 盘符、绝对路径、URL 编码后的逃逸写法和非法字符。
  - `artifact_service.safe_repo_path()` 将 repo 相对路径归一化并限制在 `runs/<job_id>/repo/` 内。
  - `log_service.safe_log_path()` 只允许当前 job `logs/` 下的单层 `.log` 文件名。
  - `read_repo_file()` 限制可预览扩展名、最大 2 MiB、二进制内容和非 UTF-8 文本。
  - `read_log()` 使用尾部块读取，限制最多 2 MiB，避免一次性读取超大日志。
  - `make_repo_zip()` 使用 `.downloads/`、UUID 文件名、临时 zip 和 `os.replace()`，避免并发下载互相覆盖或暴露半成品。
- 修复后的实际行为：
  - `/jobs` 路由定义在 `/jobs/{job_id}` 之前，`/jobs` 不会被误匹配为 job id。
  - `/jobs/{job_id}/artifacts`、`/repo/tree`、`/repo/file`、`/repo/download` 等更具体路由定义在 `/jobs/{job_id}` 之前，匹配安全。
  - 非法 job_id 请求返回 400 或 404，不会读到 runs 目录外的文件。
  - repo 文件预览只返回允许类型、大小合规、非二进制、可 UTF-8 解码的文本。
  - repo zip 并发生成时路径唯一，正常情况下不遗留 `.tmp` 文件。
  - `list_jobs()` 对大量 runs 目录做 best-effort 读取，结果按 mtime 倒序，limit 被限制在 1 到 200。
- 影响文件：
  - `web_api/main.py`
  - `web_api/path_security.py`
  - `web_api/artifact_service.py`
  - `web_api/log_service.py`
  - `web_api/schemas.py`
- 测试覆盖：
  - `tests/test_security_inputs.py`
    - 覆盖 job_id 直接和 URL 编码路径穿越。
  - `tests/test_artifacts_logs_boundaries.py`
    - 覆盖 repo 文件大小限制、二进制拒绝、扩展名白名单。
    - 覆盖日志缺失 job、无日志目录、tail_lines 尾部读取。
    - 覆盖重复和并发 repo download 的唯一 zip 与临时文件清理。
    - 覆盖 250 个异常 runs 目录下 `/jobs?limit=200` 的 best-effort 行为。
- 兼容性/注意事项：
  - `repo/download` 对稳定文件集有测试覆盖；如果压缩过程中 repo 文件被并发删除或发生底层 OSError，当前未单独测试所有异常分支。
  - `.downloads/` 中历史 zip 当前不会自动过期清理。

### PDF 上传与任务创建校验

- 问题背景：
  - `POST /jobs` 是 WebUI 创建任务入口，需要阻止伪 PDF、异常参数和不受控 Markdown 路径进入 pipeline。
- 修复内容：
  - `web_api/main.py` 对 settings 是否配置、文件名 `.pdf` 后缀、`generated_n`、`max_repair_rounds` 做入口校验。
  - `job_service.save_upload()` 读取首个 chunk 并校验 PDF magic bytes `%PDF-`。
  - `sanitize_name()` 清洗 paper_name / 文件名，只保留字母、数字、下划线、点和短横线，并去掉首尾点/下划线。
  - `skip_mineru=true` 时，Web API 和 pipeline 层都会校验 `pdf_markdown_path`。
  - `pdf_markdown_path` 必须存在、是文件、扩展名为 `.md` 或 `.markdown`，解析后必须位于 `runs/` 目录下。
- 修复后的实际行为：
  - 未配置 settings 时，`POST /jobs` 返回 400，不启动任务。
  - 文件名不是 `.pdf` 或内容不是 `%PDF-` 开头时，返回 400。
  - `generated_n` 只允许 1 到 32；`max_repair_rounds` 只允许 0 到 10。
  - 外部 Markdown 路径不能作为 `skip_mineru` 输入。
  - pipeline 层直接调用 `--skip_mineru` 时也会重复校验 Markdown 路径。
- 影响文件：
  - `web_api/main.py`
  - `web_api/job_service.py`
  - `codes/run_pipeline.py`
- 测试覆盖：
  - `tests/test_security_inputs.py`
    - 覆盖伪 PDF 内容拒绝。
    - 覆盖外部 Markdown 路径被 Web API 和 job_service 拒绝。
    - 覆盖 pipeline 层 `validate_markdown_path()` 拒绝 runs 目录外路径。
  - `tests/test_eval_fallback_and_params.py`
    - 覆盖 `generated_n` 和 `max_repair_rounds` 边界。
    - 覆盖 Web 创建任务时越界参数返回 400。
- 兼容性/注意事项：
  - MIME type 当前未被代码显式校验；实际已校验文件名后缀和 PDF magic bytes。也就是说“扩展名 + 内容”已覆盖，MIME 校验未覆盖。
  - `pdf_markdown_path` 当前限制在 `runs/` 级别，不限制到“当前 job 子目录”。

### API Key 存储、回显与子进程环境隔离

- 问题背景：
  - WebUI 需要保存 provider/model/api_key/base_url，但 API key 不能通过状态接口、命令行、日志或状态文件泄漏。
  - Windows/PowerShell 环境中可能存在旧的 provider 环境变量，不能让它们覆盖 Web settings。
- 修复内容：
  - `settings_store.get_settings_status()` 只返回 provider、model、base_url、`has_api_key` 和 evaluation fallback model 列表，不返回 `api_key` 字段。
  - `.gitignore` 包含 `.local/`、`.env`、`api.txt`、证书/密钥文件，避免常见本地 secret 被提交。
  - `job_service.build_pipeline_env()` 只保留系统 allowlist 变量，并注入 `REPRODUCE_API_KEY`、`EVAL_API_KEY` 和非空 base_url。
  - `codes/run_pipeline.py` 的 `build_clean_env()` / `get_role_env()` 清理 provider 环境变量，只把 role API key/base_url 映射到当前 provider 所需变量。
  - Popen 日志使用 UTF-8 写入，子进程环境设置 `PYTHONIOENCODING=utf-8` 和 `PYTHONUTF8=1`。
- 修复后的实际行为：
  - `/settings` 和 `/settings/status` 不回显 API key。
  - 启动任务时，API key 存在于子进程环境变量中，但不会写入 `run_status.json`、launcher log 或命令行参数。
  - 宿主机残留的 `OPENAI_API_KEY`、`OPENAI_BASE_URL`、`DEEPSEEK_BASE_URL`、`MOONSHOT_API_KEY` 等不会自动污染 Web pipeline。
  - settings 中 base_url 为空时，不会继承宿主机旧 base_url。
- 影响文件：
  - `web_api/settings_store.py`
  - `web_api/job_service.py`
  - `codes/run_pipeline.py`
  - `web_api/schemas.py`
  - `.gitignore`
- 测试覆盖：
  - `tests/test_environment_security.py`
    - 覆盖 Web pipeline env 不继承旧 provider 变量。
    - 覆盖 configured base_url 才进入环境。
    - 覆盖 pipeline role env 清理 provider 变量。
    - 覆盖 settings 接口不回显 API key。
    - 覆盖 start_job 不把 API key 写入状态、命令或 launcher log。
  - `tests/test_settings_store_resilience.py`
    - 覆盖 settings 接口在新增容错后仍不回显 API key。
- 兼容性/注意事项：
  - `.local/web_settings.json` 仍明文保存 API key；这是本地单用户模式下的当前行为，不是加密存储。
  - 下游脚本如果主动打印完整环境变量，仍可能泄露子进程环境中的 API key；当前测试覆盖 Web API 自身状态、命令和日志不泄露。

### Eval fallback 与多轮 auto-refine

- 问题背景：
  - 评测阶段支持模型 fallback，例如 `qwen3.7-max -> qwen3.7-plus`。
  - 在 auto-refine 多轮中，首轮 fallback 生效后，后续轮次应直接使用已生效模型，避免每轮重新撞原始 max 模型。
  - 最新修复前，多 fallback 链如 `qwen3.7-max -> qwen3.7-plus -> gpt-4o-mini` 在首轮降级到 `qwen3.7-plus` 后，后续轮次不会继续携带 `gpt-4o-mini`。
- 修复内容：
  - `codes/eval.py` 的 fallback 结果中记录：
    - `fallback_used`
    - `fallback_reason`
    - `fallback_from_model`
    - `fallback_eval_model`
    - `fallback_model_chain`
    - `fallback_remaining_models`
  - `codes/run_pipeline.py` 新增 fallback 链解析、格式化、剩余链提取逻辑。
  - `remember_fallback_eval_model()` 会记录当前生效模型，并将剩余 fallback 链写入 `active_eval_fallback_gpt_versions`。
  - `build_eval_cmd()` 在后续 auto-refine 轮次中使用 effective/current eval model，并继续传递剩余 fallback 链。
  - `run_status.json` 初始化和 fallback 激活后会记录 `requested_eval_model`、`effective_eval_model`、`eval_fallback_active`、`remaining_eval_fallback_models`、`eval_fallback_model_chain`。
- 修复后的实际行为：
  - 未发生 fallback 时，后续仍使用原始 eval model 和完整 fallback 链。
  - 单 fallback 链 `max -> plus` 生效后，后续轮次使用 `plus`，不再传递空 fallback 参数。
  - 多 fallback 链 `max -> plus -> gpt-4o-mini` 首轮降到 `plus` 后，后续轮次使用 `plus`，并继续携带 `gpt-4o-mini`。
  - 如果后续 `plus` 再遇到 quota-like 错误，`eval.py` 可以继续 fallback 到 `gpt-4o-mini`。
  - `eval.py` 只对 quota-like 的 `PermissionDeniedError` / `BadRequestError` 执行 fallback；非 quota-like 错误仍抛出。
- 影响文件：
  - `codes/eval.py`
  - `codes/run_pipeline.py`
  - `web_api/job_service.py`
  - `web_api/schemas.py`
- 测试覆盖：
  - `tests/test_eval_fallback_and_params.py`
    - 覆盖默认 `qwen3.7-max -> qwen3.7-plus` fallback。
    - 覆盖多 fallback 链保留剩余模型。
    - 覆盖连续 quota 错误继续 fallback 到后续模型。
    - 覆盖 `eval.py` 写入 actual/effective fallback 模型状态。
    - 覆盖 auto-refine 后续轮次复用 fallback 模型。
    - 覆盖多 fallback 链在后续 build_eval_cmd 中继续携带剩余 fallback。
    - 覆盖未发生 fallback 时仍使用原始模型和完整 fallback 链。
- 兼容性/注意事项：
  - 当时的实现曾兼容两组 Qwen 拼写；PR-05 官方模型清单复核后，当前默认 fallback 只保留 `qwen3.7-max -> qwen3.7-plus`。
  - 其它 fallback 链需要通过 settings 中的 `evaluation.fallback_models` 或 CLI `--eval_fallback_gpt_versions` 显式传入。

### 文档与工程说明同步

- 问题背景：
  - 当前仓库已经有 `docs/info/全仓目录结构与模块说明.md` 和 `docs/prompt/change_logs/`。
  - 测试确保 change log Markdown 可按 UTF-8 读取，并禁止引用旧版 change log 目录。
  - 实际读取时，`docs/info/全仓目录结构与模块说明.md` 和部分历史 change log 文本显示仍有乱码痕迹。
- 修复内容：
  - 当前代码层面已实现 Web API hardening、状态 JSON 容错、settings 容错、process lifecycle、artifact/log 边界、eval fallback 链延续等行为。
  - 本次 change log 明确按当前真实代码行为记录，不以旧文档描述为准。
- 修复后的实际行为：
  - `docs/prompt/change_logs/` 目录存在历史记录，测试能读取所有 `.md` 文件。
  - `tests/test_docs_utf8.py` 覆盖 UTF-8 可读性、private-use mojibake 字符检查和旧 change log 路径引用检查。
  - 文档内容与最新代码仍不是完全同步：尤其是多 fallback 剩余链延续、settings JSON 损坏容错，以及部分显示乱码问题。
- 影响文件：
  - `docs/info/全仓目录结构与模块说明.md`
  - `docs/prompt/change_logs/`
  - `tests/test_docs_utf8.py`
- 测试覆盖：
  - `tests/test_docs_utf8.py`
    - 覆盖 change log Markdown 可 UTF-8 读取。
    - 覆盖不包含 private-use 区乱码字符。
    - 覆盖 docs 不引用旧版 change log 目录。
- 兼容性/注意事项：
  - 文档当前为“部分同步”：路径和可读性测试通过，但文本显示和最新行为描述仍需用本 change log 后续同步。
  - 本次未修改文档文件，只输出可保存的 change log 内容。

## 测试结果

- 本次实际执行命令：

```
.\.venv\Scripts\python.exe -m pytest tests -q
```

- 当前结果：

```
79 passed, 1 warning in 6.22s
```

- warning：
  - `StarletteDeprecationWarning: Using httpx with starlette.testclient is deprecated; install httpx2 instead.`
  - 这是测试依赖层 warning，不影响当前用例通过情况。
- 本次相关测试文件：
  - `tests/test_artifacts_logs_boundaries.py`
    - 覆盖 repo 文件大小、扩展名、二进制判断、日志尾读、repo zip 并发生成、`list_jobs()` 大量 runs best-effort。
  - `tests/test_docs_utf8.py`
    - 覆盖 change log UTF-8 可读、private-use 字符检查、旧 change log 路径引用检查。
  - `tests/test_environment_security.py`
    - 覆盖 API key 不回显、子进程 env 隔离、base_url 不继承宿主残留值、状态/命令/log 不写 API key。
  - `tests/test_eval_fallback_and_params.py`
    - 覆盖 eval fallback、fallback 链延续、auto-refine 后续轮次模型选择、参数上下界、Web 创建任务参数拒绝。
  - `tests/test_json_state_resilience.py`
    - 覆盖坏/空状态 JSON 容错、状态 JSON 原子写失败保留旧文件、pipeline utils JSON 容错。
  - `tests/test_process_lifecycle.py`
    - 覆盖进程树取消和服务重启后的 `active/detached/orphaned/finished` 语义。
  - `tests/test_security_inputs.py`
    - 覆盖 job_id 路径穿越、伪 PDF 内容、外部 Markdown 路径拒绝、pipeline 层 skip_mineru 路径校验。
  - `tests/test_settings_store_resilience.py`
    - 覆盖 settings 文件不存在、损坏、空文件、结构非法、原子写失败和 API key 不回显。
- 关键行为仍缺少或仅部分覆盖：
  - MIME type 未被代码显式校验，因此没有“拒绝错误 MIME 但 PDF magic bytes 正确”的测试。
  - `repo/download` 正常和并发路径已覆盖；压缩过程中单个文件被并发删除或底层 OSError 的异常归一化未覆盖。
  - `pdf_markdown_path` 限制在 `runs/` 下已覆盖；“必须属于当前 job 子目录”不是当前代码行为，也无测试。
  - 文档测试覆盖 UTF-8 可读和旧路径引用；不覆盖中文文本是否已经从乱码恢复为可读中文。

## 已知限制

- `.local/web_settings.json` 仍以明文保存 API key；接口响应已脱敏，但本地文件访问者仍能读取。
- API key 会通过子进程环境传给 pipeline；当前代码避免写入状态、命令和 launcher log，但无法阻止下游脚本主动打印完整环境。
- `POST /jobs` 当前校验文件名 `.pdf` 和 PDF magic bytes，未显式校验上传 MIME type。
- `pdf_markdown_path` 当前限制在 `runs/` 目录内，不限制到当前 job 子目录。
- `.downloads/` 下生成的历史 repo zip 没有自动清理策略。
- 服务重启后不会重新接管 detached 进程，只会根据 PID 和状态文件识别并展示。
- `docs/info/全仓目录结构与模块说明.md` 和部分历史 change log 仍有显示乱码痕迹，且对最新 fallback 链延续和 settings JSON 容错描述不完整。

## 对外行为变化

- WebUI / API 调用者访问 `/jobs` 时，不会再因为 `/jobs/{job_id}` 的动态路由误匹配导致列表接口异常。
- 上传伪 PDF、越界参数、未配置 settings、外部 Markdown 路径时，`POST /jobs` 返回明确 4xx 错误。
- settings 文件不存在、为空、损坏或结构非法时，`/settings/status` 返回 `configured=false`；`POST /jobs` 返回明确的未配置错误，不返回 500。
- 查看历史任务、状态、摘要、日志和 repo 文件时，单个坏状态文件或不安全路径不会拖垮整个 API。
- repo 文件预览会拒绝大文件、二进制文件、非白名单扩展名和非 UTF-8 文本。
- repo 下载在重复和并发请求下生成唯一 zip，不互相覆盖。
- 取消运行中任务会尽量终止整棵进程树；服务重启后的 detached/orphaned 任务会显示为不可取消。
- eval auto-refine 多轮中，fallback 生效后后续轮次不会反复先请求原始 max 模型；多 fallback 链会继续携带剩余模型。
