# 012 WebUI 任务浏览与结果文件接口

- 修改时间：2026-07-09 09:02
- 简要总结：为 WebUI 补齐任务浏览和结果查看所需的后端接口，支持列出历史任务、查看 job artifacts、读取生成仓库文件树、读取单个代码文件内容，并下载生成仓库 zip。

## 修改文件

| 文件 | 修改内容 |
|---|---|
| `web_api/artifact_service.py` | 新增 artifact 服务：读取 `runs/` 历史任务、汇总 job 产物、列出 `repo/` 文件树、安全读取仓库文件、生成 repo zip。 |
| `web_api/schemas.py` | 新增 `JobListResponse`、`ArtifactSummaryResponse`、`RepoTreeResponse`、`RepoFileResponse` 等响应模型。 |
| `web_api/main.py` | 新增 `GET /jobs`、`GET /jobs/{job_id}/artifacts`、`GET /jobs/{job_id}/repo/tree`、`GET /jobs/{job_id}/repo/file`、`GET /jobs/{job_id}/repo/download` 接口。 |
| `docs/info/全仓目录结构与模块说明.md` | 更新 FastAPI 服务层说明、接口列表和文档版本。 |
| `docs/prompt/change_logs/README.md` | 增加第 012 篇变更记录索引。 |

## 修改原因

1. WebUI 不能只启动任务，还需要展示历史任务、任务状态、生成代码文件和下载结果。
2. 前端不应该直接读取本地文件系统，因此需要通过 FastAPI 封装安全的结果访问接口。
3. 读取仓库文件时需要限制路径越界和大文件读取，避免 WebUI 传入异常路径造成安全问题。

## 新增接口

```text
GET /jobs
GET /jobs/{job_id}/artifacts
GET /jobs/{job_id}/repo/tree
GET /jobs/{job_id}/repo/file?path=main.R
GET /jobs/{job_id}/repo/download
```

## 验证方式

执行 Python 编译检查：

```powershell
python -m py_compile web_api\artifact_service.py web_api\schemas.py web_api\main.py
```

使用 FastAPI `TestClient` 验证：

```text
GET /jobs -> 200
GET /jobs/{job_id}/artifacts -> 200
GET /jobs/{job_id}/repo/tree -> 200
GET /jobs/{job_id}/repo/file?path=main.R -> 200
GET /jobs/{job_id}/repo/download -> 200
```
