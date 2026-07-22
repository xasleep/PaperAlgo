# PR-00 工程基线

记录日期：2026-07-22

## 当前范围

本仓库的执行内核是 `codes/run_pipeline.py` 及现有阶段脚本；`web_api/` 是 FastAPI 控制面，`web_ui/` 是 React + Vite 本地控制台。默认 legacy runtime 由 FastAPI 启动 Pipeline 子进程，sqlite runtime 则由独立 Worker 启动；任务 artifacts 保存在本地文件系统，SQLite 仅保存最小控制面元数据。

当前提供标准库 SQLite 持久化控制面，但默认 `JOB_RUNTIME=legacy` 仍保持既有子进程行为；`JOB_RUNTIME=sqlite` 由 `python -m web_api.worker` 启动独立单并发 Worker，并提供 PID/create-time 进程级 reconciliation。当前没有阶段 checkpoint、阶段恢复、SSE、WebSocket、Celery、Redis、PostgreSQL、Kubernetes 或 LangGraph。WebUI 对活跃任务每 2 秒轮询，FastAPI 只支持单实例。

SQLite Worker 对本地进程先观察真实退出码，再消费取消命令；确认自然退出时不会将任务误记为 canceled。取消前续租并重新验证 PID/create time/process group，Windows 强制终止绑定到同一进程句柄并有界等待，只有完整树已确认退出才持久化 canceled。单 job 的设置、输入、命令或进程创建错误记录为 `process_launch_failed` 后继续轮询；SQLite、fencing、持久化和未知错误保持 fail fast。

## 验证命令

从仓库根目录执行 Python 测试：

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q
```

执行前端类型检查和构建：

```powershell
Set-Location .\web_ui
npm run typecheck
npm run build
npm run verify:same-origin
npm run smoke
npm run smoke:prod
Set-Location ..
```

`npm run build` 生成 `web_ui/dist/`；`npm run smoke:prod` 必须在构建后运行。`node_modules/`、`dist/` 和 `*.tsbuildinfo` 不进入版本管理，`package-lock.json` 必须进入版本管理。

## 2026-07-22 PR-04A 重新验收快照

- `python -m compileall -q codes web_api tests`：通过。
- PR-04A、API、安全边界与文档定向测试：155 passed。
- `python -m pytest -q -rs`：334 passed。
- WebUI `typecheck`、`build`、`verify:same-origin`、`smoke`、`smoke:prod`：全部通过。
- Python 测试报告 1 个既有 `StarletteDeprecationWarning`（Starlette `TestClient` 与当前 httpx 兼容提示），无 skip；该 warning 不在 PR-04A 范围内。

以上数字是本日期、本 checkout 的验收快照，不作为长期固定测试数量。Git 暂存区、无关修改和运行产物仍需在每次交付时独立检查。

## 测试政策

- 自动化测试只使用 fake provider、fake pipeline、mock/monkeypatch 和临时目录。
- 测试不得调用真实付费 LLM、MinerU、vLLM 或完整耗时 Pipeline。
- 测试不得依赖开发者机器的绝对路径、真实 API key 或既有运行目录。
- 涉及输入、路径、导出、环境变量或敏感数据的新安全逻辑，先增加失败/攻击测试，再实现修复。
- 不允许通过捕获所有异常、返回空结果或强制成功退出掩盖错误。

## 版本管理基线

应进入版本管理：

- `codes/`、`web_api/`、`web_ui/src/` 和 `web_ui/scripts/` 源码；
- `tests/` 中的 Python 测试；
- 根 `README.md`、`docs/` 下的说明、ADR 和 change logs；
- `web_ui/package.json`、`web_ui/package-lock.json`、TypeScript/Vite 配置和 `web_ui/index.html`；
- `.gitignore`、`requirements.txt`、启动脚本、经确认可分发且有意长期维护的示例输入及上游 `LICENSE`。

必须保持忽略：

- Python/Node 环境和缓存：`.venv/`、`__pycache__/`、`.pytest_cache/`、`node_modules/`、`*.pyc`、`*.tsbuildinfo`；
- 构建与运行产物：`dist/`、`.local/`、`runs/`、`outputs/`、`results/`、`mineru_outputs/`、`*.log`；
- secrets：`.env`、`.env.*`（保留可提交的 `.env.example`）、`api.txt`、私钥和证书文件。

## 已知限制

- `.local/web_settings.json` 是本地明文 JSON；接口脱敏和本地 ACL 不等于加密凭据存储。
- legacy 状态/摘要 JSON 仍可能在进程中断时损坏，FastAPI 重启后不能接管原 legacy 进程；sqlite Worker 尚无阶段 checkpoint，Worker 中断期间已死亡的已登记 Pipeline 只能明确标记失败，不能从阶段恢复。
- sqlite Worker 若发现 running job 尚未完整登记 PID/create time/process group，会将其置为 `identity_unresolved` 隔离态并阻止后续任务启动，不会自动把未知进程当作已死亡。该状态可能需要人工检查并重启本地环境。
- sqlite 取消仍由 job command 驱动；自然退出优先，未验证或超时的树级终止不会写入 canceled。Windows 的强制阶段不使用 `taskkill /T /F`，但操作系统拒绝打开/终止/等待句柄时会显式失败并需要人工检查。
- legacy runtime 中 FastAPI 重启后不能重新接管已启动的 Pipeline；sqlite runtime 的独立 Worker 不受 API 重启影响。
- 历史 repo zip 没有自动清理策略。
- `pdf_markdown_path` 被限制在 `runs/` 下，但尚未收紧到当前 job 子目录。
- Vite 开发端口与 CORS 白名单共同固定为 `5173`；更改时必须同步更新。
- 本地单用户边界不提供远程部署、认证、授权或多租户隔离。
