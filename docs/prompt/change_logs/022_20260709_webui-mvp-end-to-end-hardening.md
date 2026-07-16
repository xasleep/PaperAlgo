# 022 WebUI MVP End-to-End Hardening

修改时间：2026-07-09

## 简要总结

将 WebUI MVP 从“能打开”推进到本地单用户可验收状态：新增 FastAPI 生产托管 smoke check、React 未知路由兜底、Job Detail 运行态资源自动刷新、Create Job 前端提交校验，并补充 README 验收命令。

## 问题背景

021 修复了生产构建默认同源 API，但 WebUI 验收链路仍缺少几个关键保障：

- 缺少验证 `web_ui/dist` 被 FastAPI 托管后的真实生产 smoke check。
- React 对未知路径没有兜底页面。
- Job Detail 只持续轮询 job status，logs/artifacts/repo tree 需要手动刷新。
- Create Job 主要依赖后端校验，前端对 PDF、参数范围和 skip_mineru 条件提示不够早。
- README 尚未形成完整 MVP 验收命令清单。

## 修改内容

- 新增 `web_ui/scripts/smoke-prod-webui.mjs` 与 `npm run smoke:prod`：
  - 检查 `web_ui/dist/index.html` 是否存在，不存在时提示先运行 `npm run build`。
  - 启动临时 FastAPI/uvicorn，使用空闲本地端口。
  - 验证 `/`、`/settings`、`/jobs`、`/jobs/new`、`/jobs/smoke-job` 在 `Accept: text/html` 下返回 React shell。
  - 验证 `/health` 和 `/jobs` 在 `Accept: application/json` 下返回 JSON，避免 SPA fallback 吞掉 API。
  - 结束后清理 uvicorn 子进程，Windows 下使用 `taskkill /T /F`。
- 新增 React `NotFoundPage` 并在 `App.tsx` 增加 `*` fallback。
- 更新 `JobDetailPage`：
  - 保留手动 `Refresh Results`。
  - job 处于 queued/running/active/cancelable 时，用单一 timer 自动刷新 job status、logs、artifacts、repo tree。
  - completed/failed/canceled 终态停止自动资源轮询。
  - 组件卸载或状态变化时清理 timer，避免重叠轮询。
- 更新 `CreateJobPage`：
  - 提交前校验 PDF、`generated_n`、`max_repair_rounds`、`skip_mineru` 与 `pdf_markdown_path`。
  - 提交中禁止重复提交。
  - 后端返回 `settings_not_configured` 时显示 Settings 页面入口。
- 更新 `web_ui/README.md`，加入完整 MVP 验收命令。

## 修复后的实际行为

- 本地开发 smoke 和 FastAPI 生产托管 smoke 都可用，分别覆盖 Vite shell 与构建产物托管路径。
- 未知 WebUI 路径不再白屏，会显示 Not Found 页面并提供 Jobs、Create Job、Settings 入口。
- 运行中的任务详情会自动刷新状态和结果资源；终态任务不再持续产生无意义请求。
- 创建任务时常见输入错误可在前端直接提示，减少无效 API 请求。
- settings 未配置时，Create Job 页面会引导用户去 Settings。

## 影响范围

- 前端：
  - `web_ui/package.json`
  - `web_ui/scripts/smoke-prod-webui.mjs`
  - `web_ui/src/App.tsx`
  - `web_ui/src/pages/NotFoundPage.tsx`
  - `web_ui/src/pages/JobDetailPage.tsx`
  - `web_ui/src/pages/CreateJobPage.tsx`
  - `web_ui/src/styles.css`
  - `web_ui/README.md`
- 文档：
  - `docs/prompt/change_logs/README.md`
  - `docs/prompt/change_logs/022_20260709_webui-mvp-end-to-end-hardening.md`

## 验证方式

实际执行：

```powershell
cd web_ui
npm run typecheck
npm run build
npm run verify:same-origin
npm run smoke
npm run smoke:prod
cd ..
.\.venv\Scripts\python.exe -m pytest tests -q
```

结果：

- `npm run typecheck` 成功。
- `npm run build` 成功。
- `npm run verify:same-origin` 成功：扫描 3 个 dist 文件，未发现 `http://localhost:8000`。
- `npm run smoke` 成功：覆盖 `/settings`、`/jobs/new`、`/jobs`、`/jobs/smoke-job`。
- `npm run smoke:prod` 成功：临时 FastAPI 托管构建产物，验证 `/`、`/settings`、`/jobs`、`/jobs/new`、`/jobs/smoke-job` HTML 路由，以及 `/health`、`/jobs` JSON API。
- Python 测试通过：`110 passed, 1 warning in 7.69s`。
- Playwright 浏览器检查通过：桌面 `1280x900` 与移动 `390x844` 下，`/not-a-real-route`、`/jobs/new`、`/jobs/smoke-job` 均渲染预期页面标题，且无页面级横向溢出。
- warning 为现有 `starlette.testclient` / `httpx` deprecation warning，不影响本次行为验证。

## 已知限制 / 注意事项

- 仍使用轮询，不使用 WebSocket 或 SSE。
- smoke:prod 验证的是本机临时 uvicorn 托管路径，不覆盖真实长时间 pipeline 运行。
- Create Job 前端校验只做 MVP 级预检，后端仍是最终契约与安全边界。
- WebUI 仍是本地单用户模式，没有登录和多用户隔离。
