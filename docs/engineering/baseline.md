# PR-00 工程基线

记录日期：2026-07-22

## 当前范围

本仓库的执行内核是 `codes/run_pipeline.py` 及现有阶段脚本；`web_api/` 是 FastAPI 控制面，`web_ui/` 是 React + Vite 本地控制台。默认 legacy runtime 由 FastAPI 启动 Pipeline 子进程，sqlite runtime 则由独立 Worker 启动；任务 artifacts 保存在本地文件系统，SQLite 仅保存最小控制面元数据。

当前提供标准库 SQLite 持久化控制面，但默认 `JOB_RUNTIME=legacy` 仍保持既有子进程行为；`JOB_RUNTIME=sqlite` 由 `python -m web_api.worker` 启动独立单并发 Worker，并提供 PID/create-time 进程级 reconciliation、固定阶段 checkpoint 与默认最多 1 次的受限恢复。checkpoint adapter 只由 SQLite Worker 启用，legacy runtime 不获得阶段恢复。当前没有任意语句级 checkpoint、exactly-once、SSE、WebSocket、Celery、Redis、PostgreSQL、Kubernetes 或 LangGraph。WebUI 对活跃任务每 2 秒轮询，FastAPI 只支持单实例。

PR-05 的 Provider Registry v1 是封闭且深度不可变的显式契约；未知 schema 字段、非法 request options、非有限/越界数字和不可信 fallback 直接以脱敏错误拒绝。`GET /api/v1/providers` 从当前 Registry 动态生成 WebUI 可选项，只返回非敏感 ID/能力并使用 `no-store`；`settings/status` 仍只返回布尔状态。SQLite queued job 固定 Provider/Model/fallback、Registry version 和 Contract SHA-256，不保存 key 或 base URL。Worker 只允许同一选择下轮换凭据，不匹配或缺少快照时在 `Popen` 前以 `provider_settings_changed` fail closed；legacy runtime 不变。API key 仍是本地明文 settings 与临时子进程环境数据，不是加密存储。PR-06 cost ledger 和 PR-07 SSE 尚未实现。

PR-06A 的 Evaluation Contract 将 execution、evaluation、quality verdict 和 repair policy 分离。Evaluator timeout、Provider protocol error、malformed response、unavailable 和 quorum 不足不会被写成质量不合格；只有 evaluator completed 且 quality rejected 才能触发 repair。`files_to_fix=[]` 表示不修改任何文件，非空 repair 目标必须重新通过 TaskManifest 和 repo 路径验证。Evaluation result、feedback、repo status、SQLite events 和 summaries 不保存 prompt、完整模型响应或凭据。Cost ledger、预算 enforcement、SSE 和 WebUI 实时控制仍未实现。

SQLite Worker 对本地进程先观察真实退出码，再消费取消命令：exit 0 直接 completed；在线非零退出与 Worker 重启后 confirmed-missing 使用同一恢复资格判断；晚到 cancel 失败为 `already_finished`，不会把自然退出误记为 canceled 或触发恢复。取消前续租并重新验证 PID/create time/process group，Windows 强制终止绑定到同一进程句柄并有界等待，只有完整树已确认退出才持久化 canceled。单 job 的设置、输入、命令或进程创建错误记录为 `process_launch_failed` 后继续轮询；SQLite、fencing、持久化、乐观冲突和未知错误保持 fail fast。

PR-04B checkpoint 采用固定 schema、64 KiB 上限、原子替换、连续 stage chain 和相对路径/size/SHA-256 产物指纹。completed checkpoint 验证完整恢复状态闭包：可信 Markdown、Planning 输出和与 Planning 指纹一致的 TaskManifest、配置、分析结果，以及当前 Manifest repo 成员和必要状态结果；不会只复核最新 JSON 的少量本阶段产物。running/failed/部分阶段从上一个可信 completed 边界指向的阶段开头重跑，语义为 stage-boundary at-least-once。只有完整登记身份且已确认死亡的进程可恢复；`identity_unresolved` 永不恢复并继续占用唯一槽位。默认最多恢复 1 次，恢复使用新 launch token、保留旧进程历史，并在同一 lease-fenced 事务中持久化恢复计数。legacy runtime 不启用 checkpoint，系统不保证任意语句级恢复或 exactly-once。

## 验证命令

从仓库根目录执行 Python 测试：

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q
```

执行前端类型检查和构建：

```powershell
Set-Location .\web_ui
npm run typecheck
npm run build
npm run verify:same-origin
npm run smoke
npm run smoke:prod
Set-Location ..
```

`npm run build` 生成 `web_ui/dist/`；`npm run smoke:prod` 必须在构建后运行。`node_modules/`、`dist/` 和 `*.tsbuildinfo` 不进入版本管理，`package-lock.json` 必须进入版本管理。

## 2026-07-22 PR-04A 重新验收快照

- `python -m compileall -q codes web_api tests`：通过。
- PR-04A、API、安全边界与文档定向测试：155 passed。
- `python -m pytest -q -rs`：334 passed。
- WebUI `typecheck`、`build`、`verify:same-origin`、`smoke`、`smoke:prod`：全部通过。
- Python 测试报告 1 个既有 `StarletteDeprecationWarning`（Starlette `TestClient` 与当前 httpx 兼容提示），无 skip；该 warning 不在 PR-04A 范围内。

以上数字是本日期、本 checkout 的验收快照，不作为长期固定测试数量。Git 暂存区、无关修改和运行产物仍需在每次交付时独立检查。

## 2026-07-22 PR-04B 验收快照

- `python -m compileall -q codes web_api tests`：通过。
- checkpoint、恢复、Repository、Worker、SQLite/API 与 TaskManifest 定向测试：248 passed。
- `python -m pytest -q -rs`：388 passed。
- WebUI `typecheck`、`build`、`verify:same-origin`、`smoke`、`smoke:prod`：全部通过。
- Python 测试仍只报告 1 个既有 `StarletteDeprecationWarning`，无 skip；未运行真实 LLM、MinerU、vLLM 或完整 Pipeline。

该快照验证的是固定阶段边界与默认一次恢复，不代表 exactly-once 或任意崩溃点自动恢复。恢复测试使用 fake stage/fake pipeline 和临时数据库。

## 测试政策

- 自动化测试只使用 fake provider、fake pipeline、mock/monkeypatch 和临时目录。
- 测试不得调用真实付费 LLM、MinerU、vLLM 或完整耗时 Pipeline。
- 测试不得依赖开发者机器的绝对路径、真实 API key 或既有运行目录。
- 涉及输入、路径、导出、环境变量或敏感数据的新安全逻辑，先增加失败/攻击测试，再实现修复。
- 不允许通过捕获所有异常、返回空结果或强制成功退出掩盖错误。

## Provider/Model 契约基线（PR-05）

- 远程 LLM 调用以 `provider_id + model_id` 为唯一选择键；禁止模型前缀猜测、密钥兜底猜测或隐式 OpenAI endpoint。
- `codes/providers.v1.json` 是默认版本化清单，`codes/provider_registry.py` 负责结构校验、运行时凭据/base URL 解析、请求 `max_n`/context/output/JSON Schema 门禁，以及 timeout/retry/concurrency 约束。
- 未验证的 context window、output 上限、能力或价格必须保持 `null`/`unknown`。只有同时记录币种、输入/输出单价和生效日期的价格才可用于成本计算。
- Web Settings 保存和 job 入队前均复核 Registry；未知 Provider/模型、空白密钥或必要 base URL 缺失返回脱敏 422，且不会创建队列记录或启动 Pipeline。
- `max_n=1` 时，应用层按 `generated_n` 拆分独立单候选请求；`generated_n=1` 不显式发送 `n`，总候选数仍限制为 1–32，请求还受模型并发上限约束。fallback model 必须按字面值解析并注册在同一显式 Provider 下，非法链在客户端创建前失败。
- fake provider 测试覆盖正常响应、`max_n=1`、未知模型、缺少 key/base URL、429、timeout、部分 usage 与 context 超限；自动化测试不发出真实付费请求。
- CLI 阶段环境是最小投影：角色变量优先，缺失时读取所选模型契约声明的 Provider 原生变量；不复制父进程完整环境，不携带未选 Provider 或另一角色的凭据。
- 默认模型清单是项目当前支持清单，不是官方模型全集镜像。2026-08-06 依据 DeepSeek、Alibaba Cloud Model Studio、Kimi 与 OpenAI 官方文档复核：OpenAI active 仅保留 `gpt-4.1-mini`、`gpt-4o-mini`，`o3-mini`/`o4-mini` 因 Deprecated 不进入新任务；DeepSeek 仅保留 V4 Pro/Flash；Qwen 保留官方 `qwen3.8-max` 与 `qwen3.7-*` 拼写；Kimi 保留 `kimi-k3`、`kimi-k2.7-code`、`kimi-k2.7-code-highspeed`、`kimi-k2.6`，其中 K3 通过省略固定参数支持。
- 默认 `base_url=null` 表示要求用户显式配置 endpoint/地域，并非官方 endpoint 未知；JSON Output/Structured Output 不自动等价为 JSON Schema 支持。

PR-05 本地验收快照（2026-08-06）：定向 Provider Registry 验收 `199 passed, 1 warning`；Python 全量测试 `567 passed, 1 warning`；WebUI `npm.cmd ci`、`typecheck`、`build`、`verify:same-origin`、`smoke`、`smoke:prod` 均 exit 0。沙箱身份下曾因 Windows ACL/进程树权限出现假失败，以上通过数来自仓库所有者身份复跑。

## 版本管理基线

应进入版本管理：

- `codes/`、`web_api/`、`web_ui/src/` 和 `web_ui/scripts/` 源码；
- `tests/` 中的 Python 测试；
- 根 `README.md`、`docs/` 下的说明、ADR 和 change logs；
- `web_ui/package.json`、`web_ui/package-lock.json`、TypeScript/Vite 配置和 `web_ui/index.html`；
- `.gitignore`、`requirements.txt`、启动脚本、经确认可分发且有意长期维护的示例输入及上游 `LICENSE`。

必须保持忽略：

- Python/Node 环境和缓存：`.venv/`、`__pycache__/`、`.pytest_cache/`、`node_modules/`、`*.pyc`、`*.tsbuildinfo`；
- 构建与运行产物：`dist/`、`.local/`、`runs/`、`outputs/`、`results/`、`mineru_outputs/`、`*.log`；
- secrets：`.env`、`.env.*`（保留可提交的 `.env.example`）、`api.txt`、私钥和证书文件。

## 已知限制

- `.local/web_settings.json` 是本地明文 JSON；接口脱敏和本地 ACL 不等于加密凭据存储。
- legacy 状态/摘要 JSON 仍可能在进程中断时损坏，FastAPI 重启后不能接管原 legacy 进程；PR-04B checkpoint 只适用于 sqlite Worker，且默认最多恢复 1 次。它不覆盖任意崩溃点、不保证 exactly-once，重跑阶段可能重复 Provider/MinerU 调用、成本和文件写入。
- sqlite Worker 若发现 running job 尚未完整登记 PID/create time/process group，会将其置为 `identity_unresolved` 隔离态并阻止后续任务启动，不会自动把未知进程当作已死亡。该状态可能需要人工检查并重启本地环境。
- checkpoint 缺失、版本/schema/路径/链接/指纹校验失败、取消竞态或恢复预算耗尽时不会启动恢复；任务会返回稳定失败，可能需要人工检查本地 run 目录。
- sqlite 取消仍由 job command 驱动；自然退出优先，未验证或超时的树级终止不会写入 canceled。Windows 的强制阶段不使用 `taskkill /T /F`，但操作系统拒绝打开/终止/等待句柄时会显式失败并需要人工检查。
- legacy runtime 中 FastAPI 重启后不能重新接管已启动的 Pipeline；sqlite runtime 的独立 Worker 不受 API 重启影响。
- 历史 repo zip 没有自动清理策略。
- `pdf_markdown_path` 被限制在 `runs/` 下，但尚未收紧到当前 job 子目录。
- Vite 开发端口与 CORS 白名单共同固定为 `5173`；更改时必须同步更新。
- 本地单用户边界不提供远程部署、认证、授权或多租户隔离。
