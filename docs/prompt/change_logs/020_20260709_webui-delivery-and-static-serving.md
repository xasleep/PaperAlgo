# 020 WebUI Delivery and Static Serving

修改时间：2026-07-09

## 简要总结

对 WebUI MVP 做可交付收口：补齐 change log 索引，增加 FastAPI 可选托管 `web_ui/dist` 的能力，支持前端生产同源 API base URL，新增轻量 WebUI smoke check，并加固任务详情页资源错误提示。

## 问题背景

上一轮 WebUI 已经能在 Vite 开发模式运行，但交付到本地单用户环境时仍有几个缺口：

- change log README 未索引 019 记录。
- FastAPI 不能直接托管前端构建产物，部署时需要额外静态服务器。
- `VITE_API_BASE_URL=""` 会回退到 `http://localhost:8000`，不适合生产同源模式。
- 前端缺少轻量 smoke check 来确认关键路由在后端不可用时不会白屏。
- Job Detail 中 artifacts/logs/repo 某一路资源失败时，错误提示不够完整。

## 修改内容

- 新增 `web_api/static_ui.py`：
  - 当 `web_ui/dist/index.html` 存在时启用静态托管。
  - 挂载 `/assets` 静态资源。
  - 为非 API 路径提供 SPA fallback。
  - 提供 `spa_index_response()`，让 `/jobs` 与 `/jobs/{job_id}` 在浏览器 HTML 导航下可返回前端 shell，同时保留 JSON API 行为。
- 更新 `web_api/main.py`：
  - `/jobs` 和 `/jobs/{job_id}` 根据 `Accept: text/html` 可返回 WebUI shell。
  - API client 使用 `Accept: application/json` 时继续返回原有 JSON 契约。
- 更新 `web_ui/src/api/client.ts`：
  - 未设置 `VITE_API_BASE_URL` 时默认 `http://localhost:8000`。
  - 显式设置为空字符串时使用同源相对路径。
- 更新 Job Detail：
  - artifacts、logs、repo tree、单个 log、单个 repo file、zip 下载失败时展示结构化错误。
  - 任一资源不可用时继续显示空状态，不让整页崩溃。
- 新增 `web_ui/scripts/smoke-webui.mjs` 与 `npm run smoke`。
- 新增 `tests/test_static_ui.py` 覆盖静态托管与 API 不被 fallback 吞掉的行为。
- 更新 `web_ui/README.md`，说明开发模式、生产构建、FastAPI 托管 dist 和 smoke check。
- 将 019 加入 change log README 历史索引。

## 修复后的实际行为

开发模式：

- Vite 前端仍可使用 `VITE_API_BASE_URL=http://localhost:8000` 调用 FastAPI。
- 也可以设置 `VITE_API_BASE_URL=/api` 走 Vite proxy。

生产模式：

- 执行 `VITE_API_BASE_URL="" npm run build` 后，前端会使用同源 API 路径。
- FastAPI 启动时如果检测到 `web_ui/dist/index.html`，会自动托管构建后的 WebUI。
- `/health`、`/settings/status`、`/jobs`、`/jobs/{job_id}` 等 API 对 JSON 请求保持原契约。
- 浏览器 HTML 导航可获得前端 shell，避免生产部署需要额外静态服务器。

回归验证：

- WebUI smoke check 会启动临时 Vite dev server，验证 `/settings`、`/jobs/new`、`/jobs`、`/jobs/smoke-job` 返回 React shell。
- smoke check 不依赖真实 API key、真实后端或 pipeline。

## 影响范围

- 后端：
  - `web_api/static_ui.py`
  - `web_api/main.py`
  - `tests/test_static_ui.py`
- 前端：
  - `web_ui/src/api/client.ts`
  - `web_ui/src/components/AppShell.tsx`
  - `web_ui/src/pages/JobDetailPage.tsx`
  - `web_ui/scripts/smoke-webui.mjs`
  - `web_ui/package.json`
  - `web_ui/README.md`
- 文档：
  - `docs/prompt/change_logs/README.md`
  - `docs/prompt/change_logs/020_20260709_webui-delivery-and-static-serving.md`

## 验证方式

实际执行：

```powershell
cd web_ui
npm run typecheck
npm run build
npm run smoke
cd ..
.\.venv\Scripts\python.exe -m pytest tests -q
```

结果：

- `npm run typecheck` 成功。
- `npm run build` 成功，`tsc -b` 与 `vite build` 均通过；另用 `VITE_API_BASE_URL=""` 验证生产同源构建通过。
- `npm run smoke` 成功，覆盖 `/settings`、`/jobs/new`、`/jobs`、`/jobs/smoke-job`。
- Python 测试通过：`110 passed, 1 warning in 9.25s`。
- Playwright 浏览器检查通过：桌面 `1280x900` 与移动 `390x844` 下，`/settings`、`/jobs/new`、`/jobs`、`/jobs/smoke-job` 均渲染预期页面标题，且无页面级横向溢出。
- warning 为现有 `starlette.testclient` / `httpx` deprecation warning，不影响本次行为验证。

## 已知限制 / 注意事项

- WebUI 仍是本地单用户 MVP，没有登录和多用户隔离。
- 日志和任务状态仍使用轮询，不使用 SSE 或 WebSocket。
- smoke check 是轻量路由级检查，不替代完整浏览器端到端测试。
- 生产同源托管依赖 `web_ui/dist/index.html` 存在；未构建前 FastAPI 只提供 API。
- 浏览器 HTML 导航和 API JSON 请求通过 `Accept` 头区分；自定义 API 调用方应继续发送 JSON accept header。
