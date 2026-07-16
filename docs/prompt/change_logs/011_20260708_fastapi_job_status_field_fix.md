# 011 FastAPI 任务状态字段冲突修复

- 修改时间：2026-07-08 21:46
- 简要总结：修复 `/jobs` 创建任务时返回 `Internal Server Error` 的问题。原因是 `job_service.py` 调用 `update_status()` 时同时使用了位置参数 `run_dir` 和关键字参数 `run_dir`，导致 Python 抛出参数重复错误。

## 修改文件

| 文件 | 修改内容 |
|---|---|
| `web_api/job_service.py` | 将写入状态 JSON 的字段名从 `run_dir` 改为 `run_directory`，避免与 `update_status(run_dir, **updates)` 的函数参数名冲突。 |
| `docs/prompt/change_logs/README.md` | 增加第 011 篇变更记录索引。 |

## 修改原因

调用 `/jobs` 时，FastAPI 返回 500。复现后得到异常：

```text
TypeError: update_status() got multiple values for argument 'run_dir'
```

这是服务端代码字段命名冲突，不是用户上传命令或 API key 配置问题。

## 验证方式

使用 FastAPI `TestClient` 复现 `/jobs` 上传请求，结果：

```text
POST /jobs -> 200
```

随后立即调用 `cancel_job(job_id)` 取消 smoke test 任务，避免继续执行 MinerU 和大模型调用。
