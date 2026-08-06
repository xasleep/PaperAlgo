# 025 Explicit Provider Model Registry

修改时间：2026-08-06

## 简要总结

PR-05 将远程 LLM 选择收口为显式 `provider_id + model_id` Registry 契约，并完成 Pipeline、Evaluation、Web API、SQLite Worker、WebUI 和文档验收。系统不再根据模型名前缀、已有 API key 或默认 OpenAI endpoint 猜测 Provider。

## 问题背景

变更前，远程调用路径存在多处隐式推断：模型名前缀决定 Provider，未知模型可能落到 OpenAI-compatible 默认 endpoint，Evaluation 的 `n` 能力和 fallback 链靠硬编码，成本统计来自未注明复核日期的表格。Web Settings 和 SQLite queued job 也没有持久化非敏感的 Provider 选择快照，settings 轮换后存在静默重绑定风险。

## 修改内容

- 新增 `codes/providers.v1.json` 和 `codes/provider_registry.py`，使用封闭 schema 校验 Provider、Model、Pricing 与 `request_options`，拒绝 bool 数字、NaN、Infinity、未知字段、非法 fallback 和非 UTF-8/malformed JSON。
- 所有 Planning、Analysis、Coding、Repair、Evaluation、RAG config、Debugging 远程入口通过 Registry 创建客户端，阶段 CLI 保留既有迁移参数但必须显式传入 Provider。
- Evaluation 在 `max_n=1` 时拆分多次单候选请求，`generated_n=1` 不显式发送 `n`，合并 `choices`、`usage`、`responses`，并在 Provider 返回 choices 少于请求数量时抛出稳定脱敏错误。
- Web Settings 保存和 job 入队前复核 Provider、模型、fallback、API key 和必要 base URL；`GET /api/v1/providers` 从当前 Registry 动态返回 no-store、非敏感发现清单。
- SQLite queued job 保存 reproduce/evaluation Provider、Model、fallback、Registry version 和 Contract SHA-256；Worker 在 `Popen` 前检测快照缺失、不匹配或无效轮换凭据并以 `provider_settings_changed` fail closed。
- `codes/utils.py` 的硬编码猜价被移除，只有 Registry pricing 为 `configured` 且币种、输入/输出费率和 effective date 完整时才计算成本，否则返回 unavailable。
- 官方模型资料于 2026-08-06 复核；Registry 是 PaperAlgo 当前支持清单，不是官方全集镜像。OpenAI active allowlist 仅保留 `gpt-4.1-mini`、`gpt-4o-mini`，`o3-mini`/`o4-mini` 因 Deprecated 被拒绝；Kimi 默认 active allowlist 包含官方确认的 `kimi-k3`、`kimi-k2.7-code`、`kimi-k2.7-code-highspeed`、`kimi-k2.6`，其中 K3 使用空 `request_options` 并省略固定参数；Qwen 保留官方 `qwen3.8-max`。

## 修复后的实际行为

- 未知 Provider/模型、空白 key、缺失 base URL、重复 fallback、主模型自引用 fallback、空 token、空白 token、非字符串元素和未注册 fallback 在保存 settings、CLI 校验或直接 Evaluation 调用时 fail closed；Evaluation 在创建客户端前拒绝非法链。
- WebUI 不维护第二套硬编码 Provider allowlist，Registry 加载失败时 Settings 表单 fail closed，不读回或显示凭据。
- CLI 子进程环境只投影最小系统变量和当前角色所需凭据；相对 `PAPER2CODE_PROVIDER_REGISTRY_PATH` 在父进程中规范为绝对路径。
- SQLite idempotency replay 返回原 job 与原快照；同一选择下的安全凭据轮换允许继续，选择或 Contract 指纹变化不会到达 `Popen`。

## 影响范围

- API：新增 `GET /api/v1/providers`；`POST /api/v1/settings` 和 `POST /api/v1/jobs` 增加 Registry 校验。
- CLI/Pipeline：阶段脚本新增必需 `--provider`，`run_pipeline.py` 继续保留 `--reproduce_provider`、`--eval_provider` 等迁移面。
- SQLite：jobs 表新增非敏感 Provider 选择快照字段；历史 queued/running 行缺少快照时 fail closed。
- WebUI：Settings 页从 Registry discovery 渲染 Provider/Model 控件。
- Docs：README、baseline、ADR、模块说明和历史 change log 标注 PR-05 后的新边界。

## 验证方式

- `.\.venv\Scripts\python.exe --version`：Python 3.12.9。
- `.\.venv\Scripts\python.exe -m pip check`：No broken requirements found。
- `node --version`：v22.17.1。
- `npm.cmd --version`：10.9.2。
- `.\.venv\Scripts\python.exe -m compileall -q codes web_api tests`：exit 0。
- `npm.cmd ci`：exit 0，added/audited 76 packages；npm audit 提示 3 vulnerabilities（2 moderate, 1 high），未在本 PR 修复依赖。
- `npm.cmd run typecheck`：exit 0。
- `npm.cmd run build`：exit 0，Vite built 1592 modules。
- `npm.cmd run verify:same-origin`：exit 0，3 files checked。
- `.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider --basetemp <validationRoot>\targeted-final-owner tests/test_provider_registry.py tests/test_eval_fallback_and_params.py tests/test_api_contract.py`：exit 0，`199 passed, 1 warning`。
- `.\.venv\Scripts\python.exe -m pytest tests -q -rs -p no:cacheprovider --basetemp <validationRoot>\full-pytest-owner`：exit 0，`567 passed, 1 warning`。
- `npm.cmd run smoke`：exit 0，routes `/settings`、`/jobs/new`、`/jobs`、`/jobs/smoke-job` passed。
- `npm.cmd run smoke:prod`：exit 0，routes `/`、`/settings`、`/jobs`、`/jobs/new`、`/jobs/smoke-job`、`/api/v1/health`、`/api/v1/jobs` passed。

沙箱内运行涉及 Windows ACL 和进程树终止的 pytest/smoke 清理曾出现权限误报；同一命令在非沙箱权限下通过。未调用真实 LLM、MinerU、vLLM 或完整耗时 Pipeline。

## 已知限制 / 注意事项

- `.local/web_settings.json` 仍是本地明文 JSON；Registry 和 API 脱敏不等于加密凭据存储。
- `base_url: null` 是要求本地显式 endpoint/地域配置的策略。
- 官方 pricing 若缺少完整 effective-date tuple 或存在无法用当前 schema 安全表达的分层价格，仍保持 `unknown`，成本显示 unavailable。
- Anthropic 官方模型已复核，但本 PR 没有实现 Anthropic Messages API adapter，因此默认 `claude` Provider 仍无 active model。
- PR-06 durable cost ledger 与 PR-07 SSE 不是本次目标。
