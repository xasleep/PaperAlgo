# 024 Vite Strict Dev Port

修改时间：2026-07-09

## 简要总结

Vite dev server 固定使用 `5173`，端口被占用时直接启动失败，避免自动切换端口后触发 FastAPI CORS 白名单不匹配。

## 问题背景

023 为 FastAPI 增加了本地开发 CORS 白名单，只允许：

- `http://localhost:5173`
- `http://127.0.0.1:5173`

但 Vite dev server 默认在 `5173` 被占用时可能自动切换到 `5174`、`5175` 等端口。此时页面可以打开，但浏览器 origin 不再匹配 FastAPI CORS 白名单，Save Settings 等 API 请求会再次出现 `network_error / Failed to fetch`。

## 修改内容

- 在 `web_ui/vite.config.ts` 的 `server` 配置中启用 `strictPort: true`。
- 保留现有 `port: 5173`。
- 保留现有 `/api` proxy。
- 更新 WebUI README，说明开发服务器固定使用 `5173`，端口冲突时需要释放占用进程后重试。

## 修复后的实际行为

本地运行 `npm run dev` 时，Vite 只会尝试监听 `5173`。如果端口被占用，开发服务器直接失败并报告端口不可用，不会静默切换到未被 FastAPI CORS 白名单允许的端口。

## 影响范围

- WebUI 本地开发启动行为。
- WebUI README 开发说明。
- 不影响生产构建同源默认逻辑。
- 不改变 FastAPI CORS 白名单本身。

## 验证方式

实际执行：

```powershell
Set-Location .\web_ui
npm run typecheck
npm run build
npm run verify:same-origin
npm run smoke
npm run smoke:prod

Set-Location ..
.\.venv\Scripts\python.exe -m pytest tests -q
```

推荐补充验证：

- 临时占用 `5173` 后运行 `npm run dev`，确认 Vite 直接失败且不会切换到 `5174`。
- 释放 `5173` 后运行 `npm run dev`，确认 Vite 可正常启动在 `http://localhost:5173/`。

验证结果：

- `npm run typecheck` 通过。
- `npm run build` 通过。
- `npm run verify:same-origin` 通过：检查 3 个 dist 文件，未发现 `http://localhost:8000`。
- `npm run smoke` 通过：覆盖 `/settings`、`/jobs/new`、`/jobs`、`/jobs/smoke-job`。
- `npm run smoke:prod` 通过：临时 FastAPI 地址 `http://127.0.0.1:8210`，覆盖 HTML 路由和 JSON API。
- Python 测试通过：`113 passed, 1 warning in 7.50s`。
- 端口冲突验证通过：临时占用 IPv4/IPv6 loopback 的 `5173` 后运行 `npm run dev`，输出 `Port 5173 is already in use`，未出现 `5174`。
- 端口释放验证通过：运行 `npm run dev` 后 `http://localhost:5173/` 返回 200，且 `5173` 处于监听状态。
- warning 为现有 `starlette.testclient` / `httpx` deprecation warning，不影响本次行为验证。

## 已知限制 / 注意事项

- 如果未来确实要更换 Vite 固定开发端口，必须同步更新 FastAPI 本地 CORS 白名单。
- 该变更只影响 Vite dev server；生产构建仍走同源 API 默认值。
