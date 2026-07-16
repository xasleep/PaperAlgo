# 023 Local Dev CORS for Vite

修改时间：2026-07-09

## 简要总结

FastAPI 后端增加仅限本地 Vite 开发源的 CORS 白名单，使 WebUI 开发模式默认请求 `http://localhost:8000` 时可以直接保存 settings。

## 问题背景

WebUI 开发模式运行在 `http://localhost:5173` 或 `http://127.0.0.1:5173`，默认 API 地址是 `http://localhost:8000`。浏览器在 `POST /settings` 前会发送 CORS preflight；此前 FastAPI 没有配置 CORS，`OPTIONS /settings` 返回 405，导致 Save Settings 显示 `network_error / Failed to fetch`。

## 修改内容

- 在 FastAPI app 创建后、路由定义前注册 `CORSMiddleware`。
- 新增本地开发 origin 白名单：
  - `http://localhost:5173`
  - `http://127.0.0.1:5173`
- 仅允许必要方法：`GET`、`POST`、`OPTIONS`。
- 仅允许必要 headers：`Accept`、`Content-Type`。
- 新增 CORS 回归测试，覆盖允许 origin、拒绝非白名单 origin、实际请求 CORS 响应头，以及 settings 状态不回显 API key。
- 更新 WebUI README，说明默认直连后端可用，`/api` proxy 只是备用方案。

## 修复后的实际行为

本地启动 FastAPI `http://localhost:8000` 和 Vite `http://localhost:5173` 后，WebUI 默认配置即可直接调用后端并保存 settings，不再因为 `/settings` 预检请求 405 而显示 `Failed to fetch`。

## 安全边界

- 不使用 `*`。
- 不允许任意公网 origin。
- 不启用跨域 credentials。
- CORS 仅覆盖本地 Vite 开发源，不改变生产同源托管模式。
- `/settings` 和 `/settings/status` 仍只返回 `has_api_key` 等状态字段，不回显 API key。

## 影响范围

- FastAPI 本地开发跨域访问。
- WebUI 开发模式 Save Settings 工作流。
- 后端 CORS 与 settings 安全回归测试。
- WebUI README 开发说明。

## 验证方式

实际执行：

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q

Set-Location .\web_ui
npm run typecheck
npm run build
npm run verify:same-origin
npm run smoke
npm run smoke:prod
Set-Location ..
```

额外命令验证：

```powershell
Invoke-WebRequest -Uri "http://localhost:8000/settings" `
  -Method Options `
  -Headers @{
    Origin="http://localhost:5173"
    "Access-Control-Request-Method"="POST"
    "Access-Control-Request-Headers"="content-type"
  } `
  -UseBasicParsing
```

预期结果：`OPTIONS /settings` 不再返回 405，并包含 `access-control-allow-origin: http://localhost:5173`。

验证结果：

- Python 测试通过：`113 passed, 1 warning in 8.24s`。
- `npm run typecheck` 通过。
- `npm run build` 通过。
- `npm run verify:same-origin` 通过：检查 3 个 dist 文件，未发现 `http://localhost:8000`。
- `npm run smoke` 通过：覆盖 `/settings`、`/jobs/new`、`/jobs`、`/jobs/smoke-job`。
- `npm run smoke:prod` 通过：临时 FastAPI 地址 `http://127.0.0.1:8210`，覆盖 HTML 路由和 JSON API。
- 手动预检通过：`OPTIONS /settings` 返回 200，`access-control-allow-origin` 为 `http://localhost:5173`，`access-control-allow-methods` 为 `GET, POST, OPTIONS`。
- warning 为现有 `starlette.testclient` / `httpx` deprecation warning，不影响本次行为验证。

## 已知限制 / 注意事项

- CORS 只服务本地开发直连后端场景；生产构建仍推荐由 FastAPI 同源托管 WebUI。
- 如果未来 Vite 固定开发端口发生变化，需要显式更新白名单，不能改成通配 origin。
