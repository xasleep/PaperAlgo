# 配置文件可靠性与 Eval fallback 链延续修复

## 本次更新概述

- `.local/web_settings.json` 已改为原子写入；读取损坏、半写入、空文件或结构非法时，不再让 `/settings/status`、`POST /jobs` 直接返回 500。
- 损坏或不可用的 Web settings 当前统一按“未配置”处理；settings 状态接口仍只返回脱敏信息，不回显 API key。
- `eval.py` 现在会记录 fallback 生效后的剩余模型链，`run_pipeline.py` 在 auto-refine 后续轮次会继续携带剩余 `fallback_gpt_versions`。
- 已补充覆盖 settings 容错、settings 原子写失败、多级 fallback 链、单 fallback 链和未 fallback 行为的测试。

## 修复一：配置文件原子写入与损坏容错

- 问题背景：
  - `web_api/settings_store.py` 原先直接使用普通 JSON 读写 `.local/web_settings.json`。
  - 当 settings 文件半写入、损坏、为空或结构非法时，依赖 `load_settings()` 的接口可能因异常冒泡返回 500。
- 修复内容：
  - `save_settings()` 改为调用 `write_json_file_atomic()`，通过临时文件、`fsync()` 和 `os.replace()` 写入 settings。
  - `load_settings()` 改为调用 `read_json_file()` 容错读取，并对 Pydantic `ValidationError` / `TypeError` 做保护。
  - settings 文件不存在、损坏、空文件、非对象 JSON 或 schema 不合法时，`load_settings()` 返回 `None`。
  - settings 状态仍通过 `ProviderSettingsStatus` / `EvaluationSettingsStatus` 输出 `has_api_key`，不输出 `api_key` 字段。
- 修复后的实际行为：
  - `/settings/status` 遇到缺失或损坏的 `web_settings.json` 时返回 200，响应中 `configured=false`。
  - `POST /jobs` 遇到缺失或损坏的 `web_settings.json` 时返回 400，错误为 `API settings are not configured.`。
  - `POST /settings` 和 `/settings/status` 不回显 API key。
  - settings 原子写失败时，已有 settings 文件内容保持不变，并清理临时文件。
- 影响文件：
  - `web_api/settings_store.py`
  - `web_api/json_io.py`
  - `web_api/main.py`
- 测试覆盖：
  - `tests/test_settings_store_resilience.py`
    - 覆盖 settings 文件不存在、损坏、空文件、结构非法。
    - 覆盖 `/settings/status` 不返回 500。
    - 覆盖 `POST /jobs` 返回明确 400。
    - 覆盖 settings 原子写失败保留旧文件。
    - 覆盖 settings 接口不回显 API key。
  - `tests/test_environment_security.py::test_settings_endpoints_do_not_echo_api_keys`
    - 覆盖既有 settings 接口脱敏行为不回归。
  - `tests/test_json_state_resilience.py`
    - 覆盖共享 JSON 原子写/容错模式在状态文件上的行为；不是专门针对 `web_settings.json`，但验证了同一类 JSON 工具与状态文件可靠性。
- 兼容性/注意事项：
  - `.local/web_settings.json` 仍是本地明文 JSON 存储；本次修复的是接口脱敏、容错读取和原子写入，不是磁盘加密。
  - 损坏 settings 当前不会在 `/settings/status` 中暴露具体错误详情，而是统一表现为未配置。

## 修复二：Eval fallback 链在多轮 auto-refine 中的持续生效

- 问题背景：
  - 已有逻辑支持评测模型 fallback，也支持首轮 fallback 后后续 auto-refine 轮次复用生效模型。
  - 但多级 fallback 链存在缺口：例如 `qwen3.7-max -> qwen3.7-plus -> gpt-4o-mini`，首轮降到 `qwen3.7-plus` 后，后续轮次没有继续携带 `gpt-4o-mini`。
- 修复内容：
  - `codes/eval.py` 在 fallback 成功后写入 `fallback_remaining_models`，表示当前生效模型之后仍可用的剩余 fallback 模型。
  - `codes/run_pipeline.py` 新增 fallback 链解析、格式化和剩余链提取逻辑。
  - `remember_fallback_eval_model()` 现在会保存：
    - `active_eval_gpt_version`
    - `active_eval_fallback_gpt_versions`
    - `remaining_eval_fallback_models`
    - `eval_fallback_model_chain`
  - `build_eval_cmd()` 后续会以当前 effective eval model 作为 `--gpt_version`，并继续传递剩余 `--fallback_gpt_versions`。
- 修复后的实际行为：
  - 初始链 `qwen3.7-max,qwen3.7-plus,gpt-4o-mini` 首轮降到 `qwen3.7-plus` 后，下一轮 eval 命令使用 `--gpt_version qwen3.7-plus`，并继续携带 `--fallback_gpt_versions gpt-4o-mini`。
  - 单 fallback 链 `qwen3.7-max -> qwen3.7-plus` 生效后，后续轮次使用 `qwen3.7-plus`，且不再传递空 fallback 参数。
  - 没有发生 fallback 时，后续仍使用原始 eval model 和完整 fallback 链。
  - `eval.py` 写出的 repo/eval 状态字段与 `run_pipeline.py` 读取字段保持一致，包括 requested/effective model、fallback 是否激活、fallback chain 和剩余 chain。
- 影响文件：
  - `codes/eval.py`
  - `codes/run_pipeline.py`
  - `web_api/job_service.py`：通过 settings 中的 `evaluation.fallback_models` 继续向 pipeline 传入初始 fallback 链。
- 测试覆盖：
  - `tests/test_eval_fallback_and_params.py`
    - 覆盖默认单 fallback：`qwen3.7-max -> qwen3.7-plus`。
    - 覆盖多级 fallback 首轮成功后保留剩余链：`gpt-4o-mini`。
    - 覆盖连续 quota-like 错误时从 `qwen3.7-max` 继续 fallback 到 `qwen3.7-plus`，再到 `gpt-4o-mini`。
    - 覆盖 auto-refine 后续 `build_eval_cmd()` 继续携带剩余 fallback。
    - 覆盖未发生 fallback 时不改变原始模型和 fallback 链。
    - 覆盖状态字段记录 effective model、fallback active 和 remaining fallback models。
- 兼容性/注意事项：
  - fallback 仍只在 `PermissionDeniedError` / `BadRequestError` 且判断为 quota-like 错误时触发；非 quota-like 错误不会被吞掉。
  - 当时的实现曾兼容两组 Qwen 拼写；PR-05 官方模型清单复核后，当前默认 fallback 只保留 `qwen3.7-max -> qwen3.7-plus`。更长链需要通过 `--eval_fallback_gpt_versions` 或 Web settings 中的 `evaluation.fallback_models` 传入，并且每个 model ID 都必须注册在同一 Provider 下。

## 测试结果

- 已执行相关测试命令：

```
.\.venv\Scripts\python.exe -m pytest tests\test_settings_store_resilience.py tests\test_eval_fallback_and_params.py tests\test_json_state_resilience.py tests\test_environment_security.py::test_settings_endpoints_do_not_echo_api_keys -q
```

- 结果：

```
29 passed, 1 warning in 1.63s
```

- warning：
  - `StarletteDeprecationWarning: Using httpx with starlette.testclient is deprecated; install httpx2 instead.`
  - 该 warning 来自测试依赖层，不影响本次相关测试通过。
- 涉及的测试文件：
  - `tests/test_settings_store_resilience.py`
    - 覆盖 `web_settings.json` 缺失、损坏、空文件、非法结构、原子写失败和 API key 不回显。
  - `tests/test_eval_fallback_and_params.py`
    - 覆盖单 fallback、多级 fallback、连续 fallback、auto-refine 后续命令携带剩余 fallback 链，以及状态字段记录。
  - `tests/test_json_state_resilience.py`
    - 覆盖共享 JSON 原子写与损坏容错模式在状态文件上的行为；作为 `json_io` / 状态 JSON 可靠性的相关回归。
  - `tests/test_environment_security.py::test_settings_endpoints_do_not_echo_api_keys`
    - 覆盖 settings 接口脱敏行为不回归。
- 测试覆盖边界：
  - 已覆盖损坏、空文件、非法结构和缺失 settings。
  - 已覆盖多级 fallback 链，但使用 mock 模拟 quota-like 错误和 eval 命令构造，没有执行真实外部模型请求。
  - 已覆盖 API 响应不回显 API key；未覆盖本地磁盘加密，因为当前代码没有实现磁盘加密。

## 已知限制

- `.local/web_settings.json` 仍为明文本地文件，本次只保证原子写入、容错读取和接口脱敏。
- 损坏 settings 当前统一视为未配置，不向 `/settings/status` 返回具体损坏原因。
- fallback 链延续只对当前 `run_pipeline.py` 进程内的 auto-refine 多轮生效；如果 pipeline 进程本身退出后外部重新启动，需要重新从状态/参数进入新流程。
- fallback 触发条件仍限定为 quota-like 的 `PermissionDeniedError` / `BadRequestError`，不覆盖所有模型调用异常。
