# PR-00 工程基线

记录日期：2026-07-14

## 当前范围

本仓库的执行内核是 `codes/run_pipeline.py` 及现有阶段脚本；`web_api/` 是 FastAPI 控制面，`web_ui/` 是 React + Vite 本地控制台。FastAPI 以子进程启动 Pipeline，任务状态和 artifacts 保存在本地文件系统。

当前提供标准库 SQLite 持久化控制面，但默认 `JOB_RUNTIME=legacy` 仍保持既有子进程行为；`JOB_RUNTIME=sqlite` 只创建 queued job，尚未实现完整 Worker。当前没有 SSE、WebSocket、Celery、Redis、PostgreSQL、Kubernetes 或 LangGraph。WebUI 对活跃任务每 2 秒轮询，FastAPI 只支持单实例、单 worker。

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
- legacy 状态/摘要 JSON 仍可能在进程中断时损坏；sqlite 模式已有事务和 migration，但尚无 Worker 消费与恢复协议。
- FastAPI 重启后不能重新接管已启动的 Pipeline 子进程。
- 历史 repo zip 没有自动清理策略。
- `pdf_markdown_path` 被限制在 `runs/` 下，但尚未收紧到当前 job 子目录。
- Vite 开发端口与 CORS 白名单共同固定为 `5173`；更改时必须同步更新。
- 本地单用户边界不提供远程部署、认证、授权或多租户隔离。
