# 018 WebUI 前 API 契约收口与错误语义统一

修改时间：2026-07-09

## 简要总结

统一了 FastAPI WebUI 后端的错误响应格式、HTTP 状态码语义和任务状态字段语义，让前端可以稳定依赖 `error.code`、`job.status`、`job.process_state`、`job.cancelable` 和 `job.cancel_unavailable_reason`，不再需要解析零散英文字符串。

## 问题背景

在本次修改前，`web_api/main.py` 大量使用 `HTTPException(detail="...")` 返回字符串错误，且不同接口对“任务不存在”“repo 不存在”“不可取消”“上传无效”“日志不存在”等场景的状态码和错误文本并不统一。与此同时，`GET /jobs` 和 `GET /jobs/{job_id}` 的核心状态字段也缺少稳定收口，前端难以可靠渲染：

- 不同接口需要依赖不同英文 `detail`
- `detached` / `orphaned` 语义主要体现在文本里，而不是稳定字段里
- 状态 JSON 损坏时虽然已做 best-effort 容错，但返回体仍不够适合 WebUI 直接消费
- 错误响应可能透出本地绝对路径或内部异常文本

## 修改内容

### 1. 新增统一错误层

- 新增 `web_api/errors.py`
  - 定义统一错误 payload：

```json
{
  "error": {
    "code": "settings_not_configured",
    "message": "API settings are not configured.",
    "details": {}
  }
}
```

  - 定义项目内稳定错误类型与错误码
  - 为 FastAPI 注册统一异常处理器
  - 为未预期异常统一返回 `500 + internal_error`

### 2. 统一主动错误的状态码与错误码

本次收口覆盖了以下主要场景：

- `400`
  - `settings_not_configured`
  - `invalid_job_id`
  - `invalid_upload`
  - `invalid_parameter`
  - `invalid_repo_path`
- `404`
  - `job_not_found`
  - `repo_not_available`
  - `log_not_found`
  - `artifact_not_available`
- `409`
  - `job_not_cancelable`
- `413`
  - `file_too_large`
- `415`
  - `unsupported_file_type`
  - `binary_file_not_supported`
- `500`
  - `internal_error`

保留 FastAPI 原生 `422` 用于框架层参数校验错误。

### 3. 收口 Job 状态模型

在 `web_api/job_service.py` 中统一了任务视图构造逻辑，`GET /jobs` 和 `GET /jobs/{job_id}` 现在都会稳定返回：

- `status`
  - `queued | running | completed | failed | canceled | unknown`
- `process_state`
  - `active | finished | detached | orphaned | none`
- `cancelable`
- `cancel_unavailable_reason`
  - `already_finished`
  - `detached_after_restart`
  - `orphaned_process`
  - `process_not_registered`

并明确：

- `starting` 被规范化为 `queued`
- `detached` / `orphaned` 不再混进业务 `status`
- 状态文件损坏或缺失时返回 `unknown`，不返回 500
- `cancelable` 与 `process_state` 保持一致

### 4. 收口各接口语义

- `GET /settings/status`
  - settings 缺失、空文件、损坏、结构非法时稳定返回 `configured=false`
  - 继续保持不回显 API key
- `POST /jobs`
  - 非 PDF 扩展名：`415 + unsupported_file_type`
  - 伪 PDF 内容：`400 + invalid_upload`
  - 越界参数与非法 `pdf_markdown_path`：`400 + invalid_parameter`
- `GET /jobs`
  - 单个 run 状态 JSON 损坏时仍返回列表
  - 列表项与详情接口共享核心状态字段
- `GET /jobs/{job_id}`
  - job 不存在：`404 + job_not_found`
  - 非法 `job_id`：`400 + invalid_job_id`
  - 状态文件损坏：`200 + status=unknown`
- `POST /jobs/{job_id}/cancel`
  - completed/failed/canceled、detached、orphaned、未注册进程统一返回 `409 + job_not_cancelable`
- `GET /jobs/{job_id}/logs`
  - job 存在但无日志：`200 + logs=[]`
  - 指定日志不存在：`404 + log_not_found`
  - 非法日志文件名：`400 + invalid_repo_path`
- `GET /jobs/{job_id}/artifacts`
  - repo/results/logs 未生成时保持 `200`，不再依赖目录存在与否抛 500
- `GET /jobs/{job_id}/repo/file`
  - repo 不存在：`404 + repo_not_available`
  - 路径穿越：`400 + invalid_repo_path`
  - 文件过大：`413 + file_too_large`
  - 二进制或非 UTF-8 预览不支持：`415`
- `GET /jobs/{job_id}/repo/download`
  - repo 不存在：`404 + repo_not_available`
  - 压缩过程中的未预期异常：`500 + internal_error`

### 5. 补充契约测试

- 新增 `tests/test_api_contract.py`
  - 统一错误 schema
  - `settings_not_configured`
  - `job_not_found`
  - `invalid_job_id`
  - `job_not_cancelable`
  - repo/file 的路径穿越、过大、二进制、扩展名不支持、非 UTF-8
  - logs 的无日志、日志不存在、非法日志名
  - artifacts 未生成时返回 200
  - `/jobs` 不被 `/jobs/{job_id}` 误捕获
  - 错误响应不泄露 API key 与敏感本地路径

同时同步更新了既有测试断言，使原有安全、容错、进程生命周期与 fallback 测试继续通过。

## 修复后的实际行为

修复后，前端不再需要根据 `detail` 英文字符串决定 UI 行为，而可以稳定依赖：

- HTTP status
- `error.code`
- `job.status`
- `job.process_state`
- `job.cancelable`
- `job.cancel_unavailable_reason`

典型变化：

- `/jobs/{job_id}/cancel` 对已完成任务不再返回零散字符串，而是稳定返回 `409 + job_not_cancelable + reason=already_finished`
- 服务重启后的遗留运行任务会表现为 `status=running` 且 `process_state=detached` 或 `orphaned`，前端可直接禁用取消按钮
- `repo/file` 的各种失败场景现在可以被前端稳定区分为路径非法、repo 缺失、文件过大、二进制不支持或类型不支持
- `repo/download` 内部压缩失败时不会把本地绝对路径返回给调用方

## 影响范围

- 模块
  - `web_api/errors.py`
  - `web_api/main.py`
  - `web_api/job_service.py`
  - `web_api/artifact_service.py`
  - `web_api/log_service.py`
  - `web_api/path_security.py`
  - `web_api/json_io.py`
  - `web_api/schemas.py`
- 测试
  - `tests/test_api_contract.py`
  - `tests/test_artifacts_logs_boundaries.py`
  - `tests/test_eval_fallback_and_params.py`
  - `tests/test_process_lifecycle.py`
  - `tests/test_security_inputs.py`
  - `tests/test_settings_store_resilience.py`
- 文档
  - `docs/info/全仓目录结构与模块说明.md`
  - `docs/prompt/change_logs/README.md`

## 验证方式

实际执行：

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q
```

结果：

```text
107 passed, 1 warning in 8.91s
```

warning 为 `starlette.testclient` 对 `httpx` 的弃用提示，不影响本次契约行为验证。

## 已知限制 / 注意事项

- `.local/web_settings.json` 仍为本地明文存储，接口脱敏不等于磁盘加密
- `pdf_markdown_path` 当前限制在 `runs/` 下，尚未进一步收紧到“当前 job 子目录”
- `.downloads/` 下历史 zip 仍无自动清理策略
- `GET /jobs/{job_id}/summary` 未被纳入这次 WebUI MVP 契约主收口范围，但已保持 job 缺失与损坏文件的 best-effort 语义
