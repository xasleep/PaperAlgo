# 019 Minimal React Vite WebUI MVP

修改时间：2026-07-09

## 简要总结

新增 `web_ui` React + Vite + TypeScript 前端，实现本地 Paper2Code 控制台的最小闭环：保存 API 设置、创建任务、查看任务状态、查看日志和 repo 文件、下载 repo zip。

## 问题背景

FastAPI 后端已经收口了 WebUI 所需的接口契约和错误格式，前端可以稳定依赖 HTTP status、`error.code`、`error.message` 以及任务状态字段。项目此前缺少可操作的浏览器 UI，用户需要直接调用 API 才能完成本地 Agent 工作流。

## 修改内容

- 新增 `web_ui` Vite 工程，使用 React、TypeScript、React Router 和 `lucide-react`。
- 新增统一 API client，支持 JSON GET/POST、multipart PDF 上传、repo zip 下载、结构化后端错误解析和网络错误转换。
- 新增 Settings、Create Job、Jobs、Job Detail 四个页面。
- 新增 AppShell、StatusBadge、ErrorNotice、FileTree、LogViewer 组件。
- 新增 Vite dev server `/api` proxy 配置，并保留 `VITE_API_BASE_URL` 覆盖能力。
- 新增 `web_ui/README.md` 记录本地启动方式。

## 修复后的实际行为

用户打开前端后会直接进入本地控制台，可按以下流程完成 MVP 使用路径：

1. 在 Settings 页面保存 reproduce/evaluation provider、model、base_url 和 API key。
2. 保存成功后 API key 输入框清空，页面只显示 `has_api_key` 状态，不回显密钥。
3. 在 Create Job 页面上传 PDF 并提交 multipart `POST /jobs`。
4. 创建成功后跳转到任务详情页，详情页每 2 秒轮询任务状态。
5. 任务详情页可查看 artifacts summary、日志列表与日志内容、repo tree 与文本文件内容。
6. repo 可用时可下载 zip；repo、log 或 artifact 暂不可用时显示空状态或结构化错误，页面不会崩溃。

## 影响范围

- 新增前端目录：`web_ui/`
- 新增文档：`web_ui/README.md`
- 新增变更记录：`docs/prompt/change_logs/019_20260709_minimal-react-vite-webui-mvp.md`
- 未修改 FastAPI 后端 API 契约。
- 未修改现有 Python 业务逻辑或测试。

## 验证方式

实际执行：

```powershell
cd web_ui
npm install
npm run build
npm run typecheck
cd ..
.\.venv\Scripts\python.exe -m pytest tests -q
```

结果：

- `npm install` 成功，新增 75 个 package，0 个漏洞。
- `npm run build` 成功，`tsc -b` 与 `vite build` 均通过。
- `npm run typecheck` 成功。
- Python 测试通过：`107 passed, 1 warning in 15.44s`。
- Vite dev server 已在 `http://127.0.0.1:5173/` 启动。
- 使用 Playwright 检查桌面视口 `1280x900` 与移动视口 `390x844`，Settings、Create Job、Jobs 三个主路由均可渲染；移动端未出现页面级横向溢出，Jobs 表格使用内部横向滚动。

注意：Playwright 检查时 FastAPI 后端未启动，因此列表和设置页会显示网络错误提示；这符合当前运行环境，不影响前端构建验证。

## 已知限制 / 注意事项

- 当前无登录、权限隔离或多用户模型，只适配本地单用户控制台。
- 日志和任务状态使用轮询，不使用 SSE 或 WebSocket。
- UI 是 MVP，尚未加入前端自动化测试。
- Settings 保存仍要求重新提交 API key；前端不会回显 key，也不会写入 localStorage。
- repo 文件预览依赖后端文本预览能力，二进制、大文件或不支持的扩展名会显示结构化错误。
