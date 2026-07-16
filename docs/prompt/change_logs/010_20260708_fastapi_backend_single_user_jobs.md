# 010 FastAPI 后端骨架与单用户任务接口

- 修改时间：2026-07-08 21:25
- 简要总结：新增 `web_api/` FastAPI 服务层，把现有 `codes/run_pipeline.py` 包装成单用户后端接口，支持保存模型 API 设置、上传 PDF 创建任务、查询状态、读取日志和取消运行中的任务。

## 修改文件

| 文件 | 修改内容 |
|---|---|
| `web_api/__init__.py` | 新增 FastAPI 后端包标记。 |
| `web_api/config.py` | 定义项目路径、运行目录、本地配置路径、Provider 和运行模式常量。 |
| `web_api/schemas.py` | 定义设置、任务创建、日志和取消任务相关的 Pydantic 请求/响应模型。 |
| `web_api/settings_store.py` | 新增单用户设置持久化逻辑，将 API key 和模型配置保存到 `.local/web_settings.json`。 |
| `web_api/job_service.py` | 新增任务服务：保存上传 PDF、构造 `run_pipeline.py` 命令、注入复现/评测环境变量、启动子进程、查询状态、取消任务。 |
| `web_api/log_service.py` | 新增日志服务：列出和读取 `runs/<job_id>/logs/` 下的日志文件，并限制路径越界。 |
| `web_api/main.py` | 新增 FastAPI 路由：`/health`、`/settings`、`/settings/status`、`/jobs`、`/jobs/{job_id}`、`/jobs/{job_id}/summary`、`/jobs/{job_id}/logs`、`/jobs/{job_id}/cancel`。 |
| `requirements.txt` | 增加 `fastapi`、`uvicorn[standard]`、`python-multipart` 依赖。 |
| `.gitignore` | 增加 `.local/`，避免 API key 和上传暂存文件进入 Git。 |
| `docs/info/全仓目录结构与模块说明.md` | 增加 `web_api/` 服务层说明和启动方式。 |
| `docs/prompt/change_logs/README.md` | 增加第 010 篇变更记录索引。 |

## 修改原因

1. 后续 WebUI 不应直接拼接多个命令调用 Agent，需要一个稳定的后端入口。
2. 当前项目已经有 `run_pipeline.py` 统一编排复现、评测和修复，因此 FastAPI 层应保持轻量，只负责配置、任务、状态和日志。
3. 用户场景为单用户使用，API key 输入一次后暂时不变，因此先采用 `.local/web_settings.json` 本地持久化。
4. 任务输出继续沿用 `runs/<job_id>/`，避免新增一套与 CLI 不兼容的产物结构。

## 接口概览

```text
GET  /health
POST /settings
GET  /settings/status
POST /jobs
GET  /jobs/{job_id}
GET  /jobs/{job_id}/summary
GET  /jobs/{job_id}/logs
POST /jobs/{job_id}/cancel
```

## 启动方式

```powershell
uvicorn web_api.main:app --reload --host 127.0.0.1 --port 8000
```

Swagger 文档：

```text
http://127.0.0.1:8000/docs
```

## 验证方式

执行 Python 编译检查：

```powershell
python -m py_compile web_api\__init__.py web_api\config.py web_api\schemas.py web_api\settings_store.py web_api\job_service.py web_api\log_service.py web_api\main.py
```

并执行 FastAPI 导入检查：

```powershell
python -c "from web_api.main import app; print(app.title)"
```
