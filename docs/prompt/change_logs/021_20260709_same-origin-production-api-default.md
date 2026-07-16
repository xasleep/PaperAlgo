# 021 Same-Origin Production API Default

修改时间：2026-07-09

## 简要总结

修复 WebUI 生产构建仍包含 `http://localhost:8000` 的问题。前端现在在开发模式默认连接 `http://localhost:8000`，在生产构建默认使用同源相对路径；显式设置 `VITE_API_BASE_URL` 时仍优先生效。

## 问题背景

020 迭代希望通过 `$env:VITE_API_BASE_URL=""` 生成同源生产构建。但在 Windows PowerShell 下，空字符串环境变量传给 Node/Vite 时可能表现为未定义，导致 `client.ts` 回退到开发默认 `http://localhost:8000`。因此构建产物仍可能固化 localhost API 地址，FastAPI 托管 `web_ui/dist` 时不能真正同源部署。

## 修改内容

- 更新 `web_ui/src/api/client.ts`：
  - 显式 `VITE_API_BASE_URL` 优先生效。
  - 未设置时，`import.meta.env.PROD` 使用空字符串同源路径。
  - 未设置且为开发模式时，继续使用 `http://localhost:8000`。
- 新增 `web_ui/scripts/verify-same-origin.mjs`。
- 新增 `npm run verify:same-origin`，扫描生产构建产物，确认不包含 `http://localhost:8000`。
- 更新 `web_ui/README.md`，生产构建不再推荐 PowerShell 空字符串环境变量作为主要方案。
- 补齐 change log README 中 020 和 021 的历史索引。

## 修复后的实际行为

- `npm run dev` 且未设置 `VITE_API_BASE_URL` 时，WebUI 默认请求 `http://localhost:8000`，方便本地 Vite 联调 FastAPI。
- `npm run build` 且未设置 `VITE_API_BASE_URL` 时，WebUI 生产产物默认使用同源 API 路径。
- 如果显式设置 `VITE_API_BASE_URL=/api` 或 `VITE_API_BASE_URL=http://localhost:8000`，前端会按显式值构建。
- 防回归脚本会在构建后扫描 `dist`，发现 `http://localhost:8000` 即失败。

## 影响范围

- `web_ui/src/api/client.ts`
- `web_ui/scripts/verify-same-origin.mjs`
- `web_ui/package.json`
- `web_ui/README.md`
- `docs/prompt/change_logs/README.md`
- `docs/prompt/change_logs/021_20260709_same-origin-production-api-default.md`

## 验证方式

实际执行：

```powershell
cd web_ui
npm run typecheck
npm run build
npm run verify:same-origin
npm run smoke
cd ..
.\.venv\Scripts\python.exe -m pytest tests -q
```

结果：

- `npm run typecheck` 成功。
- `npm run build` 成功，未设置 `VITE_API_BASE_URL` 时完成生产构建。
- `npm run verify:same-origin` 成功，扫描 3 个 dist 文件，未发现 `http://localhost:8000`。
- `npm run smoke` 成功，覆盖 `/settings`、`/jobs/new`、`/jobs`、`/jobs/smoke-job`。
- Python 测试通过：`110 passed, 1 warning in 10.60s`。
- warning 为现有 `starlette.testclient` / `httpx` deprecation warning，不影响本次修复验证。

## 已知限制 / 注意事项

- `npm run verify:same-origin` 验证的是当前 `dist` 构建产物，需在 `npm run build` 后执行。
- 如果生产构建显式设置了远端 `VITE_API_BASE_URL`，该脚本仍只检查是否包含 `http://localhost:8000`。
- WebUI 仍是本地单用户 MVP，不包含登录、多用户隔离或完整 E2E 测试。
